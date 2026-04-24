"""
tests/test_p9_golden_dataset.py
-----------------------------------------------------------------------
Golden-dataset tests for Phase 9, Packet 9.5.

Covers five deterministic processing paths required by the roadmap
(spec implementation_roadmap.md Packet 9.5):

  1. Geometry gate routing       — run_geometry_selection() with fixed inputs
  2. Artifact validation         — hard + soft gates with known metric values
  3. IEP1C normalization outputs — normalize_single_page() with synthetic images
  4. Lineage write correctness   — create_lineage() and update helpers
  5. State machine transitions   — validate_transition(), is_leaf_final(), etc.

All inputs and expected outputs are declared explicitly.
No random data, no live database, no external services.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

# ── Geometry selection ─────────────────────────────────────────────────────────

from services.eep.app.gates.geometry_selection import (
    PreprocessingGateConfig,
    run_geometry_selection,
)
from shared.schemas.geometry import GeometryResponse, PageRegion

# ── Artifact validation ────────────────────────────────────────────────────────

from services.eep.app.gates.artifact_validation import (
    ArtifactImageDimensions,
    check_artifact_hard_requirements,
    compute_artifact_soft_score,
    run_artifact_validation,
)
from shared.schemas.preprocessing import (
    CropResult,
    DeskewResult,
    PreprocessBranchResponse,
    QualityMetrics,
    SplitResult,
)
from shared.schemas.ucf import BoundingBox, Dimensions, TransformRecord

# ── Normalization ──────────────────────────────────────────────────────────────

from shared.normalization.normalize import (
    normalize_result_to_branch_response,
    normalize_single_page,
)

# ── Lineage ────────────────────────────────────────────────────────────────────

from services.eep.app.db.lineage import (
    confirm_preprocessed_artifact,
    create_lineage,
    update_geometry_result,
    update_lineage_completion,
)

# ── State machine ──────────────────────────────────────────────────────────────

from shared.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    allowed_next,
    is_leaf_final,
    is_worker_terminal,
    validate_transition,
)


# ==============================================================================
# Shared test helpers
# ==============================================================================

_PROXY_W = 800
_PROXY_H = 1000
_MATERIAL = "book"


def _make_page_region(
    page_area_fraction: float = 0.60,
    bbox: tuple[int, int, int, int] = (100, 100, 700, 900),
    region_id: str = "page_0",
) -> PageRegion:
    """
    Valid page region that passes all six geometry sanity checks for an
    800×1000 proxy image with material_type='book'.

    Default bbox (100,100,700,900) → 600×800 region, area_fraction=0.60,
    aspect=0.75 — well within book bounds (0.5, 2.5).
    """
    return PageRegion(
        region_id=region_id,
        geometry_type="bbox",
        bbox=bbox,
        confidence=0.92,
        page_area_fraction=page_area_fraction,
    )


def _make_geometry(
    page_count: int = 1,
    split_required: bool = False,
    split_x: int | None = None,
    geometry_confidence: float = 0.92,
    tta_structural_agreement_rate: float = 0.95,
    tta_prediction_variance: float = 0.02,
    page_area_fraction: float = 0.60,
) -> GeometryResponse:
    return GeometryResponse(
        page_count=page_count,
        pages=[_make_page_region(page_area_fraction=page_area_fraction)],
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=tta_prediction_variance,
        tta_passes=3,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=100.0,
    )


def _make_branch_response(
    uri: str = "s3://bucket/test.tiff",
    skew_residual: float = 0.5,
    blur_score: float = 0.2,
    border_score: float = 0.7,
    foreground_coverage: float = 0.6,
    orig_w: int = 200,
    orig_h: int = 100,
    out_w: int = 180,
    out_h: int = 90,
    source_model: str = "iep1a",
) -> PreprocessBranchResponse:
    """
    Construct a PreprocessBranchResponse with all fields explicitly set.

    Crop box (5, 5, 195, 95) lies strictly within original (200×100),
    satisfying the TransformRecord validator.
    post_preprocessing_dimensions defaults to (180×90).
    """
    crop = BoundingBox(x_min=5.0, y_min=5.0, x_max=float(orig_w - 5), y_max=float(orig_h - 5))
    return PreprocessBranchResponse(
        processed_image_uri=uri,
        deskew=DeskewResult(angle_deg=0.0, residual_deg=skew_residual, method="geometry_bbox"),
        crop=CropResult(crop_box=crop, border_score=border_score, method="geometry_bbox"),
        split=SplitResult(split_required=False, method="none"),
        quality=QualityMetrics(
            skew_residual=skew_residual,
            blur_score=blur_score,
            border_score=border_score,
            foreground_coverage=foreground_coverage,
        ),
        transform=TransformRecord(
            original_dimensions=Dimensions(width=orig_w, height=orig_h),
            crop_box=crop,
            deskew_angle_deg=0.0,
            post_preprocessing_dimensions=Dimensions(width=out_w, height=out_h),
        ),
        source_model=source_model,  # type: ignore[arg-type]
        processing_time_ms=50.0,
        warnings=[],
    )


# ==============================================================================
# 1. Geometry gate routing
# ==============================================================================


class TestGeometrySelectionGolden:
    """Golden-dataset tests for run_geometry_selection()."""

    def test_both_agree_accepted(self) -> None:
        """Both models agree on page_count + split_required, high confidence → accepted."""
        iep1a = _make_geometry(geometry_confidence=0.92, tta_structural_agreement_rate=0.95)
        iep1b = _make_geometry(geometry_confidence=0.92, tta_structural_agreement_rate=0.95)

        result = run_geometry_selection(iep1a, iep1b, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "accepted"
        assert result.structural_agreement is True
        assert result.geometry_trust == "high"
        assert result.selected is not None

    def test_iep1a_higher_confidence_selected(self) -> None:
        """IEP1A confidence higher than IEP1B → selected as 'higher_confidence'."""
        iep1a = _make_geometry(geometry_confidence=0.95)
        iep1b = _make_geometry(geometry_confidence=0.82)

        result = run_geometry_selection(iep1a, iep1b, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "accepted"
        assert result.selected is not None
        assert result.selected.model == "iep1a"
        assert result.selection_reason == "higher_confidence"

    def test_iep1b_higher_confidence_selected(self) -> None:
        """IEP1B confidence higher than IEP1A → selected as 'higher_confidence' winner."""
        iep1a = _make_geometry(geometry_confidence=0.80)
        iep1b = _make_geometry(geometry_confidence=0.95)

        result = run_geometry_selection(iep1a, iep1b, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "accepted"
        assert result.selected is not None
        assert result.selected.model == "iep1b"
        assert result.selection_reason == "higher_confidence"

    def test_structural_disagreement_rectification(self) -> None:
        """
        IEP1A: split_required=False; IEP1B: split_required=True.

        structural_agreement=False → geometry_trust='low' → route='rectification'.
        Both candidates survive the cascade (IEP1B split_confidence=0.90 ≥ 0.75).
        """
        iep1a = _make_geometry(page_count=1, split_required=False)
        iep1b = _make_geometry(
            page_count=1,
            split_required=True,
            split_x=400,
            geometry_confidence=0.90,
            tta_structural_agreement_rate=0.92,  # split_confidence = min(0.90, 0.92) = 0.90
        )

        result = run_geometry_selection(iep1a, iep1b, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.structural_agreement is False
        assert result.route_decision == "rectification"
        assert result.geometry_trust == "low"
        assert result.selected is not None  # a winner was still chosen

    def test_both_fail_sanity_human_correction(self) -> None:
        """page_area_fraction=0.003 < 0.15 min → sanity fails → pending_human_correction."""
        tiny_region = _make_page_region(page_area_fraction=0.003)
        bad_geom = GeometryResponse(
            page_count=1,
            pages=[tiny_region],
            split_required=False,
            geometry_confidence=0.92,
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.02,
            tta_passes=3,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=100.0,
        )

        result = run_geometry_selection(bad_geom, bad_geom, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_sanity_failed"
        assert result.selected is None

    def test_split_confidence_too_low_human_correction(self) -> None:
        """
        Both models: split_required=True, split_confidence=0.60 < 0.75 threshold.

        Both removed by split filter → pending_human_correction, 'split_confidence_low'.
        """
        split_geom = _make_geometry(
            split_required=True,
            split_x=400,
            geometry_confidence=0.60,
            tta_structural_agreement_rate=0.65,  # split_conf = min(0.60, 0.65) = 0.60 < 0.75
        )

        result = run_geometry_selection(split_geom, split_geom, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "split_confidence_low"
        assert result.selected is None

    def test_tta_variance_too_high_human_correction(self) -> None:
        """
        Both models: tta_prediction_variance=0.20 > 0.15 ceiling.

        Both removed by TTA variance filter → pending_human_correction, 'tta_variance_high'.
        """
        unstable_geom = _make_geometry(
            split_required=False,
            tta_prediction_variance=0.20,  # > default ceiling 0.15
        )

        result = run_geometry_selection(unstable_geom, unstable_geom, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "tta_variance_high"
        assert result.selected is None

    def test_sole_survivor_iep1b_rectification(self) -> None:
        """
        IEP1A fails area sanity (page_area_fraction=0.003); IEP1B passes.

        IEP1B is sole survivor → selection_reason='sole_survivor'.
        dropout at sanity → geometry_trust='low' → route='rectification'.
        """
        bad_iep1a = GeometryResponse(
            page_count=1,
            pages=[_make_page_region(page_area_fraction=0.003)],
            split_required=False,
            geometry_confidence=0.90,
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.02,
            tta_passes=3,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=100.0,
        )
        good_iep1b = _make_geometry(geometry_confidence=0.88)

        result = run_geometry_selection(bad_iep1a, good_iep1b, _MATERIAL, _PROXY_W, _PROXY_H)

        assert result.selected is not None
        assert result.selected.model == "iep1b"
        assert result.selection_reason == "sole_survivor"
        assert result.route_decision == "rectification"
        assert result.geometry_trust == "low"


# ==============================================================================
# 2. Artifact validation
# ==============================================================================


class TestArtifactValidationGolden:
    """Golden-dataset tests for the artifact validation gate."""

    def _exact_loader(self, uri: str) -> ArtifactImageDimensions:
        """Returns dimensions that match _make_branch_response(out_w=180, out_h=90)."""
        return ArtifactImageDimensions(width=180, height=90)

    def test_all_pass_max_score(self) -> None:
        """Perfect quality metrics and matching file → passed=True, soft_score=1.0."""
        response = _make_branch_response(
            skew_residual=0.5,        # ≤ 1.0 good_max → signal score 1.0
            blur_score=0.2,           # ≤ 0.4 good_max → 1.0
            border_score=0.7,         # ≥ 0.5 good_min → 1.0
            foreground_coverage=0.6,  # in [0.2, 0.9] good range → 1.0
        )
        geometry = _make_geometry(
            geometry_confidence=0.92,           # ≥ 0.8 good_min → 1.0
            tta_structural_agreement_rate=0.95, # ≥ 0.9 good_min → 1.0
        )

        result = run_artifact_validation(response, geometry, self._exact_loader)

        assert result.hard_result.passed is True
        assert result.soft_score == pytest.approx(1.0)
        assert result.soft_passed is True
        assert result.passed is True

    def test_file_missing_hard_fail(self) -> None:
        """FileNotFoundError from image_loader → failed_checks=['file_exists'], no soft score."""
        response = _make_branch_response()

        def _missing(uri: str) -> ArtifactImageDimensions:
            raise FileNotFoundError(uri)

        result = run_artifact_validation(response, None, _missing)

        assert result.passed is False
        assert result.hard_result.failed_checks == ["file_exists"]
        assert result.soft_score is None

    def test_invalid_image_hard_fail(self) -> None:
        """Decode error → 'valid_image' check fails, no soft score."""
        response = _make_branch_response()

        def _invalid(uri: str) -> ArtifactImageDimensions:
            raise ValueError("Cannot decode image data")

        result = run_artifact_validation(response, None, _invalid)

        assert result.passed is False
        assert "valid_image" in result.hard_result.failed_checks
        assert result.soft_score is None

    def test_non_degenerate_fail_zero_width(self) -> None:
        """image_loader returns zero-width → 'non_degenerate' check fails."""
        response = _make_branch_response()

        def _zero_width(uri: str) -> ArtifactImageDimensions:
            return ArtifactImageDimensions(width=0, height=90)

        result = check_artifact_hard_requirements(response, _zero_width)

        assert result.passed is False
        assert "non_degenerate" in result.failed_checks

    def test_dimensions_inconsistent_hard_fail(self) -> None:
        """Actual image width off by 170px (> 2px tolerance) → 'dimensions_consistent' fails."""
        # post_preprocessing_dimensions = (180, 90) set by _make_branch_response
        response = _make_branch_response(out_w=180, out_h=90)

        def _wrong_dims(uri: str) -> ArtifactImageDimensions:
            return ArtifactImageDimensions(width=350, height=90)  # |350-180|=170 > 2

        result = check_artifact_hard_requirements(response, _wrong_dims)

        assert result.passed is False
        assert "dimensions_consistent" in result.failed_checks

    def test_poor_quality_soft_fail(self) -> None:
        """All quality signals at worst values → soft_score=0.0, passed=False."""
        response = _make_branch_response(
            skew_residual=6.0,         # ≥ 5.0 bad_min → 0.0
            blur_score=0.8,            # ≥ 0.7 bad_min → 0.0
            border_score=0.1,          # ≤ 0.3 bad_max → 0.0
            foreground_coverage=0.05,  # ≤ 0.1 bad_lo → 0.0
        )

        result = run_artifact_validation(response, None, self._exact_loader)

        assert result.hard_result.passed is True
        assert result.soft_score == pytest.approx(0.0)
        assert result.soft_passed is False
        assert result.passed is False

    def test_no_geometry_four_signals_only(self) -> None:
        """geometry=None → exactly 4 signal keys (no geometry_confidence, tta_agreement)."""
        response = _make_branch_response()

        result = run_artifact_validation(response, None, self._exact_loader)

        assert result.signal_scores is not None
        assert set(result.signal_scores.keys()) == {
            "skew_residual",
            "blur_score",
            "border_score",
            "foreground_coverage",
        }

    def test_midpoint_soft_score_exact(self) -> None:
        """
        Known midpoint values produce exact per-signal scores and combined=0.5.

        Derivations (default config, geometry=None):
          skew_residual=3.0  → (5.0-3.0)/(5.0-1.0) = 0.5
          blur_score=0.55    → (0.7-0.55)/(0.7-0.4) = 0.5
          border_score=0.4   → (0.4-0.3)/(0.5-0.3) = 0.5
          foreground=0.15    → (0.15-0.1)/(0.2-0.1) = 0.5
          combined           → (0.5*4)/4 = 0.5
        """
        quality = QualityMetrics(
            skew_residual=3.0,
            blur_score=0.55,
            border_score=0.4,
            foreground_coverage=0.15,
        )
        config = PreprocessingGateConfig()

        score, signal_scores = compute_artifact_soft_score(quality, None, config)

        assert signal_scores["skew_residual"] == pytest.approx(0.5)
        assert signal_scores["blur_score"] == pytest.approx(0.5)
        assert signal_scores["border_score"] == pytest.approx(0.5)
        assert signal_scores["foreground_coverage"] == pytest.approx(0.5)
        assert score == pytest.approx(0.5)


# ==============================================================================
# 3. IEP1C normalization outputs
# ==============================================================================


class TestNormalizationGolden:
    """Golden-dataset tests for normalize_single_page()."""

    def _gray_image(self, h: int = 100, w: int = 200) -> "np.ndarray":
        """Solid mid-gray H×W×3 uint8 synthetic image."""
        return np.full((h, w, 3), 128, dtype=np.uint8)

    def _simple_geometry(
        self,
        split_required: bool = False,
        split_x: int | None = None,
        tta_structural_agreement_rate: float = 0.95,
    ) -> GeometryResponse:
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            bbox=(10, 5, 190, 95),  # 180×90 region inside 200×100 image
            confidence=0.92,
            page_area_fraction=0.85,
        )
        return GeometryResponse(
            page_count=1,
            pages=[page],
            split_required=split_required,
            split_x=split_x,
            geometry_confidence=0.90,
            tta_structural_agreement_rate=tta_structural_agreement_rate,
            tta_prediction_variance=0.01,
            tta_passes=3,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=80.0,
        )

    def test_bbox_fallback_method_and_angle(self) -> None:
        """bbox geometry path sets method='geometry_bbox' and deskew angle=0.0."""
        image = self._gray_image()
        geometry = self._simple_geometry()
        page = geometry.pages[0]

        result = normalize_single_page(image, page, geometry)

        assert result.deskew.angle_deg == pytest.approx(0.0)
        assert result.deskew.method == "geometry_bbox"
        assert result.crop.method == "geometry_bbox"

    def test_bbox_crop_box_matches_geometry(self) -> None:
        """Crop box coordinates equal the bbox input after clamping to image bounds."""
        image = self._gray_image(h=100, w=200)
        geometry = self._simple_geometry()
        page = geometry.pages[0]  # bbox=(10, 5, 190, 95)

        result = normalize_single_page(image, page, geometry)

        # All four sides lie strictly within the 200×100 image → no clamping
        assert result.crop.crop_box.x_min == pytest.approx(10.0)
        assert result.crop.crop_box.y_min == pytest.approx(5.0)
        assert result.crop.crop_box.x_max == pytest.approx(190.0)
        assert result.crop.crop_box.y_max == pytest.approx(95.0)

    def test_no_geometry_uses_full_extent(self) -> None:
        """No bbox and no corners → warning is added, crop_box covers full image."""
        image = self._gray_image(h=100, w=200)
        page = PageRegion(
            region_id="page_0",
            geometry_type="mask_ref",
            bbox=None,
            corners=None,
            confidence=0.5,
            page_area_fraction=0.5,
        )
        geometry = self._simple_geometry()

        result = normalize_single_page(image, page, geometry)

        assert any("full image extent" in w for w in result.warnings)
        assert result.crop.crop_box.x_min == pytest.approx(0.0)
        assert result.crop.crop_box.y_min == pytest.approx(0.0)
        assert result.crop.crop_box.x_max == pytest.approx(200.0)
        assert result.crop.crop_box.y_max == pytest.approx(100.0)

    def test_split_required_metadata(self) -> None:
        """
        split_required=True → method='instance_boundary'.

        split_confidence = min(page.confidence=0.92, tta_agreement=0.95) = 0.92.
        """
        image = self._gray_image()
        geometry = self._simple_geometry(
            split_required=True,
            split_x=100,
            tta_structural_agreement_rate=0.95,
        )
        page = geometry.pages[0]  # confidence=0.92

        result = normalize_single_page(image, page, geometry)

        assert result.split.split_required is True
        assert result.split.split_x == 100
        assert result.split.method == "instance_boundary"
        assert result.split.split_confidence == pytest.approx(0.92)

    def test_no_split_metadata(self) -> None:
        """split_required=False → method='none', split_x=None, split_confidence=None."""
        image = self._gray_image()
        geometry = self._simple_geometry(split_required=False)
        page = geometry.pages[0]

        result = normalize_single_page(image, page, geometry)

        assert result.split.split_required is False
        assert result.split.split_x is None
        assert result.split.method == "none"
        assert result.split.split_confidence is None

    def test_transform_original_dimensions(self) -> None:
        """TransformRecord.original_dimensions matches the source image shape exactly."""
        image = self._gray_image(h=80, w=160)
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            bbox=(5, 5, 155, 75),  # within 160×80
            confidence=0.90,
            page_area_fraction=0.88,
        )
        geometry = GeometryResponse(
            page_count=1,
            pages=[page],
            split_required=False,
            geometry_confidence=0.90,
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.01,
            tta_passes=3,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=50.0,
        )

        result = normalize_single_page(image, page, geometry)

        assert result.transform.original_dimensions.width == 160
        assert result.transform.original_dimensions.height == 80
        assert result.transform.deskew_angle_deg == pytest.approx(0.0)

    def test_branch_response_assembly(self) -> None:
        """normalize_result_to_branch_response preserves all NormalizeResult fields."""
        image = self._gray_image()
        geometry = self._simple_geometry()
        page = geometry.pages[0]

        norm_result = normalize_single_page(image, page, geometry)
        branch = normalize_result_to_branch_response(
            norm_result, "iep1b", "s3://bucket/page-001.tiff"
        )

        assert branch.source_model == "iep1b"
        assert branch.processed_image_uri == "s3://bucket/page-001.tiff"
        assert branch.deskew == norm_result.deskew
        assert branch.crop == norm_result.crop
        assert branch.split == norm_result.split
        assert branch.quality == norm_result.quality
        assert branch.transform == norm_result.transform
        assert branch.warnings == norm_result.warnings


# ==============================================================================
# 4. Lineage write correctness
# ==============================================================================


class TestLineageGolden:
    """Golden-dataset tests for lineage DB write helpers (mock SQLAlchemy session)."""

    def test_create_lineage_pending_states(self) -> None:
        """create_lineage → both artifact states start as 'pending', session.add called."""
        session = MagicMock()

        record = create_lineage(
            session,
            lineage_id="lin-001",
            job_id="job-abc",
            page_number=1,
            correlation_id="corr-xyz",
            input_image_uri="s3://in/img.tiff",
            otiff_uri="s3://out/img.ptiff",
            material_type="book",
            policy_version="v1.0.0",
        )

        assert record.preprocessed_artifact_state == "pending"
        assert record.layout_artifact_state == "pending"
        session.add.assert_called_once_with(record)

    def test_create_lineage_fields(self) -> None:
        """create_lineage → all caller-supplied fields written to the record verbatim."""
        session = MagicMock()

        record = create_lineage(
            session,
            lineage_id="lin-999",
            job_id="job-xyz",
            page_number=3,
            correlation_id="corr-abc",
            input_image_uri="s3://in/page3.tiff",
            otiff_uri="s3://out/page3.ptiff",
            material_type="newspaper",
            policy_version="v2.1.0",
            input_image_hash="deadbeef",
        )

        assert record.lineage_id == "lin-999"
        assert record.job_id == "job-xyz"
        assert record.page_number == 3
        assert record.correlation_id == "corr-abc"
        assert record.input_image_uri == "s3://in/page3.tiff"
        assert record.otiff_uri == "s3://out/page3.ptiff"
        assert record.material_type == "newspaper"
        assert record.policy_version == "v2.1.0"
        assert record.input_image_hash == "deadbeef"

    def test_create_lineage_split_child(self) -> None:
        """create_lineage with split_source=True → sub_page_index and parent_page_id set."""
        session = MagicMock()

        record = create_lineage(
            session,
            lineage_id="lin-split-0",
            job_id="job-abc",
            page_number=1,
            correlation_id="corr-split",
            input_image_uri="s3://in/page1.tiff",
            otiff_uri="s3://out/page1-left.ptiff",
            material_type="book",
            policy_version="v1.0.0",
            sub_page_index=0,
            parent_page_id="page-parent-001",
            split_source=True,
        )

        assert record.sub_page_index == 0
        assert record.parent_page_id == "page-parent-001"
        assert record.split_source is True

    def test_confirm_preprocessed_artifact(self) -> None:
        """confirm_preprocessed_artifact → update called with {'preprocessed_artifact_state': 'confirmed'}."""
        session = MagicMock()

        confirm_preprocessed_artifact(session, "lin-001")

        update_mock = session.query.return_value.filter.return_value.update
        update_mock.assert_called_once()
        update_dict = update_mock.call_args[0][0]
        assert update_dict == {"preprocessed_artifact_state": "confirmed"}

    def test_update_geometry_result_fields(self) -> None:
        """update_geometry_result → update dict contains all five geometry outcome fields."""
        session = MagicMock()

        update_geometry_result(
            session,
            "lin-001",
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model="iep1a",
            structural_agreement=True,
            iep1d_used=False,
        )

        update_mock = session.query.return_value.filter.return_value.update
        update_mock.assert_called_once()
        update_dict = update_mock.call_args[0][0]
        assert update_dict["iep1a_used"] is True
        assert update_dict["iep1b_used"] is True
        assert update_dict["selected_geometry_model"] == "iep1a"
        assert update_dict["structural_agreement"] is True
        assert update_dict["iep1d_used"] is False

    def test_update_lineage_completion_fields(self) -> None:
        """update_lineage_completion → update dict contains decision, reason, path, timing."""
        session = MagicMock()

        update_lineage_completion(
            session,
            "lin-001",
            acceptance_decision="accepted",
            acceptance_reason="artifact validation passed",
            routing_path="preprocessing_only",
            total_processing_ms=1234.5,
            output_image_uri="s3://out/final.ptiff",
        )

        update_mock = session.query.return_value.filter.return_value.update
        update_mock.assert_called_once()
        update_dict = update_mock.call_args[0][0]
        assert update_dict["acceptance_decision"] == "accepted"
        assert update_dict["acceptance_reason"] == "artifact validation passed"
        assert update_dict["routing_path"] == "preprocessing_only"
        assert update_dict["total_processing_ms"] == pytest.approx(1234.5)
        assert update_dict["output_image_uri"] == "s3://out/final.ptiff"


# ==============================================================================
# 5. State machine transitions
# ==============================================================================


class TestStateTransitionGolden:
    """Golden-dataset tests for the page state machine."""

    def test_queued_to_preprocessing_allowed(self) -> None:
        """queued → preprocessing is a valid transition (Step 1 in process_page)."""
        validate_transition("queued", "preprocessing")  # must not raise

    def test_queued_to_failed_allowed(self) -> None:
        """queued → failed is a valid transition (infrastructure failure before start)."""
        validate_transition("queued", "failed")

    def test_preprocessing_all_valid_targets(self) -> None:
        """Every documented outgoing state from 'preprocessing' is allowed."""
        valid_targets = {
            "rectification",
            "layout_detection",
            "accepted",
            "pending_human_correction",
            "split",
            "failed",
        }
        for target in valid_targets:
            validate_transition("preprocessing", target)

    def test_rectification_all_valid_targets(self) -> None:
        """Every documented outgoing state from 'rectification' is allowed."""
        valid_targets = {"layout_detection", "accepted", "pending_human_correction", "split", "failed"}
        for target in valid_targets:
            validate_transition("rectification", target)

    def test_accepted_no_outgoing_transitions(self) -> None:
        "'accepted' is leaf-final — any transition out raises InvalidTransitionError."
        with pytest.raises(InvalidTransitionError):
            validate_transition("accepted", "failed")

    def test_invalid_transition_raises_with_context(self) -> None:
        """queued → accepted raises InvalidTransitionError with current and next_state set."""
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition("queued", "accepted")
        assert exc_info.value.current == "queued"
        assert exc_info.value.next_state == "accepted"

    def test_unknown_state_raises_value_error(self) -> None:
        """Unrecognized state raises ValueError, not InvalidTransitionError."""
        with pytest.raises(ValueError, match="Unknown page state"):
            validate_transition("bogus_state", "failed")

    def test_is_worker_terminal_all_five_states(self) -> None:
        """is_worker_terminal returns True for all five worker-terminal states."""
        terminal_states = {"accepted", "review", "failed", "pending_human_correction", "split"}
        for state in terminal_states:
            assert is_worker_terminal(state) is True, f"Expected {state!r} to be worker-terminal"

    def test_is_worker_terminal_false_for_active_states(self) -> None:
        """is_worker_terminal returns False for active processing states."""
        active_states = {
            "queued",
            "preprocessing",
            "rectification",
            "layout_detection",
        }
        for state in active_states:
            assert is_worker_terminal(state) is False, f"Expected {state!r} to be non-terminal"

    def test_is_leaf_final_review_failed(self) -> None:
        """is_leaf_final returns True for review and failed (permanent outcomes).
        accepted is excluded: reviewers may flag it for re-correction via the
        PTIFF QA viewer (accepted → pending_human_correction transition)."""
        assert is_leaf_final("accepted") is False
        assert is_leaf_final("review") is True
        assert is_leaf_final("failed") is True

    def test_is_leaf_final_false_for_resumable_terminal(self) -> None:
        """pending_human_correction and split are worker-terminal but NOT leaf-final."""
        assert is_leaf_final("pending_human_correction") is False
        assert is_leaf_final("split") is False

    def test_allowed_next_queued_exact(self) -> None:
        "allowed_next('queued') returns exactly {'preprocessing', 'failed'}."
        assert allowed_next("queued") == frozenset({"preprocessing", "failed"})

    def test_allowed_next_accepted_flag_only(self) -> None:
        """allowed_next('accepted') returns {pending_human_correction, semantic_norm}.
        Reviewers may flag an accepted page for re-correction; sibling corrections
        can also re-run pair-level IEP1E from accepted."""
        assert allowed_next("accepted") == frozenset({"pending_human_correction", "semantic_norm"})
