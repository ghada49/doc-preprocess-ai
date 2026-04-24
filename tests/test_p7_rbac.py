"""
tests/test_p7_rbac.py
-----------------------
Packet 7.2 RBAC enforcement contract tests.

Tests cover:
  - 401 when no bearer token is supplied
  - 401 when bearer token is invalid/malformed
  - 403 when a non-admin user accesses a job they do not own
  - 200 when a user accesses their own job
  - 200 when an admin accesses any job (ownership bypass)

Uses a mini FastAPI app containing only the jobs/status router so that
the autouse bypass in conftest.py (which targets the main app) has no
effect here and real auth enforcement is exercised.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.auth import create_access_token
from services.eep.app.db.session import get_session
from services.eep.app.jobs.status import router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_job_mock(created_by: str | None = "user-abc") -> MagicMock:
    """Return a MagicMock with all fields get_job_status reads."""
    job = MagicMock()
    job.job_id = "job-001"
    job.collection_id = "col-001"
    job.material_type = "book"
    job.pipeline_mode = "layout"
    job.ptiff_qa_mode = "manual"
    job.policy_version = "v1.0"
    job.shadow_mode = False
    job.created_by = created_by
    job.page_count = 0
    job.accepted_count = 0
    job.review_count = 0
    job.failed_count = 0
    job.pending_human_correction_count = 0
    job.created_at = _NOW
    job.updated_at = _NOW
    job.completed_at = None
    job.reading_direction = "ltr"
    return job


def _make_session(job: Any) -> MagicMock:
    session = MagicMock(spec=Session)
    session.get.return_value = job
    # query(JobPage).filter(...).all() — return empty page list
    session.query.return_value.filter.return_value.all.return_value = []
    return session


def _bearer(user_id: str, role: str = "user") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mini_app() -> FastAPI:
    """Mini FastAPI app with the jobs/status router and real auth."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture()
def inject_owned_job(mini_app: FastAPI) -> None:
    """Inject a job owned by 'user-abc'."""
    mini_app.dependency_overrides[get_session] = lambda: _make_session(
        _make_job_mock(created_by="user-abc")
    )
    yield  # type: ignore[misc]
    mini_app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# RBAC contract tests
# ---------------------------------------------------------------------------


class TestRBACJobStatus:
    """GET /v1/jobs/{job_id} — require_user + assert_job_ownership."""

    def test_401_no_token(self, mini_app: FastAPI, inject_owned_job: None) -> None:
        """No Authorization header → 401."""
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/jobs/job-001")
        assert r.status_code == 401

    def test_401_invalid_token(self, mini_app: FastAPI, inject_owned_job: None) -> None:
        """Malformed bearer token → 401."""
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get(
            "/v1/jobs/job-001",
            headers={"Authorization": "Bearer this-is-not-a-jwt"},
        )
        assert r.status_code == 401

    def test_403_non_owner_user(self, mini_app: FastAPI, inject_owned_job: None) -> None:
        """Valid token for a different user → 403."""
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/jobs/job-001", headers=_bearer("other-user", "user"))
        assert r.status_code == 403

    def test_200_owner_user(self, mini_app: FastAPI, inject_owned_job: None) -> None:
        """Valid token for the job owner → 200."""
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/jobs/job-001", headers=_bearer("user-abc", "user"))
        assert r.status_code == 200

    def test_200_admin_bypasses_ownership(
        self, mini_app: FastAPI, inject_owned_job: None
    ) -> None:
        """Admin token → 200 even for a job they don't own."""
        client = TestClient(mini_app, raise_server_exceptions=False)
        r = client.get("/v1/jobs/job-001", headers=_bearer("admin-001", "admin"))
        assert r.status_code == 200

    def test_404_unknown_job(self, mini_app: FastAPI) -> None:
        """Job not found → 404 (auth passes, DB lookup fails)."""
        mini_app.dependency_overrides[get_session] = lambda: _make_session(None)
        client = TestClient(mini_app, raise_server_exceptions=False)
        try:
            r = client.get("/v1/jobs/no-such-job", headers=_bearer("user-abc", "user"))
            assert r.status_code == 404
        finally:
            mini_app.dependency_overrides.pop(get_session, None)
