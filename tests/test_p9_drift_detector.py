"""
tests/test_p9_drift_detector.py
---------------------------------
Packet 9.3 — DriftDetector unit tests.

Tests cover:
- Baseline dataclass construction
- observe() — appends to sliding window
- is_drifting() — False when window empty
- is_drifting() — False when metric has no baseline
- is_drifting() — False when within threshold
- is_drifting() — True when window mean exceeds threshold
- is_drifting() — False when baseline std is zero
- is_drifting() — direction-independent (drift in both directions)
- Sliding window evicts old values at window_size
- window_mean() helper
- window_size() helper
- DriftDetector.load() from JSON file
- DriftDetector.load() raises FileNotFoundError for missing file
- DriftDetector.load() raises ValueError for malformed JSON
- DriftDetector.load() raises ValueError for missing 'mean'/'std' fields
- Multiple metrics are independent
- Default window_size is 200
- Default threshold_std is 3.0
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from monitoring.drift_detector import Baseline, DriftDetector


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_detector(
    metric: str = "test.metric",
    mean: float = 0.8,
    std: float = 0.05,
    window_size: int = 10,
    threshold_std: float = 3.0,
) -> DriftDetector:
    return DriftDetector(
        baselines={metric: Baseline(mean=mean, std=std)},
        window_size=window_size,
        threshold_std=threshold_std,
    )


# ── Baseline dataclass ────────────────────────────────────────────────────────


class TestBaseline:
    def test_baseline_stores_mean_and_std(self):
        b = Baseline(mean=0.85, std=0.03)
        assert b.mean == 0.85
        assert b.std == 0.03

    def test_baseline_is_frozen(self):
        b = Baseline(mean=0.5, std=0.1)
        with pytest.raises((AttributeError, TypeError)):
            b.mean = 0.9  # type: ignore[misc]


# ── observe ───────────────────────────────────────────────────────────────────


class TestObserve:
    def test_observe_adds_value_to_window(self):
        det = _make_detector()
        det.observe("test.metric", 0.8)
        assert det.window_size("test.metric") == 1

    def test_observe_multiple_values(self):
        det = _make_detector(window_size=5)
        for v in [0.8, 0.81, 0.79, 0.82, 0.78]:
            det.observe("test.metric", v)
        assert det.window_size("test.metric") == 5

    def test_observe_unknown_metric_still_stored(self):
        det = _make_detector()
        det.observe("unknown.metric", 0.5)
        assert det.window_size("unknown.metric") == 1

    def test_observe_converts_to_float(self):
        det = _make_detector()
        det.observe("test.metric", 1)  # int
        assert det.window_mean("test.metric") == 1.0


# ── Sliding window eviction ───────────────────────────────────────────────────


class TestSlidingWindow:
    def test_window_evicts_oldest_when_full(self):
        det = _make_detector(window_size=3)
        for v in [0.1, 0.2, 0.3]:
            det.observe("test.metric", v)
        # window is full: [0.1, 0.2, 0.3]
        det.observe("test.metric", 0.4)
        # oldest (0.1) should be evicted: [0.2, 0.3, 0.4]
        assert det.window_size("test.metric") == 3
        mean = det.window_mean("test.metric")
        assert mean == pytest.approx((0.2 + 0.3 + 0.4) / 3)

    def test_default_window_size_is_200(self):
        det = DriftDetector(baselines={})
        assert det._window_size == 200


# ── is_drifting ───────────────────────────────────────────────────────────────


class TestIsDrifting:
    def test_empty_window_returns_false(self):
        det = _make_detector()
        assert det.is_drifting("test.metric") is False

    def test_no_baseline_returns_false(self):
        det = _make_detector()
        det.observe("other.metric", 999.0)
        assert det.is_drifting("other.metric") is False

    def test_within_threshold_returns_false(self):
        # mean=0.8, std=0.05, threshold_std=3.0 → drift if |mean - 0.8| > 0.15
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        for _ in range(5):
            det.observe("test.metric", 0.8)  # exactly at baseline
        assert det.is_drifting("test.metric") is False

    def test_small_deviation_no_drift(self):
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        # window mean = 0.82, deviation = 0.02, threshold = 0.15 → no drift
        for _ in range(5):
            det.observe("test.metric", 0.82)
        assert det.is_drifting("test.metric") is False

    def test_large_positive_deviation_triggers_drift(self):
        # mean=0.8, std=0.05, threshold_std=3.0 → drift if deviation > 0.15
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        # window mean = 0.97, deviation = 0.17 > 0.15 → drift
        for _ in range(5):
            det.observe("test.metric", 0.97)
        assert det.is_drifting("test.metric") is True

    def test_large_negative_deviation_triggers_drift(self):
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        # window mean = 0.62, deviation = 0.18 > 0.15 → drift
        for _ in range(5):
            det.observe("test.metric", 0.62)
        assert det.is_drifting("test.metric") is True

    def test_zero_std_returns_false(self):
        det = _make_detector(mean=0.8, std=0.0)
        for _ in range(5):
            det.observe("test.metric", 999.0)  # extreme value
        assert det.is_drifting("test.metric") is False

    def test_exactly_at_threshold_boundary_no_drift(self):
        # deviation == threshold_std * std is NOT drifting (strict >)
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        # deviation = exactly 0.15 → not drifting (not strictly greater)
        for _ in range(5):
            det.observe("test.metric", 0.95)  # 0.95 - 0.8 = 0.15
        assert det.is_drifting("test.metric") is False

    def test_just_above_threshold_drifts(self):
        det = _make_detector(mean=0.8, std=0.05, threshold_std=3.0)
        # deviation = 0.151 > 0.15 → drifting
        for _ in range(5):
            det.observe("test.metric", 0.951)
        assert det.is_drifting("test.metric") is True


# ── window_mean helper ────────────────────────────────────────────────────────


class TestWindowMean:
    def test_window_mean_none_when_empty(self):
        det = _make_detector()
        assert det.window_mean("test.metric") is None

    def test_window_mean_correct(self):
        det = _make_detector()
        for v in [0.7, 0.8, 0.9]:
            det.observe("test.metric", v)
        assert det.window_mean("test.metric") == pytest.approx(0.8)


# ── Multiple independent metrics ──────────────────────────────────────────────


class TestMultipleMetrics:
    def test_metrics_are_independent(self):
        det = DriftDetector(
            baselines={
                "metric.a": Baseline(mean=0.8, std=0.05),
                "metric.b": Baseline(mean=0.5, std=0.05),
            },
            window_size=10,
            threshold_std=3.0,
        )
        # metric.a drifts
        for _ in range(5):
            det.observe("metric.a", 0.97)
        # metric.b does not drift
        for _ in range(5):
            det.observe("metric.b", 0.5)

        assert det.is_drifting("metric.a") is True
        assert det.is_drifting("metric.b") is False

    def test_observe_one_metric_does_not_affect_another(self):
        det = DriftDetector(
            baselines={"metric.a": Baseline(mean=0.5, std=0.1)},
            window_size=5,
        )
        det.observe("metric.a", 0.5)
        assert det.window_size("metric.b") == 0


# ── DriftDetector.load ────────────────────────────────────────────────────────


class TestLoad:
    def _write_baselines(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "baselines.json"
        p.write_text(json.dumps(data))
        return p

    def test_load_single_baseline(self, tmp_path):
        p = self._write_baselines(
            tmp_path,
            {"iep1a.geometry_confidence": {"mean": 0.85, "std": 0.05}},
        )
        det = DriftDetector.load(p)
        assert "iep1a.geometry_confidence" in det._baselines
        assert det._baselines["iep1a.geometry_confidence"].mean == pytest.approx(0.85)
        assert det._baselines["iep1a.geometry_confidence"].std == pytest.approx(0.05)

    def test_load_multiple_baselines(self, tmp_path):
        data = {
            "iep1a.geometry_confidence": {"mean": 0.85, "std": 0.05},
            "iep1b.geometry_confidence": {"mean": 0.83, "std": 0.06},
            "iep1c.blur_score": {"mean": 0.7, "std": 0.08},
        }
        p = self._write_baselines(tmp_path, data)
        det = DriftDetector.load(p)
        assert len(det._baselines) == 3

    def test_load_passes_window_size(self, tmp_path):
        p = self._write_baselines(tmp_path, {"m": {"mean": 0.5, "std": 0.1}})
        det = DriftDetector.load(p, window_size=50)
        assert det._window_size == 50

    def test_load_passes_threshold_std(self, tmp_path):
        p = self._write_baselines(tmp_path, {"m": {"mean": 0.5, "std": 0.1}})
        det = DriftDetector.load(p, threshold_std=2.0)
        assert det._threshold_std == 2.0

    def test_load_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            DriftDetector.load(tmp_path / "nonexistent.json")

    def test_load_raises_value_error_for_non_object_json(self, tmp_path):
        p = tmp_path / "baselines.json"
        p.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="top-level must be a JSON object"):
            DriftDetector.load(p)

    def test_load_raises_value_error_for_missing_mean(self, tmp_path):
        p = self._write_baselines(
            tmp_path, {"iep1a.geometry_confidence": {"std": 0.05}}
        )
        with pytest.raises(ValueError, match="missing field"):
            DriftDetector.load(p)

    def test_load_raises_value_error_for_missing_std(self, tmp_path):
        p = self._write_baselines(
            tmp_path, {"iep1a.geometry_confidence": {"mean": 0.85}}
        )
        with pytest.raises(ValueError, match="missing field"):
            DriftDetector.load(p)

    def test_load_raises_value_error_for_non_object_entry(self, tmp_path):
        p = self._write_baselines(
            tmp_path, {"iep1a.geometry_confidence": [0.85, 0.05]}
        )
        with pytest.raises(ValueError, match="must be an object"):
            DriftDetector.load(p)

    def test_loaded_detector_can_detect_drift(self, tmp_path):
        p = self._write_baselines(
            tmp_path,
            {"iep1a.geometry_confidence": {"mean": 0.85, "std": 0.05}},
        )
        det = DriftDetector.load(p, window_size=10, threshold_std=3.0)
        for _ in range(5):
            det.observe("iep1a.geometry_confidence", 0.5)  # far below baseline
        assert det.is_drifting("iep1a.geometry_confidence") is True

    def test_loaded_detector_no_drift_at_baseline(self, tmp_path):
        p = self._write_baselines(
            tmp_path,
            {"iep1a.geometry_confidence": {"mean": 0.85, "std": 0.05}},
        )
        det = DriftDetector.load(p, window_size=10, threshold_std=3.0)
        for _ in range(5):
            det.observe("iep1a.geometry_confidence", 0.85)
        assert det.is_drifting("iep1a.geometry_confidence") is False
