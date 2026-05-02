"""tests.test_p3_artifact_validation
-------------------------------------
Tests for Packet 3.4: artifact hard requirement checks.

Covers:
  - ARTIFACT_HARD_CHECK_NAMES constant
  - ArtifactHardCheckResult.as_dict() serialization
  - check_artifact_hard_requirements:
      - all five checks pass
      - file_exists fails (FileNotFoundError → early return)
      - valid_image fails (other exception)
      - non_degenerate fails (width=0 or height=0)
      - bounds_consistent fails (crop box outside original dims)
      - dimensions_consistent fails (actual dims differ from expected)
      - bounds_consistent evaluated even when valid_image fails
      - dimensions_consistent skipped when valid_image fails
      - dimensions_consistent skipped when non_degenerate fails
      - rounding tolerance respected
      - custom dimension_tolerance
      - multiple checks can fail simultaneously
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from services.eep.app.gates.artifact_validation import (
    ARTIFACT_HARD_CHECK_NAMES,
    ArtifactHardCheckResult,
    ArtifactImageDimensions,
    ArtifactValidationResult,
    _normalize_decreasing,
    _normalize_increasing,
    _normalize_range,
    build_artifact_gate_log_record,
    check_artifact_hard_requirements,
    compute_artifact_soft_score,
    run_artifact_validation,
)
from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from shared.schemas.preprocessing import (
    CropResult,
    DeskewResult,
    PreprocessBranchResponse,
    QualityMetrics,
    SplitResult,
)
from shared.schemas.ucf import BoundingBox, Dimensions, TransformRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Typical proxy image and artifact dimensions used throughout.
_ORIG_W = 1200
_ORIG_H = 900
_ARTIFACT_W = 300
_ARTIFACT_H = 400


def _make_transform(
    orig_w: int = _ORIG_W,
    orig_h: int = _ORIG_H,
    crop_x_min: float = 100.0,
    crop_y_min: float = 50.0,
    crop_x_max: float = 400.0,  # width = 300 → matches _ARTIFACT_W
    crop_y_max: float = 450.0,  # height = 400 → matches _ARTIFACT_H
    post_w: int = _ARTIFACT_W,
    post_h: int = _ARTIFACT_H,
) -> TransformRecord:
    return TransformRecord(
        original_dimensions=Dimensions(width=orig_w, height=orig_h),
        crop_box=BoundingBox(
            x_min=crop_x_min,
            y_min=crop_y_min,
            x_max=crop_x_max,
            y_max=crop_y_max,
        ),
        deskew_angle_deg=0.0,
        post_preprocessing_dimensions=Dimensions(width=post_w, height=post_h),
    )


def _make_response(
    transform: TransformRecord | None = None,
    processed_image_uri: str = "file:///artifacts/page_0.tiff",
) -> PreprocessBranchResponse:
    if transform is None:
        transform = _make_transform()
    return PreprocessBranchResponse(
        processed_image_uri=processed_image_uri,
        deskew=DeskewResult(angle_deg=0.0, residual_deg=0.0, method="geometry_quad"),
        crop=CropResult(
            crop_box=transform.crop_box,
            border_score=0.85,
            method="geometry_quad",
        ),
        split=SplitResult(split_required=False, method="instance_boundary"),
        quality=QualityMetrics(
            skew_residual=0.3,
            blur_score=0.2,
            border_score=0.8,
            foreground_coverage=0.6,
        ),
        transform=transform,
        source_model="iep1a",
        processing_time_ms=120.0,
        warnings=[],
    )


def _loader_ok(
    width: int = _ARTIFACT_W, height: int = _ARTIFACT_H
) -> Callable[[str], ArtifactImageDimensions]:
    """Returns a loader that always succeeds with the given dimensions."""

    def _load(uri: str) -> ArtifactImageDimensions:
        return ArtifactImageDimensions(width=width, height=height)

    return _load


def _loader_missing() -> Callable[[str], ArtifactImageDimensions]:
    """Returns a loader that raises FileNotFoundError."""

    def _load(uri: str) -> ArtifactImageDimensions:
        raise FileNotFoundError(uri)

    return _load


def _loader_corrupt() -> Callable[[str], ArtifactImageDimensions]:
    """Returns a loader that raises ValueError (decode failure)."""

    def _load(uri: str) -> ArtifactImageDimensions:
        raise ValueError("not a valid image")

    return _load


# ---------------------------------------------------------------------------
# ARTIFACT_HARD_CHECK_NAMES
# ---------------------------------------------------------------------------


class TestArtifactHardCheckNames:
    def test_contains_all_five_canonical_names(self) -> None:
        expected = {
            "file_exists",
            "valid_image",
            "non_degenerate",
            "bounds_consistent",
            "dimensions_consistent",
        }
        assert set(ARTIFACT_HARD_CHECK_NAMES) == expected
        assert len(ARTIFACT_HARD_CHECK_NAMES) == 5

    def test_is_tuple(self) -> None:
        assert isinstance(ARTIFACT_HARD_CHECK_NAMES, tuple)


# ---------------------------------------------------------------------------
# ArtifactHardCheckResult.as_dict()
# ---------------------------------------------------------------------------


class TestArtifactHardCheckResultAsDict:
    def test_passed_true_empty_failures(self) -> None:
        r = ArtifactHardCheckResult(passed=True, failed_checks=[])
        assert r.as_dict() == {"passed": True, "failed_checks": []}

    def test_passed_false_with_failures(self) -> None:
        r = ArtifactHardCheckResult(passed=False, failed_checks=["file_exists"])
        d = r.as_dict()
        assert d["passed"] is False
        failed = d["failed_checks"]
        assert isinstance(failed, list)
        assert "file_exists" in failed

    def test_as_dict_returns_copy_of_failed_checks(self) -> None:
        r = ArtifactHardCheckResult(passed=False, failed_checks=["non_degenerate"])
        d = r.as_dict()
        failed = d["failed_checks"]
        assert isinstance(failed, list)
        failed.clear()
        assert r.failed_checks != []


# ---------------------------------------------------------------------------
# check_artifact_hard_requirements — all pass
# ---------------------------------------------------------------------------


class TestAllChecksPass:
    def test_all_five_pass_returns_passed_true(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert result.passed is True
        assert result.failed_checks == []

    def test_all_pass_with_exact_dimensions(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(300, 400))
        assert result.passed is True


# ---------------------------------------------------------------------------
# Check 1: file_exists
# ---------------------------------------------------------------------------


class TestFileExistsCheck:
    def test_missing_file_fails_file_exists(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_missing())
        assert result.passed is False
        assert "file_exists" in result.failed_checks

    def test_missing_file_early_return_no_other_checks(self) -> None:
        """When file is missing the function returns immediately — only file_exists fails."""
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_missing())
        assert result.failed_checks == ["file_exists"]

    def test_missing_file_passes_no_other_names(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_missing())
        for name in ("valid_image", "non_degenerate", "bounds_consistent", "dimensions_consistent"):
            assert name not in result.failed_checks


# ---------------------------------------------------------------------------
# Check 2: valid_image
# ---------------------------------------------------------------------------


class TestValidImageCheck:
    def test_corrupt_file_fails_valid_image(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        assert result.passed is False
        assert "valid_image" in result.failed_checks

    def test_corrupt_file_does_not_fail_file_exists(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        assert "file_exists" not in result.failed_checks

    def test_corrupt_file_skips_non_degenerate(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        assert "non_degenerate" not in result.failed_checks

    def test_corrupt_file_skips_dimensions_consistent(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        assert "dimensions_consistent" not in result.failed_checks

    def test_corrupt_file_still_checks_bounds_consistent(self) -> None:
        """bounds_consistent is data-only; it runs even when image can't be decoded."""
        resp = _make_response()  # valid bounds
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        # valid_image fails but bounds_consistent should NOT fail (bounds are fine)
        assert "bounds_consistent" not in result.failed_checks

    def test_corrupt_file_with_bad_bounds_reports_both(self) -> None:
        """Both valid_image and bounds_consistent can fail together."""
        t = _make_transform()
        bad_transform = TransformRecord.model_construct(
            original_dimensions=t.original_dimensions,
            crop_box=BoundingBox.model_construct(x_min=-10.0, y_min=0.0, x_max=400.0, y_max=450.0),
            deskew_angle_deg=0.0,
            post_preprocessing_dimensions=t.post_preprocessing_dimensions,
        )
        valid = _make_response()
        resp = PreprocessBranchResponse.model_construct(
            **{**valid.model_dump(), "transform": bad_transform}
        )
        result = check_artifact_hard_requirements(resp, _loader_corrupt())
        assert "valid_image" in result.failed_checks
        assert "bounds_consistent" in result.failed_checks


