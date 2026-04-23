"""Unit tests for preprocessing golden gate merge (IEP1A + IEP1B)."""

from __future__ import annotations

from services.retraining_worker.app import golden_gate_merge as ggm
from services.retraining_worker.app.golden_gate_merge import (
    _aggregate_gate_results,
    merge_iep1a_iep1b_gate_results,
)


def test_cross_model_agreement_perfect_overlap() -> None:
    assert ggm._cross_model_agreement_score([[0.0, 0.0, 0.5, 0.5]], [[0.0, 0.0, 0.5, 0.5]]) == 1.0


def test_merge_all_pass() -> None:
    iep1a = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": True, "regressions": 0},
    }
    iep1b = {
        "geometry_iou": {"pass": True, "value": 0.85},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "keypoint_distance": {"pass": True, "value": 0.01},
        "golden_dataset": {"pass": True, "regressions": 0},
    }
    merged = merge_iep1a_iep1b_gate_results(iep1a, iep1b)
    assert set(merged.keys()) == {
        "geometry_iou",
        "split_precision",
        "structural_agreement_rate",
        "golden_dataset",
        "latency_p95",
    }
    assert merged["geometry_iou"]["pass"] is True
    assert merged["geometry_iou"]["value"] == 0.85
    assert merged["split_precision"]["pass"] is True
    assert merged["golden_dataset"]["pass"] is True
    assert merged["golden_dataset"]["regressions"] == 0


def test_merge_geometry_fail_if_either_fails() -> None:
    iep1a = {"geometry_iou": {"pass": False, "value": 0.5}, "split_detection_rate": {"pass": True, "value": 1.0}, "golden_dataset": {"pass": True, "regressions": 0}}
    iep1b = {"geometry_iou": {"pass": True, "value": 0.9}, "split_detection_rate": {"pass": True, "value": 1.0}, "golden_dataset": {"pass": True, "regressions": 0}}
    merged = merge_iep1a_iep1b_gate_results(iep1a, iep1b)
    assert merged["geometry_iou"]["pass"] is False


def test_merge_accepts_measured_structural_and_latency() -> None:
    iep1a = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": True, "regressions": 0},
    }
    iep1b = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": True, "regressions": 0},
    }
    merged = merge_iep1a_iep1b_gate_results(
        iep1a,
        iep1b,
        structural_agreement_rate={"pass": False, "value": 0.4},
        latency_p95={"pass": True, "value": 2.5},
    )
    assert merged["structural_agreement_rate"] == {"pass": False, "value": 0.4}
    assert merged["latency_p95"] == {"pass": True, "value": 2.5}


def test_aggregate_gate_results_min_value_and_sum_regressions() -> None:
    a = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "golden_dataset": {"pass": True, "regressions": 1},
    }
    b = {
        "geometry_iou": {"pass": True, "value": 0.7},
        "golden_dataset": {"pass": False, "regressions": 2},
    }
    agg = _aggregate_gate_results([a, b])
    assert agg["geometry_iou"]["pass"] is True
    assert agg["geometry_iou"]["value"] == 0.7
    assert agg["golden_dataset"]["pass"] is False
    assert agg["golden_dataset"]["regressions"] == 3


def test_aggregate_gate_results_pass_false_if_any_material_fails() -> None:
    a = {"geometry_iou": {"pass": True, "value": 0.9}}
    b = {"geometry_iou": {"pass": False, "value": 0.95}}
    agg = _aggregate_gate_results([a, b])
    assert agg["geometry_iou"]["pass"] is False
    assert agg["geometry_iou"]["value"] == 0.9


def test_merge_regressions_sum() -> None:
    iep1a = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": False, "regressions": 2},
    }
    iep1b = {
        "geometry_iou": {"pass": True, "value": 0.9},
        "split_detection_rate": {"pass": True, "value": 1.0},
        "golden_dataset": {"pass": True, "regressions": 1},
    }
    merged = merge_iep1a_iep1b_gate_results(iep1a, iep1b)
    assert merged["golden_dataset"]["regressions"] == 3
    assert merged["golden_dataset"]["pass"] is False
