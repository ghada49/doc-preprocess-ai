"""
tests/test_p6_iep2b_postprocess.py
-------------------------------------
Packet 6.4 — IEP2B postprocessing unit tests.

Tests _iou, _nms_per_type, _reassign_ids, and postprocess_regions.

Covers:
  _iou:
    - non-overlapping → 0.0
    - identical → 1.0
    - partial overlap → value in (0, 1)
    - touching/adjacent → 0.0

  _nms_per_type:
    - two non-overlapping same-type → both kept
    - two overlapping same-type → higher-confidence kept
    - two different types (even with overlapping bbox) → both kept
    - empty input → []

  _reassign_ids:
    - IDs are r1, r2, … sequential
    - sorted by (y_min, x_min)
    - same y → sorted by x_min
    - originals not mutated
    - empty input → []

  postprocess_regions (integration):
    - returns list[Region]
    - all IDs unique and match ^r\\d+$
    - all types are canonical RegionType values
    - duplicate overlapping same-type region removed by NMS
    - confidences unchanged (no recalibration in IEP2B)
    - empty input → []
    - IEP2B does NOT return column_structure (not part of this module)
"""

from __future__ import annotations

import re

from services.iep2b.app.postprocess import _iou, _nms_per_type, _reassign_ids, postprocess_regions
from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

