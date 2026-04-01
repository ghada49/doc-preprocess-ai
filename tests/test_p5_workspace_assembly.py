"""
tests/test_p5_workspace_assembly.py
-------------------------------------
Packet 5.0 — Correction workspace data assembly tests.

Covers:
  - PageNotInCorrectionError when job not found
  - PageNotInCorrectionError when page not found
  - PageNotInCorrectionError when page status != pending_human_correction
  - original-only scenario (no lineage, no gate, no derived artifact)
  - original + branch artifacts (lineage + gate + output_image_uri)
  - iep1c_normalized == best_output_uri in all cases
  - iep1d_rectified retrieved from service_invocations.metrics when available
  - iep1d_rectified is None when iep1d_used=False
  - iep1d_rectified is None when metrics is None (current implementation)
  - geometry summaries built from quality_gate_log iep1a/iep1b JSONB
  - missing optional iep1b branch output (iep1b_geometry is None)
  - current_crop_box from human_correction_fields (re-correction priority)
  - current_crop_box from gate geometry (first-time correction fallback)
  - current_split_x from gate geometry for split page
  - current_split_x None for single-page
  - current_deskew_angle from human_correction_fields when available
  - current_deskew_angle None for fresh correction (not in DB)
  - split child page (sub_page_index=0, sub_page_index=1)
  - review_reasons list from job_pages (empty when null in DB)
  - pipeline_mode from job record
  - material_type from job record
  - malformed gate JSONB does not crash assembly (returns None geometry)
  - selected_model=None in gate falls back to iep1a then iep1b
  - selected_model='iep1b' uses iep1b geometry for crop_box derivation

Session is mocked — no live database required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.eep.app.correction.workspace_assembly import (
    PageNotInCorrectionError,
    _geometry_summary_from_jsonb,
    _params_from_gate,
    _params_from_human_correction,
    assemble_correction_workspace,
)
from services.eep.app.correction.workspace_schema import CorrectionWorkspaceResponse
from services.eep.app.db.models import Job, JobPage, PageLineage, QualityGateLog

# ── Fixtures and factories ─────────────────────────────────────────────────────


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


def _make_job(
    job_id: str = "job-001",
    material_type: str = "book",
    pipeline_mode: str = "layout",
) -> Job:
    job = MagicMock(spec=Job)
    job.job_id = job_id
    job.material_type = material_type
    job.pipeline_mode = pipeline_mode
    return job


def _make_page(
    page_id: str = "page-001",
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    status: str = "pending_human_correction",
    review_reasons: list[str] | None = None,
    output_image_uri: str | None = None,
    input_image_uri: str = "s3://bucket/raw/page1.tiff",
) -> JobPage:
    page = MagicMock(spec=JobPage)
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    page.review_reasons = review_reasons
    page.output_image_uri = output_image_uri
    page.input_image_uri = input_image_uri
    return page


def _make_lineage(
    job_id: str = "job-001",
    page_number: int = 1,
    sub_page_index: int | None = None,
    lineage_id: str = "lin-001",
    otiff_uri: str = "s3://bucket/raw/page1.tiff",
    output_image_uri: str | None = None,
    iep1d_used: bool = False,
    human_correction_fields: dict[str, Any] | None = None,
) -> PageLineage:
    lin = MagicMock(spec=PageLineage)
    lin.job_id = job_id
    lin.page_number = page_number
    lin.sub_page_index = sub_page_index
    lin.lineage_id = lineage_id
    lin.otiff_uri = otiff_uri
    lin.output_image_uri = output_image_uri
    lin.iep1d_used = iep1d_used
    lin.human_correction_fields = human_correction_fields
    return lin


def _make_gate(
    job_id: str = "job-001",
    page_number: int = 1,
    gate_type: str = "geometry_selection",
    iep1a_geometry: dict[str, Any] | None = None,
    iep1b_geometry: dict[str, Any] | None = None,
    selected_model: str | None = "iep1a",
) -> QualityGateLog:
    gate = MagicMock(spec=QualityGateLog)
    gate.job_id = job_id
    gate.page_number = page_number
    gate.gate_type = gate_type
    gate.iep1a_geometry = iep1a_geometry
    gate.iep1b_geometry = iep1b_geometry
    gate.selected_model = selected_model
    gate.created_at = datetime.now(tz=UTC)
    return gate


def _geo_jsonb(
    page_count: int = 1,
    split_required: bool = False,
    geometry_confidence: float = 0.9,
    split_x: int | None = None,
    bbox: tuple[int, ...] = (100, 80, 2400, 3200),
) -> dict[str, Any]:
    return {
        "page_count": page_count,
        "split_required": split_required,
        "geometry_confidence": geometry_confidence,
        "split_x": split_x,
        "pages": [
            {
                "region_id": "page_0",
                "geometry_type": "bbox",
                "bbox": list(bbox),
                "confidence": geometry_confidence,
                "page_area_fraction": 0.9,
            }
        ],
    }


def _setup_session(
    session: MagicMock,
    job: Job | None,
    page: JobPage | None,
    lineage: PageLineage | None = None,
    gate: QualityGateLog | None = None,
    child_pages: list[JobPage] | None = None,
    inv_metrics: dict[str, Any] | None = None,
) -> None:
    """
    Wire the mock session so that query().filter().first() returns the correct objects
    for each model type, in the call order used by assemble_correction_workspace.
    """
    # Build side effects list in the order queries are made:
    # 1. _fetch_job     → Job
    # 2. _fetch_page    → JobPage
    # 3. _fetch_lineage → PageLineage
    # 4. _fetch_latest_geometry_gate → QualityGateLog (chained .filter().order_by().first())
    # 5. _fetch_iep1d_rectified_uri  → ServiceInvocation (only when iep1d_used=True)

    # We use a counter-based approach to return different results for successive calls.
    call_results: list[Any] = []

    # Job query
    if job is not None:
        call_results.append(job)
    else:
        call_results.append(None)

    # Page query
    if page is not None:
        call_results.append(page)
    else:
        call_results.append(None)

    # Lineage query
    call_results.append(lineage)

    # Gate query (has extra .order_by() call)
    call_results.append(gate)

    # Child-page query (has extra .order_by() + .all() call)
    call_results.append(child_pages or [])

    # IEP1D invocation query (only called when iep1d_used=True)
    if lineage is not None and lineage.iep1d_used:
        if inv_metrics is not None:
            inv = MagicMock()
            inv.metrics = inv_metrics
            call_results.append(inv)
        else:
            call_results.append(None)

    # Set up the mock chain: session.query(...).filter(...).first() or
    # session.query(...).filter(...).order_by(...).first()
    # We make query() return a fresh chain that returns results in order.
    _counter = [0]

    def _make_chain(result: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.first.return_value = result
        chain.all.return_value = result if isinstance(result, list) else []
        return chain

    def _query_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        idx = _counter[0]
        _counter[0] += 1
        if idx < len(call_results):
            return _make_chain(call_results[idx])
        return _make_chain(None)

    session.query.side_effect = _query_side_effect


# ── Unit tests for internal helpers ───────────────────────────────────────────


class TestGeometrySummaryFromJsonb:
    def test_valid_jsonb(self) -> None:
        geo = {"page_count": 2, "split_required": True, "geometry_confidence": 0.87}
        result = _geometry_summary_from_jsonb(geo)
        assert result is not None
        assert result.page_count == 2
        assert result.split_required is True
        assert result.geometry_confidence == pytest.approx(0.87)

    def test_missing_key_returns_none(self) -> None:
        result = _geometry_summary_from_jsonb({"page_count": 1})
        assert result is None

    def test_empty_dict_returns_none(self) -> None:
        assert _geometry_summary_from_jsonb({}) is None

    def test_invalid_confidence_returns_none(self) -> None:
        geo = {"page_count": 1, "split_required": False, "geometry_confidence": 1.5}
        assert _geometry_summary_from_jsonb(geo) is None


class TestParamsFromHumanCorrection:
    def test_full_correction_fields(self) -> None:
        hcf = {"crop_box": [100, 80, 2400, 3200], "deskew_angle": 1.3, "split_x": None}
        crop, deskew, split = _params_from_human_correction(hcf)
        assert crop == [100, 80, 2400, 3200]
        assert deskew == pytest.approx(1.3)
        assert split is None

    def test_split_correction_fields(self) -> None:
        hcf = {"crop_box": [50, 60, 1200, 3000], "deskew_angle": 0.5, "split_x": 600}
        crop, deskew, split = _params_from_human_correction(hcf)
        assert crop == [50, 60, 1200, 3000]
        assert split == 600

    def test_all_null_fields(self) -> None:
        hcf = {"crop_box": None, "deskew_angle": None, "split_x": None}
        crop, deskew, split = _params_from_human_correction(hcf)
        assert crop is None
        assert deskew is None
        assert split is None

    def test_coerces_crop_box_to_int(self) -> None:
        hcf = {"crop_box": [100.9, 80.1, 2400.5, 3200.0]}
        crop, _, _ = _params_from_human_correction(hcf)
        assert crop == [100, 80, 2400, 3200]
        assert all(isinstance(v, int) for v in crop)

    def test_coerces_split_x_to_int(self) -> None:
        hcf = {"split_x": 600.7}
        _, _, split = _params_from_human_correction(hcf)
        assert split == 600
        assert isinstance(split, int)


class TestParamsFromGate:
    def test_selected_iep1a_provides_crop_and_split_x(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=1200, bbox=(100, 80, 2400, 3200)),
            selected_model="iep1a",
        )
        crop, deskew, split = _params_from_gate(gate)
        assert crop == [100, 80, 2400, 3200]
        assert deskew is None  # never available from gate
        assert split == 1200

    def test_selected_iep1b_provides_crop_from_iep1b(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(bbox=(10, 10, 500, 800)),
            iep1b_geometry=_geo_jsonb(bbox=(50, 50, 900, 1200)),
            selected_model="iep1b",
        )
        crop, _, _ = _params_from_gate(gate)
        assert crop == [50, 50, 900, 1200]

    def test_selected_model_none_falls_back_to_iep1a(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(bbox=(100, 80, 2400, 3200)),
            iep1b_geometry=None,
            selected_model=None,
        )
        crop, _, _ = _params_from_gate(gate)
        assert crop == [100, 80, 2400, 3200]

    def test_selected_model_none_falls_back_to_iep1b_when_no_iep1a(self) -> None:
        gate = _make_gate(
            iep1a_geometry=None,
            iep1b_geometry=_geo_jsonb(bbox=(200, 100, 1800, 2800)),
            selected_model=None,
        )
        crop, _, _ = _params_from_gate(gate)
        assert crop == [200, 100, 1800, 2800]

    def test_no_geometry_returns_all_none(self) -> None:
        gate = _make_gate(iep1a_geometry=None, iep1b_geometry=None, selected_model=None)
        crop, deskew, split = _params_from_gate(gate)
        assert crop is None
        assert deskew is None
        assert split is None

    def test_single_page_no_split_x(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=None),
            selected_model="iep1a",
        )
        _, _, split = _params_from_gate(gate)
        assert split is None

    def test_split_page_has_split_x(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=1150, split_required=True, page_count=2),
            selected_model="iep1a",
        )
        _, _, split = _params_from_gate(gate)
        assert split == 1150

    def test_deskew_angle_always_none(self) -> None:
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(),
            selected_model="iep1a",
        )
        _, deskew, _ = _params_from_gate(gate)
        assert deskew is None


# ── Integration: assemble_correction_workspace ────────────────────────────────


class TestAssembleCorrectionWorkspace:
    # ── Error cases ────────────────────────────────────────────────────────────

    def test_raises_when_job_not_found(self, session: MagicMock) -> None:
        _setup_session(session, job=None, page=None)
        with pytest.raises(PageNotInCorrectionError, match="Job not found"):
            assemble_correction_workspace(session, "missing-job", 1)

    def test_raises_when_page_not_found(self, session: MagicMock) -> None:
        _setup_session(session, job=_make_job(), page=None)
        with pytest.raises(PageNotInCorrectionError, match="Page not found"):
            assemble_correction_workspace(session, "job-001", 1)

    def test_raises_when_page_status_is_accepted(self, session: MagicMock) -> None:
        page = _make_page(status="accepted")
        _setup_session(session, job=_make_job(), page=page)
        with pytest.raises(PageNotInCorrectionError, match="status is 'accepted'"):
            assemble_correction_workspace(session, "job-001", 1)

    def test_raises_when_page_status_is_queued(self, session: MagicMock) -> None:
        page = _make_page(status="queued")
        _setup_session(session, job=_make_job(), page=page)
        with pytest.raises(PageNotInCorrectionError, match="status is 'queued'"):
            assemble_correction_workspace(session, "job-001", 1)

    def test_raises_when_page_status_is_preprocessing(self, session: MagicMock) -> None:
        page = _make_page(status="preprocessing")
        _setup_session(session, job=_make_job(), page=page)
        with pytest.raises(PageNotInCorrectionError):
            assemble_correction_workspace(session, "job-001", 1)

    # ── Scenario: original-only (no derived artifacts, no lineage, no gate) ───

    def test_original_only_scenario(self, session: MagicMock) -> None:
        """Page in correction with no lineage and no gate data available."""
        job = _make_job(material_type="newspaper", pipeline_mode="layout")
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["geometry_failed"],
            output_image_uri=None,
            input_image_uri="s3://bucket/raw/page5.tiff",
        )
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 5)

        assert isinstance(result, CorrectionWorkspaceResponse)
        assert result.job_id == "job-001"
        assert result.page_number == 5
        assert result.sub_page_index is None
        assert result.material_type == "newspaper"
        assert result.pipeline_mode == "layout"
        assert result.review_reasons == ["geometry_failed"]
        # No lineage → falls back to input_image_uri
        assert result.original_otiff_uri == "s3://bucket/raw/page5.tiff"
        assert result.best_output_uri is None
        assert result.branch_outputs.iep1c_normalized is None
        assert result.branch_outputs.iep1d_rectified is None
        assert result.branch_outputs.iep1a_geometry is None
        assert result.branch_outputs.iep1b_geometry is None
        assert result.current_crop_box is None
        assert result.current_deskew_angle is None
        assert result.current_split_x is None
        assert result.suggested_page_structure == "single"
        assert result.child_pages == []

    # ── Scenario: original + branch artifacts ─────────────────────────────────

    def test_original_plus_branch_artifacts_scenario(self, session: MagicMock) -> None:
        """Page in correction with full lineage, gate data, and normalized artifact."""
        job = _make_job(material_type="book", pipeline_mode="layout")
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["structural_disagreement_post_rectification"],
            output_image_uri="s3://bucket/normalized/page1.tiff",
        )
        lineage = _make_lineage(
            otiff_uri="s3://bucket/raw/page1.tiff",
            output_image_uri="s3://bucket/normalized/page1.tiff",
            iep1d_used=False,
        )
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(
                page_count=2,
                split_required=True,
                geometry_confidence=0.87,
                split_x=1200,
                bbox=(100, 80, 2400, 3200),
            ),
            iep1b_geometry=_geo_jsonb(
                page_count=1,
                split_required=False,
                geometry_confidence=0.91,
                bbox=(110, 85, 2390, 3190),
            ),
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)

        assert result.original_otiff_uri == "s3://bucket/raw/page1.tiff"
        assert result.best_output_uri == "s3://bucket/normalized/page1.tiff"
        # iep1c_normalized == best_output_uri
        assert result.branch_outputs.iep1c_normalized == "s3://bucket/normalized/page1.tiff"
        # iep1d not used → null
        assert result.branch_outputs.iep1d_rectified is None
        # IEP1A geometry summary
        assert result.branch_outputs.iep1a_geometry is not None
        assert result.branch_outputs.iep1a_geometry.page_count == 2
        assert result.branch_outputs.iep1a_geometry.split_required is True
        assert result.branch_outputs.iep1a_geometry.geometry_confidence == pytest.approx(0.87)
        # IEP1B geometry summary
        assert result.branch_outputs.iep1b_geometry is not None
        assert result.branch_outputs.iep1b_geometry.page_count == 1
        assert result.branch_outputs.iep1b_geometry.split_required is False
        # Correction params derived from gate (iep1a selected)
        assert result.current_crop_box == [100, 80, 2400, 3200]
        assert result.current_split_x == 1200
        assert result.current_deskew_angle is None  # never from gate
        assert result.suggested_page_structure == "spread"

    # ── Missing optional branch outputs ───────────────────────────────────────

    def test_missing_iep1b_branch_output(self, session: MagicMock) -> None:
        """IEP1B geometry is null in the gate log."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["geometry_sanity_failed"],
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(geometry_confidence=0.8),
            iep1b_geometry=None,
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)

        assert result.branch_outputs.iep1a_geometry is not None
        assert result.branch_outputs.iep1b_geometry is None

    def test_missing_iep1d_branch_output(self, session: MagicMock) -> None:
        """IEP1D was not used — iep1d_rectified is always None."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["artifact_validation_failed"],
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(iep1d_used=False, output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.branch_outputs.iep1d_rectified is None

    # ── IEP1D URI retrieval ────────────────────────────────────────────────────

    def test_iep1d_rectified_uri_from_metrics(self, session: MagicMock) -> None:
        """iep1d_rectified populated from service_invocations.metrics when available."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["structural_disagreement_post_rectification"],
            output_image_uri="s3://bucket/rescue_norm.tiff",
        )
        lineage = _make_lineage(
            iep1d_used=True,
            output_image_uri="s3://bucket/rescue_norm.tiff",
        )
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(
            session,
            job=job,
            page=page,
            lineage=lineage,
            gate=gate,
            inv_metrics={"rectified_image_uri": "s3://bucket/iep1d_rect.tiff"},
        )

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.branch_outputs.iep1d_rectified == "s3://bucket/iep1d_rect.tiff"

    def test_iep1d_rectified_uri_none_when_metrics_null(self, session: MagicMock) -> None:
        """iep1d_rectified is None when service_invocations.metrics is None."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["rectification_failed"],
        )
        lineage = _make_lineage(iep1d_used=True)
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(
            session,
            job=job,
            page=page,
            lineage=lineage,
            gate=gate,
            inv_metrics=None,
        )

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.branch_outputs.iep1d_rectified is None

    # ── Split vs non-split page ───────────────────────────────────────────────

    def test_non_split_page_split_x_none(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["geometry_sanity_failed"],
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=None, split_required=False, page_count=1),
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.current_split_x is None
        assert result.suggested_page_structure == "single"

    def test_split_page_has_split_x_from_gate(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["split_confidence_low"],
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=1150, split_required=True, page_count=2),
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.current_split_x == 1150
        assert result.suggested_page_structure == "spread"

    def test_split_child_sub_page_index_0(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            sub_page_index=0,
            review_reasons=["geometry_unexpected_split_on_child"],
        )
        lineage = _make_lineage(sub_page_index=0)
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1, sub_page_index=0)
        assert result.sub_page_index == 0
        assert result.child_pages == []

    def test_split_child_sub_page_index_1(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            sub_page_index=1,
            review_reasons=["artifact_validation_failed"],
        )
        lineage = _make_lineage(sub_page_index=1)
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1, sub_page_index=1)
        assert result.sub_page_index == 1
        assert result.child_pages == []

    def test_existing_child_pages_are_returned_for_parent_workspace(
        self, session: MagicMock
    ) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["split_confidence_low"],
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=1150, split_required=True, page_count=2),
            selected_model="iep1a",
        )
        children = [
            _make_page(
                page_id="child-0",
                sub_page_index=0,
                status="pending_human_correction",
                output_image_uri="s3://bucket/jobs/job-001/corrected/1_0.tiff",
            ),
            _make_page(
                page_id="child-1",
                sub_page_index=1,
                status="ptiff_qa_pending",
                output_image_uri="s3://bucket/jobs/job-001/corrected/1_1.tiff",
            ),
        ]
        _setup_session(
            session,
            job=job,
            page=page,
            lineage=lineage,
            gate=gate,
            child_pages=children,
        )

        result = assemble_correction_workspace(session, "job-001", 1)

        assert result.suggested_page_structure == "spread"
        assert [child.sub_page_index for child in result.child_pages] == [0, 1]
        assert result.child_pages[0].status == "pending_human_correction"
        assert (
            result.child_pages[1].output_image_uri == "s3://bucket/jobs/job-001/corrected/1_1.tiff"
        )

    def test_child_workspace_includes_sibling_navigation(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            sub_page_index=0,
            review_reasons=["artifact_validation_failed"],
            output_image_uri="s3://bucket/jobs/job-001/corrected/1_0.tiff",
        )
        lineage = _make_lineage(
            sub_page_index=0,
            output_image_uri="s3://bucket/jobs/job-001/corrected/1_0.tiff",
        )
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(split_x=1150, split_required=True, page_count=2),
            selected_model="iep1a",
        )
        children = [
            _make_page(
                page_id="child-0",
                sub_page_index=0,
                status="pending_human_correction",
                output_image_uri="s3://bucket/jobs/job-001/corrected/1_0.tiff",
            ),
            _make_page(
                page_id="child-1",
                sub_page_index=1,
                status="ptiff_qa_pending",
                output_image_uri="s3://bucket/jobs/job-001/corrected/1_1.tiff",
            ),
        ]
        _setup_session(
            session,
            job=job,
            page=page,
            lineage=lineage,
            gate=gate,
            child_pages=children,
        )

        result = assemble_correction_workspace(session, "job-001", 1, sub_page_index=0)

        assert result.sub_page_index == 0
        assert [child.sub_page_index for child in result.child_pages] == [0, 1]

    # ── Human correction field priority ───────────────────────────────────────

    def test_human_correction_fields_take_priority_over_gate(self, session: MagicMock) -> None:
        """Re-correction: human_correction_fields override gate-derived values."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["artifact_validation_failed"],
            output_image_uri="s3://bucket/corrected.tiff",
        )
        lineage = _make_lineage(
            output_image_uri="s3://bucket/corrected.tiff",
            human_correction_fields={
                "crop_box": [200, 150, 1900, 2900],
                "deskew_angle": 2.5,
                "split_x": None,
            },
        )
        gate = _make_gate(
            iep1a_geometry=_geo_jsonb(bbox=(100, 80, 2400, 3200)),
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)

        # human_correction_fields wins
        assert result.current_crop_box == [200, 150, 1900, 2900]
        assert result.current_deskew_angle == pytest.approx(2.5)
        assert result.current_split_x is None

    def test_deskew_angle_from_prior_correction(self, session: MagicMock) -> None:
        """deskew_angle is available only when there is a prior human correction."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["artifact_validation_failed"],
        )
        lineage = _make_lineage(
            human_correction_fields={
                "crop_box": [100, 80, 2400, 3200],
                "deskew_angle": 1.5,
                "split_x": None,
            }
        )
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.current_deskew_angle == pytest.approx(1.5)

    def test_deskew_angle_none_for_fresh_correction(self, session: MagicMock) -> None:
        """No prior correction → deskew_angle is always None (not stored in DB)."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["normalization_failed"],
        )
        lineage = _make_lineage(human_correction_fields=None)
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.current_deskew_angle is None

    # ── review_reasons handling ───────────────────────────────────────────────

    def test_review_reasons_none_in_db_returns_empty_list(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(status="pending_human_correction", review_reasons=None)
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.review_reasons == []

    def test_review_reasons_list_preserved(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            review_reasons=["tta_variance_high", "split_confidence_low"],
        )
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.review_reasons == ["tta_variance_high", "split_confidence_low"]

    # ── Job metadata propagation ───────────────────────────────────────────────

    def test_material_type_from_job(self, session: MagicMock) -> None:
        job = _make_job(material_type="newspaper")
        page = _make_page(status="pending_human_correction")
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.material_type == "newspaper"

    def test_pipeline_mode_preprocess_from_job(self, session: MagicMock) -> None:
        job = _make_job(pipeline_mode="preprocess")
        page = _make_page(status="pending_human_correction")
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.pipeline_mode == "preprocess"

    def test_pipeline_mode_layout_from_job(self, session: MagicMock) -> None:
        job = _make_job(pipeline_mode="layout")
        page = _make_page(status="pending_human_correction")
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.pipeline_mode == "layout"

    # ── best_output_uri fallback chain ────────────────────────────────────────

    def test_best_output_uri_from_page_record(self, session: MagicMock) -> None:
        """job_pages.output_image_uri takes precedence."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            output_image_uri="s3://bucket/page_output.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/lineage_output.tiff")
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.best_output_uri == "s3://bucket/page_output.tiff"

    def test_best_output_uri_falls_back_to_lineage(self, session: MagicMock) -> None:
        """Falls back to page_lineage.output_image_uri when page has none."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            output_image_uri=None,
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/lineage_output.tiff")
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.best_output_uri == "s3://bucket/lineage_output.tiff"

    def test_best_output_uri_none_when_both_missing(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(status="pending_human_correction", output_image_uri=None)
        lineage = _make_lineage(output_image_uri=None)
        _setup_session(session, job=job, page=page, lineage=lineage, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.best_output_uri is None

    # ── iep1c_normalized == best_output_uri invariant ─────────────────────────

    def test_iep1c_normalized_equals_best_output_uri(self, session: MagicMock) -> None:
        """iep1c_normalized is always the same as best_output_uri."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            output_image_uri="s3://bucket/norm.tiff",
        )
        lineage = _make_lineage(output_image_uri="s3://bucket/norm.tiff")
        gate = _make_gate(iep1a_geometry=_geo_jsonb(), selected_model="iep1a")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.branch_outputs.iep1c_normalized == result.best_output_uri

    # ── Malformed gate JSONB tolerance ────────────────────────────────────────

    def test_malformed_gate_jsonb_does_not_crash(self, session: MagicMock) -> None:
        """Malformed/missing geometry JSONB keys → geometry summary is None."""
        job = _make_job()
        page = _make_page(status="pending_human_correction", review_reasons=["geometry_failed"])
        lineage = _make_lineage()
        gate = _make_gate(
            iep1a_geometry={"unexpected_key": "bad_data"},  # malformed
            iep1b_geometry=None,
            selected_model="iep1a",
        )
        _setup_session(session, job=job, page=page, lineage=lineage, gate=gate)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.branch_outputs.iep1a_geometry is None
        assert result.branch_outputs.iep1b_geometry is None
        # crop_box also falls back to None (no usable geometry)
        assert result.current_crop_box is None

    # ── original_otiff_uri source ─────────────────────────────────────────────

    def test_original_otiff_uri_from_lineage(self, session: MagicMock) -> None:
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            input_image_uri="s3://bucket/input_fallback.tiff",
        )
        lineage = _make_lineage(otiff_uri="s3://bucket/raw/authoritative.tiff")
        _setup_session(session, job=job, page=page, lineage=lineage, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.original_otiff_uri == "s3://bucket/raw/authoritative.tiff"

    def test_original_otiff_uri_falls_back_to_input_image_uri(self, session: MagicMock) -> None:
        """When lineage is absent, falls back to job_pages.input_image_uri."""
        job = _make_job()
        page = _make_page(
            status="pending_human_correction",
            input_image_uri="s3://bucket/fallback_input.tiff",
        )
        _setup_session(session, job=job, page=page, lineage=None, gate=None)

        result = assemble_correction_workspace(session, "job-001", 1)
        assert result.original_otiff_uri == "s3://bucket/fallback_input.tiff"
