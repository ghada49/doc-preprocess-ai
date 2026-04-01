"""
tests/test_p5_correction_queue.py
-----------------------------------
Packet 5.1 — Correction queue read endpoint tests.

Covers:
  GET /v1/correction-queue (list endpoint)
    - Returns 200 with empty list when no pending pages
    - Returns pending pages with correct fields
    - Filter by job_id
    - Filter by material_type
    - Filter by review_reason
    - Pagination offset/limit reflected in response
    - Total count reflects full match set, not just current page

  GET /v1/correction-queue/{job_id}/{page_number} (detail endpoint)
    - Returns 200 with CorrectionWorkspaceResponse when page is in correction
    - 404 when job not found
    - 404 when page not found
    - 409 when page not in pending_human_correction
    - 422 when multiple sub-pages pending and sub_page_index not provided
    - Auto-selects sub_page_index when exactly one sub-page pending
    - Accepts explicit sub_page_index query param

Session is mocked; no live database required.
HTTP endpoints are tested via FastAPI TestClient with dependency override.
assemble_correction_workspace is patched for detail endpoint tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import false

from services.eep.app.correction.workspace_assembly import PageNotInCorrectionError
from services.eep.app.correction.workspace_schema import (
    BranchOutputs,
    ChildPageSummary,
    CorrectionWorkspaceResponse,
)
from services.eep.app.db.session import get_session
from services.eep.app.main import app

pytestmark = pytest.mark.usefixtures("_bypass_require_user")

# ── Factories ──────────────────────────────────────────────────────────────────


def _make_job(
    job_id: str = "job-001",
    material_type: str = "book",
    pipeline_mode: str = "layout",
) -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    job.material_type = material_type
    job.pipeline_mode = pipeline_mode
    return job


def _make_page(
    page_id: str = "p1",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    review_reasons: list[str] | None = None,
    output_image_uri: str | None = None,
    status_updated_at: datetime | None = None,
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.review_reasons = review_reasons
    page.output_image_uri = output_image_uri
    page.status_updated_at = status_updated_at or datetime(2026, 3, 1, tzinfo=UTC)
    return page


def _minimal_workspace(
    job_id: str = "job-001",
    page_number: int = 1,
) -> CorrectionWorkspaceResponse:
    return CorrectionWorkspaceResponse(
        job_id=job_id,
        page_number=page_number,
        sub_page_index=None,
        material_type="book",
        pipeline_mode="layout",
        review_reasons=["geometry_sanity_failed"],
        branch_outputs=BranchOutputs(),
        suggested_page_structure="single",
        child_pages=[],
    )


def _make_list_session(
    rows: list[tuple[Any, Any]],
    total: int | None = None,
) -> MagicMock:
    """
    Build a mock session for the list endpoint.

    db.query(...).join(...).filter(...).with_entities(...).scalar() → total
    db.query(...).join(...).filter(...).order_by(...).offset(...).limit(...).all() → rows
    """
    session = MagicMock()
    effective_total = total if total is not None else len(rows)

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.join.return_value = chain
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.offset.return_value = chain
        chain.limit.return_value = chain
        chain.with_entities.return_value = chain
        chain.scalar.return_value = effective_total
        chain.all.return_value = rows
        chain.exists.return_value = false()
        return chain

    session.query.side_effect = query_se
    return session


def _make_detail_session(
    pending_pages: list[Any],
) -> MagicMock:
    """
    Build a mock session for the disambiguation query in the detail endpoint.

    db.query(JobPage).filter(...).all() → pending_pages
    """
    session = MagicMock()

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.all.return_value = pending_pages
        return chain

    session.query.side_effect = query_se
    return session


# ── List endpoint tests ────────────────────────────────────────────────────────


class TestListCorrectionQueue:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_empty_queue_returns_200(self) -> None:
        session = _make_list_session(rows=[], total=0)
        self._inject(session)

        r = self.client.get("/v1/correction-queue")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []
        assert data["offset"] == 0
        assert data["limit"] == 50

    def test_returns_pending_pages_with_correct_fields(self) -> None:
        job = _make_job(material_type="newspaper", pipeline_mode="layout")
        page = _make_page(
            job_id="job-001",
            page_number=3,
            review_reasons=["geometry_sanity_failed"],
            output_image_uri="s3://bucket/norm.tiff",
            status_updated_at=datetime(2026, 3, 10, 8, 0, 0, tzinfo=UTC),
        )

        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["job_id"] == "job-001"
        assert item["page_number"] == 3
        assert item["sub_page_index"] is None
        assert item["material_type"] == "newspaper"
        assert item["pipeline_mode"] == "layout"
        assert item["review_reasons"] == ["geometry_sanity_failed"]
        assert item["output_image_uri"] == "s3://bucket/norm.tiff"
        assert "2026-03-10" in item["waiting_since"]

    def test_review_reasons_null_in_db_becomes_empty_list(self) -> None:
        job = _make_job()
        page = _make_page(review_reasons=None)
        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue")
        assert r.status_code == 200
        assert r.json()["items"][0]["review_reasons"] == []

    def test_pagination_offset_limit_reflected_in_response(self) -> None:
        job = _make_job()
        pages = [((_make_page(page_id=f"p{i}", page_number=i), job)) for i in range(1, 4)]
        # Simulate page 2 of 10 (offset=5, limit=5)
        session = _make_list_session(rows=pages[:2], total=10)
        self._inject(session)

        r = self.client.get("/v1/correction-queue?offset=5&limit=5")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 10
        assert data["offset"] == 5
        assert data["limit"] == 5
        assert len(data["items"]) == 2  # only 2 rows returned by mock

    def test_filter_by_job_id_passes_param(self) -> None:
        """Query param job_id is accepted; mock returns filtered result."""
        job = _make_job(job_id="job-filtered")
        page = _make_page(job_id="job-filtered")
        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue?job_id=job-filtered")
        assert r.status_code == 200
        assert r.json()["items"][0]["job_id"] == "job-filtered"

    def test_filter_by_material_type_passes_param(self) -> None:
        job = _make_job(material_type="newspaper")
        page = _make_page()
        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue?material_type=newspaper")
        assert r.status_code == 200
        assert r.json()["items"][0]["material_type"] == "newspaper"

    def test_filter_by_review_reason_passes_param(self) -> None:
        job = _make_job()
        page = _make_page(review_reasons=["artifact_validation_failed"])
        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue?review_reason=artifact_validation_failed")
        assert r.status_code == 200
        assert "artifact_validation_failed" in r.json()["items"][0]["review_reasons"]

    def test_no_results_when_filter_matches_nothing(self) -> None:
        session = _make_list_session(rows=[], total=0)
        self._inject(session)

        r = self.client.get("/v1/correction-queue?job_id=nonexistent-job")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["items"] == []

    def test_multiple_pages_returned(self) -> None:
        job = _make_job()
        pages = [((_make_page(page_id=f"p{i}", page_number=i), job)) for i in range(1, 4)]
        session = _make_list_session(rows=pages, total=3)
        self._inject(session)

        r = self.client.get("/v1/correction-queue")
        assert r.status_code == 200
        assert r.json()["total"] == 3
        assert len(r.json()["items"]) == 3

    def test_split_child_sub_page_index_in_response(self) -> None:
        job = _make_job()
        page = _make_page(page_number=2, sub_page_index=1)
        session = _make_list_session(rows=[(page, job)], total=1)
        self._inject(session)

        r = self.client.get("/v1/correction-queue")
        assert r.status_code == 200
        assert r.json()["items"][0]["sub_page_index"] == 1


# ── Detail endpoint tests ──────────────────────────────────────────────────────


class TestGetCorrectionWorkspace:
    def setup_method(self) -> None:
        self.client = TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def _inject(self, session: MagicMock) -> None:
        app.dependency_overrides[get_session] = lambda: session

    def test_returns_workspace_for_pending_page(self) -> None:
        workspace = _minimal_workspace(job_id="job-001", page_number=1)
        page = _make_page(sub_page_index=None)

        session = _make_detail_session(pending_pages=[page])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            return_value=workspace,
        ) as mock_assemble:
            r = self.client.get("/v1/correction-queue/job-001/1")

        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == "job-001"
        assert data["page_number"] == 1
        assert data["suggested_page_structure"] == "single"
        assert data["child_pages"] == []
        mock_assemble.assert_called_once()

    def test_assembler_called_with_correct_sub_page_index(self) -> None:
        """When one sub-page found, its sub_page_index is passed to assembler."""
        workspace = _minimal_workspace()
        page = _make_page(sub_page_index=0)

        session = _make_detail_session(pending_pages=[page])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            return_value=workspace,
        ) as mock_assemble:
            self.client.get("/v1/correction-queue/job-001/1")

        _, _, _, resolved = mock_assemble.call_args[0]
        assert resolved == 0

    def test_explicit_sub_page_index_bypasses_disambiguation(self) -> None:
        """When sub_page_index is provided, no disambiguation query is made."""
        workspace = _minimal_workspace()

        # Session that would fail if queried (disambiguation skipped)
        session = MagicMock()
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            return_value=workspace,
        ) as mock_assemble:
            r = self.client.get("/v1/correction-queue/job-001/1?sub_page_index=1")

        assert r.status_code == 200
        # DB disambiguation query must NOT have been called
        session.query.assert_not_called()
        _, _, _, resolved = mock_assemble.call_args[0]
        assert resolved == 1

    def test_422_when_multiple_sub_pages_pending(self) -> None:
        """Multiple sub-pages in correction with no sub_page_index → 422."""
        p0 = _make_page(sub_page_index=0)
        p1 = _make_page(sub_page_index=1)

        session = _make_detail_session(pending_pages=[p0, p1])
        self._inject(session)

        r = self.client.get("/v1/correction-queue/job-001/1")
        assert r.status_code == 422
        assert "sub_page_index" in r.json()["detail"]

    def test_404_when_job_not_found(self) -> None:
        session = _make_detail_session(pending_pages=[])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            side_effect=PageNotInCorrectionError("Job not found: 'missing-job'"),
        ):
            r = self.client.get("/v1/correction-queue/missing-job/1")

        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_404_when_page_not_found(self) -> None:
        session = _make_detail_session(pending_pages=[])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            side_effect=PageNotInCorrectionError("Page not found: job='job-001' page=99 sub=None"),
        ):
            r = self.client.get("/v1/correction-queue/job-001/99")

        assert r.status_code == 404

    def test_409_when_page_not_in_correction(self) -> None:
        """Page exists but is in accepted state → 409."""
        session = _make_detail_session(pending_pages=[])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            side_effect=PageNotInCorrectionError(
                "Page status is 'accepted', not 'pending_human_correction'"
            ),
        ):
            r = self.client.get("/v1/correction-queue/job-001/1")

        assert r.status_code == 409

    def test_returns_full_workspace_fields(self) -> None:
        """Response schema contains all CorrectionWorkspaceResponse fields."""
        workspace = CorrectionWorkspaceResponse(
            job_id="job-123",
            page_number=2,
            sub_page_index=None,
            material_type="newspaper",
            pipeline_mode="preprocess",
            review_reasons=["artifact_validation_failed", "tta_variance_high"],
            original_otiff_uri="s3://bucket/raw.tiff",
            best_output_uri="s3://bucket/norm.tiff",
            branch_outputs=BranchOutputs(
                iep1c_normalized="s3://bucket/norm.tiff",
            ),
            suggested_page_structure="spread",
            child_pages=[
                ChildPageSummary(
                    sub_page_index=0,
                    status="pending_human_correction",
                    output_image_uri="s3://bucket/jobs/job-123/corrected/2_0.tiff",
                ),
                ChildPageSummary(
                    sub_page_index=1,
                    status="ptiff_qa_pending",
                    output_image_uri="s3://bucket/jobs/job-123/corrected/2_1.tiff",
                ),
            ],
            current_crop_box=[50, 60, 1200, 1800],
            current_deskew_angle=0.5,
            current_split_x=None,
        )
        page = _make_page(sub_page_index=None)
        session = _make_detail_session(pending_pages=[page])
        self._inject(session)

        with patch(
            "services.eep.app.correction.queue.assemble_correction_workspace",
            return_value=workspace,
        ):
            r = self.client.get("/v1/correction-queue/job-123/2")

        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == "job-123"
        assert data["page_number"] == 2
        assert data["material_type"] == "newspaper"
        assert data["pipeline_mode"] == "preprocess"
        assert data["review_reasons"] == ["artifact_validation_failed", "tta_variance_high"]
        assert data["original_otiff_uri"] == "s3://bucket/raw.tiff"
        assert data["best_output_uri"] == "s3://bucket/norm.tiff"
        assert data["branch_outputs"]["iep1c_normalized"] == "s3://bucket/norm.tiff"
        assert data["suggested_page_structure"] == "spread"
        assert [child["sub_page_index"] for child in data["child_pages"]] == [0, 1]
        assert data["current_crop_box"] == [50, 60, 1200, 1800]
        assert data["current_deskew_angle"] == pytest.approx(0.5)
        assert data["current_split_x"] is None
