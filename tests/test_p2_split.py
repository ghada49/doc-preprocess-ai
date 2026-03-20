"""
tests/test_p2_split.py
-----------------------
Contract tests for Packet 2.6: split normalization path.

Covers:
  - split_and_normalize: slices image at split_x and normalizes each child
  - Coordinate shifting for right-child page regions
  - Error cases: split_required=False, split_x=None, wrong page count
  - Both quadrilateral and bbox geometry paths
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pytest

from shared.normalization.normalize import NormalizeResult
from shared.normalization.split import split_and_normalize
from shared.schemas.geometry import GeometryResponse, PageRegion

# ── Helpers ───────────────────────────────────────────────────────────────────


def _solid(h: int = 400, w: int = 600, c: int = 3, fill: int = 128) -> np.ndarray:
    """Solid-color H×W×C uint8 image."""
    return np.full((h, w, c), fill, dtype=np.uint8)


def _make_split_geo(
    *,
    confidence: float = 0.90,
    geometry_type: Literal["quadrilateral", "mask_ref", "bbox"] = "quadrilateral",
    w: int = 600,
    h: int = 400,
    tta_agreement: float = 1.0,
) -> GeometryResponse:
    """Construct a two-page GeometryResponse for split tests."""
    split_x = w // 2
    pages = []
    for i in range(2):
        half_w = w // 2
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
    return GeometryResponse(
        page_count=2,
        pages=pages,
        split_required=True,
        split_x=split_x,
        geometry_confidence=confidence,
        tta_structural_agreement_rate=tta_agreement,
        tta_prediction_variance=0.001,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=1.0,
    )


# ── TestSplitAndNormalizeBasic ─────────────────────────────────────────────────


class TestSplitAndNormalizeBasic:
    """Core behaviour of split_and_normalize."""

    def _run(
        self, *, w: int = 600, h: int = 400, confidence: float = 0.90
    ) -> tuple[NormalizeResult, NormalizeResult]:
        img = _solid(h, w)
        geo = _make_split_geo(w=w, h=h, confidence=confidence)
        return split_and_normalize(img, geo)

    def test_returns_two_element_tuple(self) -> None:
        result = self._run()
        assert isinstance(result, tuple) and len(result) == 2

    def test_left_is_normalize_result(self) -> None:
        left, _ = self._run()
        assert isinstance(left, NormalizeResult)

    def test_right_is_normalize_result(self) -> None:
        _, right = self._run()
        assert isinstance(right, NormalizeResult)

    def test_left_image_is_ndarray(self) -> None:
        left, _ = self._run()
        assert isinstance(left.image, np.ndarray)

    def test_right_image_is_ndarray(self) -> None:
        _, right = self._run()
        assert isinstance(right.image, np.ndarray)

    def test_left_original_width_equals_split_x(self) -> None:
        left, _ = self._run(w=600)
        assert left.transform.original_dimensions.width == 300  # split_x = w//2

    def test_right_original_width_equals_remainder(self) -> None:
        _, right = self._run(w=600)
        assert right.transform.original_dimensions.width == 300  # w - split_x

    def test_left_original_height_equals_full_image_height(self) -> None:
        left, _ = self._run(h=400)
        assert left.transform.original_dimensions.height == 400

    def test_right_original_height_equals_full_image_height(self) -> None:
        _, right = self._run(h=400)
        assert right.transform.original_dimensions.height == 400

    def test_left_split_required_true(self) -> None:
        left, _ = self._run()
        assert left.split.split_required is True

    def test_right_split_required_true(self) -> None:
        _, right = self._run()
        assert right.split.split_required is True

    def test_left_split_method_instance_boundary(self) -> None:
        left, _ = self._run()
        assert left.split.method == "instance_boundary"

    def test_right_split_method_instance_boundary(self) -> None:
        _, right = self._run()
        assert right.split.method == "instance_boundary"

    def test_left_image_not_empty(self) -> None:
        left, _ = self._run()
        assert left.image.size > 0

    def test_right_image_not_empty(self) -> None:
        _, right = self._run()
        assert right.image.size > 0

    def test_both_processing_time_nonnegative(self) -> None:
        left, right = self._run()
        assert left.processing_time_ms >= 0.0
        assert right.processing_time_ms >= 0.0


# ── TestSplitAndNormalizeErrors ────────────────────────────────────────────────


class TestSplitAndNormalizeErrors:
    """Error cases for split_and_normalize."""

    def test_raises_if_split_not_required(self) -> None:
        img = _solid(400, 600)
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=(20, 20, 580, 380),
            confidence=0.9,
            page_area_fraction=0.9,
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
        with pytest.raises(ValueError, match="split_required=True"):
            split_and_normalize(img, geo)

    def test_raises_if_split_x_none(self) -> None:
        img = _solid(400, 600)
        pages = []
        for i in range(2):
            pages.append(
                PageRegion(
                    region_id=f"page_{i}",
                    geometry_type="bbox",
                    corners=None,
                    bbox=(20, 20, 280, 380),
                    confidence=0.9,
                    page_area_fraction=0.4,
                )
            )
        geo = GeometryResponse(
            page_count=2,
            pages=pages,
            split_required=True,
            split_x=None,  # intentionally None
            geometry_confidence=0.9,
            tta_structural_agreement_rate=1.0,
            tta_prediction_variance=0.001,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=1.0,
        )
        with pytest.raises(ValueError, match="split_x"):
            split_and_normalize(img, geo)

    def test_raises_if_not_two_pages(self) -> None:
        img = _solid(400, 600)
        page = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=(20, 20, 580, 380),
            confidence=0.9,
            page_area_fraction=0.9,
        )
        # Construct a fresh object with only 1 page by bypassing Pydantic
        # validation via model_construct so we can test our own guard
        bad_geo = GeometryResponse.model_construct(
            page_count=2,
            pages=[page],  # only 1, mismatches our guard
            split_required=True,
            split_x=300,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=1.0,
            tta_prediction_variance=0.001,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=1.0,
        )
        with pytest.raises(ValueError, match="2 pages"):
            split_and_normalize(img, bad_geo)


# ── TestSplitAndNormalizeBboxPath ──────────────────────────────────────────────


class TestSplitAndNormalizeBboxPath:
    """split_and_normalize with bbox geometry (affine deskew path)."""

    def _run(self, w: int = 600, h: int = 400) -> tuple[NormalizeResult, NormalizeResult]:
        img = _solid(h, w)
        geo = _make_split_geo(w=w, h=h, geometry_type="bbox")
        return split_and_normalize(img, geo)

    def test_returns_two_results(self) -> None:
        left, right = self._run()
        assert isinstance(left, NormalizeResult)
        assert isinstance(right, NormalizeResult)

    def test_left_deskew_method_is_bbox(self) -> None:
        left, _ = self._run()
        assert left.deskew.method == "geometry_bbox"

    def test_right_deskew_method_is_bbox(self) -> None:
        _, right = self._run()
        assert right.deskew.method == "geometry_bbox"

    def test_left_angle_is_zero(self) -> None:
        left, _ = self._run()
        assert left.deskew.angle_deg == pytest.approx(0.0)

    def test_right_angle_is_zero(self) -> None:
        _, right = self._run()
        assert right.deskew.angle_deg == pytest.approx(0.0)


# ── TestSplitAndNormalizeQuality ───────────────────────────────────────────────


class TestSplitAndNormalizeQuality:
    """Quality metrics and split confidence propagation."""

    def _run(
        self, *, confidence: float = 0.90, tta_agreement: float = 1.0
    ) -> tuple[NormalizeResult, NormalizeResult]:
        img = _solid(400, 600)
        geo = _make_split_geo(confidence=confidence, tta_agreement=tta_agreement)
        return split_and_normalize(img, geo)

    def test_left_quality_blur_score_placeholder(self) -> None:
        left, _ = self._run()
        assert left.quality.blur_score == pytest.approx(0.0)

    def test_right_quality_blur_score_placeholder(self) -> None:
        _, right = self._run()
        assert right.quality.blur_score == pytest.approx(0.0)

    def test_split_confidence_propagated_to_left(self) -> None:
        left, _ = self._run(confidence=0.90, tta_agreement=1.0)
        # split_confidence = min(0.90, 1.0) = 0.90
        assert left.split.split_confidence == pytest.approx(0.90)

    def test_split_confidence_propagated_to_right(self) -> None:
        _, right = self._run(confidence=0.90, tta_agreement=1.0)
        assert right.split.split_confidence == pytest.approx(0.90)

    def test_split_confidence_uses_min_of_page_and_tta(self) -> None:
        left, right = self._run(confidence=0.85, tta_agreement=0.75)
        assert left.split.split_confidence == pytest.approx(0.75)
        assert right.split.split_confidence == pytest.approx(0.75)

    def test_quality_split_confidence_matches_split_confidence(self) -> None:
        left, right = self._run()
        assert left.quality.split_confidence == left.split.split_confidence
        assert right.quality.split_confidence == right.split.split_confidence


# ── TestSplitCoordinateShift ───────────────────────────────────────────────────


class TestSplitCoordinateShift:
    """Verify right-child coordinates are correctly shifted into half-image space."""

    def test_right_crop_box_x_max_within_right_half_width(self) -> None:
        img = _solid(400, 600)
        geo = _make_split_geo(w=600, h=400, geometry_type="bbox")
        _, right = split_and_normalize(img, geo)
        right_half_w = 600 - 300  # 300
        assert right.transform.crop_box.x_max <= float(right_half_w)

    def test_right_crop_box_x_min_nonnegative(self) -> None:
        img = _solid(400, 600)
        geo = _make_split_geo(w=600, h=400, geometry_type="bbox")
        _, right = split_and_normalize(img, geo)
        assert right.transform.crop_box.x_min >= 0.0

    def test_left_crop_box_x_max_within_left_half_width(self) -> None:
        img = _solid(400, 600)
        geo = _make_split_geo(w=600, h=400, geometry_type="bbox")
        left, _ = split_and_normalize(img, geo)
        left_half_w = 300  # split_x
        assert left.transform.crop_box.x_max <= float(left_half_w)
