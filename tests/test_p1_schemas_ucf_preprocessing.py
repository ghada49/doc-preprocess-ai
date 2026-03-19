"""
tests/test_p1_schemas_ucf_preprocessing.py
-------------------------------------------
Packet 1.1 validator tests for:
  - shared.schemas.ucf          (Dimensions, BoundingBox, TransformRecord,
                                  ProcessingContext, validate_bbox_in_context)
  - shared.schemas.preprocessing (DeskewResult, CropResult, SplitResult,
                                   QualityMetrics, PreprocessBranchResponse,
                                   PreprocessError)

Definition of done: validators work, schema fields match spec.
"""

import pytest
from pydantic import ValidationError

from shared.schemas.preprocessing import (
    CropResult,
    DeskewResult,
    PreprocessBranchResponse,
    PreprocessError,
    QualityMetrics,
    SplitResult,
)
from shared.schemas.ucf import (
    BoundingBox,
    Dimensions,
    ProcessingContext,
    TransformRecord,
    validate_bbox_in_context,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _dims(w: int = 2400, h: int = 3200) -> Dimensions:
    return Dimensions(width=w, height=h)


def _bbox(x0: float = 100, y0: float = 80, x1: float = 2300, y1: float = 3100) -> BoundingBox:
    return BoundingBox(x_min=x0, y_min=y0, x_max=x1, y_max=y1)


def _transform(
    orig_w: int = 2400,
    orig_h: int = 3200,
    x0: float = 100,
    y0: float = 80,
    x1: float = 2300,
    y1: float = 3100,
    angle: float = 1.5,
    post_w: int = 2200,
    post_h: int = 3020,
) -> TransformRecord:
    return TransformRecord(
        original_dimensions=Dimensions(width=orig_w, height=orig_h),
        crop_box=BoundingBox(x_min=x0, y_min=y0, x_max=x1, y_max=y1),
        deskew_angle_deg=angle,
        post_preprocessing_dimensions=Dimensions(width=post_w, height=post_h),
    )


def _ctx(post_w: int = 2200, post_h: int = 3020) -> ProcessingContext:
    return ProcessingContext(
        canonical_dimensions=Dimensions(width=post_w, height=post_h),
        transform=_transform(post_w=post_w, post_h=post_h),
    )


def _branch_response() -> PreprocessBranchResponse:
    cb = _bbox()
    return PreprocessBranchResponse(
        processed_image_uri="s3://bucket/jobs/j1/pages/1.tiff",
        deskew=DeskewResult(angle_deg=1.5, residual_deg=0.05, method="geometry_quad"),
        crop=CropResult(crop_box=cb, border_score=0.9, method="geometry_quad"),
        split=SplitResult(
            split_required=False,
            split_x=None,
            split_confidence=None,
            method="instance_boundary",
        ),
        quality=QualityMetrics(
            skew_residual=0.05,
            blur_score=0.8,
            border_score=0.9,
            split_confidence=None,
            foreground_coverage=0.95,
        ),
        transform=_transform(),
        source_model="iep1a",
        processing_time_ms=145.2,
        warnings=[],
    )


# ── Dimensions ─────────────────────────────────────────────────────────────────


class TestDimensions:
    def test_valid(self) -> None:
        d = Dimensions(width=1920, height=1080)
        assert d.width == 1920
        assert d.height == 1080

    def test_zero_width_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Dimensions(width=0, height=100)

    def test_zero_height_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Dimensions(width=100, height=0)

    def test_negative_width_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Dimensions(width=-1, height=100)

    def test_negative_height_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Dimensions(width=100, height=-1)

    def test_min_valid(self) -> None:
        d = Dimensions(width=1, height=1)
        assert d.width == 1


# ── BoundingBox ────────────────────────────────────────────────────────────────


class TestBoundingBox:
    def test_valid(self) -> None:
        bb = _bbox()
        assert bb.x_min == 100.0
        assert bb.y_max == 3100.0

    def test_x_equal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BoundingBox(x_min=50.0, y_min=20.0, x_max=50.0, y_max=200.0)

    def test_x_inverted_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BoundingBox(x_min=200.0, y_min=20.0, x_max=100.0, y_max=200.0)

    def test_y_equal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BoundingBox(x_min=10.0, y_min=200.0, x_max=100.0, y_max=200.0)

    def test_y_inverted_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BoundingBox(x_min=10.0, y_min=300.0, x_max=100.0, y_max=200.0)

    def test_float_coordinates_accepted(self) -> None:
        bb = BoundingBox(x_min=10.5, y_min=20.5, x_max=100.5, y_max=200.5)
        assert bb.x_min == 10.5

    def test_negative_coordinates_accepted(self) -> None:
        # BoundingBox alone does not enforce non-negativity — that is TransformRecord's job
        bb = BoundingBox(x_min=-10.0, y_min=-5.0, x_max=100.0, y_max=200.0)
        assert bb.x_min == -10.0


# ── TransformRecord ────────────────────────────────────────────────────────────


class TestTransformRecord:
    def test_valid(self) -> None:
        t = _transform()
        assert t.deskew_angle_deg == 1.5

    def test_crop_x_max_exceeds_width(self) -> None:
        with pytest.raises(ValidationError):
            _transform(orig_w=2400, x1=2401)  # x_max > original width

    def test_crop_y_max_exceeds_height(self) -> None:
        with pytest.raises(ValidationError):
            _transform(orig_h=3200, y1=3201)  # y_max > original height

    def test_crop_x_min_negative(self) -> None:
        with pytest.raises(ValidationError):
            _transform(x0=-1)  # x_min < 0

    def test_crop_y_min_negative(self) -> None:
        with pytest.raises(ValidationError):
            _transform(y0=-1)  # y_min < 0

    def test_crop_exactly_at_boundary(self) -> None:
        # Crop box touching the full image boundary is valid
        t = _transform(orig_w=2400, orig_h=3200, x0=0, y0=0, x1=2400, y1=3200)
        assert t.crop_box.x_max == 2400

    def test_negative_deskew_angle_valid(self) -> None:
        t = _transform(angle=-2.5)
        assert t.deskew_angle_deg == -2.5


# ── ProcessingContext ──────────────────────────────────────────────────────────


class TestProcessingContext:
    def test_valid(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        assert ctx.canonical_dimensions.width == 2200

    def test_mismatch_width_rejected(self) -> None:
        t = _transform(post_w=2200, post_h=3020)
        with pytest.raises(ValidationError):
            ProcessingContext(
                canonical_dimensions=Dimensions(width=1920, height=3020),  # width mismatch
                transform=t,
            )

    def test_mismatch_height_rejected(self) -> None:
        t = _transform(post_w=2200, post_h=3020)
        with pytest.raises(ValidationError):
            ProcessingContext(
                canonical_dimensions=Dimensions(width=2200, height=1080),  # height mismatch
                transform=t,
            )

    def test_both_mismatch_rejected(self) -> None:
        t = _transform(post_w=2200, post_h=3020)
        with pytest.raises(ValidationError):
            ProcessingContext(
                canonical_dimensions=Dimensions(width=1920, height=1080),
                transform=t,
            )


# ── validate_bbox_in_context ───────────────────────────────────────────────────


class TestValidateBboxInContext:
    def test_valid_bbox(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        bbox = BoundingBox(x_min=10, y_min=10, x_max=100, y_max=100)
        validate_bbox_in_context(bbox, ctx)  # must not raise

    def test_bbox_x_max_out_of_bounds(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        bbox = BoundingBox(x_min=10, y_min=10, x_max=2201, y_max=100)  # x_max > 2200
        with pytest.raises(ValueError, match="outside canonical_dimensions"):
            validate_bbox_in_context(bbox, ctx)

    def test_bbox_y_max_out_of_bounds(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        bbox = BoundingBox(x_min=10, y_min=10, x_max=100, y_max=3021)  # y_max > 3020
        with pytest.raises(ValueError, match="outside canonical_dimensions"):
            validate_bbox_in_context(bbox, ctx)

    def test_bbox_touching_boundary_valid(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        bbox = BoundingBox(x_min=0, y_min=0, x_max=2200, y_max=3020)
        validate_bbox_in_context(bbox, ctx)  # must not raise

    def test_bbox_negative_x_min_rejected(self) -> None:
        ctx = _ctx(post_w=2200, post_h=3020)
        bbox = BoundingBox(x_min=-1, y_min=10, x_max=100, y_max=100)
        with pytest.raises(ValueError):
            validate_bbox_in_context(bbox, ctx)


# ── DeskewResult ───────────────────────────────────────────────────────────────


class TestDeskewResult:
    def test_valid(self) -> None:
        d = DeskewResult(angle_deg=1.5, residual_deg=0.1, method="geometry_quad")
        assert d.method == "geometry_quad"

    def test_zero_residual_valid(self) -> None:
        d = DeskewResult(angle_deg=0.0, residual_deg=0.0, method="geometry_bbox")
        assert d.residual_deg == 0.0

    def test_negative_residual_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DeskewResult(angle_deg=1.5, residual_deg=-0.1, method="geometry_quad")

    def test_negative_angle_valid(self) -> None:
        d = DeskewResult(angle_deg=-3.0, residual_deg=0.05, method="geometry_quad")
        assert d.angle_deg == -3.0


# ── CropResult ─────────────────────────────────────────────────────────────────


class TestCropResult:
    def test_valid(self) -> None:
        c = CropResult(crop_box=_bbox(), border_score=0.9, method="geometry_quad")
        assert c.border_score == 0.9

    def test_border_score_zero_valid(self) -> None:
        c = CropResult(crop_box=_bbox(), border_score=0.0, method="geometry_quad")
        assert c.border_score == 0.0

    def test_border_score_one_valid(self) -> None:
        c = CropResult(crop_box=_bbox(), border_score=1.0, method="geometry_quad")
        assert c.border_score == 1.0

    def test_border_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CropResult(crop_box=_bbox(), border_score=1.01, method="geometry_quad")

    def test_border_score_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CropResult(crop_box=_bbox(), border_score=-0.1, method="geometry_quad")


# ── SplitResult ────────────────────────────────────────────────────────────────


class TestSplitResult:
    def test_valid_no_split(self) -> None:
        s = SplitResult(
            split_required=False, split_x=None, split_confidence=None, method="instance_boundary"
        )
        assert not s.split_required
        assert s.split_x is None
        assert s.split_confidence is None

    def test_valid_with_split(self) -> None:
        s = SplitResult(
            split_required=True, split_x=1200, split_confidence=0.92, method="instance_boundary"
        )
        assert s.split_x == 1200
        assert s.split_confidence == 0.92

    def test_negative_split_x_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SplitResult(
                split_required=True, split_x=-1, split_confidence=0.9, method="instance_boundary"
            )

    def test_zero_split_x_valid(self) -> None:
        s = SplitResult(
            split_required=True, split_x=0, split_confidence=0.8, method="instance_boundary"
        )
        assert s.split_x == 0

    def test_split_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SplitResult(
                split_required=True, split_x=1200, split_confidence=1.5, method="instance_boundary"
            )

    def test_split_confidence_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SplitResult(
                split_required=True,
                split_x=1200,
                split_confidence=-0.1,
                method="instance_boundary",
            )


# ── QualityMetrics ─────────────────────────────────────────────────────────────


class TestQualityMetrics:
    def test_valid_no_split(self) -> None:
        q = QualityMetrics(
            skew_residual=0.05,
            blur_score=0.8,
            border_score=0.9,
            split_confidence=None,
            foreground_coverage=0.95,
        )
        assert q.foreground_coverage == 0.95

    def test_valid_with_split_confidence(self) -> None:
        q = QualityMetrics(
            skew_residual=0.05,
            blur_score=0.8,
            border_score=0.9,
            split_confidence=0.87,
            foreground_coverage=0.95,
        )
        assert q.split_confidence == 0.87

    def test_negative_skew_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualityMetrics(
                skew_residual=-0.1,
                blur_score=0.8,
                border_score=0.9,
                split_confidence=None,
                foreground_coverage=0.95,
            )

    def test_blur_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualityMetrics(
                skew_residual=0.05,
                blur_score=1.1,
                border_score=0.9,
                split_confidence=None,
                foreground_coverage=0.95,
            )

    def test_foreground_coverage_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualityMetrics(
                skew_residual=0.05,
                blur_score=0.8,
                border_score=0.9,
                split_confidence=None,
                foreground_coverage=-0.01,
            )

    def test_split_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QualityMetrics(
                skew_residual=0.05,
                blur_score=0.8,
                border_score=0.9,
                split_confidence=1.5,
                foreground_coverage=0.95,
            )


# ── PreprocessBranchResponse ───────────────────────────────────────────────────


class TestPreprocessBranchResponse:
    def test_valid_iep1a(self) -> None:
        r = _branch_response()
        assert r.source_model == "iep1a"
        assert r.processing_time_ms == 145.2
        assert r.warnings == []

    def test_valid_iep1b(self) -> None:
        data = _branch_response().model_dump()
        data["source_model"] = "iep1b"
        r = PreprocessBranchResponse(**data)
        assert r.source_model == "iep1b"

    def test_invalid_source_model_rejected(self) -> None:
        data = _branch_response().model_dump()
        data["source_model"] = "iep1c"
        with pytest.raises(ValidationError):
            PreprocessBranchResponse(**data)

    def test_negative_processing_time_rejected(self) -> None:
        data = _branch_response().model_dump()
        data["processing_time_ms"] = -1.0
        with pytest.raises(ValidationError):
            PreprocessBranchResponse(**data)

    def test_zero_processing_time_valid(self) -> None:
        data = _branch_response().model_dump()
        data["processing_time_ms"] = 0.0
        r = PreprocessBranchResponse(**data)
        assert r.processing_time_ms == 0.0

    def test_warnings_list(self) -> None:
        data = _branch_response().model_dump()
        data["warnings"] = ["low_contrast", "partial_border"]
        r = PreprocessBranchResponse(**data)
        assert len(r.warnings) == 2


# ── PreprocessError ────────────────────────────────────────────────────────────


class TestPreprocessError:
    def test_valid_escalate(self) -> None:
        e = PreprocessError(
            error_code="GEOMETRY_FAILED",
            error_message="No valid geometry candidates",
            fallback_action="ESCALATE_REVIEW",
        )
        assert e.fallback_action == "ESCALATE_REVIEW"

    def test_valid_retry(self) -> None:
        e = PreprocessError(
            error_code="TIMEOUT",
            error_message="Service timed out after 30s",
            fallback_action="RETRY",
        )
        assert e.error_code == "TIMEOUT"

    def test_all_error_codes_valid(self) -> None:
        codes = ["INVALID_IMAGE", "UNSUPPORTED_FORMAT", "TIMEOUT", "INTERNAL", "GEOMETRY_FAILED"]
        for code in codes:
            # model_validate accepts dict[str, Any]; avoids Literal mismatch for str variable
            e = PreprocessError.model_validate(
                {"error_code": code, "error_message": "test", "fallback_action": "RETRY"}
            )
            assert e.error_code == code

    def test_invalid_error_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            # model_validate used so mypy does not flag the deliberately-invalid literal
            PreprocessError.model_validate(
                {"error_code": "UNKNOWN_ERROR", "error_message": "oops", "fallback_action": "RETRY"}
            )

    def test_invalid_fallback_action_rejected(self) -> None:
        with pytest.raises(ValidationError):
            # model_validate used so mypy does not flag the deliberately-invalid literal
            PreprocessError.model_validate(
                {"error_code": "INTERNAL", "error_message": "oops", "fallback_action": "IGNORE"}
            )
