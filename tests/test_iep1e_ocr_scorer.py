"""
tests/test_iep1e_ocr_scorer.py
--------------------------------
Unit tests for shared.semantic_norm.ocr_scorer.

All tests mock PaddleOCR — no real OCR engine is loaded.
Tests cover:
  - blank page (zero results) → no exception, zero scores
  - all-Arabic text → reading_direction="rtl"
  - all-Latin text  → reading_direction="ltr"
  - split-script spread → dominant direction wins
  - confidence gate: ambiguous → orientation_confident=False
  - confidence gate: clear winner → orientation_confident=True
  - early-exit: only two rotations scored when gate met after 0° + 90°
  - assign_reading_order: LTR and RTL orderings
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from shared.semantic_norm.ocr_scorer import (
    CONF_DIFF_THRESHOLD,
    CONF_RATIO_THRESHOLD,
    assign_reading_order,
    determine_reading_direction,
    extract_script_evidence,
    score_rotation,
    select_orientation,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_ocr_line(text: str, conf: float = 0.9) -> list:
    """Return one OCR result line in PaddleOCR format: [bbox, (text, conf)]."""
    bbox = [[0, 0], [100, 0], [100, 20], [0, 20]]
    return [bbox, (text, conf)]


def _mock_ocr(return_values: dict[int, list]) -> MagicMock:
    """
    Build a mock PaddleOCR engine whose .ocr(image, cls=False) returns
    the values in return_values keyed by call order.

    If return_values is {0: [lines], 1: [lines], ...}, the i-th call returns
    return_values.get(i, []).
    """
    ocr = MagicMock()
    call_count = [0]

    def _ocr_side_effect(image, cls=False):
        idx = call_count[0]
        call_count[0] += 1
        return [return_values.get(idx, [])]

    ocr.ocr.side_effect = _ocr_side_effect
    return ocr


def _blank_image() -> np.ndarray:
    return np.zeros((100, 100, 3), dtype=np.uint8)


# ── extract_script_evidence ───────────────────────────────────────────────────


class TestExtractScriptEvidence:
    def test_blank_page_no_results(self):
        ev = extract_script_evidence([])
        assert ev.n_boxes == 0
        assert ev.n_chars == 0
        assert ev.arabic_ratio == 0.0
        assert ev.latin_ratio == 0.0
        assert ev.garbage_ratio == 0.0
        assert ev.mean_conf == 0.0

    def test_none_results_do_not_raise(self):
        ev = extract_script_evidence([None])
        assert ev.n_boxes == 0

    def test_arabic_text(self):
        # Arabic characters: ب ت ث
        lines = [_make_ocr_line("بتث", 0.95)]
        ev = extract_script_evidence([lines])
        assert ev.n_boxes == 1
        assert ev.arabic_ratio > 0.5
        assert ev.latin_ratio == 0.0

    def test_latin_text(self):
        lines = [_make_ocr_line("Hello", 0.90)]
        ev = extract_script_evidence([lines])
        assert ev.n_boxes == 1
        assert ev.latin_ratio > 0.5
        assert ev.arabic_ratio == 0.0

    def test_mixed_text_ratios_sum_le_one(self):
        lines = [_make_ocr_line("Helloبتث", 0.80)]
        ev = extract_script_evidence([lines])
        assert ev.arabic_ratio + ev.latin_ratio + ev.garbage_ratio <= 1.0 + 1e-9

    def test_mean_conf_correct(self):
        lines = [
            _make_ocr_line("abc", 0.8),
            _make_ocr_line("def", 0.6),
        ]
        ev = extract_script_evidence([lines])
        assert abs(ev.mean_conf - 0.7) < 1e-6

    def test_malformed_line_skipped(self):
        # A line with only one element (no text/conf pair)
        malformed = [[0, 0], [100, 0]]
        ev = extract_script_evidence([[malformed]])
        assert ev.n_boxes == 0


# ── score_rotation ────────────────────────────────────────────────────────────


class TestScoreRotation:
    def test_blank_page_all_zero_scores(self):
        ocr = _mock_ocr({0: [], 1: [], 2: [], 3: []})
        img = _blank_image()
        scores = score_rotation(img, ocr)
        assert set(scores.keys()) == {0, 90, 180, 270}
        for deg, (s, ev) in scores.items():
            assert s == 0.0
            assert ev.n_boxes == 0

    def test_best_rotation_has_highest_score(self):
        # 0° → rich Arabic text; others → blank
        arabic_lines = [_make_ocr_line("بتثجحخ" * 5, 0.95)]
        ocr = _mock_ocr({0: arabic_lines, 1: [], 2: [], 3: []})
        img = _blank_image()
        scores = score_rotation(img, ocr)
        best_deg = max(scores, key=lambda d: scores[d][0])
        assert best_deg == 0

    def test_all_rotations_always_scored(self):
        """All four rotations are always tried — early-exit was removed for correctness."""
        # 0° → many rich boxes; 90°/180°/270° → blank (mock returns [] for unset keys)
        big_lines = [_make_ocr_line("Hello world " * 10, 0.95)] * 20
        ocr = _mock_ocr({0: big_lines})
        img = _blank_image()
        scores = score_rotation(img, ocr)
        # All four rotations must be tried (no early exit)
        assert ocr.ocr.call_count == 4
        assert scores[0][0] > 0.0
        assert scores[180][0] == 0.0
        assert scores[270][0] == 0.0

    def test_early_exit_not_triggered_when_scores_close(self):
        """When scores are similar, all four rotations must be tried."""
        close_lines = [_make_ocr_line("ab", 0.6)]
        ocr = _mock_ocr({0: close_lines, 1: close_lines, 2: close_lines, 3: close_lines})
        img = _blank_image()
        scores = score_rotation(img, ocr)
        assert ocr.ocr.call_count == 4

    def test_ocr_exception_produces_zero_score(self):
        ocr = MagicMock()
        ocr.ocr.side_effect = RuntimeError("GPU OOM")
        img = _blank_image()
        scores = score_rotation(img, ocr)
        for deg, (s, _) in scores.items():
            assert s == 0.0


# ── select_orientation ────────────────────────────────────────────────────────


class TestSelectOrientation:
    def test_all_zeros_returns_0deg_not_confident(self):
        from shared.schemas.semantic_norm import ScriptEvidence

        zero_ev = ScriptEvidence(
            arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
            n_boxes=0, n_chars=0, mean_conf=0.0,
        )
        scores = {d: (0.0, zero_ev) for d in (0, 90, 180, 270)}
        result = select_orientation(scores)
        assert result.best_rotation_deg == 0
        assert result.orientation_confident is False

    def test_confident_when_gate_passes(self):
        from shared.schemas.semantic_norm import ScriptEvidence

        zero_ev = ScriptEvidence(
            arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
            n_boxes=0, n_chars=0, mean_conf=0.0,
        )
        # best=200, second=10 → ratio=20, diff=190 → confident
        scores = {
            0: (200.0, zero_ev),
            90: (10.0, zero_ev),
            180: (5.0, zero_ev),
            270: (3.0, zero_ev),
        }
        result = select_orientation(scores)
        assert result.best_rotation_deg == 0
        assert result.orientation_confident is True

    def test_not_confident_when_ratio_below_threshold(self):
        from shared.schemas.semantic_norm import ScriptEvidence

        zero_ev = ScriptEvidence(
            arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
            n_boxes=0, n_chars=0, mean_conf=0.0,
        )
        # ratio = 105/100 = 1.05 < CONF_RATIO_THRESHOLD
        scores = {
            0: (105.0, zero_ev),
            90: (100.0, zero_ev),
            180: (1.0, zero_ev),
            270: (1.0, zero_ev),
        }
        result = select_orientation(scores)
        assert result.orientation_confident is False

    def test_not_confident_when_diff_below_threshold(self):
        from shared.schemas.semantic_norm import ScriptEvidence

        zero_ev = ScriptEvidence(
            arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
            n_boxes=0, n_chars=0, mean_conf=0.0,
        )
        # ratio OK, but diff = 100 - 99 = 1 < CONF_DIFF_THRESHOLD
        scores = {
            0: (100.0, zero_ev),
            90: (99.0, zero_ev),
            180: (1.0, zero_ev),
            270: (1.0, zero_ev),
        }
        result = select_orientation(scores)
        assert result.orientation_confident is False


# ── determine_reading_direction ───────────────────────────────────────────────


class TestDetermineReadingDirection:
    def test_empty_returns_unresolved(self):
        assert determine_reading_direction([]) == "unresolved"

    def test_all_blank_returns_unresolved(self):
        from shared.schemas.semantic_norm import ScriptEvidence

        ev = ScriptEvidence(
            arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
            n_boxes=0, n_chars=0, mean_conf=0.0,
        )
        assert determine_reading_direction([ev, ev]) == "unresolved"

    def test_arabic_dominant_returns_rtl(self):
        lines = [_make_ocr_line("بتثجحخدذ", 0.9)] * 5
        ev = extract_script_evidence([lines])
        assert determine_reading_direction([ev]) == "rtl"

    def test_latin_dominant_returns_ltr(self):
        lines = [_make_ocr_line("Hello world test", 0.9)] * 5
        ev = extract_script_evidence([lines])
        assert determine_reading_direction([ev]) == "ltr"

    def test_split_script_follows_dominant(self):
        """Arabic page has more chars than Latin page → rtl."""
        arabic_lines = [_make_ocr_line("بتثجحخدذرزسشصضطظعغفقكلمنهوي", 0.9)] * 3
        latin_lines = [_make_ocr_line("Hi", 0.9)]
        ev_arabic = extract_script_evidence([arabic_lines])
        ev_latin = extract_script_evidence([latin_lines])
        direction = determine_reading_direction([ev_arabic, ev_latin])
        assert direction == "rtl"


# ── assign_reading_order ──────────────────────────────────────────────────────


class TestAssignReadingOrder:
    def test_single_page_returns_unchanged(self):
        uris = ["page0.tiff"]
        result = assign_reading_order(uris, [300.0], "ltr")
        assert result == ["page0.tiff"]

    def test_ltr_returns_left_first(self):
        uris = ["left.tiff", "right.tiff"]
        x_centers = [100.0, 400.0]
        result = assign_reading_order(uris, x_centers, "ltr")
        assert result == ["left.tiff", "right.tiff"]

    def test_rtl_returns_right_first(self):
        uris = ["left.tiff", "right.tiff"]
        x_centers = [100.0, 400.0]
        result = assign_reading_order(uris, x_centers, "rtl")
        assert result == ["right.tiff", "left.tiff"]

    def test_unresolved_defaults_to_ltr(self):
        uris = ["left.tiff", "right.tiff"]
        x_centers = [100.0, 400.0]
        result = assign_reading_order(uris, x_centers, "unresolved")
        assert result == ["left.tiff", "right.tiff"]

    def test_physical_order_independent_of_input_order(self):
        """x_centers determine left/right regardless of list order."""
        uris = ["right.tiff", "left.tiff"]  # right is first in input list
        x_centers = [400.0, 100.0]          # but has larger x_center
        result = assign_reading_order(uris, x_centers, "ltr")
        assert result == ["left.tiff", "right.tiff"]
