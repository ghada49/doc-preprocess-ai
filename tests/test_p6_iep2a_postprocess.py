"""
tests/test_p6_iep2a_postprocess.py
-------------------------------------
Packet 6.2 — IEP2A postprocessing unit tests.

Tests every internal function and the public postprocess_regions entry point.

Covers:
  _iou:
    - non-overlapping boxes → 0.0
    - identical boxes → 1.0
    - partial overlap → value in (0, 1)
    - touching (adjacent) boxes → 0.0

  _nms_per_type:
    - two non-overlapping same-type regions → both kept
    - two overlapping same-type regions → higher-confidence kept
    - two regions of different types → both kept regardless of overlap
    - three regions: two overlapping + one not → two kept

  _recalibrate:
    - interior region (not small, not edge) → confidence unchanged
    - small region (<1% page area) → confidence × 0.8
    - edge region (left) → confidence × 0.9
    - edge region (right) → confidence × 0.9
    - edge region (top) → confidence × 0.9
    - edge region (bottom) → confidence × 0.9
    - small + edge → confidence × 0.72 (compound)
    - clamped to 0.0 when result < 0
    - clamped to 1.0 never exceeded

  _dbscan_1d:
    - empty input → []
    - single point → [0]
    - all points within eps → single cluster (all label 0)
    - two separated groups → two distinct labels
    - label count == input length

  _infer_column_structure:
    - no text_block regions → None
    - one text_block → ColumnStructure(column_count=1, column_boundaries=[])
    - two text_blocks far apart → column_count=2, one boundary in (0,1)
    - two text_blocks close together → column_count=1 (merged cluster)
    - column_boundaries length == column_count − 1 (ColumnStructure invariant)
    - all boundaries in [0, 1]
    - boundaries sorted ascending

  _reassign_ids:
    - IDs are r1, r2, … sequential
    - regions sorted by (y_min, x_min)
    - original regions are not mutated

  postprocess_regions (integration):
    - returns a tuple of (list[Region], ColumnStructure | None)
    - all returned IDs are unique and match ^r\\d+$
    - all returned region types are canonical RegionType values
    - column_structure is non-None when text_blocks are present
    - NMS removes a duplicate overlapping same-type region
    - confidence after recalibration <= raw confidence
    - empty input → ([], None)
"""

from __future__ import annotations

import re

from services.iep2a.app.postprocess import (
    _dbscan_1d,
    _infer_column_structure,
    _iou,
    _nms_per_type,
    _reassign_ids,
    _recalibrate,
    postprocess_regions,
)
from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

_REGION_ID_RE = re.compile(r"^r\d+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_region(
    rid: str,
    rtype: RegionType,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    confidence: float = 0.9,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        confidence=confidence,
    )


def _bb(x_min: float, y_min: float, x_max: float, y_max: float) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


# ---------------------------------------------------------------------------
# _iou
# ---------------------------------------------------------------------------


class TestIou:
    def test_non_overlapping(self) -> None:
        assert _iou(_bb(0, 0, 10, 10), _bb(20, 20, 30, 30)) == 0.0

    def test_identical(self) -> None:
        assert abs(_iou(_bb(0, 0, 10, 10), _bb(0, 0, 10, 10)) - 1.0) < 1e-9

    def test_partial_overlap(self) -> None:
        # Two 10×10 boxes shifted by (5,5): 5×5 intersection, 175 union
        val = _iou(_bb(0, 0, 10, 10), _bb(5, 5, 15, 15))
        assert 0.0 < val < 1.0
        assert abs(val - 25.0 / 175.0) < 1e-9

    def test_adjacent_touching(self) -> None:
        # Boxes share an edge, zero-area intersection
        assert _iou(_bb(0, 0, 10, 10), _bb(10, 0, 20, 10)) == 0.0

    def test_one_inside_other(self) -> None:
        # Inner box completely inside outer box
        val = _iou(_bb(0, 0, 10, 10), _bb(2, 2, 8, 8))
        area_inner = 6 * 6
        area_outer = 10 * 10
        union = area_outer  # inner is subset
        assert abs(val - area_inner / union) < 1e-9


# ---------------------------------------------------------------------------
# _nms_per_type
# ---------------------------------------------------------------------------


