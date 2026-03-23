"""
tests/test_p6_layout_consensus.py
----------------------------------
Packet 6.5 — EEP layout consensus gate tests.

Covers:
  - LayoutGateConfig default values match spec Section 8.4
  - evaluate_layout_consensus happy path: agreed=True when regions match well
  - agreed=False when match_ratio < min_match_ratio
  - agreed=False when type_histogram_match=False (even if match_ratio OK)
  - agreed=False when both conditions fail
  - Single-model fallback: iep2b_regions=None → agreed=False, single_model_mode=True
  - Single-model fallback fields are all zeroed
  - Empty IEP2A regions
  - Empty IEP2B regions
  - Both empty
  - IoU below threshold: regions not matched even when same type
  - Type mismatch: high IoU but different RegionType → no match
  - One-to-one constraint: same IEP2B region cannot match two IEP2A regions
  - consensus_confidence formula: spot-check values
  - consensus_confidence clamped to [0, 1]
  - match_ratio = 1.0 on perfect match
  - unmatched counts correct
  - mean_matched_iou correct
  - Custom config overrides default thresholds
  - type_histogram_match: diff == max_type_count_diff passes; diff > max fails
  - iep2b_region_count=0 for single-model fallback (not None)
  - single_model_mode=False in dual-model mode
  - LayoutConsensusResult is a valid Pydantic model (serialisable)
  - Histogram match: type present in iep2b only is checked
  - Histogram match: type present in iep2a only is checked
  - Greedy matching: higher-IoU pair wins over lower-IoU pair in contest
"""

from __future__ import annotations

from services.eep.app.gates.layout_gate import (
    LayoutGateConfig,
    _greedy_match,
    _iou,
    _type_histogram_match,
    evaluate_layout_consensus,
)
from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

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
    confidence: float = 0.8,
) -> Region:
    return Region(
        id=rid,
        type=rtype,
        bbox=BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max),
        confidence=confidence,
    )


def _bbox(x_min: float, y_min: float, x_max: float, y_max: float) -> BoundingBox:
    return BoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


# Six canonical types — used to construct test fixtures
_T = RegionType.title
_TB = RegionType.text_block
_TBL = RegionType.table
_IMG = RegionType.image
_CAP = RegionType.caption


# A set of 5 well-matching IEP2A/IEP2B region pairs (one per canonical type).
# IEP2B regions are slightly offset from IEP2A but substantially overlapping.
_IEP2A_WELL_MATCHED: list[Region] = [
    _make_region("r1", _T, 50, 20, 950, 110),
    _make_region("r2", _TB, 50, 130, 450, 600),
    _make_region("r3", _TB, 500, 130, 950, 600),
    _make_region("r4", _IMG, 50, 620, 450, 900),
    _make_region("r5", _CAP, 50, 910, 450, 960),
    _make_region("r6", _TBL, 500, 620, 950, 960),
]

# IEP2B counterparts: same type, same bbox (IoU = 1.0 for simplicity)
_IEP2B_WELL_MATCHED: list[Region] = [
    _make_region("r1", _T, 50, 20, 950, 110),
    _make_region("r2", _TB, 50, 130, 450, 600),
    _make_region("r3", _TB, 500, 130, 950, 600),
    _make_region("r4", _IMG, 50, 620, 450, 900),
    _make_region("r5", _CAP, 50, 910, 450, 960),
    _make_region("r6", _TBL, 500, 620, 950, 960),
]


# ---------------------------------------------------------------------------
# LayoutGateConfig defaults
# ---------------------------------------------------------------------------


class TestLayoutGateConfigDefaults:
    def test_match_iou_threshold(self) -> None:
        assert LayoutGateConfig().match_iou_threshold == 0.5

    def test_min_match_ratio(self) -> None:
        assert LayoutGateConfig().min_match_ratio == 0.7

    def test_max_type_count_diff(self) -> None:
        assert LayoutGateConfig().max_type_count_diff == 1

    def test_min_consensus_confidence(self) -> None:
        assert LayoutGateConfig().min_consensus_confidence == 0.6