# ---------------------------------------------------------------------------
# Check 3: non_degenerate
# ---------------------------------------------------------------------------


class TestNonDegenerateCheck:
    def test_zero_width_fails_non_degenerate(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(width=0, height=400))
        assert result.passed is False
        assert "non_degenerate" in result.failed_checks

    def test_zero_height_fails_non_degenerate(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(width=300, height=0))
        assert result.passed is False
        assert "non_degenerate" in result.failed_checks

    def test_zero_both_fails_non_degenerate(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(width=0, height=0))
        assert "non_degenerate" in result.failed_checks

    def test_non_degenerate_skips_dimensions_consistent(self) -> None:
        """When non_degenerate fails, dimensions_consistent must not be checked."""
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(width=0, height=0))
        assert "dimensions_consistent" not in result.failed_checks

    def test_positive_dimensions_passes(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(1, 1))
        assert "non_degenerate" not in result.failed_checks


# ---------------------------------------------------------------------------
# Check 4: bounds_consistent
# ---------------------------------------------------------------------------


class TestBoundsConsistentCheck:
    def _make_out_of_bounds_response(
        self,
        x_min: float = 0.0,
        y_min: float = 0.0,
        x_max: float = 400.0,
        y_max: float = 450.0,
    ) -> PreprocessBranchResponse:
        """Build response with invalid crop_box — must bypass Pydantic validation on both
        TransformRecord and PreprocessBranchResponse using model_construct."""
        t = _make_transform()
        bad_transform = TransformRecord.model_construct(
            original_dimensions=t.original_dimensions,
            crop_box=BoundingBox.model_construct(
                x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max
            ),
            deskew_angle_deg=0.0,
            post_preprocessing_dimensions=t.post_preprocessing_dimensions,
        )
        valid = _make_response()
        return PreprocessBranchResponse.model_construct(
            **{**valid.model_dump(), "transform": bad_transform}
        )

    def test_negative_x_min_fails_bounds(self) -> None:
        resp = self._make_out_of_bounds_response(x_min=-1.0)
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert "bounds_consistent" in result.failed_checks

    def test_negative_y_min_fails_bounds(self) -> None:
        resp = self._make_out_of_bounds_response(y_min=-5.0)
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert "bounds_consistent" in result.failed_checks

    def test_x_max_beyond_original_width_fails_bounds(self) -> None:
        # orig_w = 1200; x_max > 1200
        resp = self._make_out_of_bounds_response(x_max=1300.0)
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert "bounds_consistent" in result.failed_checks

    def test_y_max_beyond_original_height_fails_bounds(self) -> None:
        # orig_h = 900; y_max > 900
        resp = self._make_out_of_bounds_response(y_max=1000.0)
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert "bounds_consistent" in result.failed_checks

    def test_crop_at_boundary_passes(self) -> None:
        # Exactly at edge: x_max == orig_w, y_max == orig_h → passes
        t = _make_transform(
            crop_x_min=0.0,
            crop_y_min=0.0,
            crop_x_max=float(_ORIG_W),
            crop_y_max=float(_ORIG_H),
            post_w=_ORIG_W,
            post_h=_ORIG_H,
        )
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(_ORIG_W, _ORIG_H))
        assert "bounds_consistent" not in result.failed_checks

    def test_valid_bounds_do_not_fail(self) -> None:
        resp = _make_response()
        result = check_artifact_hard_requirements(resp, _loader_ok())
        assert "bounds_consistent" not in result.failed_checks


