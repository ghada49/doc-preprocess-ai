"""tests.test_p3_geometry_selection
----------------------------------
Tests for Packet 3.1: structural agreement + six per-model sanity checks.

Covers:
  - check_structural_agreement: agree / disagree on page_count and split_required
  - check_sanity check 1: within_bounds
  - check_sanity check 2: non_degenerate
  - check_sanity check 3: area_fraction_plausible
  - check_sanity check 4: aspect_ratio_plausible
  - check_sanity check 5: corner_ordering_valid
  - check_sanity check 6: regions_non_overlapping
  - SanityCheckResult.as_dict() serialization
  - Combined: all checks pass, multiple checks fail simultaneously
"""

from __future__ import annotations

from typing import Literal

import pytest

from services.eep.app.gates.geometry_selection import (
    PreprocessingGateConfig,
    _bbox_iou,
    _corners_convex_and_valid,
    _quadrilateral_area,
    check_sanity,
    check_structural_agreement,
)
from shared.schemas.geometry import GeometryResponse, PageRegion

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

PROXY_W = 800
PROXY_H = 600
CONFIG = PreprocessingGateConfig()

# A valid convex quadrilateral (portrait-ish, well within proxy bounds)
VALID_CORNERS: list[tuple[float, float]] = [
    (50.0, 30.0),
    (350.0, 30.0),
    (350.0, 530.0),
    (50.0, 530.0),
]
VALID_BBOX: tuple[int, int, int, int] = (
    50,
    30,
    350,
    530,
)  # w=300, h=500 → ratio 0.6 (book: [0.5, 2.5] ✓)


def _region(
    region_id: str = "page_0",
    geometry_type: Literal["quadrilateral", "mask_ref", "bbox"] = "quadrilateral",
    corners: list[tuple[float, float]] | None = None,
    bbox: tuple[int, int, int, int] | None = None,
    confidence: float = 0.9,
    page_area_fraction: float = 0.5,
) -> PageRegion:
    return PageRegion(
        region_id=region_id,
        geometry_type=geometry_type,
        corners=corners if corners is not None else list(VALID_CORNERS),
        bbox=bbox if bbox is not None else VALID_BBOX,
        confidence=confidence,
        page_area_fraction=page_area_fraction,
    )


def _response(
    page_count: int = 1,
    pages: list[PageRegion] | None = None,
    split_required: bool = False,
    split_x: int | None = None,
    geometry_confidence: float = 0.9,
    tta_structural_agreement_rate: float = 0.95,
    tta_prediction_variance: float = 0.05,
    tta_passes: int = 5,
    uncertainty_flags: list[str] | None = None,
    warnings: list[str] | None = None,
    processing_time_ms: float = 100.0,
) -> GeometryResponse:
    if pages is None:
        pages = [_region()]
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=tta_prediction_variance,
        tta_passes=tta_passes,
        uncertainty_flags=uncertainty_flags or [],
        warnings=warnings or [],
        processing_time_ms=processing_time_ms,
    )


def _two_page_response(
    b1: tuple[int, int, int, int] = (10, 10, 390, 590),
    b2: tuple[int, int, int, int] = (410, 10, 790, 590),
    corners1: list[tuple[float, float]] | None = None,
    corners2: list[tuple[float, float]] | None = None,
) -> GeometryResponse:
    """Helper to build a page_count=2 response with two non-overlapping pages."""
    r1 = _region(
        region_id="page_0",
        corners=corners1 or [(10.0, 10.0), (390.0, 10.0), (390.0, 590.0), (10.0, 590.0)],
        bbox=b1,
        page_area_fraction=0.48,
    )
    r2 = _region(
        region_id="page_1",
        corners=corners2 or [(410.0, 10.0), (790.0, 10.0), (790.0, 590.0), (410.0, 590.0)],
        bbox=b2,
        page_area_fraction=0.48,
    )
    return _response(
        page_count=2,
        pages=[r1, r2],
        split_required=True,
        split_x=400,
    )


# ---------------------------------------------------------------------------
# check_structural_agreement
# ---------------------------------------------------------------------------