class TestNmsPerType:
    def test_non_overlapping_same_type_both_kept(self) -> None:
        r1 = _make_region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _make_region("r2", RegionType.text_block, 200, 200, 300, 300)
        result = _nms_per_type([r1, r2])
        assert len(result) == 2

    def test_overlapping_same_type_higher_conf_kept(self) -> None:
        # Identical bounding boxes, IoU = 1.0 > 0.5 → only higher-conf kept
        hi = _make_region("r1", RegionType.title, 0, 0, 100, 100, confidence=0.9)
        lo = _make_region("r2", RegionType.title, 0, 0, 100, 100, confidence=0.6)
        result = _nms_per_type([lo, hi])  # pass lower-conf first
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_different_types_both_kept_even_if_overlapping(self) -> None:
        # Same bbox, different types → different NMS groups → both kept
        r1 = _make_region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _make_region("r2", RegionType.image, 0, 0, 100, 100)
        result = _nms_per_type([r1, r2])
        assert len(result) == 2

    def test_three_regions_two_overlapping(self) -> None:
        # r1 and r2 overlap (same bbox), r3 does not overlap r1
        r1 = _make_region("r1", RegionType.caption, 0, 0, 100, 100, confidence=0.9)
        r2 = _make_region("r2", RegionType.caption, 0, 0, 100, 100, confidence=0.7)
        r3 = _make_region("r3", RegionType.caption, 500, 500, 600, 600, confidence=0.8)
        result = _nms_per_type([r1, r2, r3])
        assert len(result) == 2
        confidences = {r.confidence for r in result}
        assert 0.9 in confidences
        assert 0.8 in confidences

    def test_empty_input(self) -> None:
        assert _nms_per_type([]) == []


# ---------------------------------------------------------------------------
# _recalibrate
# ---------------------------------------------------------------------------

_PAGE_W = 1000.0
_PAGE_H = 1000.0
# edge margin = 0.05 × 1000 = 50