# ---------------------------------------------------------------------------
# Check 5: dimensions_consistent
# ---------------------------------------------------------------------------


class TestDimensionsConsistentCheck:
    def test_exact_match_passes(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(300, 400))
        assert "dimensions_consistent" not in result.failed_checks

    def test_within_tolerance_passes(self) -> None:
        # Default tolerance = 2; actual 302×401 vs expected 300×400 → within tol
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(302, 401), dimension_tolerance=2)
        assert "dimensions_consistent" not in result.failed_checks

    def test_at_tolerance_boundary_passes(self) -> None:
        # diff == tolerance exactly → passes (<=)
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(302, 402), dimension_tolerance=2)
        assert "dimensions_consistent" not in result.failed_checks

    def test_exceeds_tolerance_fails(self) -> None:
        # diff == 3 > tolerance 2 → fails
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(303, 400), dimension_tolerance=2)
        assert result.passed is False
        assert "dimensions_consistent" in result.failed_checks

    def test_height_exceeds_tolerance_fails(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(300, 405), dimension_tolerance=2)
        assert "dimensions_consistent" in result.failed_checks

    def test_custom_tolerance_zero_strict(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        # Even 1-pixel diff fails with tolerance=0
        result = check_artifact_hard_requirements(resp, _loader_ok(301, 400), dimension_tolerance=0)
        assert "dimensions_consistent" in result.failed_checks

    def test_custom_tolerance_zero_exact_passes(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(300, 400), dimension_tolerance=0)
        assert "dimensions_consistent" not in result.failed_checks

    def test_large_discrepancy_fails(self) -> None:
        t = _make_transform(post_w=300, post_h=400)
        resp = _make_response(transform=t)
        result = check_artifact_hard_requirements(resp, _loader_ok(600, 800))
        assert "dimensions_consistent" in result.failed_checks


# ---------------------------------------------------------------------------
# Multiple failures
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    def _bad_bounds_response(self) -> PreprocessBranchResponse:
        bad_transform = TransformRecord.model_construct(
            original_dimensions=Dimensions(width=_ORIG_W, height=_ORIG_H),
            crop_box=BoundingBox.model_construct(x_min=-5.0, y_min=0.0, x_max=400.0, y_max=450.0),
            deskew_angle_deg=0.0,
            post_preprocessing_dimensions=Dimensions(width=_ARTIFACT_W, height=_ARTIFACT_H),
        )
        valid = _make_response()
        return PreprocessBranchResponse.model_construct(
            **{**valid.model_dump(), "transform": bad_transform}
        )

    def test_non_degenerate_and_bounds_fail_together(self) -> None:
        resp = self._bad_bounds_response()
        # Zero-width image → non_degenerate; bad crop → bounds_consistent
        result = check_artifact_hard_requirements(resp, _loader_ok(width=0, height=400))
        assert "non_degenerate" in result.failed_checks
        assert "bounds_consistent" in result.failed_checks

    def test_as_dict_reflects_all_failures(self) -> None:
        resp = self._bad_bounds_response()
        result = check_artifact_hard_requirements(resp, _loader_ok(width=0, height=400))
        d = result.as_dict()
        failed = d["failed_checks"]
        assert isinstance(failed, list)
        assert "non_degenerate" in failed
        assert "bounds_consistent" in failed


# ===========================================================================
# Packet 3.5 — soft signal scoring, run_artifact_validation, gate log record
# ===========================================================================

_CONFIG = PreprocessingGateConfig()

# A quality object with all "good" signals (within good ranges).
_GOOD_QUALITY = QualityMetrics(
    skew_residual=0.5,  # good: < 1.0°
    blur_score=0.2,  # good: < 0.4
    border_score=0.8,  # good: > 0.5
    foreground_coverage=0.6,  # good: 0.2–0.9
)

# A quality object with all "suspicious" signals (at or beyond bad bounds).
_BAD_QUALITY = QualityMetrics(
    skew_residual=6.0,  # suspicious: > 5.0°
    blur_score=0.9,  # suspicious: > 0.7
    border_score=0.1,  # suspicious: < 0.3
    foreground_coverage=0.05,  # suspicious: < 0.1
)


def _make_geometry(
    geometry_confidence: float = 0.9,
    tta_structural_agreement_rate: float = 0.95,
) -> object:
    """Build a minimal GeometryResponse-like object for soft scoring tests."""
    from shared.schemas.geometry import GeometryResponse, PageRegion

    region = PageRegion(
        region_id="page_0",
        geometry_type="quadrilateral",
        corners=[(50.0, 30.0), (350.0, 30.0), (350.0, 530.0), (50.0, 530.0)],
        bbox=(50, 30, 350, 530),
        confidence=geometry_confidence,
        page_area_fraction=0.5,
    )
    return GeometryResponse(
        page_count=1,
        pages=[region],
        split_required=False,
        split_x=None,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=0.05,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=50.0,
    )


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


class TestNormalizeDecreasing:
    def test_at_good_max_returns_one(self) -> None:
        assert _normalize_decreasing(1.0, 1.0, 5.0) == pytest.approx(1.0)

    def test_below_good_max_returns_one(self) -> None:
        assert _normalize_decreasing(0.5, 1.0, 5.0) == pytest.approx(1.0)

    def test_at_bad_min_returns_zero(self) -> None:
        assert _normalize_decreasing(5.0, 1.0, 5.0) == pytest.approx(0.0)

    def test_above_bad_min_returns_zero(self) -> None:
        assert _normalize_decreasing(7.0, 1.0, 5.0) == pytest.approx(0.0)

    def test_midpoint_returns_half(self) -> None:
        # midpoint between 1.0 and 5.0 is 3.0 → score = (5-3)/(5-1) = 0.5
        assert _normalize_decreasing(3.0, 1.0, 5.0) == pytest.approx(0.5)


class TestNormalizeIncreasing:
    def test_at_good_min_returns_one(self) -> None:
        assert _normalize_increasing(0.9, 0.7, 0.9) == pytest.approx(1.0)

    def test_above_good_min_returns_one(self) -> None:
        assert _normalize_increasing(0.95, 0.7, 0.9) == pytest.approx(1.0)

    def test_at_bad_max_returns_zero(self) -> None:
        assert _normalize_increasing(0.7, 0.7, 0.9) == pytest.approx(0.0)

    def test_below_bad_max_returns_zero(self) -> None:
        assert _normalize_increasing(0.5, 0.7, 0.9) == pytest.approx(0.0)

    def test_midpoint_returns_half(self) -> None:
        # midpoint between 0.7 and 0.9 is 0.8 → score = (0.8-0.7)/(0.9-0.7) = 0.5
        assert _normalize_increasing(0.8, 0.7, 0.9) == pytest.approx(0.5)


class TestNormalizeRange:
    def test_in_good_range_returns_one(self) -> None:
        assert _normalize_range(0.5, 0.1, 0.2, 0.9, 0.95) == pytest.approx(1.0)

    def test_at_good_lo_returns_one(self) -> None:
        assert _normalize_range(0.2, 0.1, 0.2, 0.9, 0.95) == pytest.approx(1.0)

    def test_at_good_hi_returns_one(self) -> None:
        assert _normalize_range(0.9, 0.1, 0.2, 0.9, 0.95) == pytest.approx(1.0)

    def test_at_bad_lo_returns_zero(self) -> None:
        assert _normalize_range(0.1, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.0)

    def test_below_bad_lo_returns_zero(self) -> None:
        assert _normalize_range(0.0, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.0)

    def test_at_bad_hi_returns_zero(self) -> None:
        assert _normalize_range(0.95, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.0)

    def test_above_bad_hi_returns_zero(self) -> None:
        assert _normalize_range(1.0, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.0)

    def test_low_ramp_midpoint(self) -> None:
        # midpoint between bad_lo=0.1 and good_lo=0.2 is 0.15 → 0.5
        assert _normalize_range(0.15, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.5)

    def test_high_ramp_midpoint(self) -> None:
        # midpoint between good_hi=0.9 and bad_hi=0.95 is 0.925 → 0.5
        assert _normalize_range(0.925, 0.1, 0.2, 0.9, 0.95) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# compute_artifact_soft_score
# ---------------------------------------------------------------------------


class TestComputeArtifactSoftScore:
    def test_all_good_signals_score_near_one(self) -> None:
        score, signals = compute_artifact_soft_score(_GOOD_QUALITY, None, _CONFIG)
        assert score == pytest.approx(1.0)

    def test_all_bad_signals_score_zero(self) -> None:
        score, signals = compute_artifact_soft_score(_BAD_QUALITY, None, _CONFIG)
        assert score == pytest.approx(0.0)

    def test_signal_scores_all_keys_present_without_geometry(self) -> None:
        _, signals = compute_artifact_soft_score(_GOOD_QUALITY, None, _CONFIG)
        assert set(signals.keys()) == {
            "skew_residual",
            "blur_score",
            "border_score",
            "foreground_coverage",
        }

    def test_signal_scores_includes_geometry_signals_when_provided(self) -> None:
        geo = _make_geometry(geometry_confidence=0.9, tta_structural_agreement_rate=0.95)
        _, signals = compute_artifact_soft_score(_GOOD_QUALITY, geo, _CONFIG)  # type: ignore[arg-type]
        assert "geometry_confidence" in signals
        assert "tta_agreement" in signals

    def test_good_geometry_raises_score(self) -> None:
        score_no_geo, _ = compute_artifact_soft_score(_GOOD_QUALITY, None, _CONFIG)
        geo = _make_geometry(geometry_confidence=0.9, tta_structural_agreement_rate=0.95)
        score_with_geo, _ = compute_artifact_soft_score(_GOOD_QUALITY, geo, _CONFIG)  # type: ignore[arg-type]
        # Both should be high (near 1.0); the exact value depends on weighting.
        assert score_no_geo == pytest.approx(1.0)
        assert score_with_geo == pytest.approx(1.0)

    def test_bad_geometry_lowers_score(self) -> None:
        # All quality signals are good but geometry is terrible.
        geo = _make_geometry(geometry_confidence=0.3, tta_structural_agreement_rate=0.6)
        score, signals = compute_artifact_soft_score(_GOOD_QUALITY, geo, _CONFIG)  # type: ignore[arg-type]
        # geometry_confidence and tta_agreement both 0.0 → score < 1.0
        assert score < 1.0
        assert signals["geometry_confidence"] == pytest.approx(0.0)
        assert signals["tta_agreement"] == pytest.approx(0.0)

    def test_score_is_weighted_mean(self) -> None:
        # With equal weights of 1.0 and 4 signals all at score 0.5:
        # combined = (0.5 * 4) / 4 = 0.5
        mid_quality = QualityMetrics(
            skew_residual=3.0,  # midpoint(1.0, 5.0) → 0.5
            blur_score=0.55,  # midpoint(0.4, 0.7) → 0.5
            border_score=0.4,  # midpoint(0.3, 0.5) → 0.5
            foreground_coverage=0.15,  # midpoint(0.1, 0.2) → 0.5
        )
        score, _ = compute_artifact_soft_score(mid_quality, None, _CONFIG)
        assert score == pytest.approx(0.5)

    def test_custom_weights_skew_result(self) -> None:
        # Only blur_score weight is nonzero → combined score == blur_score signal.
        cfg = PreprocessingGateConfig(
            weight_skew_residual=0.0,
            weight_blur_score=1.0,
            weight_border_score=0.0,
            weight_foreground_coverage=0.0,
        )
        good_blur = QualityMetrics(
            skew_residual=10.0,  # terrible (but zero-weighted)
            blur_score=0.2,  # good → normalized 1.0
            border_score=0.1,  # terrible (but zero-weighted)
            foreground_coverage=0.0,  # terrible (but zero-weighted)
        )
        score, signals = compute_artifact_soft_score(good_blur, None, cfg)
        assert score == pytest.approx(1.0)
        assert signals["blur_score"] == pytest.approx(1.0)

    def test_scores_are_clamped_to_0_1(self) -> None:
        # Values well outside the ranges must not produce scores outside [0, 1].
        extreme_quality = QualityMetrics(
            skew_residual=100.0,
            blur_score=1.0,
            border_score=0.0,
            foreground_coverage=0.0,
        )
        score, signals = compute_artifact_soft_score(extreme_quality, None, _CONFIG)
        assert 0.0 <= score <= 1.0
        for v in signals.values():
            assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# run_artifact_validation
# ---------------------------------------------------------------------------


class TestRunArtifactValidation:
    def test_hard_fail_skips_soft_scoring(self) -> None:
        resp = _make_response()
        result = run_artifact_validation(resp, None, _loader_missing())
        assert result.passed is False
        assert result.soft_score is None
        assert result.signal_scores is None
        assert result.soft_passed is None

    def test_hard_pass_soft_pass_yields_passed_true(self) -> None:
        resp = _make_response()
        result = run_artifact_validation(resp, None, _loader_ok(), _CONFIG)
        assert result.hard_result.passed is True
        assert result.passed is True
        assert result.soft_passed is True
        assert result.soft_score is not None
        assert result.soft_score >= _CONFIG.artifact_validation_threshold

    def test_hard_pass_soft_fail_yields_passed_false(self) -> None:
        resp = _make_response()
        # Override quality with all-bad signals using model_copy (preserves nested objects).
        bad_resp = resp.model_copy(update={"quality": _BAD_QUALITY})
        result = run_artifact_validation(bad_resp, None, _loader_ok(), _CONFIG)
        assert result.hard_result.passed is True
        assert result.soft_passed is False
        assert result.passed is False
        assert result.soft_score is not None
        assert result.soft_score < _CONFIG.artifact_validation_threshold

    def test_newspaper_low_soft_geometry_only_can_pass(self) -> None:
        resp = _make_response()
        cfg = PreprocessingGateConfig(
            artifact_validation_threshold=0.9,
            artifact_validation_thresholds={"book": 0.9, "newspaper": 0.9},
        )
        low_geometry = _make_geometry(
            geometry_confidence=0.2,
            tta_structural_agreement_rate=0.95,
        )
        result = run_artifact_validation(
            resp,
            low_geometry,  # type: ignore[arg-type]
            _loader_ok(),
            cfg,
            material_type="newspaper",
        )
        assert result.hard_result.passed is True
        assert result.soft_score is not None
        assert result.soft_passed is True
        assert result.passed is True

    def test_book_low_soft_geometry_remains_strict(self) -> None:
        resp = _make_response()
        cfg = PreprocessingGateConfig(
            artifact_validation_threshold=0.9,
            artifact_validation_thresholds={"book": 0.9, "newspaper": 0.9},
        )
        low_geometry = _make_geometry(
            geometry_confidence=0.2,
            tta_structural_agreement_rate=0.95,
        )
        result = run_artifact_validation(
            resp,
            low_geometry,  # type: ignore[arg-type]
            _loader_ok(),
            cfg,
            material_type="book",
        )
        assert result.hard_result.passed is True
        assert result.soft_passed is False
        assert result.passed is False

    def test_newspaper_hard_failure_still_fails(self) -> None:
        resp = _make_response()
        result = run_artifact_validation(
            resp,
            _make_geometry(),  # type: ignore[arg-type]
            _loader_missing(),
            _CONFIG,
            material_type="newspaper",
        )
        assert result.hard_result.passed is False
        assert result.passed is False

    def test_custom_threshold_zero_always_passes(self) -> None:
        cfg = PreprocessingGateConfig(artifact_validation_threshold=0.0)
        resp = _make_response()
        bad_resp = resp.model_copy(update={"quality": _BAD_QUALITY})
        result = run_artifact_validation(bad_resp, None, _loader_ok(), cfg)
        assert result.soft_passed is True
        assert result.passed is True

    def test_default_config_used_when_none_passed(self) -> None:
        resp = _make_response()
        result = run_artifact_validation(resp, None, _loader_ok())
        assert result.soft_score is not None


# ---------------------------------------------------------------------------
# build_artifact_gate_log_record
# ---------------------------------------------------------------------------


class TestBuildArtifactGateLogRecord:
    def _accepted_result(self) -> ArtifactValidationResult:
        resp = _make_response()
        return run_artifact_validation(resp, None, _loader_ok(), _CONFIG)

    def test_required_keys_present(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 80.0
        )
        required = {
            "gate_id",
            "job_id",
            "page_number",
            "gate_type",
            "iep1a_geometry",
            "iep1b_geometry",
            "structural_agreement",
            "selected_model",
            "selection_reason",
            "sanity_check_results",
            "split_confidence",
            "tta_variance",
            "artifact_validation_score",
            "route_decision",
            "review_reason",
            "processing_time_ms",
        }
        assert required.issubset(record.keys())

    def test_gate_id_is_valid_uuid4(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        parsed = uuid.UUID(str(record["gate_id"]))
        assert parsed.version == 4

    def test_gate_id_unique_per_call(self) -> None:
        result = self._accepted_result()
        r1 = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        r2 = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        assert r1["gate_id"] != r2["gate_id"]

    def test_accepted_route_in_record(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 60.0
        )
        assert record["route_decision"] == "accepted"
        assert record["review_reason"] is None

    def test_pending_human_route_and_reason(self) -> None:
        resp = _make_response()
        hard_fail = run_artifact_validation(resp, None, _loader_missing())
        record = build_artifact_gate_log_record(
            hard_fail,
            "job-2",
            1,
            "artifact_validation_final",
            "pending_human_correction",
            "artifact_quality_low",
            100.0,
        )
        assert record["route_decision"] == "pending_human_correction"
        assert record["review_reason"] == "artifact_quality_low"

    def test_soft_score_in_record(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        assert record["artifact_validation_score"] is not None
        score = record["artifact_validation_score"]
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_hard_fail_soft_score_none_in_record(self) -> None:
        resp = _make_response()
        hard_fail = run_artifact_validation(resp, None, _loader_missing())
        record = build_artifact_gate_log_record(
            hard_fail,
            "job-1",
            0,
            "artifact_validation",
            "pending_human_correction",
            "file_missing",
            50.0,
        )
        assert record["artifact_validation_score"] is None

    def test_geometry_columns_are_none(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        assert record["iep1a_geometry"] is None
        assert record["iep1b_geometry"] is None
        assert record["structural_agreement"] is None
        assert record["selected_model"] is None

    def test_post_rectification_gate_type(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation_final", "accepted", None, 50.0
        )
        assert record["gate_type"] == "artifact_validation_final"

    def test_processing_time_is_int(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 77.3
        )
        assert record["processing_time_ms"] == 77

    def test_sanity_check_results_from_hard_result(self) -> None:
        result = self._accepted_result()
        record = build_artifact_gate_log_record(
            result, "job-1", 0, "artifact_validation", "accepted", None, 50.0
        )
        sr = record["sanity_check_results"]
        assert isinstance(sr, dict)
        assert sr["passed"] is True
        assert sr["hard_checks"]["passed"] is True
        assert "soft_score" in sr
        assert "signal_scores" in sr
        assert sr["route_decision"] == "accepted"
