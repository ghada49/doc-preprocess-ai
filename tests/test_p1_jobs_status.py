"""
tests/test_p1_jobs_status.py
-----------------------------
Packet 1.9 contract tests for GET /v1/jobs/{job_id}.

Tests cover:
  - HTTP 200 with correct response schema (summary + pages)
  - HTTP 404 for unknown job_id
  - Job status derivation follows leaf-page rules exactly:
      all queued            → "queued"
      any non-terminal      → "running"
      all terminal, ≥1 non-failed → "done"
      all terminal, all failed    → "failed"
  - ptiff_qa_pending is counted as non-terminal (job stays "running")
  - Split-parent pages (status='split') are excluded from derivation
  - Per-page fields populated correctly

Uses MagicMock session (same pattern as test_p1_jobs_create.py) to avoid
PostgreSQL-specific JSONB DDL incompatibility with SQLite.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from services.eep.app.jobs.status import _derive_job_status, router

# ---------------------------------------------------------------------------
# ORM object builders
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _job(**kwargs: Any) -> Job:
    defaults: dict[str, Any] = dict(
        job_id="job-001",
        collection_id="col-001",
        material_type="book",
        pipeline_mode="layout",
        ptiff_qa_mode="manual",
        policy_version="v1.0",
        shadow_mode=False,
        status="queued",
        page_count=1,
        accepted_count=0,
        review_count=0,
        failed_count=0,
        pending_human_correction_count=0,
        created_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        completed_at=None,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _page(**kwargs: Any) -> JobPage:
    defaults: dict[str, Any] = dict(
        page_id="page-001",
        job_id="job-001",
        page_number=1,
        sub_page_index=None,
        status="queued",
        routing_path=None,
        escalated_to_gpu=False,
        input_image_uri="s3://bucket/p1.tiff",
        output_image_uri=None,
        output_layout_uri=None,
        quality_summary=None,
        layout_consensus_result=None,
        acceptance_decision=None,
        review_reasons=None,
        processing_time_ms=None,
        status_updated_at=None,
        created_at=_NOW,
        completed_at=None,
    )
    defaults.update(kwargs)
    return JobPage(**defaults)


# ---------------------------------------------------------------------------
# Mock session factory
# ---------------------------------------------------------------------------


def _mock_db(job: Job | None, pages: list[JobPage]) -> MagicMock:
    """Return a mock Session that serves the given job and pages."""
    session = MagicMock(spec=Session)
    session.get.return_value = job
    session.query.return_value.filter.return_value.all.return_value = pages
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_MOCK_ADMIN = CurrentUser(user_id="test-admin", role="admin")


@pytest.fixture()
def test_app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[require_user] = lambda: _MOCK_ADMIN
    return _app


@pytest.fixture()
def client_for(
    test_app: FastAPI,
) -> Generator[Callable[[Job | None, list[JobPage]], TestClient], None, None]:
    """
    Return a factory: client_for(job, pages) → TestClient.
    Injects a mock session that returns the given job and pages.
    """

    def _make(job: Job | None, pages: list[JobPage]) -> TestClient:
        test_app.dependency_overrides[get_session] = lambda: _mock_db(job, pages)
        return TestClient(test_app)

    yield _make
    test_app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# HTTP status
# ---------------------------------------------------------------------------


class TestJobStatusHttpCodes:
    def test_200_for_existing_job(self, client_for: Any) -> None:
        client = client_for(_job(), [_page()])
        assert client.get("/v1/jobs/job-001").status_code == 200

    def test_404_for_unknown_job(self, client_for: Any) -> None:
        client = client_for(None, [])
        assert client.get("/v1/jobs/no-such-job").status_code == 404

    def test_404_has_detail(self, client_for: Any) -> None:
        client = client_for(None, [])
        r = client.get("/v1/jobs/no-such-job")
        assert "detail" in r.json()


# ---------------------------------------------------------------------------
# Response schema — top-level structure
# ---------------------------------------------------------------------------


class TestJobStatusResponseSchema:
    def test_has_summary(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert "summary" in r.json()

    def test_has_pages(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert "pages" in r.json()

    def test_summary_has_job_id(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["job_id"] == "job-001"

    def test_summary_has_status(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert "status" in r.json()["summary"]

    def test_summary_has_page_count(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert "page_count" in r.json()["summary"]

    def test_summary_has_created_at(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert "created_at" in r.json()["summary"]

    def test_pages_is_list(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert isinstance(r.json()["pages"], list)

    def test_pages_count_matches(self, client_for: Any) -> None:
        pages = [_page(page_id="p1", page_number=1), _page(page_id="p2", page_number=2)]
        r = client_for(_job(page_count=2), pages).get("/v1/jobs/job-001")
        assert len(r.json()["pages"]) == 2


# ---------------------------------------------------------------------------
# Summary field values
# ---------------------------------------------------------------------------


class TestJobSummaryValues:
    def test_collection_id(self, client_for: Any) -> None:
        r = client_for(_job(collection_id="col-xyz"), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["collection_id"] == "col-xyz"

    def test_pipeline_mode(self, client_for: Any) -> None:
        r = client_for(_job(pipeline_mode="preprocess"), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["pipeline_mode"] == "preprocess"

    def test_ptiff_qa_mode(self, client_for: Any) -> None:
        r = client_for(_job(ptiff_qa_mode="auto_continue"), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["ptiff_qa_mode"] == "auto_continue"

    def test_material_type(self, client_for: Any) -> None:
        r = client_for(_job(material_type="newspaper"), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["material_type"] == "newspaper"

    def test_shadow_mode(self, client_for: Any) -> None:
        r = client_for(_job(shadow_mode=True), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["shadow_mode"] is True

    def test_policy_version(self, client_for: Any) -> None:
        r = client_for(_job(policy_version="v2.0"), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["policy_version"] == "v2.0"

    def test_page_count(self, client_for: Any) -> None:
        r = client_for(_job(page_count=5), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["page_count"] == 5

    def test_accepted_count(self, client_for: Any) -> None:
        r = client_for(_job(accepted_count=3), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["accepted_count"] == 3

    def test_review_count(self, client_for: Any) -> None:
        r = client_for(_job(review_count=1), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["review_count"] == 1

    def test_failed_count(self, client_for: Any) -> None:
        r = client_for(_job(failed_count=2), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["failed_count"] == 2

    def test_pending_human_correction_count(self, client_for: Any) -> None:
        r = client_for(_job(pending_human_correction_count=1), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["pending_human_correction_count"] == 1

    def test_completed_at_none(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["summary"]["completed_at"] is None


# ---------------------------------------------------------------------------
# Per-page fields
# ---------------------------------------------------------------------------


class TestPageStatusFields:
    def test_page_number(self, client_for: Any) -> None:
        r = client_for(_job(), [_page(page_number=7)]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["page_number"] == 7

    def test_page_status(self, client_for: Any) -> None:
        r = client_for(_job(), [_page(status="preprocessing")]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["status"] == "preprocessing"

    def test_sub_page_index_none(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["sub_page_index"] is None

    def test_sub_page_index_set(self, client_for: Any) -> None:
        r = client_for(_job(), [_page(sub_page_index=0)]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["sub_page_index"] == 0

    def test_output_image_uri_none(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["output_image_uri"] is None

    def test_output_image_uri_set(self, client_for: Any) -> None:
        r = client_for(_job(), [_page(output_image_uri="s3://bucket/out.tiff")]).get(
            "/v1/jobs/job-001"
        )
        assert r.json()["pages"][0]["output_image_uri"] == "s3://bucket/out.tiff"

    def test_acceptance_decision_none(self, client_for: Any) -> None:
        r = client_for(_job(), [_page()]).get("/v1/jobs/job-001")
        assert r.json()["pages"][0]["acceptance_decision"] is None


# ---------------------------------------------------------------------------
# Job status derivation — unit tests on _derive_job_status
# ---------------------------------------------------------------------------


class TestDeriveJobStatusUnit:
    """Direct unit tests on the _derive_job_status helper."""

    def test_empty_pages_returns_queued(self) -> None:
        assert _derive_job_status([]) == "queued"

    def test_all_queued_returns_queued(self) -> None:
        pages = [_page(status="queued"), _page(page_id="p2", status="queued")]
        assert _derive_job_status(pages) == "queued"

    def test_any_preprocessing_returns_running(self) -> None:
        pages = [_page(status="queued"), _page(page_id="p2", status="preprocessing")]
        assert _derive_job_status(pages) == "running"

    def test_any_rectification_returns_running(self) -> None:
        assert _derive_job_status([_page(status="rectification")]) == "running"

    def test_any_layout_detection_returns_running(self) -> None:
        assert _derive_job_status([_page(status="layout_detection")]) == "running"

    def test_ptiff_qa_pending_returns_running(self) -> None:
        """ptiff_qa_pending is non-terminal — job must be 'running', not 'done'."""
        assert _derive_job_status([_page(status="ptiff_qa_pending")]) == "running"

    def test_ptiff_qa_pending_mixed_with_accepted_still_running(self) -> None:
        """Any ptiff_qa_pending page keeps the job running even if others are terminal."""
        pages = [
            _page(page_id="p1", status="accepted"),
            _page(page_id="p2", status="ptiff_qa_pending"),
        ]
        assert _derive_job_status(pages) == "running"

    def test_all_accepted_returns_done(self) -> None:
        pages = [_page(status="accepted"), _page(page_id="p2", status="accepted")]
        assert _derive_job_status(pages) == "done"

    def test_accepted_and_review_returns_done(self) -> None:
        pages = [_page(status="accepted"), _page(page_id="p2", status="review")]
        assert _derive_job_status(pages) == "done"

    def test_accepted_and_failed_returns_done(self) -> None:
        pages = [_page(status="accepted"), _page(page_id="p2", status="failed")]
        assert _derive_job_status(pages) == "done"

    def test_pending_human_correction_and_accepted_returns_done(self) -> None:
        """pending_human_correction is worker-terminal — job may be done."""
        pages = [
            _page(page_id="p1", status="accepted"),
            _page(page_id="p2", status="pending_human_correction"),
        ]
        assert _derive_job_status(pages) == "done"

    def test_all_failed_returns_failed(self) -> None:
        pages = [_page(status="failed"), _page(page_id="p2", status="failed")]
        assert _derive_job_status(pages) == "failed"

    def test_single_failed_returns_failed(self) -> None:
        assert _derive_job_status([_page(status="failed")]) == "failed"

    def test_failed_and_review_returns_done(self) -> None:
        """Mixed terminal with at least one non-failed → done."""
        pages = [_page(status="failed"), _page(page_id="p2", status="review")]
        assert _derive_job_status(pages) == "done"

    def test_split_pages_excluded(self) -> None:
        """Split-parent records must not affect derivation."""
        # One split parent (excluded) + one accepted leaf → done
        pages = [
            _page(page_id="parent", status="split"),
            _page(page_id="child", sub_page_index=0, status="accepted"),
        ]
        assert _derive_job_status(pages) == "done"

    def test_only_split_pages_returns_queued(self) -> None:
        """If all pages are split parents (leaf list is empty) → queued.

        The endpoint filters split-parents before calling _derive_job_status,
        so an all-split scenario reaches this function as an empty list.
        """
        assert _derive_job_status([]) == "queued"


# ---------------------------------------------------------------------------
# Job status derivation — integration via endpoint
# ---------------------------------------------------------------------------


class TestJobStatusDerivationViaEndpoint:
    """Verify derivation via the full HTTP endpoint (mock session)."""

    def test_all_queued_returns_queued(self, client_for: Any) -> None:
        pages = [_page(status="queued")]
        r = client_for(_job(), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "queued"

    def test_processing_page_returns_running(self, client_for: Any) -> None:
        pages = [_page(status="preprocessing")]
        r = client_for(_job(), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "running"

    def test_ptiff_qa_pending_returns_running(self, client_for: Any) -> None:
        """Core Packet 1.9 requirement: ptiff_qa_pending → running."""
        pages = [_page(status="ptiff_qa_pending")]
        r = client_for(_job(), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "running"

    def test_all_accepted_returns_done(self, client_for: Any) -> None:
        pages = [_page(status="accepted")]
        r = client_for(_job(accepted_count=1), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "done"

    def test_all_failed_returns_failed(self, client_for: Any) -> None:
        pages = [_page(status="failed")]
        r = client_for(_job(failed_count=1), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "failed"

    def test_mixed_terminal_returns_done(self, client_for: Any) -> None:
        pages = [
            _page(page_id="p1", status="accepted"),
            _page(page_id="p2", status="review"),
            _page(page_id="p3", status="failed"),
        ]
        r = client_for(
            _job(page_count=3, accepted_count=1, review_count=1, failed_count=1), pages
        ).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "done"

    def test_split_parent_excluded_from_derivation(self, client_for: Any) -> None:
        """split parent row must not be counted; only accepted child counts."""
        pages = [
            _page(page_id="parent", sub_page_index=None, status="split"),
            _page(page_id="child-0", sub_page_index=0, status="accepted"),
            _page(page_id="child-1", sub_page_index=1, status="accepted"),
        ]
        r = client_for(_job(page_count=2, accepted_count=2), pages).get("/v1/jobs/job-001")
        assert r.json()["summary"]["status"] == "done"

    def test_split_parent_in_response_pages_list(self, client_for: Any) -> None:
        """Split parent is excluded from derivation but still appears in pages list."""
        pages = [
            _page(page_id="parent", status="split"),
            _page(page_id="child", sub_page_index=0, status="accepted"),
        ]
        r = client_for(_job(page_count=2), pages).get("/v1/jobs/job-001")
        assert len(r.json()["pages"]) == 2