# ---------------------------------------------------------------------------
# IoU utility
# ---------------------------------------------------------------------------


class TestIou:
    def test_identical_boxes_iou_one(self) -> None:
        b = _bbox(0, 0, 100, 100)
        assert abs(_iou(b, b) - 1.0) < 1e-9

    def test_non_overlapping_iou_zero(self) -> None:
        a = _bbox(0, 0, 10, 10)
        b = _bbox(20, 20, 30, 30)
        assert _iou(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        a = _bbox(0, 0, 10, 10)  # area 100
        b = _bbox(5, 5, 15, 15)  # area 100, intersection 5×5=25
        # union = 100 + 100 - 25 = 175
        assert abs(_iou(a, b) - 25 / 175) < 1e-9

    def test_touching_edges_iou_zero(self) -> None:
        a = _bbox(0, 0, 10, 10)
        b = _bbox(10, 0, 20, 10)
        assert _iou(a, b) == 0.0

    def test_one_inside_other(self) -> None:
        outer = _bbox(0, 0, 100, 100)
        inner = _bbox(25, 25, 75, 75)
        # inner area = 2500; outer area = 10000; intersection = 2500
        # union = 10000
        assert abs(_iou(outer, inner) - 2500 / 10000) < 1e-9


# ---------------------------------------------------------------------------
# _greedy_match
# ---------------------------------------------------------------------------


class TestGreedyMatch:
    def test_perfect_match_all_pairs_found(self) -> None:
        matches = _greedy_match(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED, 0.5)
        assert len(matches) == len(_IEP2A_WELL_MATCHED)

    def test_no_match_when_iou_below_threshold(self) -> None:
        a = [_make_region("r1", _T, 0, 0, 10, 10)]
        b = [_make_region("r1", _T, 100, 100, 200, 200)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 0

    def test_no_match_on_type_mismatch(self) -> None:
        # Same bbox but different type.
        a = [_make_region("r1", _T, 0, 0, 100, 100)]
        b = [_make_region("r1", _TB, 0, 0, 100, 100)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 0

    def test_one_to_one_constraint(self) -> None:
        # One IEP2B region that overlaps with two IEP2A regions.
        # Only one match should be made.
        a = [
            _make_region("r1", _T, 0, 0, 100, 100),
            _make_region("r2", _T, 5, 5, 95, 95),
        ]
        b = [_make_region("r1", _T, 0, 0, 100, 100)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 1

    def test_greedy_prefers_higher_iou(self) -> None:
        # r2 in iep2a has IoU=1.0 with the single iep2b region;
        # r1 has partial overlap.  r2 should get the match.
        a = [
            _make_region("r1", _T, 0, 0, 50, 100),  # partial overlap
            _make_region("r2", _T, 0, 0, 100, 100),  # exact match
        ]
        b = [_make_region("r1", _T, 0, 0, 100, 100)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 1
        matched_a_idx = matches[0][0]
        # r2 is at index 1
        assert matched_a_idx == 1

    def test_iou_at_threshold_included(self) -> None:
        # Create two boxes with exactly 50% IoU (threshold = 0.5 → should match).
        # Boxes: a=(0,0,10,10) area=100; b=(5,0,15,10) area=100
        # intersection = 5×10 = 50; union = 100+100-50 = 150; iou = 50/150 ≈ 0.333
        # Let's use a=(0,0,10,20) area=200; b=(5,0,15,20) area=200
        # intersection = 5×20=100; union=200+200-100=300; iou=100/300≈0.333 — still not 0.5
        # For exactly 0.5: use a=(0,0,10,10), b=(0,0,10,10) → iou=1.0, trivial.
        # For iou=0.5: a=(0,0,4,4)=16; b=(2,0,6,4)=16; inter=(2,0,4,4)=8; union=24; iou=8/24≈0.33
        # Correct: a=(0,0,6,6)=36; b=(3,0,9,6)=36; inter=(3,0,6,6)=18; union=54; iou=18/54=0.33
        # For iou=0.5 exactly: inter=A*0.5/(2-0.5)=A/3? No.
        # iou = inter / (A+B-inter) = 0.5 → inter = 0.5*(A+B-inter) → inter = (0.5*A+0.5*B)/(1.5)
        # For A=B: inter = A/1.5 → inter/A = 2/3 → use a=(0,0,3,1); b=(1,0,4,1):
        # area_a=3, area_b=3, inter=(1,0,3,1)=2, union=3+3-2=4, iou=2/4=0.5
        a = [_make_region("r1", _T, 0, 0, 3, 1)]
        b = [_make_region("r1", _T, 1, 0, 4, 1)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 1

    def test_iou_below_threshold_excluded(self) -> None:
        # iou just below 0.5
        # a=(0,0,3,1), b=(2,0,5,1): inter=(2,0,3,1)=1; union=3+3-1=5; iou=0.2 < 0.5
        a = [_make_region("r1", _T, 0, 0, 3, 1)]
        b = [_make_region("r1", _T, 2, 0, 5, 1)]
        matches = _greedy_match(a, b, 0.5)
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# _type_histogram_match
# ---------------------------------------------------------------------------


class TestTypeHistogramMatch:
    def test_identical_histograms_match(self) -> None:
        a = [_make_region("r1", _T, 0, 0, 10, 10), _make_region("r2", _TB, 0, 0, 10, 10)]
        b = [_make_region("r1", _T, 0, 0, 10, 10), _make_region("r2", _TB, 0, 0, 10, 10)]
        assert _type_histogram_match(a, b, max_diff=1) is True

    def test_diff_equal_to_max_passes(self) -> None:
        # a has 2 titles, b has 1 title → diff=1 == max_diff=1
        a = [_make_region("r1", _T, 0, 0, 10, 10), _make_region("r2", _T, 0, 0, 10, 10)]
        b = [_make_region("r1", _T, 0, 0, 10, 10)]
        assert _type_histogram_match(a, b, max_diff=1) is True

    def test_diff_greater_than_max_fails(self) -> None:
        # a has 3 titles, b has 1 title → diff=2 > max_diff=1
        a = [
            _make_region("r1", _T, 0, 0, 10, 10),
            _make_region("r2", _T, 0, 0, 10, 10),
            _make_region("r3", _T, 0, 0, 10, 10),
        ]
        b = [_make_region("r1", _T, 0, 0, 10, 10)]
        assert _type_histogram_match(a, b, max_diff=1) is False

    def test_type_only_in_iep2b_checked(self) -> None:
        # iep2a has no table; iep2b has 2 tables → diff=2 > max_diff=1
        a = [_make_region("r1", _T, 0, 0, 10, 10)]
        b = [
            _make_region("r1", _T, 0, 0, 10, 10),
            _make_region("r2", _TBL, 0, 0, 10, 10),
            _make_region("r3", _TBL, 0, 0, 10, 10),
        ]
        assert _type_histogram_match(a, b, max_diff=1) is False

    def test_type_only_in_iep2a_checked(self) -> None:
        # iep2a has 2 images; iep2b has 0 → diff=2 > max_diff=1
        a = [
            _make_region("r1", _IMG, 0, 0, 10, 10),
            _make_region("r2", _IMG, 0, 0, 10, 10),
        ]
        b: list[Region] = []
        assert _type_histogram_match(a, b, max_diff=1) is False

    def test_both_empty_match(self) -> None:
        assert _type_histogram_match([], [], max_diff=1) is True

    def test_max_diff_zero_requires_identical(self) -> None:
        a = [_make_region("r1", _T, 0, 0, 10, 10), _make_region("r2", _T, 0, 0, 10, 10)]
        b = [_make_region("r1", _T, 0, 0, 10, 10)]
        assert _type_histogram_match(a, b, max_diff=0) is False


# ---------------------------------------------------------------------------
# evaluate_layout_consensus — single-model fallback
# ---------------------------------------------------------------------------


class TestSingleModelFallback:
    def test_agreed_false(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.agreed is False

    def test_single_model_mode_true(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.single_model_mode is True

    def test_iep2a_count_correct(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.iep2a_region_count == len(_IEP2A_WELL_MATCHED)

    def test_iep2b_count_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.iep2b_region_count == 0

    def test_matched_regions_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.matched_regions == 0

    def test_unmatched_iep2a_equals_iep2a_count(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.unmatched_iep2a == len(_IEP2A_WELL_MATCHED)

    def test_unmatched_iep2b_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.unmatched_iep2b == 0

    def test_mean_matched_iou_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.mean_matched_iou == 0.0

    def test_consensus_confidence_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.consensus_confidence == 0.0

    def test_type_histogram_match_false(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        assert result.type_histogram_match is False

    def test_empty_iep2a_single_model(self) -> None:
        result = evaluate_layout_consensus([], None)
        assert result.agreed is False
        assert result.single_model_mode is True
        assert result.iep2a_region_count == 0


# ---------------------------------------------------------------------------
# evaluate_layout_consensus — dual-model happy path (agreed=True)
# ---------------------------------------------------------------------------


class TestDualModelAgreed:
    def test_agreed_true_on_perfect_match(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.agreed is True

    def test_single_model_mode_false_in_dual(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.single_model_mode is False

    def test_iep2a_count(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.iep2a_region_count == len(_IEP2A_WELL_MATCHED)

    def test_iep2b_count(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.iep2b_region_count == len(_IEP2B_WELL_MATCHED)

    def test_all_regions_matched(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.matched_regions == len(_IEP2A_WELL_MATCHED)

    def test_unmatched_both_zero(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.unmatched_iep2a == 0
        assert result.unmatched_iep2b == 0

    def test_mean_matched_iou_one_on_identical(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert abs(result.mean_matched_iou - 1.0) < 1e-5

    def test_type_histogram_match_true(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert result.type_histogram_match is True

    def test_consensus_confidence_near_one_on_perfect(self) -> None:
        # With match_ratio=1.0, mean_iou=1.0, histogram_flag=1.0:
        # 0.6*1 + 0.2*1 + 0.2*1 = 1.0
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert abs(result.consensus_confidence - 1.0) < 1e-5


# ---------------------------------------------------------------------------
# evaluate_layout_consensus — agreed=False cases
# ---------------------------------------------------------------------------


class TestDualModelDisagreed:
    def test_agreed_false_when_match_ratio_low(self) -> None:
        # IEP2A has 6 regions; IEP2B has 6 non-overlapping regions of same type.
        iep2a = [
            _make_region("r1", _T, 0, 0, 100, 50),
            _make_region("r2", _TB, 0, 60, 100, 200),
            _make_region("r3", _TB, 0, 210, 100, 400),
            _make_region("r4", _IMG, 0, 410, 100, 550),
            _make_region("r5", _CAP, 0, 560, 100, 600),
            _make_region("r6", _TBL, 0, 610, 100, 800),
        ]
        iep2b = [
            _make_region("r1", _T, 500, 0, 600, 50),  # non-overlapping
            _make_region("r2", _TB, 500, 60, 600, 200),
            _make_region("r3", _TB, 500, 210, 600, 400),
            _make_region("r4", _IMG, 500, 410, 600, 550),
            _make_region("r5", _CAP, 500, 560, 600, 600),
            _make_region("r6", _TBL, 500, 610, 600, 800),
        ]
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert result.agreed is False
        assert result.matched_regions == 0

    def test_agreed_false_when_histogram_mismatch(self) -> None:
        # Same bboxes but iep2b adds 3 extra text_blocks (diff=3 > 1).
        extra = [
            _make_region("r7", _TB, 50, 130, 450, 600),
            _make_region("r8", _TB, 50, 130, 450, 600),
            _make_region("r9", _TB, 50, 130, 450, 600),
        ]
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED + extra)
        assert result.type_histogram_match is False
        assert result.agreed is False

    def test_match_ratio_computed_correctly(self) -> None:
        # 1 match out of max(2, 2) = 2 → match_ratio = 0.5 < 0.7
        iep2a = [
            _make_region("r1", _T, 0, 0, 100, 100),
            _make_region("r2", _TB, 0, 0, 100, 100),
        ]
        iep2b = [
            _make_region("r1", _T, 0, 0, 100, 100),  # match
            _make_region("r2", _TB, 500, 500, 600, 600),  # no match
        ]
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert result.matched_regions == 1
        assert result.agreed is False  # 0.5 < 0.7

    def test_unmatched_counts_correct(self) -> None:
        iep2a = [
            _make_region("r1", _T, 0, 0, 100, 100),
            _make_region("r2", _TB, 0, 0, 100, 100),
        ]
        iep2b = [
            _make_region("r1", _T, 0, 0, 100, 100),  # match
            _make_region("r2", _TB, 500, 500, 600, 600),  # no match
        ]
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert result.unmatched_iep2a == 1  # r2 unmatched from iep2a
        assert result.unmatched_iep2b == 1  # r2 unmatched from iep2b


# ---------------------------------------------------------------------------
# consensus_confidence formula
# ---------------------------------------------------------------------------


class TestConsensusConfidenceFormula:
    def test_no_match_and_histogram_mismatch_gives_zero(self) -> None:
        # 0 matches → match_ratio=0, mean_iou=0
        # iep2a has 3 titles, iep2b has 1 title far away → diff=2>1 → histogram_match=False(0)
        # consensus_confidence = 0.6*0 + 0.2*0 + 0.2*0 = 0
        iep2a = [
            _make_region("r1", _T, 0, 0, 10, 10),
            _make_region("r2", _T, 0, 11, 10, 21),
            _make_region("r3", _T, 0, 22, 10, 32),
        ]
        iep2b = [_make_region("r1", _T, 500, 500, 600, 600)]  # no overlap with any iep2a
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert result.matched_regions == 0
        assert result.type_histogram_match is False  # diff=2 > 1
        assert result.consensus_confidence == 0.0

    def test_formula_with_known_values(self) -> None:
        # 1 match out of max(1,1)=1 → match_ratio=1.0
        # iou=1.0, hist_match=True(1.0)
        # expected = 0.6*1 + 0.2*1 + 0.2*1 = 1.0
        iep2a = [_make_region("r1", _T, 0, 0, 100, 100)]
        iep2b = [_make_region("r1", _T, 0, 0, 100, 100)]
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert abs(result.consensus_confidence - 1.0) < 1e-5

    def test_histogram_mismatch_reduces_confidence(self) -> None:
        # Same bboxes, match_ratio=1.0, mean_iou=1.0, hist=False(0.0)
        # expected = 0.6*1 + 0.2*1 + 0.2*0 = 0.8
        iep2a = [
            _make_region("r1", _T, 0, 0, 100, 100),
            _make_region("r2", _T, 0, 0, 100, 100),
            _make_region("r3", _T, 0, 0, 100, 100),
        ]
        iep2b = [
            _make_region("r1", _T, 0, 0, 100, 100),
        ]
        result = evaluate_layout_consensus(iep2a, iep2b)
        # match_ratio = 1/max(3,1) = 1/3 ≈ 0.333; hist_match=False(diff=2>1)
        # consensus = 0.6*(1/3) + 0.2*(1.0) + 0.2*0 = 0.2 + 0.2 = 0.4
        assert result.type_histogram_match is False
        assert result.consensus_confidence > 0.0  # partial match still contributes

    def test_consensus_confidence_in_range(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        assert 0.0 <= result.consensus_confidence <= 1.0

    def test_consensus_confidence_in_range_no_match(self) -> None:
        iep2a = [_make_region("r1", _T, 0, 0, 10, 10)]
        iep2b = [_make_region("r1", _T, 200, 200, 300, 300)]
        result = evaluate_layout_consensus(iep2a, iep2b)
        assert 0.0 <= result.consensus_confidence <= 1.0


# ---------------------------------------------------------------------------
# Edge cases: empty lists
# ---------------------------------------------------------------------------


class TestEdgeCasesEmpty:
    def test_both_empty_dual(self) -> None:
        result = evaluate_layout_consensus([], [])
        assert result.iep2a_region_count == 0
        assert result.iep2b_region_count == 0
        assert result.matched_regions == 0
        # Histograms trivially match (no type present in either), so
        # consensus_confidence = 0.6*0 + 0.2*0 + 0.2*1.0 = 0.2
        assert 0.0 <= result.consensus_confidence <= 1.0

    def test_both_empty_agreed_false(self) -> None:
        # total=0, match_ratio undefined → 0; agreed=False (0 < 0.7)
        result = evaluate_layout_consensus([], [])
        assert result.agreed is False

    def test_empty_iep2b_dual(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, [])
        assert result.iep2b_region_count == 0
        assert result.matched_regions == 0
        assert result.agreed is False
        assert result.single_model_mode is False

    def test_empty_iep2a_dual(self) -> None:
        result = evaluate_layout_consensus([], _IEP2B_WELL_MATCHED)
        assert result.iep2a_region_count == 0
        assert result.matched_regions == 0
        assert result.agreed is False


# ---------------------------------------------------------------------------
# Custom config
# ---------------------------------------------------------------------------


class TestCustomConfig:
    def test_lower_min_match_ratio_allows_agreement(self) -> None:
        # 1 match out of 2: match_ratio = 0.5, which is >= 0.5 (custom)
        iep2a = [
            _make_region("r1", _T, 0, 0, 100, 100),
            _make_region("r2", _TB, 0, 0, 100, 100),
        ]
        iep2b = [
            _make_region("r1", _T, 0, 0, 100, 100),  # match
            _make_region("r2", _TB, 500, 500, 600, 600),  # no match
        ]
        cfg = LayoutGateConfig(min_match_ratio=0.5)
        result = evaluate_layout_consensus(iep2a, iep2b, cfg)
        assert result.matched_regions == 1
        # hist_match: both have 1 title, 1 text_block → True
        assert result.agreed is True

    def test_stricter_iou_threshold_reduces_matches(self) -> None:
        # Regions overlap partially (IoU ≈ 0.33), below strict threshold 0.8
        a = [_make_region("r1", _T, 0, 0, 3, 1)]  # iou ≈ 0.33 with b
        b = [_make_region("r1", _T, 1, 0, 4, 1)]
        cfg = LayoutGateConfig(match_iou_threshold=0.8)
        result = evaluate_layout_consensus(a, b, cfg)
        assert result.matched_regions == 0

    def test_larger_max_type_count_diff_allows_agreement(self) -> None:
        # iep2b has 2 extra text_blocks (diff=2), normally fails at max_diff=1
        extra = [
            _make_region("r7", _TB, 50, 130, 450, 600),
            _make_region("r8", _TB, 50, 130, 450, 600),
        ]
        cfg = LayoutGateConfig(max_type_count_diff=2)
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED + extra, cfg)
        assert result.type_histogram_match is True


# ---------------------------------------------------------------------------
# LayoutConsensusResult serialisability
# ---------------------------------------------------------------------------


class TestSerialisability:
    def test_result_serialisable(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, _IEP2B_WELL_MATCHED)
        d = result.model_dump()
        assert isinstance(d, dict)
        assert "single_model_mode" in d
        assert "agreed" in d
        assert "consensus_confidence" in d

    def test_single_model_serialisable(self) -> None:
        result = evaluate_layout_consensus(_IEP2A_WELL_MATCHED, None)
        d = result.model_dump()
        assert d["single_model_mode"] is True
        assert d["agreed"] is False