class TestRecalibrate:
    def test_interior_region_unchanged(self) -> None:
        # Completely interior: all sides well inside edge margin
        r = _make_region("r1", RegionType.text_block, 100, 100, 800, 800, confidence=0.8)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.8) < 1e-9

    def test_small_region_penalty(self) -> None:
        # Area = 50×50 = 2500; page area = 1e6; fraction = 0.25% < 1% → small
        r = _make_region("r1", RegionType.caption, 100, 100, 150, 150, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.8) < 1e-9

    def test_edge_left(self) -> None:
        # x_min=20 <= 50 → edge
        r = _make_region("r1", RegionType.text_block, 20, 100, 400, 800, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.9) < 1e-9

    def test_edge_right(self) -> None:
        # x_max=990 >= 950 → edge
        r = _make_region("r1", RegionType.text_block, 200, 100, 990, 800, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.9) < 1e-9

    def test_edge_top(self) -> None:
        # y_min=10 <= 50 → edge
        r = _make_region("r1", RegionType.title, 100, 10, 800, 200, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.9) < 1e-9

    def test_edge_bottom(self) -> None:
        # y_max=980 >= 950 → edge
        r = _make_region("r1", RegionType.caption, 100, 700, 800, 980, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.9) < 1e-9

    def test_small_and_edge_compound(self) -> None:
        # Small (area < 1%) AND edge → ×0.8 × 0.9 = ×0.72
        r = _make_region("r1", RegionType.caption, 10, 10, 50, 50, confidence=1.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert abs(result[0].confidence - 0.72) < 1e-9

    def test_confidence_clamped_to_zero(self) -> None:
        r = _make_region("r1", RegionType.caption, 10, 10, 50, 50, confidence=0.0)
        result = _recalibrate([r], _PAGE_W, _PAGE_H)
        assert result[0].confidence == 0.0

    def test_original_region_not_mutated(self) -> None:
        r = _make_region("r1", RegionType.title, 10, 100, 800, 800, confidence=0.9)
        original_conf = r.confidence
        _recalibrate([r], _PAGE_W, _PAGE_H)
        assert r.confidence == original_conf

    def test_empty_input(self) -> None:
        assert _recalibrate([], _PAGE_W, _PAGE_H) == []


# ---------------------------------------------------------------------------
# _dbscan_1d
# ---------------------------------------------------------------------------


class TestDbscan1d:
    def test_empty_input(self) -> None:
        assert _dbscan_1d([], eps=10.0) == []

    def test_single_point(self) -> None:
        assert _dbscan_1d([5.0], eps=10.0) == [0]

    def test_all_within_eps_single_cluster(self) -> None:
        labels = _dbscan_1d([0.0, 5.0, 9.0], eps=10.0)
        assert len(set(labels)) == 1  # all same cluster

    def test_two_separated_clusters(self) -> None:
        labels = _dbscan_1d([10.0, 12.0, 100.0, 102.0], eps=5.0)
        assert len(labels) == 4
        # First two in one cluster, last two in another
        assert labels[0] == labels[1]
        assert labels[2] == labels[3]
        assert labels[0] != labels[2]

    def test_label_count_equals_input_length(self) -> None:
        values = [1.0, 50.0, 100.0, 150.0, 200.0]
        labels = _dbscan_1d(values, eps=5.0)
        assert len(labels) == len(values)

    def test_three_clusters(self) -> None:
        labels = _dbscan_1d([0.0, 100.0, 200.0], eps=10.0)
        assert len(set(labels)) == 3


# ---------------------------------------------------------------------------
# _infer_column_structure
# ---------------------------------------------------------------------------


class TestInferColumnStructure:
    def test_no_text_blocks_returns_none(self) -> None:
        regions = [
            _make_region("r1", RegionType.title, 0, 0, 100, 50),
            _make_region("r2", RegionType.image, 0, 100, 100, 300),
        ]
        assert _infer_column_structure(regions, 1000.0, 0.08) is None

    def test_single_text_block_single_column(self) -> None:
        r = _make_region("r1", RegionType.text_block, 50, 50, 450, 600)
        cs = _infer_column_structure([r], 1000.0, 0.08)
        assert cs is not None
        assert cs.column_count == 1
        assert cs.column_boundaries == []

    def test_two_text_blocks_far_apart_two_columns(self) -> None:
        # centroids: 250 and 750; eps = 0.08×1000 = 80; distance = 500 >> 80
        r1 = _make_region("r1", RegionType.text_block, 50, 100, 450, 600)
        r2 = _make_region("r2", RegionType.text_block, 550, 100, 950, 600)
        cs = _infer_column_structure([r1, r2], 1000.0, 0.08)
        assert cs is not None
        assert cs.column_count == 2
        assert len(cs.column_boundaries) == 1
        assert 0.0 < cs.column_boundaries[0] < 1.0

    def test_two_text_blocks_close_together_one_column(self) -> None:
        # centroids: 250 and 260; eps = 0.08×1000 = 80; distance = 10 < 80
        r1 = _make_region("r1", RegionType.text_block, 200, 100, 300, 600)
        r2 = _make_region("r2", RegionType.text_block, 210, 650, 310, 900)
        cs = _infer_column_structure([r1, r2], 1000.0, 0.08)
        assert cs is not None
        assert cs.column_count == 1
        assert cs.column_boundaries == []

    def test_column_boundaries_length_invariant(self) -> None:
        # For any column count, len(boundaries) == column_count − 1
        r1 = _make_region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _make_region("r2", RegionType.text_block, 400, 0, 500, 100)
        r3 = _make_region("r3", RegionType.text_block, 800, 0, 900, 100)
        cs = _infer_column_structure([r1, r2, r3], 1000.0, 0.08)
        assert cs is not None
        assert len(cs.column_boundaries) == cs.column_count - 1

    def test_column_boundaries_in_0_1(self) -> None:
        r1 = _make_region("r1", RegionType.text_block, 50, 0, 450, 100)
        r2 = _make_region("r2", RegionType.text_block, 550, 0, 950, 100)
        cs = _infer_column_structure([r1, r2], 1000.0, 0.08)
        assert cs is not None
        for b in cs.column_boundaries:
            assert 0.0 <= b <= 1.0

    def test_column_boundaries_sorted_ascending(self) -> None:
        r1 = _make_region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _make_region("r2", RegionType.text_block, 400, 0, 500, 100)
        r3 = _make_region("r3", RegionType.text_block, 800, 0, 900, 100)
        cs = _infer_column_structure([r1, r2, r3], 1000.0, 0.08)
        assert cs is not None
        for i in range(1, len(cs.column_boundaries)):
            assert cs.column_boundaries[i] > cs.column_boundaries[i - 1]


# ---------------------------------------------------------------------------
# _reassign_ids
# ---------------------------------------------------------------------------


class TestReassignIds:
    def test_sequential_ids(self) -> None:
        regions = [
            _make_region("r9", RegionType.text_block, 0, 0, 100, 100),
            _make_region("r8", RegionType.title, 0, 200, 100, 300),
        ]
        result = _reassign_ids(regions)
        ids = [r.id for r in result]
        assert ids == ["r1", "r2"]

    def test_sorted_by_y_then_x(self) -> None:
        # r_bottom starts lower (higher y_min) → should get a higher ID
        r_top = _make_region("r1", RegionType.text_block, 200, 50, 400, 200)
        r_bottom = _make_region("r2", RegionType.text_block, 100, 400, 300, 600)
        result = _reassign_ids([r_bottom, r_top])
        assert result[0].id == "r1"
        # r_top has lower y_min, so it comes first
        assert result[0].bbox.y_min < result[1].bbox.y_min

    def test_same_y_sorted_by_x(self) -> None:
        r_right = _make_region("r1", RegionType.text_block, 500, 100, 700, 300)
        r_left = _make_region("r2", RegionType.text_block, 100, 100, 300, 300)
        result = _reassign_ids([r_right, r_left])
        assert result[0].bbox.x_min < result[1].bbox.x_min
        assert result[0].id == "r1"

    def test_original_not_mutated(self) -> None:
        r = _make_region("r5", RegionType.title, 0, 0, 100, 100)
        _reassign_ids([r])
        assert r.id == "r5"

    def test_empty_input(self) -> None:
        assert _reassign_ids([]) == []


# ---------------------------------------------------------------------------
# postprocess_regions — integration
# ---------------------------------------------------------------------------


class TestPostprocessRegions:
    def test_returns_tuple(self) -> None:
        r = _make_region("r1", RegionType.text_block, 100, 100, 400, 600)
        result = postprocess_regions([r])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_empty_input(self) -> None:
        regions, col_struct = postprocess_regions([])
        assert regions == []
        assert col_struct is None

    def test_ids_unique_and_canonical(self) -> None:
        raw = [
            _make_region("r1", RegionType.title, 50, 30, 950, 120),
            _make_region("r2", RegionType.text_block, 50, 140, 450, 600),
            _make_region("r3", RegionType.text_block, 510, 140, 950, 600),
            _make_region("r4", RegionType.image, 50, 620, 450, 900),
            _make_region("r5", RegionType.caption, 50, 910, 450, 960),
            _make_region("r6", RegionType.table, 510, 620, 950, 960),
        ]
        regions, _ = postprocess_regions(raw)
        ids = [r.id for r in regions]
        assert len(ids) == len(set(ids)), "IDs must be unique"
        for rid in ids:
            assert _REGION_ID_RE.match(rid), f"ID {rid!r} must match ^r\\d+$"

    def test_region_types_all_canonical(self) -> None:
        canonical = frozenset(rt.value for rt in RegionType)
        raw = [
            _make_region("r1", RegionType.title, 50, 30, 950, 120),
            _make_region("r2", RegionType.text_block, 100, 140, 400, 600),
        ]
        regions, _ = postprocess_regions(raw)
        for r in regions:
            assert r.type.value in canonical

    def test_column_structure_non_none_when_text_blocks_present(self) -> None:
        # Two well-separated text_blocks → 2-column structure
        raw = [
            _make_region("r1", RegionType.text_block, 50, 100, 450, 600),
            _make_region("r2", RegionType.text_block, 550, 100, 950, 600),
        ]
        _, col_struct = postprocess_regions(raw, page_width=1000.0, page_height=1000.0)
        assert col_struct is not None
        assert col_struct.column_count == 2

    def test_nms_removes_duplicate_overlapping_region(self) -> None:
        # Two identical same-type regions → NMS keeps one
        hi = _make_region("r1", RegionType.table, 100, 100, 800, 800, confidence=0.9)
        lo = _make_region("r2", RegionType.table, 100, 100, 800, 800, confidence=0.6)
        regions, _ = postprocess_regions([hi, lo])
        table_regions = [r for r in regions if r.type == RegionType.table]
        assert len(table_regions) == 1
        assert table_regions[0].confidence <= 0.9

    def test_confidence_after_recalibration_lte_raw(self) -> None:
        # Recalibration only applies penalties, never boosts
        raw = [_make_region("r1", RegionType.text_block, 100, 100, 400, 600, confidence=0.8)]
        regions, _ = postprocess_regions(raw, page_width=1000.0, page_height=1000.0)
        assert all(r.confidence <= 0.8 + 1e-9 for r in regions)

    def test_page_dims_inferred_when_not_provided(self) -> None:
        # Should not raise even when page_width/height are omitted
        raw = [
            _make_region("r1", RegionType.text_block, 100, 100, 400, 600),
            _make_region("r2", RegionType.text_block, 500, 100, 900, 600),
        ]
        regions, col_struct = postprocess_regions(raw)
        assert len(regions) > 0

    def test_explicit_page_dims_accepted(self) -> None:
        raw = [_make_region("r1", RegionType.image, 100, 100, 400, 400)]
        regions, _ = postprocess_regions(raw, page_width=800.0, page_height=1200.0)
        assert len(regions) == 1
