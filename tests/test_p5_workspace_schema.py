"""
tests/test_p5_workspace_schema.py
----------------------------------
Packet 5.0 — Correction workspace schema validation tests.

Covers:
  - GeometrySummary: valid construction, field ranges, invalid values
  - BranchOutputs: all-null, partial, and full population
  - CorrectionWorkspaceResponse: valid construction with full payload
  - CorrectionWorkspaceResponse: original-only scenario (no derived artifacts)
  - CorrectionWorkspaceResponse: original + branch artifacts scenario
  - CorrectionWorkspaceResponse: missing optional branch outputs (iep1d null)
  - CorrectionWorkspaceResponse: split vs non-split page (current_split_x)
  - CorrectionWorkspaceResponse: pipeline_mode field (preprocess vs layout)
  - CorrectionWorkspaceResponse: current_crop_box length constraint (must be 4)
  - CorrectionWorkspaceResponse: geometry_confidence out-of-range rejected
  - CorrectionWorkspaceResponse: page_number < 1 rejected
  - CorrectionWorkspaceResponse: review_reasons is always a list (not null)
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from services.eep.app.correction.workspace_schema import (
    BranchOutputs,
    CorrectionWorkspaceResponse,
    GeometrySummary,
)

# ── GeometrySummary ────────────────────────────────────────────────────────────


class TestGeometrySummary:
    def test_valid_single_page(self) -> None:
        gs = GeometrySummary(page_count=1, split_required=False, geometry_confidence=0.9)
        assert gs.page_count == 1
        assert gs.split_required is False
        assert gs.geometry_confidence == pytest.approx(0.9)

    def test_valid_split_page(self) -> None:
        gs = GeometrySummary(page_count=2, split_required=True, geometry_confidence=0.75)
        assert gs.page_count == 2
        assert gs.split_required is True

    def test_page_count_0_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometrySummary(page_count=0, split_required=False, geometry_confidence=0.5)

    def test_page_count_3_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometrySummary(page_count=3, split_required=False, geometry_confidence=0.5)

    def test_geometry_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometrySummary(page_count=1, split_required=False, geometry_confidence=-0.01)

    def test_geometry_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeometrySummary(page_count=1, split_required=False, geometry_confidence=1.01)

    def test_geometry_confidence_boundary_zero(self) -> None:
        gs = GeometrySummary(page_count=1, split_required=False, geometry_confidence=0.0)
        assert gs.geometry_confidence == 0.0

    def test_geometry_confidence_boundary_one(self) -> None:
        gs = GeometrySummary(page_count=1, split_required=False, geometry_confidence=1.0)
        assert gs.geometry_confidence == 1.0


# ── BranchOutputs ──────────────────────────────────────────────────────────────


class TestBranchOutputs:
    def test_all_null_defaults(self) -> None:
        bo = BranchOutputs()
        assert bo.iep1a_geometry is None
        assert bo.iep1b_geometry is None
        assert bo.iep1c_normalized is None
        assert bo.iep1d_rectified is None

    def test_partial_population_iep1a_only(self) -> None:
        bo = BranchOutputs(
            iep1a_geometry=GeometrySummary(
                page_count=1, split_required=False, geometry_confidence=0.88
            )
        )
        assert bo.iep1a_geometry is not None
        assert bo.iep1b_geometry is None

    def test_full_population(self) -> None:
        gs_a = GeometrySummary(page_count=2, split_required=True, geometry_confidence=0.87)
        gs_b = GeometrySummary(page_count=1, split_required=False, geometry_confidence=0.91)
        bo = BranchOutputs(
            iep1a_geometry=gs_a,
            iep1b_geometry=gs_b,
            iep1c_normalized="s3://bucket/iep1c.tiff",
            iep1d_rectified="s3://bucket/iep1d.tiff",
        )
        assert bo.iep1a_geometry == gs_a
        assert bo.iep1b_geometry == gs_b
        assert bo.iep1c_normalized == "s3://bucket/iep1c.tiff"
        assert bo.iep1d_rectified == "s3://bucket/iep1d.tiff"

    def test_iep1d_null_when_not_used(self) -> None:
        bo = BranchOutputs(iep1c_normalized="s3://bucket/norm.tiff")
        assert bo.iep1d_rectified is None


# ── Helpers for building workspace responses ───────────────────────────────────


def _minimal_workspace(**overrides: Any) -> CorrectionWorkspaceResponse:
    """Build the smallest valid CorrectionWorkspaceResponse."""
    defaults: dict[str, Any] = {
        "job_id": "job-001",
        "page_number": 1,
        "sub_page_index": None,
        "material_type": "book",
        "pipeline_mode": "layout",
        "review_reasons": ["structural_disagreement_post_rectification"],
        "branch_outputs": BranchOutputs(),
    }
    defaults.update(overrides)
    return CorrectionWorkspaceResponse(**defaults)


def _full_branch_outputs() -> BranchOutputs:
    return BranchOutputs(
        iep1a_geometry=GeometrySummary(page_count=2, split_required=True, geometry_confidence=0.87),
        iep1b_geometry=GeometrySummary(
            page_count=1, split_required=False, geometry_confidence=0.91
        ),
        iep1c_normalized="s3://bucket/normalized.tiff",
        iep1d_rectified=None,
    )


# ── CorrectionWorkspaceResponse ────────────────────────────────────────────────


class TestCorrectionWorkspaceResponseSchema:
    def test_valid_minimal(self) -> None:
        ws = _minimal_workspace()
        assert ws.job_id == "job-001"
        assert ws.page_number == 1
        assert ws.sub_page_index is None
        assert ws.material_type == "book"
        assert ws.pipeline_mode == "layout"
        assert ws.review_reasons == ["structural_disagreement_post_rectification"]
        assert ws.original_otiff_uri is None
        assert ws.best_output_uri is None
        assert ws.current_crop_box is None
        assert ws.current_deskew_angle is None
        assert ws.current_split_x is None

    def test_page_number_below_1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_workspace(page_number=0)

    def test_page_number_1_valid(self) -> None:
        ws = _minimal_workspace(page_number=1)
        assert ws.page_number == 1

    def test_invalid_material_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_workspace(material_type="magazine")

    def test_all_valid_material_types(self) -> None:
        for mt in ("book", "newspaper", "archival_document"):
            ws = _minimal_workspace(material_type=mt)
            assert ws.material_type == mt

    def test_invalid_pipeline_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_workspace(pipeline_mode="ocr")

    def test_pipeline_mode_preprocess(self) -> None:
        ws = _minimal_workspace(pipeline_mode="preprocess")
        assert ws.pipeline_mode == "preprocess"

    def test_pipeline_mode_layout(self) -> None:
        ws = _minimal_workspace(pipeline_mode="layout")
        assert ws.pipeline_mode == "layout"

    def test_review_reasons_empty_list_valid(self) -> None:
        ws = _minimal_workspace(review_reasons=[])
        assert ws.review_reasons == []

    def test_review_reasons_multiple_codes(self) -> None:
        ws = _minimal_workspace(review_reasons=["geometry_sanity_failed", "tta_variance_high"])
        assert len(ws.review_reasons) == 2

    def test_sub_page_index_0_valid(self) -> None:
        ws = _minimal_workspace(sub_page_index=0)
        assert ws.sub_page_index == 0

    def test_sub_page_index_1_valid(self) -> None:
        ws = _minimal_workspace(sub_page_index=1)
        assert ws.sub_page_index == 1

    # ── current_crop_box ──────────────────────────────────────────────────────

    def test_current_crop_box_valid_4_ints(self) -> None:
        ws = _minimal_workspace(current_crop_box=[100, 80, 2400, 3200])
        assert ws.current_crop_box == [100, 80, 2400, 3200]

    def test_current_crop_box_too_short_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_workspace(current_crop_box=[100, 80, 2400])

    def test_current_crop_box_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_workspace(current_crop_box=[100, 80, 2400, 3200, 0])

    def test_current_crop_box_none_valid(self) -> None:
        ws = _minimal_workspace(current_crop_box=None)
        assert ws.current_crop_box is None

    # ── Scenario: original-only (no derived artifacts) ────────────────────────

    def test_original_only_scenario(self) -> None:
        """No derived artifacts available — only the OTIFF is present."""
        ws = CorrectionWorkspaceResponse(
            job_id="job-original-only",
            page_number=3,
            sub_page_index=None,
            material_type="newspaper",
            pipeline_mode="layout",
            review_reasons=["geometry_failed"],
            original_otiff_uri="s3://bucket/raw/page3.tiff",
            best_output_uri=None,
            branch_outputs=BranchOutputs(),
            current_crop_box=None,
            current_deskew_angle=None,
            current_split_x=None,
        )
        assert ws.original_otiff_uri == "s3://bucket/raw/page3.tiff"
        assert ws.best_output_uri is None
        assert ws.branch_outputs.iep1c_normalized is None
        assert ws.branch_outputs.iep1d_rectified is None
        assert ws.branch_outputs.iep1a_geometry is None
        assert ws.branch_outputs.iep1b_geometry is None

    # ── Scenario: original + branch artifacts ─────────────────────────────────

    def test_original_plus_branch_artifacts_scenario(self) -> None:
        """Both OTIFF and derived artifacts are present."""
        ws = CorrectionWorkspaceResponse(
            job_id="job-full",
            page_number=1,
            sub_page_index=None,
            material_type="book",
            pipeline_mode="layout",
            review_reasons=["structural_disagreement_post_rectification"],
            original_otiff_uri="s3://bucket/raw/page1.tiff",
            best_output_uri="s3://bucket/normalized/page1.tiff",
            branch_outputs=_full_branch_outputs(),
            current_crop_box=[100, 80, 2400, 3200],
            current_deskew_angle=1.3,
            current_split_x=None,
        )
        assert ws.original_otiff_uri == "s3://bucket/raw/page1.tiff"
        assert ws.best_output_uri == "s3://bucket/normalized/page1.tiff"
        assert ws.branch_outputs.iep1c_normalized == "s3://bucket/normalized.tiff"
        assert ws.branch_outputs.iep1a_geometry is not None
        assert ws.branch_outputs.iep1a_geometry.page_count == 2
        assert ws.branch_outputs.iep1b_geometry is not None
        assert ws.branch_outputs.iep1b_geometry.page_count == 1
        assert ws.current_crop_box == [100, 80, 2400, 3200]
        assert ws.current_deskew_angle == pytest.approx(1.3)

    # ── Scenario: missing optional branch outputs ──────────────────────────────

    def test_missing_iep1d_branch_output(self) -> None:
        """IEP1D was not used — iep1d_rectified is null."""
        bo = BranchOutputs(
            iep1a_geometry=GeometrySummary(
                page_count=1, split_required=False, geometry_confidence=0.9
            ),
            iep1b_geometry=GeometrySummary(
                page_count=1, split_required=False, geometry_confidence=0.85
            ),
            iep1c_normalized="s3://bucket/norm.tiff",
            iep1d_rectified=None,
        )
        ws = _minimal_workspace(
            original_otiff_uri="s3://bucket/raw.tiff",
            best_output_uri="s3://bucket/norm.tiff",
            branch_outputs=bo,
        )
        assert ws.branch_outputs.iep1d_rectified is None
        assert ws.branch_outputs.iep1c_normalized == "s3://bucket/norm.tiff"

    def test_missing_iep1b_branch_output(self) -> None:
        """IEP1B failed — iep1b_geometry is null."""
        bo = BranchOutputs(
            iep1a_geometry=GeometrySummary(
                page_count=1, split_required=False, geometry_confidence=0.8
            ),
            iep1b_geometry=None,
            iep1c_normalized="s3://bucket/norm.tiff",
        )
        ws = _minimal_workspace(branch_outputs=bo)
        assert ws.branch_outputs.iep1a_geometry is not None
        assert ws.branch_outputs.iep1b_geometry is None

    # ── Scenario: split vs non-split page ─────────────────────────────────────

    def test_non_split_page_split_x_null(self) -> None:
        ws = _minimal_workspace(current_split_x=None)
        assert ws.current_split_x is None

    def test_split_page_has_split_x(self) -> None:
        ws = _minimal_workspace(current_split_x=1200)
        assert ws.current_split_x == 1200

    def test_split_child_sub_page_index_0(self) -> None:
        ws = _minimal_workspace(sub_page_index=0, current_split_x=None)
        assert ws.sub_page_index == 0
        assert ws.current_split_x is None

    def test_split_child_sub_page_index_1(self) -> None:
        ws = _minimal_workspace(sub_page_index=1, current_split_x=None)
        assert ws.sub_page_index == 1

    # ── Serialization ─────────────────────────────────────────────────────────

    def test_serializes_to_dict_matching_spec_shape(self) -> None:
        """Verify the JSON shape matches the spec Section 11.3 example."""
        ws = CorrectionWorkspaceResponse(
            job_id="job-123",
            page_number=1,
            sub_page_index=None,
            material_type="book",
            pipeline_mode="layout",
            review_reasons=["structural_disagreement_post_rectification"],
            original_otiff_uri="s3://bucket/raw.tiff",
            best_output_uri="s3://bucket/best.tiff",
            branch_outputs=BranchOutputs(
                iep1a_geometry=GeometrySummary(
                    page_count=2, split_required=True, geometry_confidence=0.87
                ),
                iep1b_geometry=GeometrySummary(
                    page_count=1, split_required=False, geometry_confidence=0.91
                ),
                iep1c_normalized="s3://bucket/norm.tiff",
                iep1d_rectified=None,
            ),
            current_crop_box=[100, 80, 2400, 3200],
            current_deskew_angle=1.3,
            current_split_x=None,
        )
        d = ws.model_dump()
        assert d["job_id"] == "job-123"
        assert d["page_number"] == 1
        assert d["sub_page_index"] is None
        assert d["material_type"] == "book"
        assert d["pipeline_mode"] == "layout"
        assert d["review_reasons"] == ["structural_disagreement_post_rectification"]
        assert d["original_otiff_uri"] == "s3://bucket/raw.tiff"
        assert d["best_output_uri"] == "s3://bucket/best.tiff"
        bo = d["branch_outputs"]
        assert bo["iep1a_geometry"]["page_count"] == 2
        assert bo["iep1a_geometry"]["split_required"] is True
        assert bo["iep1a_geometry"]["geometry_confidence"] == pytest.approx(0.87)
        assert bo["iep1b_geometry"]["page_count"] == 1
        assert bo["iep1b_geometry"]["split_required"] is False
        assert bo["iep1c_normalized"] == "s3://bucket/norm.tiff"
        assert bo["iep1d_rectified"] is None
        assert d["current_crop_box"] == [100, 80, 2400, 3200]
        assert d["current_deskew_angle"] == pytest.approx(1.3)
        assert d["current_split_x"] is None