class TestCheckStructuralAgreement:
    def test_both_single_page_agree(self) -> None:
        a = _response(page_count=1, split_required=False)
        b = _response(page_count=1, split_required=False)
        assert check_structural_agreement(a, b) is True

    def test_both_two_page_agree(self) -> None:
        a = _two_page_response()
        b = _two_page_response()
        assert check_structural_agreement(a, b) is True

    def test_disagree_on_page_count(self) -> None:
        a = _response(page_count=1, split_required=False)
        b = _two_page_response()
        assert check_structural_agreement(a, b) is False

    def test_disagree_on_split_required(self) -> None:
        # Same page_count but different split_required
        a = _response(page_count=1, split_required=False)
        # Manually build a response that has page_count=1 but split_required=True
        # (would be unusual but tests the comparison)
        b_split = GeometryResponse(
            page_count=1,
            pages=[_region()],
            split_required=True,
            split_x=None,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.9,
            tta_prediction_variance=0.05,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=100.0,
        )
        assert check_structural_agreement(a, b_split) is False

    def test_agree_both_split(self) -> None:
        a = _two_page_response()
        b = _two_page_response()
        assert check_structural_agreement(a, b) is True


# ---------------------------------------------------------------------------
# Private helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_quadrilateral_area_rectangle(self) -> None:
        corners: list[tuple[float, float]] = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)]
        assert _quadrilateral_area(corners) == pytest.approx(50.0)

    def test_quadrilateral_area_degenerate(self) -> None:
        corners: list[tuple[float, float]] = [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0), (5.0, 5.0)]
        assert _quadrilateral_area(corners) == pytest.approx(0.0)

    def test_corners_convex_rectangle(self) -> None:
        corners: list[tuple[float, float]] = [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 200.0),
            (0.0, 200.0),
        ]
        assert _corners_convex_and_valid(corners) is True

    def test_corners_convex_ccw_rectangle(self) -> None:
        # Same rectangle, CCW order
        corners: list[tuple[float, float]] = [
            (0.0, 0.0),
            (0.0, 200.0),
            (100.0, 200.0),
            (100.0, 0.0),
        ]
        assert _corners_convex_and_valid(corners) is True

    def test_corners_self_intersecting_bowtie(self) -> None:
        # Bowtie — self-intersecting
        corners: list[tuple[float, float]] = [
            (0.0, 0.0),
            (100.0, 100.0),
            (100.0, 0.0),
            (0.0, 100.0),
        ]
        assert _corners_convex_and_valid(corners) is False

    def test_corners_too_few(self) -> None:
        assert _corners_convex_and_valid([(0.0, 0.0), (1.0, 1.0)]) is False

    def test_bbox_iou_no_overlap(self) -> None:
        assert _bbox_iou((0, 0, 10, 10), (20, 20, 30, 30)) == pytest.approx(0.0)

    def test_bbox_iou_full_overlap(self) -> None:
        assert _bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)

    def test_bbox_iou_partial_overlap(self) -> None:
        # (0,0,10,10) and (5,0,15,10) share a 5×10=50 strip
        # union = 100 + 100 - 50 = 150
        iou = _bbox_iou((0, 0, 10, 10), (5, 0, 15, 10))
        assert iou == pytest.approx(50 / 150)


# ---------------------------------------------------------------------------
# check_sanity — check 1: within_bounds
# ---------------------------------------------------------------------------


class TestSanityWithinBounds:
    def test_all_within_bounds_passes(self) -> None:
        result = check_sanity(_response(), "book", PROXY_W, PROXY_H, CONFIG)
        assert "within_bounds" not in result.failed_checks

    def test_corner_outside_x_fails(self) -> None:
        bad_corners: list[tuple[float, float]] = [
            (50.0, 30.0),
            (900.0, 30.0),  # x > PROXY_W=800
            (900.0, 530.0),
            (50.0, 530.0),
        ]
        r = _region(corners=bad_corners, bbox=(50, 30, 900, 530))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "within_bounds" in result.failed_checks
        assert result.passed is False

    def test_corner_outside_y_fails(self) -> None:
        bad_corners: list[tuple[float, float]] = [
            (50.0, 30.0),
            (350.0, 30.0),
            (350.0, 700.0),  # y > PROXY_H=600
            (50.0, 700.0),
        ]
        r = _region(corners=bad_corners, bbox=(50, 30, 350, 700))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "within_bounds" in result.failed_checks

    def test_negative_corner_fails(self) -> None:
        bad_corners: list[tuple[float, float]] = [
            (-10.0, 30.0),  # x < 0
            (350.0, 30.0),
            (350.0, 530.0),
            (-10.0, 530.0),
        ]
        r = _region(corners=bad_corners, bbox=(0, 30, 350, 530))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "within_bounds" in result.failed_checks

    def test_bbox_outside_bounds_fails(self) -> None:
        r = _region(corners=list(VALID_CORNERS), bbox=(50, 30, 850, 530))  # x_max > 800
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "within_bounds" in result.failed_checks


