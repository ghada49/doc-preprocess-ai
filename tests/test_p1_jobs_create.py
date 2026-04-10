"""
tests/test_p1_jobs_create.py
-----------------------------
Packet 1.8 contract tests for POST /v1/jobs.

Tests cover:
  - HTTP 201 with correct JSON schema on success
  - job_id is a valid UUID4
  - status is always "queued"
  - page_count matches submitted pages
  - created_at is present (ISO datetime string)
  - DB: Job and JobPage objects are created with correct field values
  - DB: pipeline_mode is stored on the Job row
  - Redis: one PageTask enqueued per submitted page
  - Validation: missing/invalid fields → 422
  - Redis unavailable → 503

Uses:
  - MagicMock session (avoids PostgreSQL-specific JSONB DDL incompatibility
    with SQLite; the DB schema contract is validated by the migration tests)
  - fakeredis.FakeRedis for the Redis queue
  - A minimal FastAPI app containing only the jobs router
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import fakeredis
import pytest
import redis as redis_lib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from services.eep.app.jobs.create import router
from services.eep.app.redis_client import get_redis
from shared.schemas.queue import QUEUE_PAGE_TASKS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

_MINIMAL_BODY: dict[str, Any] = {
    "collection_id": "col-001",
    "material_type": "book",
    "pages": [{"page_number": 1, "input_uri": "s3://libraryai/uploads/abc.tiff"}],
    "pipeline_mode": "layout",
    "policy_version": "v1.0",
}

_THREE_PAGE_BODY: dict[str, Any] = {
    "collection_id": "col-multi",
    "material_type": "newspaper",
    "pages": [
        {"page_number": 1, "input_uri": "s3://libraryai/uploads/p1.tiff"},
        {"page_number": 2, "input_uri": "s3://libraryai/uploads/p2.tiff"},
        {"page_number": 3, "input_uri": "s3://libraryai/uploads/p3.tiff"},
    ],
    "pipeline_mode": "preprocess",
    "policy_version": "v1.1",
    "shadow_mode": True,
}


# ---------------------------------------------------------------------------
# Mock session factory
# ---------------------------------------------------------------------------


def _make_mock_session() -> MagicMock:
    """
    Return a MagicMock that behaves like a SQLAlchemy Session for the
    job-creation path:
      - add() — accumulates added ORM objects in session.added_objects
      - commit() — no-op (no real DB)
      - refresh(job) — sets job.created_at so the endpoint can return it
      - rollback() — no-op
    """
    session = MagicMock(spec=Session)
    added: list[Any] = []
    session.added_objects = added
    session.add.side_effect = lambda obj: added.append(obj)

    def _refresh(obj: Any) -> None:
        if isinstance(obj, Job):
            # Simulate server-side default: set created_at so the endpoint
            # can include it in the response.
            obj.created_at = datetime.now(tz=timezone.utc)

    session.refresh.side_effect = _refresh
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_MOCK_ADMIN = CurrentUser(user_id="test-admin", role="admin")


@pytest.fixture()
def test_app() -> FastAPI:
    """Minimal FastAPI app with only the jobs router."""
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[require_user] = lambda: _MOCK_ADMIN
    return _app


@pytest.fixture()
def mock_db(test_app: FastAPI) -> Generator[MagicMock, None, None]:
    """Inject a mock session; return it so tests can inspect added objects."""
    session = _make_mock_session()
    test_app.dependency_overrides[get_session] = lambda: session
    yield session
    test_app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def fake_r(test_app: FastAPI) -> Generator[fakeredis.FakeRedis, None, None]:
    """Inject a fresh fakeredis instance."""
    r: fakeredis.FakeRedis = fakeredis.FakeRedis(decode_responses=True)
    test_app.dependency_overrides[get_redis] = lambda: r
    yield r
    test_app.dependency_overrides.pop(get_redis, None)


@pytest.fixture()
def client(test_app: FastAPI, mock_db: MagicMock, fake_r: fakeredis.FakeRedis) -> TestClient:
    """TestClient with mock DB and fakeredis active."""
    return TestClient(test_app)


@pytest.fixture()
def job_response(client: TestClient) -> dict[str, Any]:
    """Successful POST /v1/jobs response for the minimal body."""
    r = client.post("/v1/jobs", json=_MINIMAL_BODY)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# HTTP status
# ---------------------------------------------------------------------------


class TestJobCreateStatus:
    def test_returns_201_on_success(self, client: TestClient) -> None:
        assert client.post("/v1/jobs", json=_MINIMAL_BODY).status_code == 201

    def test_method_get_not_allowed(self, client: TestClient) -> None:
        assert client.get("/v1/jobs").status_code == 405


# ---------------------------------------------------------------------------
# Response schema — field presence
# ---------------------------------------------------------------------------


class TestJobCreateResponseFields:
    def test_has_job_id(self, job_response: dict[str, Any]) -> None:
        assert "job_id" in job_response

    def test_has_status(self, job_response: dict[str, Any]) -> None:
        assert "status" in job_response

    def test_has_page_count(self, job_response: dict[str, Any]) -> None:
        assert "page_count" in job_response

    def test_has_created_at(self, job_response: dict[str, Any]) -> None:
        assert "created_at" in job_response


# ---------------------------------------------------------------------------
# Response values
# ---------------------------------------------------------------------------


class TestJobCreateResponseValues:
    def test_status_is_queued(self, job_response: dict[str, Any]) -> None:
        assert job_response["status"] == "queued"

    def test_job_id_is_uuid(self, job_response: dict[str, Any]) -> None:
        assert _UUID_RE.match(
            job_response["job_id"]
        ), f"job_id is not a valid UUID: {job_response['job_id']!r}"

    def test_page_count_single(self, job_response: dict[str, Any]) -> None:
        assert job_response["page_count"] == 1

    def test_page_count_multi(self, client: TestClient) -> None:
        r = client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert r.status_code == 201
        assert r.json()["page_count"] == 3

    def test_created_at_is_non_empty_string(self, job_response: dict[str, Any]) -> None:
        assert isinstance(job_response["created_at"], str)
        assert len(job_response["created_at"]) > 0

    def test_job_ids_are_unique_per_call(self, client: TestClient) -> None:
        r1 = client.post("/v1/jobs", json=_MINIMAL_BODY)
        r2 = client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["job_id"] != r2.json()["job_id"]


# ---------------------------------------------------------------------------
# DB — Job object construction
# ---------------------------------------------------------------------------


def _added_jobs(mock_db: MagicMock) -> list[Job]:
    return [o for o in mock_db.added_objects if isinstance(o, Job)]


def _added_pages(mock_db: MagicMock) -> list[JobPage]:
    return [o for o in mock_db.added_objects if isinstance(o, JobPage)]


class TestJobDbRecord:
    def test_job_object_created(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert len(_added_jobs(mock_db)) == 1

    def test_pipeline_mode_layout(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].pipeline_mode == "layout"

    def test_pipeline_mode_preprocess(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert _added_jobs(mock_db)[0].pipeline_mode == "preprocess"

    def test_material_type_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].material_type == "book"

    def test_collection_id_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].collection_id == "col-001"

    def test_page_count_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert _added_jobs(mock_db)[0].page_count == 3

    def test_status_is_queued(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].status == "queued"

    def test_shadow_mode_false_by_default(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].shadow_mode is False

    def test_shadow_mode_true_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert _added_jobs(mock_db)[0].shadow_mode is True

    def test_policy_version_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].policy_version == "v1.0"

    def test_created_by_is_set_from_jwt(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_jobs(mock_db)[0].created_by == "test-admin"

    def test_db_commit_called(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        mock_db.commit.assert_called_once()

    def test_db_refresh_called_on_job(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        jobs = _added_jobs(mock_db)
        assert len(jobs) == 1
        mock_db.refresh.assert_called_once_with(jobs[0])


# ---------------------------------------------------------------------------
# DB — JobPage object construction
# ---------------------------------------------------------------------------


class TestJobPageDbRecords:
    def test_one_page_added(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert len(_added_pages(mock_db)) == 1

    def test_three_pages_added(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert len(_added_pages(mock_db)) == 3

    def test_page_status_is_queued(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert all(p.status == "queued" for p in _added_pages(mock_db))

    def test_page_numbers_match(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert sorted(p.page_number for p in _added_pages(mock_db)) == [1, 2, 3]

    def test_input_uri_stored(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert _added_pages(mock_db)[0].input_image_uri == "s3://libraryai/uploads/abc.tiff"

    def test_sub_page_index_is_none(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert all(p.sub_page_index is None for p in _added_pages(mock_db))

    def test_page_job_id_matches_response(self, client: TestClient, mock_db: MagicMock) -> None:
        r = client.post("/v1/jobs", json=_MINIMAL_BODY)
        job_id = r.json()["job_id"]
        assert all(p.job_id == job_id for p in _added_pages(mock_db))

    def test_page_ids_are_unique(self, client: TestClient, mock_db: MagicMock) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        ids = [p.page_id for p in _added_pages(mock_db)]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Redis queue — tasks enqueued
# ---------------------------------------------------------------------------


def _drain_queue(r: fakeredis.FakeRedis) -> list[dict[str, Any]]:
    """Pop all items from QUEUE_PAGE_TASKS and return as dicts."""
    items = []
    while True:
        raw = r.rpop(QUEUE_PAGE_TASKS)
        if raw is None:
            break
        items.append(json.loads(raw))  # type: ignore[arg-type]
    return items


class TestJobCreateQueuedTasks:
    def test_one_task_enqueued(self, client: TestClient, fake_r: fakeredis.FakeRedis) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert len(_drain_queue(fake_r)) == 1

    def test_three_tasks_enqueued(self, client: TestClient, fake_r: fakeredis.FakeRedis) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert len(_drain_queue(fake_r)) == 3

    def test_task_job_id_matches_response(
        self, client: TestClient, fake_r: fakeredis.FakeRedis
    ) -> None:
        r = client.post("/v1/jobs", json=_MINIMAL_BODY)
        job_id = r.json()["job_id"]
        tasks = _drain_queue(fake_r)
        assert all(t["job_id"] == job_id for t in tasks)

    def test_task_page_numbers_match(self, client: TestClient, fake_r: fakeredis.FakeRedis) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        tasks = _drain_queue(fake_r)
        assert sorted(t["page_number"] for t in tasks) == [1, 2, 3]

    def test_task_ids_are_unique(self, client: TestClient, fake_r: fakeredis.FakeRedis) -> None:
        client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        tasks = _drain_queue(fake_r)
        ids = [t["task_id"] for t in tasks]
        assert len(set(ids)) == len(ids)

    def test_task_has_page_id(self, client: TestClient, fake_r: fakeredis.FakeRedis) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        tasks = _drain_queue(fake_r)
        assert all("page_id" in t for t in tasks)

    def test_task_retry_count_is_zero(
        self, client: TestClient, fake_r: fakeredis.FakeRedis
    ) -> None:
        client.post("/v1/jobs", json=_MINIMAL_BODY)
        tasks = _drain_queue(fake_r)
        assert all(t["retry_count"] == 0 for t in tasks)


# ---------------------------------------------------------------------------
# Validation — 422 errors
# ---------------------------------------------------------------------------


class TestJobCreateValidation:
    def test_missing_collection_id(self, client: TestClient) -> None:
        body = {k: v for k, v in _MINIMAL_BODY.items() if k != "collection_id"}
        assert client.post("/v1/jobs", json=body).status_code == 422

    def test_missing_policy_version(self, client: TestClient) -> None:
        body = {k: v for k, v in _MINIMAL_BODY.items() if k != "policy_version"}
        assert client.post("/v1/jobs", json=body).status_code == 422

    def test_missing_pages(self, client: TestClient) -> None:
        body = {k: v for k, v in _MINIMAL_BODY.items() if k != "pages"}
        assert client.post("/v1/jobs", json=body).status_code == 422

    def test_empty_pages_list(self, client: TestClient) -> None:
        assert client.post("/v1/jobs", json={**_MINIMAL_BODY, "pages": []}).status_code == 422

    def test_invalid_material_type(self, client: TestClient) -> None:
        assert (
            client.post("/v1/jobs", json={**_MINIMAL_BODY, "material_type": "scroll"}).status_code
            == 422
        )

    def test_invalid_pipeline_mode(self, client: TestClient) -> None:
        assert (
            client.post("/v1/jobs", json={**_MINIMAL_BODY, "pipeline_mode": "batch"}).status_code
            == 422
        )

    def test_page_number_zero(self, client: TestClient) -> None:
        body = {**_MINIMAL_BODY, "pages": [{"page_number": 0, "input_uri": "s3://x/y.tiff"}]}
        assert client.post("/v1/jobs", json=body).status_code == 422

    def test_page_number_negative(self, client: TestClient) -> None:
        body = {**_MINIMAL_BODY, "pages": [{"page_number": -1, "input_uri": "s3://x/y.tiff"}]}
        assert client.post("/v1/jobs", json=body).status_code == 422

    def test_missing_input_uri(self, client: TestClient) -> None:
        body = {**_MINIMAL_BODY, "pages": [{"page_number": 1}]}
        assert client.post("/v1/jobs", json=body).status_code == 422


# ---------------------------------------------------------------------------
# Redis failure → 503
# ---------------------------------------------------------------------------


class TestJobCreateRedisFailure:
    def test_redis_error_returns_503(self, client: TestClient) -> None:
        from unittest.mock import patch

        import services.eep.app.jobs.create as create_mod

        with patch.object(
            create_mod,
            "enqueue_page_task",
            side_effect=redis_lib.RedisError("queue down"),
        ):
            r = client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert r.status_code == 503

    def test_503_has_detail(self, client: TestClient) -> None:
        from unittest.mock import patch

        import services.eep.app.jobs.create as create_mod

        with patch.object(
            create_mod,
            "enqueue_page_task",
            side_effect=redis_lib.RedisError("queue down"),
        ):
            r = client.post("/v1/jobs", json=_MINIMAL_BODY)
        assert "detail" in r.json()

    def test_partial_enqueue_still_raises_503(self, client: TestClient) -> None:
        """
        If N tasks enqueue successfully but task N+1 fails, the caller still
        gets 503 — there is no partial success without error propagation.
        """
        from unittest.mock import patch

        import services.eep.app.jobs.create as create_mod

        call_count = 0

        def _fail_on_second(*_args: Any, **_kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise redis_lib.RedisError("second enqueue fails")

        with patch.object(create_mod, "enqueue_page_task", side_effect=_fail_on_second):
            r = client.post("/v1/jobs", json=_THREE_PAGE_BODY)
        assert r.status_code == 503

    def test_warning_log_emitted_on_redis_failure(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A WARNING log must be emitted when enqueue fails after DB commit,
        containing the job_id and partial-enqueue count so that operators
        know reconciliation (Packet 4.7) is required.
        """
        import logging
        from unittest.mock import patch

        import services.eep.app.jobs.create as create_mod

        with caplog.at_level(logging.WARNING, logger="services.eep.app.jobs.create"):
            with patch.object(
                create_mod,
                "enqueue_page_task",
                side_effect=redis_lib.RedisError("queue down"),
            ):
                client.post("/v1/jobs", json=_MINIMAL_BODY)

        assert any(
            "WARNING" in r.levelname for r in caplog.records
        ), "Expected a WARNING log record from create_job on Redis failure"

    def test_warning_log_contains_job_id(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING log must include the job_id (UUID) so operators can trace it."""
        import logging
        from unittest.mock import patch

        import services.eep.app.jobs.create as create_mod

        with caplog.at_level(logging.WARNING, logger="services.eep.app.jobs.create"):
            with patch.object(
                create_mod,
                "enqueue_page_task",
                side_effect=redis_lib.RedisError("queue down"),
            ):
                client.post("/v1/jobs", json=_MINIMAL_BODY)

        # The warning message must contain a UUID (the job_id).
        _uuid_in_str = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        assert any(
            _uuid_in_str.search(record.getMessage()) for record in caplog.records
        ), "Expected a UUID (job_id) in the WARNING log message"
