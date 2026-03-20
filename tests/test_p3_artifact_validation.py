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

from collections.abc import Callable

from services.eep.app.gates.artifact_validation import (
    ARTIFACT_HARD_CHECK_NAMES,
    ArtifactHardCheckResult,
    ArtifactImageDimensions,
    check_artifact_hard_requirements,
)
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
