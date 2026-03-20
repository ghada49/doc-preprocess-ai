"""
tests/test_p2_normalization.py
-------------------------------
Contract tests for Packet 2.5: shared normalization core.

Covers:
  - perspective.py : four_point_transform
  - deskew.py      : compute_deskew_angle, apply_affine_deskew
  - normalize.py   : normalize_single_page, NormalizeResult,
                     normalize_result_to_branch_response

All tests use synthetic numpy arrays — no real images or storage required.
Quality metrics are asserted to be placeholder zeros in Packet 2.5.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pytest

from shared.normalization.deskew import apply_affine_deskew, compute_deskew_angle
from shared.normalization.normalize import (
    NormalizeResult,
    normalize_result_to_branch_response,
    normalize_single_page,
)
from shared.normalization.perspective import four_point_transform
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import (
    CropResult,
    DeskewResult,
    PreprocessBranchResponse,
    QualityMetrics,
    SplitResult,
)
from shared.schemas.ucf import BoundingBox, TransformRecord

# ── Helpers ───────────────────────────────────────────────────────────────────


def _solid(h: int = 400, w: int = 600, c: int = 3, fill: int = 128) -> np.ndarray:
    """Solid-color H×W×C uint8 image."""
    return np.full((h, w, c), fill, dtype=np.uint8)


def _make_geo(
    *,
    page_count: int = 1,
    confidence: float = 0.95,
    geometry_type: Literal["quadrilateral", "mask_ref", "bbox"] = "quadrilateral",
    w: int = 600,
    h: int = 400,
    tta_agreement: float = 1.0,
) -> GeometryResponse:
    """Construct a minimal GeometryResponse for testing."""
    pages = []
    for i in range(page_count):
        half_w = w // page_count
        x0 = i * half_w + 20
        x1 = (i + 1) * half_w - 20
        y0, y1 = 20, h - 20
        area_fraction = round((x1 - x0) * (y1 - y0) / (w * h), 4)
        corners: list[tuple[float, float]] | None = (
            [
                (float(x0), float(y0)),
                (float(x1), float(y0)),
                (float(x1), float(y1)),
                (float(x0), float(y1)),
            ]
            if geometry_type == "quadrilateral"
            else None
        )
        pages.append(
            PageRegion(
                region_id=f"page_{i}",
                geometry_type=geometry_type,
                corners=corners,
                bbox=(x0, y0, x1, y1),
                confidence=confidence,
                page_area_fraction=area_fraction,
            )
        )
    split_required = page_count == 2
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=w // 2 if split_required else None,
        geometry_confidence=confidence,
        tta_structural_agreement_rate=tta_agreement,
        tta_prediction_variance=0.001,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=1.0,
    )


# ── TestFourPointTransform ─────────────────────────────────────────────────────


class TestFourPointTransform:
    """Tests for shared.normalization.perspective.four_point_transform."""

    def test_returns_three_element_tuple(self) -> None:
        img = _solid(400, 600)
        corners = [(10.0, 10.0), (590.0, 10.0), (590.0, 390.0), (10.0, 390.0)]
        result = four_point_transform(img, corners)
        assert isinstance(result, tuple) and len(result) == 3

    def test_warped_is_ndarray(self) -> None:
        img = _solid(400, 600)
        warped, _, _ = four_point_transform(
            img, [(10.0, 10.0), (590.0, 10.0), (590.0, 390.0), (10.0, 390.0)]
        )
        assert isinstance(warped, np.ndarray)

    def test_homography_is_3x3(self) -> None:
        img = _solid(400, 600)
        _, _, homography = four_point_transform(
            img, [(10.0, 10.0), (590.0, 10.0), (590.0, 390.0), (10.0, 390.0)]
        )
        assert homography.shape == (3, 3)

    def test_source_bbox_correct_for_axis_aligned(self) -> None:
        img = _solid(400, 600)
        corners = [(10.0, 20.0), (590.0, 20.0), (590.0, 380.0), (10.0, 380.0)]
        _, bbox, _ = four_point_transform(img, corners)
        x_min, y_min, x_max, y_max = bbox
        assert x_min == pytest.approx(10.0)
        assert y_min == pytest.approx(20.0)
        assert x_max == pytest.approx(590.0)
        assert y_max == pytest.approx(380.0)

    def test_output_dimensions_match_bbox_size(self) -> None:
        img = _solid(400, 600)
        x0, y0, x1, y1 = 10.0, 20.0, 590.0, 380.0
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        warped, _, _ = four_point_transform(img, corners)
        assert warped.shape[1] == int(x1 - x0)  # width
        assert warped.shape[0] == int(y1 - y0)  # height

    def test_grayscale_image_supported(self) -> None:
        img = np.full((200, 300), 200, dtype=np.uint8)
        corners = [(5.0, 5.0), (295.0, 5.0), (295.0, 195.0), (5.0, 195.0)]
        warped, _, _ = four_point_transform(img, corners)
        assert warped.ndim == 2

    def test_disordered_corners_normalised(self) -> None:
        """Corners given in reverse order produce the same bbox."""
        img = _solid(400, 600)
        # bottom-right, top-right, bottom-left, top-left
        corners = [(590.0, 390.0), (590.0, 10.0), (10.0, 390.0), (10.0, 10.0)]
        warped, bbox, _ = four_point_transform(img, corners)
        assert warped.ndim == 3
        x_min, y_min, x_max, y_max = bbox
        assert x_min == pytest.approx(10.0)
        assert y_max == pytest.approx(390.0)

    def test_output_channels_preserved(self) -> None:
        img = _solid(200, 300, c=3)
        corners = [(5.0, 5.0), (295.0, 5.0), (295.0, 195.0), (5.0, 195.0)]
        warped, _, _ = four_point_transform(img, corners)
        assert warped.shape[2] == 3

    def test_source_bbox_is_four_floats(self) -> None:
        img = _solid(200, 300)
        corners = [(0.0, 0.0), (299.0, 0.0), (299.0, 199.0), (0.0, 199.0)]
        _, bbox, _ = four_point_transform(img, corners)
        assert len(bbox) == 4
        assert all(isinstance(v, float) for v in bbox)


# ── TestComputeDeskewAngle ─────────────────────────────────────────────────────


class TestComputeDeskewAngle:
    """Tests for shared.normalization.deskew.compute_deskew_angle."""

    def test_axis_aligned_returns_near_zero(self) -> None:
        corners = [(10.0, 10.0), (590.0, 10.0), (590.0, 390.0), (10.0, 390.0)]
        angle = compute_deskew_angle(corners)
        assert abs(angle) < 0.01

    def test_returns_float(self) -> None:
        corners = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        assert isinstance(compute_deskew_angle(corners), float)

    def test_small_skew_magnitude(self) -> None:
        """Near-horizontal edges yield a small angle."""
        corners = [(0.0, 0.0), (100.0, 2.0), (100.0, 102.0), (0.0, 100.0)]
        assert abs(compute_deskew_angle(corners)) < 5.0

    def test_symmetric_positive_skew(self) -> None:
        """Both edges tilted upward to the right → negative angle (atan2 sign)."""
        corners = [(10.0, 20.0), (590.0, 10.0), (590.0, 390.0), (10.0, 400.0)]
        angle = compute_deskew_angle(corners)
        assert isinstance(angle, float)
        assert abs(angle) < 5.0  # small realistic skew

    def test_disordered_corners_give_same_result(self) -> None:
        """Reordering the input corners should not change the computed angle."""
        corners_std = [(10.0, 10.0), (590.0, 10.0), (590.0, 390.0), (10.0, 390.0)]
        corners_rev = [(10.0, 390.0), (590.0, 390.0), (590.0, 10.0), (10.0, 10.0)]
        a1 = compute_deskew_angle(corners_std)
        a2 = compute_deskew_angle(corners_rev)
        assert abs(a1 - a2) < 0.01


# ── TestApplyAffineDeskew ──────────────────────────────────────────────────────


class TestApplyAffineDeskew:
    """Tests for shared.normalization.deskew.apply_affine_deskew."""

    def test_returns_tuple(self) -> None:
        img = _solid(200, 300)
        cropped, residual = apply_affine_deskew(img, 0.0, (10, 10, 290, 190))
        assert isinstance(cropped, np.ndarray)
        assert isinstance(residual, float)

    def test_residual_is_zero_placeholder(self) -> None:
        img = _solid(200, 300)
        _, residual = apply_affine_deskew(img, 5.0, (10, 10, 290, 190))
        assert residual == 0.0

    def test_zero_angle_crops_exact_bbox(self) -> None:
        img = _solid(200, 300)
        x0, y0, x1, y1 = 10, 20, 290, 180
        cropped, _ = apply_affine_deskew(img, 0.0, (x0, y0, x1, y1))
        assert cropped.shape[0] == y1 - y0
        assert cropped.shape[1] == x1 - x0

    def test_non_zero_angle_returns_valid_image(self) -> None:
        img = _solid(400, 600)
        cropped, _ = apply_affine_deskew(img, 3.5, (20, 20, 580, 380))
        assert cropped.ndim == 3
        assert cropped.size > 0

    def test_bbox_clamped_to_image_bounds(self) -> None:
        """Out-of-bounds bbox is silently clamped; no exception raised."""
        img = _solid(100, 100)
        cropped, _ = apply_affine_deskew(img, 0.0, (-10, -10, 110, 110))
        assert cropped.shape[0] <= 100
        assert cropped.shape[1] <= 100

    def test_channels_preserved(self) -> None:
        img = _solid(200, 300, c=3)
        cropped, _ = apply_affine_deskew(img, 2.0, (10, 10, 290, 190))
        assert cropped.shape[2] == 3

    def test_grayscale_supported(self) -> None:
        img = np.full((200, 300), 100, dtype=np.uint8)
        cropped, _ = apply_affine_deskew(img, 0.0, (10, 10, 290, 190))
        assert cropped.ndim == 2


# ── TestNormalizeSinglePageQuad ────────────────────────────────────────────────


class TestNormalizeSinglePageQuad:
    """normalize_single_page with quadrilateral geometry (perspective path)."""

    def _run(self, *, w: int = 600, h: int = 400, confidence: float = 0.95) -> NormalizeResult:
        img = _solid(h, w)
        geo = _make_geo(w=w, h=h, confidence=confidence, geometry_type="quadrilateral")
        return normalize_single_page(img, geo.pages[0], geo)

    def test_returns_normalize_result(self) -> None:
        assert isinstance(self._run(), NormalizeResult)

    def test_image_is_ndarray(self) -> None:
        assert isinstance(self._run().image, np.ndarray)

    def test_image_is_three_dimensional(self) -> None:
        assert self._run().image.ndim == 3

    def test_deskew_method_is_geometry_quad(self) -> None:
        assert self._run().deskew.method == "geometry_quad"

    def test_crop_method_is_geometry_quad(self) -> None:
        assert self._run().crop.method == "geometry_quad"

    def test_deskew_record_type(self) -> None:
        assert isinstance(self._run().deskew, DeskewResult)

    def test_deskew_residual_nonnegative(self) -> None:
        assert self._run().deskew.residual_deg >= 0.0

    def test_crop_record_type(self) -> None:
        assert isinstance(self._run().crop, CropResult)

    def test_crop_box_is_bounding_box(self) -> None:
        assert isinstance(self._run().crop.crop_box, BoundingBox)

    def test_split_not_required_single_page(self) -> None:
        r = self._run()
        assert r.split.split_required is False
        assert r.split.split_x is None
        assert r.split.split_confidence is None

    def test_quality_blur_score_placeholder(self) -> None:
        assert self._run().quality.blur_score == pytest.approx(0.0)

    def test_quality_border_score_in_range(self) -> None:
        assert 0.0 <= self._run().quality.border_score <= 1.0

    def test_quality_foreground_coverage_placeholder(self) -> None:
        assert self._run().quality.foreground_coverage == pytest.approx(0.0)

    def test_quality_split_confidence_none_single_page(self) -> None:
        assert self._run().quality.split_confidence is None

    def test_transform_type(self) -> None:
        assert isinstance(self._run().transform, TransformRecord)

    def test_transform_original_dimensions(self) -> None:
        r = self._run(w=600, h=400)
        assert r.transform.original_dimensions.width == 600
        assert r.transform.original_dimensions.height == 400

    def test_post_preprocessing_dimensions_match_output_image(self) -> None:
        r = self._run()
        h, w = r.image.shape[:2]
        assert r.transform.post_preprocessing_dimensions.width == w
        assert r.transform.post_preprocessing_dimensions.height == h

    def test_crop_box_within_original_dimensions(self) -> None:
        r = self._run(w=600, h=400)
        cb = r.transform.crop_box
        assert cb.x_min >= 0.0
        assert cb.y_min >= 0.0
        assert cb.x_max <= 600.0
        assert cb.y_max <= 400.0

    def test_warnings_is_list(self) -> None:
        assert isinstance(self._run().warnings, list)

    def test_processing_time_ms_nonnegative(self) -> None:
        assert self._run().processing_time_ms >= 0.0


# ── TestNormalizeSinglePageBbox ────────────────────────────────────────────────


class TestNormalizeSinglePageBbox:
    """normalize_single_page with bbox geometry (affine deskew path)."""

    def _run(self, *, w: int = 600, h: int = 400) -> NormalizeResult:
        img = _solid(h, w)
        geo = _make_geo(w=w, h=h, geometry_type="bbox")
        return normalize_single_page(img, geo.pages[0], geo)

    def test_returns_normalize_result(self) -> None:
        assert isinstance(self._run(), NormalizeResult)

    def test_deskew_method_is_geometry_bbox(self) -> None:
        assert self._run().deskew.method == "geometry_bbox"

    def test_crop_method_is_geometry_bbox(self) -> None:
        assert self._run().crop.method == "geometry_bbox"

    def test_angle_zero_for_bbox_path(self) -> None:
        assert self._run().deskew.angle_deg == pytest.approx(0.0)

    def test_output_image_valid(self) -> None:
        r = self._run()
        assert isinstance(r.image, np.ndarray)
        assert r.image.size > 0

    def test_crop_box_within_original(self) -> None:
        r = self._run(w=600, h=400)
        cb = r.transform.crop_box
        assert cb.x_min >= 0.0
        assert cb.y_min >= 0.0
        assert cb.x_max <= 600.0
        assert cb.y_max <= 400.0


# ── TestNormalizeSinglePageNoGeometry ─────────────────────────────────────────


class TestNormalizeSinglePageNoGeometry:
    """normalize_single_page when PageRegion has no bbox and no corners."""

    def test_degenerate_page_uses_full_image(self) -> None:
        """A page with geometry_type='bbox' and bbox=None falls back to full image."""
        img = _solid(200, 300)
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=None,
            confidence=0.9,
            page_area_fraction=1.0,
        )
        geo = GeometryResponse(
            page_count=1,
            pages=[page],
            split_required=False,
            split_x=None,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=1.0,
            tta_prediction_variance=0.001,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=1.0,
        )
        result = normalize_single_page(img, page, geo)
        assert isinstance(result, NormalizeResult)
        assert len(result.warnings) > 0
        assert "no geometry available" in result.warnings[0]

    def test_degenerate_page_output_size_matches_original(self) -> None:
        """Full-image fallback: output is the full rotated image."""
        img = _solid(200, 300)
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=None,
            confidence=0.9,
            page_area_fraction=1.0,
        )
        geo = GeometryResponse(
            page_count=1,
            pages=[page],
            split_required=False,
            split_x=None,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=1.0,
            tta_prediction_variance=0.001,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=1.0,
        )
        result = normalize_single_page(img, page, geo)
        h, w = result.image.shape[:2]
        assert h == 200
        assert w == 300


# ── TestNormalizeSinglePageSplitMetadata ──────────────────────────────────────


class TestNormalizeSinglePageSplitMetadata:
    """Split metadata propagation in normalize_single_page."""

    def test_split_confidence_when_split_required(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=2, confidence=0.90, w=600, h=400)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.split.split_required is True
        assert result.split.split_x == 300
        assert result.split.split_confidence == pytest.approx(0.90)  # min(0.90, 1.0)

    def test_split_confidence_uses_min_of_page_and_tta(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=2, confidence=0.85, w=600, h=400, tta_agreement=0.75)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.split.split_confidence == pytest.approx(0.75)  # min(0.85, 0.75)

    def test_quality_split_confidence_matches_split_result(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=2, confidence=0.90, w=600, h=400)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.quality.split_confidence == result.split.split_confidence

    def test_split_method_instance_boundary_when_split(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=2, w=600, h=400)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.split.method == "instance_boundary"

    def test_split_method_none_when_no_split(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=1, w=600, h=400)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.split.method == "none"

    def test_split_confidence_none_when_no_split(self) -> None:
        img = _solid(400, 600)
        geo = _make_geo(page_count=1, w=600, h=400)
        result = normalize_single_page(img, geo.pages[0], geo)
        assert result.split.split_confidence is None
        assert result.quality.split_confidence is None


# ── TestNormalizeResultTypes ───────────────────────────────────────────────────


class TestNormalizeResultTypes:
    """NormalizeResult field type assertions."""

    def _result(self) -> NormalizeResult:
        img = _solid(200, 300)
        geo = _make_geo(w=300, h=200)
        return normalize_single_page(img, geo.pages[0], geo)

    def test_quality_is_quality_metrics(self) -> None:
        assert isinstance(self._result().quality, QualityMetrics)

    def test_split_is_split_result(self) -> None:
        assert isinstance(self._result().split, SplitResult)

    def test_deskew_is_deskew_result(self) -> None:
        assert isinstance(self._result().deskew, DeskewResult)

    def test_crop_is_crop_result(self) -> None:
        assert isinstance(self._result().crop, CropResult)

    def test_transform_is_transform_record(self) -> None:
        assert isinstance(self._result().transform, TransformRecord)

    def test_post_dims_match_output_image(self) -> None:
        r = self._result()
        h, w = r.image.shape[:2]
        assert r.transform.post_preprocessing_dimensions.width == w
        assert r.transform.post_preprocessing_dimensions.height == h

    def test_crop_border_score_in_range(self) -> None:
        assert 0.0 <= self._result().crop.border_score <= 1.0


# ── TestNormalizeResultToBranchResponse ───────────────────────────────────────


class TestNormalizeResultToBranchResponse:
    """
    Phase 2 DoD: 'IEP1C produces real PreprocessBranchResponse'.

    normalize_result_to_branch_response is the IEP1C adapter that assembles
    the canonical output schema from a completed NormalizeResult plus the two
    caller-supplied fields (source_model from Phase 3 selection, and
    processed_image_uri from Phase 4 storage write).
    """

    _URI = "s3://libraryai-test/jobs/job1/pages/1/output.ptiff"

    def _result(self) -> NormalizeResult:
        img = _solid(200, 300)
        geo = _make_geo(w=300, h=200)
        return normalize_single_page(img, geo.pages[0], geo)

    def _branch(
        self, source_model: Literal["iep1a", "iep1b"] = "iep1a"
    ) -> PreprocessBranchResponse:
        return normalize_result_to_branch_response(self._result(), source_model, self._URI)

    def test_returns_preprocess_branch_response(self) -> None:
        assert isinstance(self._branch(), PreprocessBranchResponse)

    def test_processed_image_uri_preserved(self) -> None:
        assert self._branch().processed_image_uri == self._URI

    def test_source_model_iep1a(self) -> None:
        assert self._branch("iep1a").source_model == "iep1a"

    def test_source_model_iep1b(self) -> None:
        assert self._branch("iep1b").source_model == "iep1b"

    def test_deskew_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.deskew == r.deskew

    def test_crop_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.crop == r.crop

    def test_split_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.split == r.split

    def test_quality_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.quality == r.quality

    def test_transform_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.transform == r.transform

    def test_processing_time_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.processing_time_ms == r.processing_time_ms

    def test_warnings_preserved(self) -> None:
        r = self._result()
        b = normalize_result_to_branch_response(r, "iep1a", self._URI)
        assert b.warnings == r.warnings

    def test_pydantic_model_validates(self) -> None:
        # Confirm the assembled model passes Pydantic validation by round-tripping.
        b = self._branch()
        assert PreprocessBranchResponse.model_validate(b.model_dump()) is not None
