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

import uuid
from typing import Literal

import pytest

from services.eep.app.gates.geometry_selection import (
    GeometryCandidate,
    GeometrySelectionResult,
    PreprocessingGateConfig,
    _DEFAULT_AREA_FRACTION_BOUNDS,
    _bbox_iou,
    _area_fraction_bounds_for_material,
    _compute_split_confidence,
    _corners_convex_and_valid,
    _quadrilateral_area,
    _select_candidate,
    apply_split_confidence_filter,
    apply_tta_variance_filter,
    build_geometry_gate_log_record,
    check_page_area_preference,
    check_sanity,
    check_structural_agreement,
    run_geometry_selection,
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
        # Use an explicit max so the check fires (default is 1.0, making max
        # effectively unreachable since fractions are <= 1.0).
        cfg = PreprocessingGateConfig(geometry_sanity_area_max_fraction=0.98)
        r = _region(page_area_fraction=0.99)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, cfg)
        assert "area_fraction_plausible" in result.failed_checks

    def test_custom_config_boundary(self) -> None:
        cfg = PreprocessingGateConfig(
            geometry_sanity_area_min_fraction=0.20,
            geometry_sanity_area_max_fraction=0.90,
        )
        r = _region(page_area_fraction=0.15)  # below custom min
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, cfg)
        assert "area_fraction_plausible" in result.failed_checks

    def test_newspaper_uses_lower_default_min_than_book(self) -> None:
        r = _region(page_area_fraction=0.12)
        book = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, CONFIG)
        newspaper = check_sanity(_response(pages=[r]), "newspaper", PROXY_W, PROXY_H, CONFIG)
        assert _area_fraction_bounds_for_material(CONFIG, "book") == (0.15, 1.0)
        assert _area_fraction_bounds_for_material(CONFIG, "newspaper") == (0.10, 1.0)
        assert "area_fraction_plausible" in book.failed_checks
        assert "area_fraction_plausible" not in newspaper.failed_checks

    def test_microfilm_default_area_threshold_matches_book(self) -> None:
        assert _area_fraction_bounds_for_material(CONFIG, "microfilm") == (0.15, 1.0)

    def test_legacy_scalar_area_override_preserves_book_threshold_behavior(self) -> None:
        cfg = PreprocessingGateConfig(geometry_sanity_area_min_fraction=0.20)
        r = _region(page_area_fraction=0.15)
        result = check_sanity(_response(pages=[r]), "book", PROXY_W, PROXY_H, cfg)
        assert cfg.area_fraction_bounds == dict(_DEFAULT_AREA_FRACTION_BOUNDS)
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


# ===========================================================================
# Packet 3.2 — split confidence filter, TTA variance filter, page area preference
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers for 3.2 tests
# ---------------------------------------------------------------------------


def _candidate(
    model: Literal["iep1a", "iep1b"] = "iep1a",
    geometry_confidence: float = 0.9,
    tta_structural_agreement_rate: float = 0.95,
    tta_prediction_variance: float = 0.05,
    split_required: bool = False,
    split_x: int | None = None,
    page_count: int = 1,
    page_area_fraction: float = 0.5,
) -> GeometryCandidate:
    """Build a GeometryCandidate with controllable split / variance / confidence."""
    pages: list[PageRegion] = []
    for i in range(page_count):
        pages.append(
            _region(
                region_id=f"page_{i}",
                page_area_fraction=page_area_fraction,
            )
        )
    resp = GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=geometry_confidence,
        tta_structural_agreement_rate=tta_structural_agreement_rate,
        tta_prediction_variance=tta_prediction_variance,
        tta_passes=5,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=50.0,
    )
    return GeometryCandidate(model=model, response=resp)


# ---------------------------------------------------------------------------
# _compute_split_confidence
# ---------------------------------------------------------------------------


class TestComputeSplitConfidence:
    def test_formula_min_of_confidence_and_tta_rate(self) -> None:
        # min(0.8, 0.9) = 0.8
        c = _candidate(geometry_confidence=0.8, tta_structural_agreement_rate=0.9)
        assert _compute_split_confidence(c.response) == pytest.approx(0.8)

    def test_formula_tta_rate_is_lower(self) -> None:
        # min(0.95, 0.7) = 0.7
        c = _candidate(geometry_confidence=0.95, tta_structural_agreement_rate=0.7)
        assert _compute_split_confidence(c.response) == pytest.approx(0.7)

    def test_formula_both_equal(self) -> None:
        c = _candidate(geometry_confidence=0.85, tta_structural_agreement_rate=0.85)
        assert _compute_split_confidence(c.response) == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# apply_split_confidence_filter