_REGION_ID_RE = re.compile(r"^r\d+$")
_CANONICAL_TYPES = frozenset(rt.value for rt in RegionType)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bb(x_min: float, y_min: float, x_max: float, y_max: float) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _region(
    rid: str,
    rtype: RegionType,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    confidence: float = 0.85,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=_bb(x_min, y_min, x_max, y_max),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# _iou
# ---------------------------------------------------------------------------


class TestIou:
    def test_non_overlapping(self) -> None:
        assert _iou(_bb(0, 0, 10, 10), _bb(20, 20, 30, 30)) == 0.0

    def test_identical(self) -> None:
        assert abs(_iou(_bb(0, 0, 10, 10), _bb(0, 0, 10, 10)) - 1.0) < 1e-9

    def test_partial_overlap(self) -> None:
        val = _iou(_bb(0, 0, 10, 10), _bb(5, 5, 15, 15))
        assert 0.0 < val < 1.0
        assert abs(val - 25.0 / 175.0) < 1e-9

    def test_adjacent_touching(self) -> None:
        assert _iou(_bb(0, 0, 10, 10), _bb(10, 0, 20, 10)) == 0.0


# ---------------------------------------------------------------------------
# _nms_per_type
# ---------------------------------------------------------------------------


class TestNmsPerType:
    def test_non_overlapping_both_kept(self) -> None:
        r1 = _region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _region("r2", RegionType.text_block, 200, 200, 300, 300)
        assert len(_nms_per_type([r1, r2])) == 2

    def test_overlapping_higher_conf_kept(self) -> None:
        hi = _region("r1", RegionType.title, 0, 0, 100, 100, confidence=0.9)
        lo = _region("r2", RegionType.title, 0, 0, 100, 100, confidence=0.6)
        result = _nms_per_type([lo, hi])
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_different_types_both_kept_even_if_overlapping(self) -> None:
        r1 = _region("r1", RegionType.text_block, 0, 0, 100, 100)
        r2 = _region("r2", RegionType.image, 0, 0, 100, 100)
        assert len(_nms_per_type([r1, r2])) == 2

    def test_empty_input(self) -> None:
        assert _nms_per_type([]) == []


# ---------------------------------------------------------------------------
# _reassign_ids
# ---------------------------------------------------------------------------


class TestReassignIds:
    def test_sequential_ids(self) -> None:
        regions = [
            _region("r9", RegionType.text_block, 0, 0, 100, 100),
            _region("r8", RegionType.title, 0, 200, 100, 300),
        ]
        result = _reassign_ids(regions)
        assert [r.id for r in result] == ["r1", "r2"]

    def test_sorted_by_y_then_x(self) -> None:
        r_top = _region("r1", RegionType.text_block, 200, 50, 400, 200)
        r_bottom = _region("r2", RegionType.text_block, 100, 400, 300, 600)
        result = _reassign_ids([r_bottom, r_top])
        assert result[0].bbox.y_min < result[1].bbox.y_min
        assert result[0].id == "r1"

    def test_same_y_sorted_by_x(self) -> None:
        r_right = _region("r1", RegionType.text_block, 500, 100, 700, 300)
        r_left = _region("r2", RegionType.text_block, 100, 100, 300, 300)
        result = _reassign_ids([r_right, r_left])
        assert result[0].bbox.x_min < result[1].bbox.x_min
        assert result[0].id == "r1"

    def test_original_not_mutated(self) -> None:
        r = _region("r5", RegionType.caption, 0, 0, 100, 100)
        _reassign_ids([r])
        assert r.id == "r5"

    def test_empty_input(self) -> None:
        assert _reassign_ids([]) == []


# ---------------------------------------------------------------------------
# postprocess_regions — integration
# ---------------------------------------------------------------------------


class TestPostprocessRegions:
    def test_returns_list(self) -> None:
        r = _region("r1", RegionType.text_block, 100, 100, 400, 600)
        result = postprocess_regions([r])
        assert isinstance(result, list)

    def test_empty_input(self) -> None:
        assert postprocess_regions([]) == []

    def test_ids_unique_and_canonical(self) -> None:
        raw = [
            _region("r1", RegionType.title, 45, 25, 955, 115),
            _region("r2", RegionType.text_block, 45, 135, 455, 610),
            _region("r3", RegionType.text_block, 505, 135, 955, 610),
            _region("r4", RegionType.image, 45, 625, 455, 905),
            _region("r5", RegionType.caption, 45, 915, 455, 965),
            _region("r6", RegionType.table, 505, 625, 955, 965),
        ]
        result = postprocess_regions(raw)
        ids = [r.id for r in result]
        assert len(ids) == len(set(ids)), "IDs must be unique"
        for rid in ids:
            assert _REGION_ID_RE.match(rid), f"ID {rid!r} must match ^r\\d+$"

    def test_region_types_all_canonical(self) -> None:
        raw = [
            _region("r1", RegionType.title, 45, 25, 955, 115),
            _region("r2", RegionType.text_block, 45, 135, 455, 610),
        ]
        for r in postprocess_regions(raw):
            assert r.type.value in _CANONICAL_TYPES

    def test_nms_removes_duplicate(self) -> None:
        hi = _region("r1", RegionType.table, 100, 100, 800, 800, confidence=0.9)
        lo = _region("r2", RegionType.table, 100, 100, 800, 800, confidence=0.6)
        result = postprocess_regions([hi, lo])
        tables = [r for r in result if r.type == RegionType.table]
        assert len(tables) == 1
        assert tables[0].confidence == 0.9

    def test_confidence_unchanged(self) -> None:
        """IEP2B postprocessing must not modify confidence values."""
        raw = [_region("r1", RegionType.text_block, 100, 100, 400, 600, confidence=0.77)]
        result = postprocess_regions(raw)
        assert len(result) == 1
        assert abs(result[0].confidence - 0.77) < 1e-9

    def test_non_overlapping_all_kept(self) -> None:
        raw = [
            _region("r1", RegionType.title, 45, 25, 955, 115),
            _region("r2", RegionType.text_block, 45, 135, 455, 610),
            _region("r3", RegionType.text_block, 505, 135, 955, 610),
            _region("r4", RegionType.image, 45, 625, 455, 905),
            _region("r5", RegionType.caption, 45, 915, 455, 965),
            _region("r6", RegionType.table, 505, 625, 955, 965),
        ]
        result = postprocess_regions(raw)
        assert len(result) == 6