# ---------------------------------------------------------------------------
# check_sanity — check 2: non_degenerate
# ---------------------------------------------------------------------------


class TestSanityNonDegenerate:
    def test_valid_quad_passes(self) -> None:
        result = check_sanity(_response(), "book", PROXY_W, PROXY_H, CONFIG)
        assert "non_degenerate" not in result.failed_checks

    def test_zero_area_quad_fails(self) -> None:
        degenerate_corners: list[tuple[float, float]] = [
            (100.0, 100.0),
            (100.0, 100.0),
            (100.0, 100.0),
            (100.0, 100.0),
        ]
        r = _region(corners=degenerate_corners, bbox=(100, 100, 200, 200))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "non_degenerate" in result.failed_checks

    def test_zero_width_bbox_fails(self) -> None:
        r = _region(
            corners=list(VALID_CORNERS),
            bbox=(100, 50, 100, 400),  # width = 0
        )
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "non_degenerate" in result.failed_checks

    def test_zero_height_bbox_fails(self) -> None:
        r = _region(
            corners=list(VALID_CORNERS),
            bbox=(50, 200, 350, 200),  # height = 0
        )
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "non_degenerate" in result.failed_checks

    def test_bbox_geometry_type_no_corners(self) -> None:
        """Non-degenerate bbox-only region passes when bbox is valid."""
        r = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=(50, 30, 350, 530),
            confidence=0.9,
            page_area_fraction=0.5,
        )
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "non_degenerate" not in result.failed_checks


# ---------------------------------------------------------------------------
# check_sanity — check 3: area_fraction_plausible
# ---------------------------------------------------------------------------


class TestSanityAreaFraction:
    def test_valid_fraction_passes(self) -> None:
        r = _region(page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "area_fraction_plausible" not in result.failed_checks

    def test_fraction_at_min_boundary_passes(self) -> None:
        r = _region(page_area_fraction=0.15)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "area_fraction_plausible" not in result.failed_checks

    def test_fraction_at_max_boundary_passes(self) -> None:
        r = _region(page_area_fraction=0.98)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "area_fraction_plausible" not in result.failed_checks

    def test_fraction_below_min_fails(self) -> None:
        r = _region(page_area_fraction=0.05)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "area_fraction_plausible" in result.failed_checks

    def test_fraction_above_max_fails(self) -> None:
        r = _region(page_area_fraction=0.99)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "area_fraction_plausible" in result.failed_checks

    def test_custom_config_boundary(self) -> None:
        cfg = PreprocessingGateConfig(
            geometry_sanity_area_min_fraction=0.20,
            geometry_sanity_area_max_fraction=0.90,
        )
        r = _region(page_area_fraction=0.15)  # below custom min
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, cfg)
        assert "area_fraction_plausible" in result.failed_checks


# ---------------------------------------------------------------------------
# check_sanity — check 4: aspect_ratio_plausible
# ---------------------------------------------------------------------------