# ---------------------------------------------------------------------------


class TestApplySplitConfidenceFilter:
    def test_single_page_passes_through_regardless_of_confidence(self) -> None:
        """split_required=False → filter never removes the candidate."""
        c = _candidate("iep1a", geometry_confidence=0.1, split_required=False)
        result = apply_split_confidence_filter([c], CONFIG)
        assert len(result) == 1
        assert result[0].model == "iep1a"

    def test_split_high_confidence_passes(self) -> None:
        # min(0.9, 0.95) = 0.9 >= threshold 0.75
        c = _candidate(
            "iep1a",
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.95,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([c], CONFIG)
        assert len(result) == 1

    def test_split_low_confidence_removed(self) -> None:
        # min(0.5, 0.95) = 0.5 < threshold 0.75
        c = _candidate(
            "iep1a",
            geometry_confidence=0.5,
            tta_structural_agreement_rate=0.95,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([c], CONFIG)
        assert len(result) == 0

    def test_split_low_tta_rate_removed(self) -> None:
        # min(0.95, 0.6) = 0.6 < threshold 0.75
        c = _candidate(
            "iep1b",
            geometry_confidence=0.95,
            tta_structural_agreement_rate=0.6,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([c], CONFIG)
        assert len(result) == 0

    def test_split_at_threshold_passes(self) -> None:
        # min(0.75, 0.9) = 0.75 == threshold 0.75 → passes (>=)
        c = _candidate(
            "iep1a",
            geometry_confidence=0.75,
            tta_structural_agreement_rate=0.9,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([c], CONFIG)
        assert len(result) == 1

    def test_both_candidates_one_passes_one_fails(self) -> None:
        high = _candidate(
            "iep1a",
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.95,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        low = _candidate(
            "iep1b",
            geometry_confidence=0.4,
            tta_structural_agreement_rate=0.95,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([high, low], CONFIG)
        assert len(result) == 1
        assert result[0].model == "iep1a"

    def test_both_candidates_fail_returns_empty(self) -> None:
        """Empty list signals caller (3.3) to route to pending_human_correction."""
        low_a = _candidate(
            "iep1a",
            geometry_confidence=0.4,
            tta_structural_agreement_rate=0.9,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        low_b = _candidate(
            "iep1b",
            geometry_confidence=0.5,
            tta_structural_agreement_rate=0.6,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([low_a, low_b], CONFIG)
        assert result == []

    def test_mixed_split_and_single_page_candidates(self) -> None:
        """Single-page candidate bypasses filter; split candidate is evaluated."""
        single = _candidate("iep1a", geometry_confidence=0.1, split_required=False)
        split_low = _candidate(
            "iep1b",
            geometry_confidence=0.3,
            tta_structural_agreement_rate=0.9,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([single, split_low], CONFIG)
        assert len(result) == 1
        assert result[0].model == "iep1a"

    def test_custom_threshold(self) -> None:
        cfg = PreprocessingGateConfig(split_confidence_threshold=0.60)
        # min(0.65, 0.9) = 0.65 >= 0.60 → passes
        c = _candidate(
            "iep1a",
            geometry_confidence=0.65,
            tta_structural_agreement_rate=0.9,
            split_required=True,
            split_x=400,
            page_count=2,
        )
        result = apply_split_confidence_filter([c], cfg)
        assert len(result) == 1

    def test_empty_candidates_returns_empty(self) -> None:
        assert apply_split_confidence_filter([], CONFIG) == []


# ---------------------------------------------------------------------------
# apply_tta_variance_filter
# ---------------------------------------------------------------------------


class TestApplyTtaVarianceFilter:
    def test_low_variance_passes(self) -> None:
        c = _candidate("iep1a", tta_prediction_variance=0.05)
        result = apply_tta_variance_filter([c], CONFIG)
        assert len(result) == 1

    def test_variance_at_ceiling_passes(self) -> None:
        # <= ceiling → passes (spec: remove if > ceiling)
        c = _candidate("iep1a", tta_prediction_variance=0.15)
        result = apply_tta_variance_filter([c], CONFIG)
        assert len(result) == 1

    def test_variance_above_ceiling_removed(self) -> None:
        c = _candidate("iep1a", tta_prediction_variance=0.20)
        result = apply_tta_variance_filter([c], CONFIG)
        assert len(result) == 0

    def test_one_passes_one_fails(self) -> None:
        stable = _candidate("iep1a", tta_prediction_variance=0.10)
        unstable = _candidate("iep1b", tta_prediction_variance=0.25)
        result = apply_tta_variance_filter([stable, unstable], CONFIG)
        assert len(result) == 1
        assert result[0].model == "iep1a"

    def test_both_fail_returns_empty(self) -> None:
        """Empty list signals caller (3.3) to route to pending_human_correction."""
        a = _candidate("iep1a", tta_prediction_variance=0.20)
        b = _candidate("iep1b", tta_prediction_variance=0.30)
        result = apply_tta_variance_filter([a, b], CONFIG)
        assert result == []

    def test_both_pass(self) -> None:
        a = _candidate("iep1a", tta_prediction_variance=0.05)
        b = _candidate("iep1b", tta_prediction_variance=0.10)
        result = apply_tta_variance_filter([a, b], CONFIG)
        assert len(result) == 2

    def test_custom_ceiling(self) -> None:
        cfg = PreprocessingGateConfig(tta_variance_ceiling=0.25)
        c = _candidate("iep1a", tta_prediction_variance=0.20)
        result = apply_tta_variance_filter([c], cfg)
        assert len(result) == 1

    def test_empty_candidates_returns_empty(self) -> None:
        assert apply_tta_variance_filter([], CONFIG) == []


# ---------------------------------------------------------------------------
# check_page_area_preference
# ---------------------------------------------------------------------------


class TestCheckPageAreaPreference:
    def test_both_large_page_area_no_preference(self) -> None:
        a = _candidate("iep1a", page_area_fraction=0.5)
        b = _candidate("iep1b", page_area_fraction=0.6)
        assert check_page_area_preference([a, b], CONFIG) is False

    def test_iep1a_small_page_triggers_preference(self) -> None:
        # page_area_fraction=0.2 < threshold 0.30 → prefer IEP1B
        a = _candidate("iep1a", page_area_fraction=0.2)
        b = _candidate("iep1b", page_area_fraction=0.5)
        assert check_page_area_preference([a, b], CONFIG) is True

    def test_iep1b_small_page_triggers_preference(self) -> None:
        a = _candidate("iep1a", page_area_fraction=0.5)
        b = _candidate("iep1b", page_area_fraction=0.15)
        assert check_page_area_preference([a, b], CONFIG) is True

    def test_at_threshold_no_preference(self) -> None:
        # page_area_fraction == threshold (0.30) → not < threshold → no preference
        a = _candidate("iep1a", page_area_fraction=0.30)
        b = _candidate("iep1b", page_area_fraction=0.30)
        assert check_page_area_preference([a, b], CONFIG) is False

    def test_below_threshold_triggers_preference(self) -> None:
        a = _candidate("iep1a", page_area_fraction=0.29)
        b = _candidate("iep1b", page_area_fraction=0.5)
        assert check_page_area_preference([a, b], CONFIG) is True

    def test_single_candidate_no_preference(self) -> None:
        """Preference is only meaningful as a tiebreaker with two candidates."""
        c = _candidate("iep1b", page_area_fraction=0.1)
        assert check_page_area_preference([c], CONFIG) is False

    def test_empty_candidates_no_preference(self) -> None:
        assert check_page_area_preference([], CONFIG) is False

    def test_custom_threshold(self) -> None:
        cfg = PreprocessingGateConfig(page_area_preference_threshold=0.40)
        a = _candidate("iep1a", page_area_fraction=0.35)  # below custom 0.40 → True
        b = _candidate("iep1b", page_area_fraction=0.5)
        assert check_page_area_preference([a, b], cfg) is True

    def test_two_page_split_any_region_below_threshold(self) -> None:
        """For two-page spreads, any single region below threshold triggers preference."""
        # Build a two-page candidate where page_0 has fraction 0.48 and page_1 has 0.20
        pages: list[PageRegion] = [
            _region(region_id="page_0", page_area_fraction=0.48),
            _region(region_id="page_1", page_area_fraction=0.20),
        ]
        resp = GeometryResponse(
            page_count=2,
            pages=pages,
            split_required=True,
            split_x=400,
            geometry_confidence=0.9,
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.05,
            tta_passes=5,
            uncertainty_flags=[],
            warnings=[],
            processing_time_ms=50.0,
        )
        a = GeometryCandidate(model="iep1a", response=resp)
        b = _candidate("iep1b", page_area_fraction=0.5)
        assert check_page_area_preference([a, b], CONFIG) is True


# ===========================================================================
# Packet 3.3 — GeometrySelectionResult, run_geometry_selection, gate log record
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers for 3.3 tests
# ---------------------------------------------------------------------------


def _valid_iep1a(
    geometry_confidence: float = 0.90,
    tta_prediction_variance: float = 0.05,
    split_required: bool = False,
    page_area_fraction: float = 0.5,
) -> GeometryResponse:
    """A GeometryResponse that passes all sanity checks (default config)."""
    return _response(
        geometry_confidence=geometry_confidence,
        tta_prediction_variance=tta_prediction_variance,
        tta_structural_agreement_rate=0.95,
        split_required=split_required,
        split_x=400 if split_required else None,
        page_count=2 if split_required else 1,
        pages=(
            [
                _region(region_id="page_0", page_area_fraction=page_area_fraction),
                _region(region_id="page_1", page_area_fraction=page_area_fraction),
            ]
            if split_required
            else [_region(page_area_fraction=page_area_fraction)]
        ),
    )


def _valid_iep1b(
    geometry_confidence: float = 0.88,
    tta_prediction_variance: float = 0.05,
    split_required: bool = False,
    page_area_fraction: float = 0.5,
) -> GeometryResponse:
    """A GeometryResponse that passes all sanity checks (default config)."""
    return _response(
        geometry_confidence=geometry_confidence,
        tta_prediction_variance=tta_prediction_variance,
        tta_structural_agreement_rate=0.95,
        split_required=split_required,
        split_x=400 if split_required else None,
        page_count=2 if split_required else 1,
        pages=(
            [
                _region(region_id="page_0", page_area_fraction=page_area_fraction),
                _region(region_id="page_1", page_area_fraction=page_area_fraction),
            ]
            if split_required
            else [_region(page_area_fraction=page_area_fraction)]
        ),
    )


# ---------------------------------------------------------------------------
# _select_candidate
# ---------------------------------------------------------------------------


class TestSelectCandidate:
    def test_sole_survivor_returns_reason_sole_survivor(self) -> None:
        c = _candidate("iep1a", geometry_confidence=0.8)
        winner, reason = _select_candidate([c], page_area_preference=False)
        assert winner.model == "iep1a"
        assert reason == "sole_survivor"

    def test_page_area_preference_picks_iep1b(self) -> None:
        a = _candidate("iep1a", geometry_confidence=0.95)
        b = _candidate("iep1b", geometry_confidence=0.80)
        winner, reason = _select_candidate([a, b], page_area_preference=True)
        assert winner.model == "iep1b"
        assert reason == "page_area_preference"

    def test_higher_confidence_wins(self) -> None:
        a = _candidate("iep1a", geometry_confidence=0.75)
        b = _candidate("iep1b", geometry_confidence=0.92)
        winner, reason = _select_candidate([a, b], page_area_preference=False)
        assert winner.model == "iep1b"
        assert reason == "higher_confidence"

    def test_tie_falls_back_to_iep1a(self) -> None:
        a = _candidate("iep1a", geometry_confidence=0.85)
        b = _candidate("iep1b", geometry_confidence=0.85)
        winner, reason = _select_candidate([a, b], page_area_preference=False)
        assert winner.model == "iep1a"
        assert reason == "default_iep1a"

    def test_page_area_preference_no_iep1b_falls_to_confidence(self) -> None:
        """If page_area_preference is True but only iep1a present, use confidence."""
        a = _candidate("iep1a", geometry_confidence=0.9)
        # Only one candidate — sole_survivor takes priority before preference check.
        winner, reason = _select_candidate([a], page_area_preference=True)
        assert winner.model == "iep1a"
        assert reason == "sole_survivor"


# ---------------------------------------------------------------------------
# run_geometry_selection — high trust path
# ---------------------------------------------------------------------------


class TestRunGeometrySelectionHighTrust:
    def test_both_models_agree_and_pass_filters_yields_accepted(self) -> None:
        a = _valid_iep1a()
        b = _valid_iep1b()
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "accepted"
        assert result.geometry_trust == "high"
        assert result.structural_agreement is True
        assert result.selected is not None
        assert result.review_reason is None

    def test_high_trust_selects_higher_confidence(self) -> None:
        a = _valid_iep1a(geometry_confidence=0.95)
        b = _valid_iep1b(geometry_confidence=0.80)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.selected is not None
        assert result.selected.model == "iep1a"
        assert result.selection_reason == "higher_confidence"

    def test_sanity_results_populated_for_both_models(self) -> None:
        a = _valid_iep1a()
        b = _valid_iep1b()
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert "iep1a" in result.sanity_results
        assert "iep1b" in result.sanity_results
        assert result.sanity_results["iep1a"]["passed"] is True
        assert result.sanity_results["iep1b"]["passed"] is True

    def test_tta_variance_per_model_populated(self) -> None:
        a = _valid_iep1a(tta_prediction_variance=0.06)
        b = _valid_iep1b(tta_prediction_variance=0.09)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.tta_variance_per_model == pytest.approx({"iep1a": 0.06, "iep1b": 0.09})

    def test_split_confidence_none_when_no_split(self) -> None:
        a = _valid_iep1a(split_required=False)
        b = _valid_iep1b(split_required=False)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.split_confidence_per_model is None

    def test_split_confidence_populated_when_split_required(self) -> None:
        a = _valid_iep1a(geometry_confidence=0.90, split_required=True)
        b = _valid_iep1b(geometry_confidence=0.88, split_required=True)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.split_confidence_per_model is not None
        assert "iep1a" in result.split_confidence_per_model
        assert "iep1b" in result.split_confidence_per_model

    def test_page_area_preference_not_triggered_for_normal_pages(self) -> None:
        a = _valid_iep1a(page_area_fraction=0.5)
        b = _valid_iep1b(page_area_fraction=0.5)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.page_area_preference_triggered is False

    def test_newspaper_accepts_strong_iep1a_when_iep1b_mildly_fails_area(self) -> None:
        a = _valid_iep1a(
            geometry_confidence=0.94,
            tta_prediction_variance=0.02,
            page_area_fraction=0.50,
        )
        b = _valid_iep1b(
            geometry_confidence=0.88,
            tta_prediction_variance=0.03,
            page_area_fraction=0.09,
        )
        result = run_geometry_selection(a, b, "newspaper", PROXY_W, PROXY_H)
        assert result.structural_agreement is True
        assert result.route_decision == "accepted"
        assert result.geometry_trust == "high"
        assert result.selected is not None
        assert result.selected.model == "iep1a"
        assert result.selection_reason == "newspaper_iep1a_mild_iep1b_area_fallback"
        assert result.sanity_results["iep1b"]["failed_checks"] == ["area_fraction_plausible"]


# ---------------------------------------------------------------------------
# run_geometry_selection — low trust path
# ---------------------------------------------------------------------------


class TestRunGeometrySelectionLowTrust:
    def test_structural_disagreement_yields_low_trust_rectification(self) -> None:
        # iep1a says 1 page, iep1b says 2 pages → disagreement
        a = _valid_iep1a()  # page_count=1, split_required=False
        b = _response(
            page_count=2,
            pages=[
                _region(region_id="page_0", page_area_fraction=0.48),
                _region(region_id="page_1", page_area_fraction=0.48),
            ],
            split_required=True,
            split_x=400,
            geometry_confidence=0.88,
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.05,
        )
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.structural_agreement is False
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"
        assert result.selected is not None  # a winner is still selected

    def test_one_model_fails_sanity_yields_low_trust(self) -> None:
        a = _valid_iep1a()
        # b has area_fraction_plausible failure (0.01 < 0.15 min)
        bad_region = _region(page_area_fraction=0.01)
        b = _response(pages=[bad_region])
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"
        assert result.selected is not None
        assert result.selected.model == "iep1a"
        assert result.selection_reason == "sole_survivor"

    def test_one_model_fails_tta_variance_yields_low_trust(self) -> None:
        a = _valid_iep1a(tta_prediction_variance=0.05)
        b = _valid_iep1b(tta_prediction_variance=0.99)  # > ceiling 0.15
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"
        assert result.selected is not None
        assert result.selected.model == "iep1a"

    def test_single_model_always_low_trust(self) -> None:
        a = _valid_iep1a()
        result = run_geometry_selection(a, None, "book", PROXY_W, PROXY_H)
        assert result.geometry_trust == "low"
        assert result.route_decision == "rectification"
        assert result.structural_agreement is None
        assert result.selected is not None
        assert result.selected.model == "iep1a"

    def test_page_area_preference_triggered_still_rectification_when_low_trust(self) -> None:
        # One model dropped → low trust → rectification even if preference fires
        a = _valid_iep1a(page_area_fraction=0.2)  # small page → preference fires
        b_bad = _response(pages=[_region(page_area_fraction=0.01)])  # fails sanity
        result = run_geometry_selection(a, b_bad, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "rectification"
        assert result.geometry_trust == "low"


# ---------------------------------------------------------------------------
# run_geometry_selection — pending_human_correction path
# ---------------------------------------------------------------------------


    def test_book_thresholds_unchanged_when_iep1b_mildly_fails_area(self) -> None:
        a = _valid_iep1a(
            geometry_confidence=0.94,
            tta_prediction_variance=0.02,
            page_area_fraction=0.50,
        )
        b = _valid_iep1b(
            geometry_confidence=0.88,
            tta_prediction_variance=0.03,
            page_area_fraction=0.12,
        )
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.structural_agreement is True
        assert result.route_decision == "rectification"
        assert result.geometry_trust == "low"
        assert result.sanity_results["iep1b"]["failed_checks"] == ["area_fraction_plausible"]

    def test_newspaper_severe_iep1b_area_failure_does_not_auto_accept(self) -> None:
        a = _valid_iep1a(
            geometry_confidence=0.94,
            tta_prediction_variance=0.02,
            page_area_fraction=0.50,
        )
        b = _valid_iep1b(
            geometry_confidence=0.88,
            tta_prediction_variance=0.03,
            page_area_fraction=0.03,
        )
        result = run_geometry_selection(a, b, "newspaper", PROXY_W, PROXY_H)
        assert result.structural_agreement is True
        assert result.route_decision == "rectification"
        assert result.geometry_trust == "low"
        assert result.selected is not None
        assert result.selected.model == "iep1a"


class TestRunGeometrySelectionPendingHuman:
    def test_both_fail_sanity_routes_to_human_sanity_reason(self) -> None:
        bad_a = _response(pages=[_region(page_area_fraction=0.01)])
        bad_b = _response(pages=[_region(page_area_fraction=0.01)])
        result = run_geometry_selection(bad_a, bad_b, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_sanity_failed"
        assert result.selected is None
        assert result.geometry_trust is None
        assert result.selection_reason is None

    def test_newspaper_both_models_severe_area_failure_routes_to_human(self) -> None:
        bad_a = _response(pages=[_region(page_area_fraction=0.03)])
        bad_b = _response(pages=[_region(page_area_fraction=0.03)])
        result = run_geometry_selection(bad_a, bad_b, "newspaper", PROXY_W, PROXY_H)
        assert result.structural_agreement is True
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_sanity_failed"
        assert result.selected is None

    def test_both_fail_split_confidence_routes_to_human_split_reason(self) -> None:
        # Both pass sanity but fail split confidence filter.
        # Use non-overlapping bboxes so regions_non_overlapping passes.
        split_pages = [
            _region(region_id="page_0", bbox=(10, 10, 390, 590), page_area_fraction=0.48),
            _region(region_id="page_1", bbox=(410, 10, 790, 590), page_area_fraction=0.48),
        ]
        a = _response(
            split_required=True,
            split_x=400,
            page_count=2,
            pages=split_pages,
            geometry_confidence=0.3,  # min(0.3, 0.95) = 0.3 < 0.75 → fails filter
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.05,
        )
        b = _response(
            split_required=True,
            split_x=400,
            page_count=2,
            pages=list(split_pages),
            geometry_confidence=0.4,  # min(0.4, 0.95) = 0.4 < 0.75 → fails filter
            tta_structural_agreement_rate=0.95,
            tta_prediction_variance=0.05,
        )
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "split_confidence_low"

    def test_both_fail_tta_variance_routes_to_human_tta_reason(self) -> None:
        a = _valid_iep1a(tta_prediction_variance=0.99)  # fails TTA filter
        b = _valid_iep1b(tta_prediction_variance=0.88)  # fails TTA filter
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "tta_variance_high"

    def test_no_models_provided_routes_to_human_fallback_reason(self) -> None:
        result = run_geometry_selection(None, None, "book", PROXY_W, PROXY_H)
        assert result.route_decision == "pending_human_correction"
        assert result.review_reason == "geometry_selection_failed"
        assert result.selected is None
        assert result.geometry_trust is None

    def test_pending_human_sanity_results_still_populated(self) -> None:
        bad_a = _response(pages=[_region(page_area_fraction=0.01)])
        bad_b = _response(pages=[_region(page_area_fraction=0.01)])
        result = run_geometry_selection(bad_a, bad_b, "book", PROXY_W, PROXY_H)
        assert "iep1a" in result.sanity_results
        assert "iep1b" in result.sanity_results
        assert result.sanity_results["iep1a"]["passed"] is False
        assert result.sanity_results["iep1b"]["passed"] is False


# ---------------------------------------------------------------------------
# build_geometry_gate_log_record
# ---------------------------------------------------------------------------


class TestBuildGeometryGateLogRecord:
    def _accepted_result(
        self,
    ) -> tuple[GeometrySelectionResult, GeometryResponse, GeometryResponse]:
        a = _valid_iep1a()
        b = _valid_iep1b()
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        return result, a, b

    def test_required_keys_present(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 120.0
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

    def test_gate_id_is_valid_uuid(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 50.0
        )
        gate_id = record["gate_id"]
        assert isinstance(gate_id, str)
        # Validate UUID4 format.
        parsed = uuid.UUID(str(gate_id))
        assert parsed.version == 4

    def test_gate_id_unique_per_call(self) -> None:
        result, a, b = self._accepted_result()
        r1 = build_geometry_gate_log_record(result, "job-1", 0, "geometry_selection", a, b, 50.0)
        r2 = build_geometry_gate_log_record(result, "job-1", 0, "geometry_selection", a, b, 50.0)
        assert r1["gate_id"] != r2["gate_id"]

    def test_accepted_route_decision_in_record(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 80.0
        )
        assert record["route_decision"] == "accepted"
        assert record["review_reason"] is None

    def test_pending_human_route_decision_in_record(self) -> None:
        bad_a = _response(pages=[_region(page_area_fraction=0.01)])
        bad_b = _response(pages=[_region(page_area_fraction=0.01)])
        result = run_geometry_selection(bad_a, bad_b, "book", PROXY_W, PROXY_H)
        record = build_geometry_gate_log_record(
            result, "job-2", 1, "geometry_selection", bad_a, bad_b, 200.0
        )
        assert record["route_decision"] == "pending_human_correction"
        assert record["review_reason"] == "geometry_sanity_failed"
        assert record["selected_model"] is None

    def test_artifact_validation_score_always_none(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 50.0
        )
        assert record["artifact_validation_score"] is None

    def test_processing_time_ms_is_int(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 99.7
        )
        assert record["processing_time_ms"] == 99

    def test_none_model_produces_none_geometry(self) -> None:
        a = _valid_iep1a()
        result = run_geometry_selection(a, None, "book", PROXY_W, PROXY_H)
        record = build_geometry_gate_log_record(
            result, "job-3", 0, "geometry_selection", a, None, 60.0
        )
        assert record["iep1b_geometry"] is None
        assert isinstance(record["iep1a_geometry"], dict)

    def test_post_rectification_gate_type(self) -> None:
        result, a, b = self._accepted_result()
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection_post_rectification", a, b, 50.0
        )
        assert record["gate_type"] == "geometry_selection_post_rectification"

    def test_selected_model_name_matches_winner(self) -> None:
        a = _valid_iep1a(geometry_confidence=0.95)
        b = _valid_iep1b(geometry_confidence=0.80)
        result = run_geometry_selection(a, b, "book", PROXY_W, PROXY_H)
        record = build_geometry_gate_log_record(
            result, "job-1", 0, "geometry_selection", a, b, 50.0
        )
        assert record["selected_model"] == "iep1a"