class TestSanityAspectRatio:
    def test_book_portrait_passes(self) -> None:
        # bbox w=300, h=500 → ratio=0.6, book bounds [0.5, 2.5]
        r = _region(bbox=(50, 30, 350, 530))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" not in result.failed_checks

    def test_book_at_lower_bound_passes(self) -> None:
        # ratio = 0.5 exactly
        r = _region(bbox=(0, 0, 100, 200))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" not in result.failed_checks

    def test_book_below_lower_bound_fails(self) -> None:
        # ratio ≈ 0.3 — too portrait for book lower bound (0.5)
        r = _region(bbox=(0, 0, 90, 300), page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" in result.failed_checks

    def test_book_above_upper_bound_fails(self) -> None:
        # ratio = 3.0 — too landscape for book upper bound (2.5)
        r = _region(bbox=(0, 0, 300, 100), page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" in result.failed_checks

    def test_newspaper_tall_column_passes(self) -> None:
        # ratio ≈ 0.33, newspaper bounds [0.3, 5.0]
        r = _region(bbox=(0, 0, 100, 300), page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "newspaper", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" not in result.failed_checks

    def test_newspaper_tall_column_fails_for_book(self) -> None:
        # Same region, but material_type="book" → ratio 0.33 fails book bounds [0.5, 2.5]
        r = _region(bbox=(0, 0, 100, 300), page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" in result.failed_checks

    def test_archival_document_landscape_passes(self) -> None:
        # ratio = 2.0, archival_document bounds [0.5, 3.0]
        r = _region(bbox=(0, 0, 400, 200), page_area_fraction=0.5)
        result = check_sanity(_response(pages=[r]), "archival_document", PROXY_W, PROXY_H, CONFIG)
        assert "aspect_ratio_plausible" not in result.failed_checks


# ---------------------------------------------------------------------------
# check_sanity — check 5: corner_ordering_valid
# ---------------------------------------------------------------------------


class TestSanityCornerOrdering:
    def test_convex_rectangle_passes(self) -> None:
        r = _region(corners=list(VALID_CORNERS))
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "corner_ordering_valid" not in result.failed_checks

    def test_self_intersecting_bowtie_fails(self) -> None:
        bowtie: list[tuple[float, float]] = [
            (50.0, 30.0),
            (350.0, 530.0),
            (350.0, 30.0),
            (50.0, 530.0),
        ]
        r = _region(corners=bowtie)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "corner_ordering_valid" in result.failed_checks

    def test_non_quadrilateral_geometry_type_skips_check(self) -> None:
        """corner_ordering_valid only applies to quadrilateral geometry_type."""
        r = PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            corners=None,
            bbox=(50, 30, 350, 530),
            confidence=0.9,
            page_area_fraction=0.5,
        )
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "corner_ordering_valid" not in result.failed_checks

    def test_slightly_non_convex_trapezoid_passes(self) -> None:
        """A slight trapezoid (non-rectangle but convex) should pass."""
        trapezoid: list[tuple[float, float]] = [
            (80.0, 30.0),
            (320.0, 30.0),
            (350.0, 530.0),
            (50.0, 530.0),
        ]
        r = _region(corners=trapezoid)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert "corner_ordering_valid" not in result.failed_checks


# ---------------------------------------------------------------------------
# check_sanity — check 6: regions_non_overlapping
# ---------------------------------------------------------------------------


class TestSanityRegionsNonOverlapping:
    def test_non_overlapping_two_pages_passes(self) -> None:
        resp = _two_page_response(
            b1=(10, 10, 390, 590),
            b2=(410, 10, 790, 590),
        )
        result = check_sanity(resp, "book", PROXY_W, PROXY_H, CONFIG)
        assert "regions_non_overlapping" not in result.failed_checks

    def test_heavily_overlapping_two_pages_fails(self) -> None:
        c: list[tuple[float, float]] = [(10.0, 10.0), (500.0, 10.0), (500.0, 590.0), (10.0, 590.0)]
        resp2 = GeometryResponse(
            page_count=2,
            pages=[
                PageRegion(
                    region_id="page_0",
                    geometry_type="quadrilateral",
                    corners=c,
                    bbox=(10, 10, 500, 590),
                    confidence=0.9,
                    page_area_fraction=0.46,
                ),
                PageRegion(
                    region_id="page_1",
                    geometry_type="quadrilateral",
                    corners=c,
                    bbox=(10, 10, 500, 590),
                    confidence=0.9,
                    page_area_fraction=0.46,
                ),
            ],
            split_required=True,
            split_x=300,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.9,
            tta_prediction_variance=0.05,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=100.0,
        )
        result = check_sanity(resp2, "book", PROXY_W, PROXY_H, CONFIG)
        assert "regions_non_overlapping" in result.failed_checks

    def test_partial_overlap_above_threshold_fails(self) -> None:
        # b1 = (0,0,200,400); b2 = (150,0,400,400)
        # intersection = 50×400 = 20000
        # union = 200×400 + 250×400 - 20000 = 80000 + 100000 - 20000 = 160000
        # IoU = 20000/160000 ≈ 0.125 > 0.1 → fails
        b1: tuple[int, int, int, int] = (0, 0, 200, 400)
        b2: tuple[int, int, int, int] = (150, 0, 400, 400)
        c1: list[tuple[float, float]] = [(0.0, 0.0), (200.0, 0.0), (200.0, 400.0), (0.0, 400.0)]
        c2: list[tuple[float, float]] = [(150.0, 0.0), (400.0, 0.0), (400.0, 400.0), (150.0, 400.0)]
        resp = GeometryResponse(
            page_count=2,
            pages=[
                PageRegion(
                    region_id="page_0",
                    geometry_type="quadrilateral",
                    corners=c1,
                    bbox=b1,
                    confidence=0.9,
                    page_area_fraction=0.25,
                ),
                PageRegion(
                    region_id="page_1",
                    geometry_type="quadrilateral",
                    corners=c2,
                    bbox=b2,
                    confidence=0.9,
                    page_area_fraction=0.31,
                ),
            ],
            split_required=True,
            split_x=175,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.9,
            tta_prediction_variance=0.05,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=100.0,
        )
        result = check_sanity(resp, "book", PROXY_W, PROXY_H, CONFIG)
        assert "regions_non_overlapping" in result.failed_checks

    def test_single_page_skips_overlap_check(self) -> None:
        """Overlap check only runs when page_count == 2."""
        result = check_sanity(_response(), "book", PROXY_W, PROXY_H, CONFIG)
        assert "regions_non_overlapping" not in result.failed_checks


# ---------------------------------------------------------------------------
# Combined and SanityCheckResult tests
# ---------------------------------------------------------------------------


class TestSanityCheckResultCombined:
    def test_all_checks_pass_returns_passed_true(self) -> None:
        result = check_sanity(_response(), "book", PROXY_W, PROXY_H, CONFIG)
        assert result.passed is True
        assert result.failed_checks == []

    def test_multiple_failures_reported(self) -> None:
        # area_fraction too low AND corner outside bounds
        bad_corners: list[tuple[float, float]] = [
            (-5.0, 30.0),  # x < 0 → within_bounds fails
            (350.0, 30.0),
            (350.0, 530.0),
            (-5.0, 530.0),
        ]
        r = _region(
            corners=bad_corners,
            bbox=(0, 30, 350, 530),
            page_area_fraction=0.05,  # area_fraction_plausible fails
        )
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        assert result.passed is False
        assert "within_bounds" in result.failed_checks
        assert "area_fraction_plausible" in result.failed_checks

    def test_as_dict_passed(self) -> None:
        result = check_sanity(_response(), "book", PROXY_W, PROXY_H, CONFIG)
        d = result.as_dict()
        assert d == {"passed": True, "failed_checks": []}

    def test_as_dict_failed(self) -> None:
        r = _region(page_area_fraction=0.01)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        d = result.as_dict()
        assert d["passed"] is False
        failed = d["failed_checks"]
        assert isinstance(failed, list)
        assert "area_fraction_plausible" in failed

    def test_as_dict_returns_copy_of_failed_checks(self) -> None:
        """Mutating as_dict() result must not affect the original."""
        r = _region(page_area_fraction=0.01)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        d = result.as_dict()
        failed = d["failed_checks"]
        assert isinstance(failed, list)
        failed.clear()
        assert result.failed_checks != []  # original unchanged

    def test_two_page_all_checks_pass(self) -> None:
        resp = _two_page_response(
            b1=(10, 10, 390, 590),
            b2=(410, 10, 790, 590),
        )
        result = check_sanity(resp, "book", PROXY_W, PROXY_H, CONFIG)
        assert result.passed is True
        assert result.failed_checks == []

    def test_sanity_check_names_constant(self) -> None:
        """The SANITY_CHECK_NAMES constant must list exactly the 6 canonical names."""
        from services.eep.app.gates.geometry_selection import SANITY_CHECK_NAMES

        expected = {
            "within_bounds",
            "non_degenerate",
            "area_fraction_plausible",
            "aspect_ratio_plausible",
            "corner_ordering_valid",
            "regions_non_overlapping",
        }
        assert set(SANITY_CHECK_NAMES) == expected
        assert len(SANITY_CHECK_NAMES) == 6
