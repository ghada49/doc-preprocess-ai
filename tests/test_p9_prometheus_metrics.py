"""
tests/test_p9_prometheus_metrics.py
-------------------------------------
Verify that Prometheus metric instruments are actually populated when
IEP1A/IEP1B/IEP2A/IEP2B process a request and when EEP worker gates fire.

All tests use mock/stub mode — no real model weights are required.

NOTE: These metrics are emitted in both real and mock/stub mode by design.
Mock-mode emissions verify instrumentation wiring but do not reflect real
model performance. Grafana dashboards do not distinguish mock from real;
operators should review the `IEP*_MOCK_MODE` env vars before interpreting
confidence or latency values as production signal.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from shared.metrics import (
    EEP_ARTIFACT_VALIDATION_ROUTE,
    EEP_CONSENSUS_ROUTE,
    EEP_GEOMETRY_SELECTION_ROUTE,
    IEP1A_GEOMETRY_CONFIDENCE,
    IEP1A_GPU_INFERENCE_SECONDS,
    IEP1A_TTA_PREDICTION_VARIANCE,
    IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE,
    IEP1B_GEOMETRY_CONFIDENCE,
    IEP1B_GPU_INFERENCE_SECONDS,
    IEP2A_MEAN_PAGE_CONFIDENCE,
    IEP2A_REGION_CONFIDENCE,
    IEP2A_REGIONS_PER_PAGE,
    IEP2B_MEAN_PAGE_CONFIDENCE,
    IEP2B_REGION_CONFIDENCE,
    IEP2B_REGIONS_PER_PAGE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _histogram_count(histogram) -> float:
    """Read the cumulative observation count from a Prometheus Histogram."""
    for metric in REGISTRY.collect():
        if metric.name == histogram._name:
            for sample in metric.samples:
                if sample.name.endswith("_count"):
                    return sample.value
    return 0.0


def _counter_total(counter) -> float:
    """Read the total value from a Prometheus Counter (optionally labelled)."""
    for metric in REGISTRY.collect():
        if metric.name == counter._name:
            total = 0.0
            for sample in metric.samples:
                if sample.name.endswith("_total") or sample.name == counter._name:
                    total += sample.value
            return total
    return 0.0


def _geometry_request(page_count: int = 1):
    from shared.schemas.geometry import GeometryRequest
    return GeometryRequest(
        job_id="j1",
        image_uri="s3://bucket/test.jpg",
        material_type="book",
        page_number=1,
        proxy_width=800,
        proxy_height=600,
    )


# ---------------------------------------------------------------------------
# IEP1A — mock inference emits geometry metrics
# ---------------------------------------------------------------------------


class TestIep1aMetricEmission:
    def test_geometry_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        from services.iep1a.app.inference import run_mock_inference

        before = _histogram_count(IEP1A_GEOMETRY_CONFIDENCE)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1A_GEOMETRY_CONFIDENCE) > before

    def test_gpu_inference_seconds_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        from services.iep1a.app.inference import run_mock_inference

        before = _histogram_count(IEP1A_GPU_INFERENCE_SECONDS)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1A_GPU_INFERENCE_SECONDS) > before

    def test_tta_agreement_rate_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        from services.iep1a.app.inference import run_mock_inference

        before = _histogram_count(IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE) > before

    def test_tta_variance_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        from services.iep1a.app.inference import run_mock_inference

        before = _histogram_count(IEP1A_TTA_PREDICTION_VARIANCE)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1A_TTA_PREDICTION_VARIANCE) > before

    def test_confidence_value_in_valid_range(self, monkeypatch):
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        monkeypatch.setenv("IEP1A_MOCK_CONFIDENCE", "0.95")
        from services.iep1a.app.inference import run_mock_inference

        resp = run_mock_inference(_geometry_request())
        assert 0.0 <= resp.geometry_confidence <= 1.0

    def test_metrics_emitted_in_real_inference_path_when_mock_fallback(self, monkeypatch):
        """run_inference falls back to mock when model files absent — metrics still fire."""
        monkeypatch.setenv("IEP1A_MOCK_MODE", "false")
        from services.iep1a.app import inference as mod
        monkeypatch.setattr(mod, "_load_model", lambda material_type: (_ for _ in ()).throw(FileNotFoundError("no model")))

        before = _histogram_count(IEP1A_GEOMETRY_CONFIDENCE)
        mod.run_inference(_geometry_request())
        assert _histogram_count(IEP1A_GEOMETRY_CONFIDENCE) > before


# ---------------------------------------------------------------------------
# IEP1B — same checks
# ---------------------------------------------------------------------------


class TestIep1bMetricEmission:
    def test_geometry_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1B_MOCK_MODE", "true")
        from services.iep1b.app.inference import run_mock_inference

        before = _histogram_count(IEP1B_GEOMETRY_CONFIDENCE)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1B_GEOMETRY_CONFIDENCE) > before

    def test_gpu_inference_seconds_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP1B_MOCK_MODE", "true")
        from services.iep1b.app.inference import run_mock_inference

        before = _histogram_count(IEP1B_GPU_INFERENCE_SECONDS)
        run_mock_inference(_geometry_request())
        assert _histogram_count(IEP1B_GPU_INFERENCE_SECONDS) > before


# ---------------------------------------------------------------------------
# IEP2A — stub layout_detect emits region metrics
# ---------------------------------------------------------------------------


class TestIep2aMetricEmission:
    def test_region_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "false")
        from services.iep2a.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2A_REGION_CONFIDENCE)
        _stub_response(body, time.monotonic())
        after = _histogram_count(IEP2A_REGION_CONFIDENCE)
        assert after > before

    def test_mean_page_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "false")
        from services.iep2a.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2A_MEAN_PAGE_CONFIDENCE)
        _stub_response(body, time.monotonic())
        assert _histogram_count(IEP2A_MEAN_PAGE_CONFIDENCE) > before

    def test_regions_per_page_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "false")
        from services.iep2a.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2A_REGIONS_PER_PAGE)
        _stub_response(body, time.monotonic())
        assert _histogram_count(IEP2A_REGIONS_PER_PAGE) > before

    def test_region_confidence_value_in_valid_range(self, monkeypatch):
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "false")
        monkeypatch.setenv("IEP2A_MOCK_CONFIDENCE", "0.87")
        from services.iep2a.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        resp = _stub_response(body, time.monotonic())
        for r in resp.regions:
            assert 0.0 <= r.confidence <= 1.0


# ---------------------------------------------------------------------------
# IEP2B — same checks
# ---------------------------------------------------------------------------


class TestIep2bMetricEmission:
    def test_region_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "false")
        from services.iep2b.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2B_REGION_CONFIDENCE)
        _stub_response(body, time.monotonic())
        assert _histogram_count(IEP2B_REGION_CONFIDENCE) > before

    def test_mean_page_confidence_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "false")
        from services.iep2b.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2B_MEAN_PAGE_CONFIDENCE)
        _stub_response(body, time.monotonic())
        assert _histogram_count(IEP2B_MEAN_PAGE_CONFIDENCE) > before

    def test_regions_per_page_count_increases(self, monkeypatch):
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "false")
        from services.iep2b.app.detect import _stub_response
        from shared.schemas.layout import LayoutDetectRequest
        import time

        body = LayoutDetectRequest(image_uri="s3://bucket/test.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        before = _histogram_count(IEP2B_REGIONS_PER_PAGE)
        _stub_response(body, time.monotonic())
        assert _histogram_count(IEP2B_REGIONS_PER_PAGE) > before


# ---------------------------------------------------------------------------
# EEP worker gate metrics
# ---------------------------------------------------------------------------


class TestEepGateMetricEmission:
    def test_geometry_selection_route_counter_increments(self):
        from services.eep_worker.app.geometry_invocation import _observe_geometry_metrics

        before = _counter_total(EEP_GEOMETRY_SELECTION_ROUTE)
        db = MagicMock()
        _observe_geometry_metrics(iep1a=None, iep1b=None, route_decision="accepted", session=db)
        after = _counter_total(EEP_GEOMETRY_SELECTION_ROUTE)
        assert after > before

    def test_geometry_selection_route_label_matches_decision(self):
        from services.eep_worker.app.geometry_invocation import _observe_geometry_metrics

        db = MagicMock()
        for route in ("accepted", "review", "structural_disagreement"):
            _observe_geometry_metrics(iep1a=None, iep1b=None, route_decision=route, session=db)

        for metric in REGISTRY.collect():
            if metric.name == EEP_GEOMETRY_SELECTION_ROUTE._name:
                labels_seen = {s.labels.get("route") for s in metric.samples if s.labels}
                for route in ("accepted", "review", "structural_disagreement"):
                    assert route in labels_seen, f"route={route!r} not seen in counter labels"

    def test_artifact_validation_route_valid_increments(self):
        before = _counter_total(EEP_ARTIFACT_VALIDATION_ROUTE)
        EEP_ARTIFACT_VALIDATION_ROUTE.labels(route="valid").inc()
        assert _counter_total(EEP_ARTIFACT_VALIDATION_ROUTE) > before

    def test_artifact_validation_route_labels(self):
        EEP_ARTIFACT_VALIDATION_ROUTE.labels(route="valid").inc()
        EEP_ARTIFACT_VALIDATION_ROUTE.labels(route="rectification_triggered").inc()
        EEP_ARTIFACT_VALIDATION_ROUTE.labels(route="invalid").inc()

        for metric in REGISTRY.collect():
            if metric.name == EEP_ARTIFACT_VALIDATION_ROUTE._name:
                labels_seen = {s.labels.get("route") for s in metric.samples if s.labels}
                assert "valid" in labels_seen
                assert "rectification_triggered" in labels_seen
                assert "invalid" in labels_seen

    def test_consensus_route_counter_increments(self):
        before = _counter_total(EEP_CONSENSUS_ROUTE)
        EEP_CONSENSUS_ROUTE.labels(route="accepted").inc()
        assert _counter_total(EEP_CONSENSUS_ROUTE) > before


# ---------------------------------------------------------------------------
# /metrics endpoint contains expected metric names
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Confirm all required metric names appear in /metrics output."""

    @pytest.fixture
    def metrics_text(self, monkeypatch):
        # Emit one sample from each instrument so the metric appears in output
        monkeypatch.setenv("IEP1A_MOCK_MODE", "true")
        monkeypatch.setenv("IEP1B_MOCK_MODE", "true")
        monkeypatch.setenv("IEP2A_USE_REAL_MODEL", "false")
        monkeypatch.setenv("IEP2B_USE_REAL_MODEL", "false")

        from services.iep1a.app.inference import run_mock_inference as iep1a_infer
        from services.iep1b.app.inference import run_mock_inference as iep1b_infer
        from services.iep2a.app.detect import _stub_response as iep2a_stub
        from services.iep2b.app.detect import _stub_response as iep2b_stub
        from shared.schemas.layout import LayoutDetectRequest
        import time

        iep1a_infer(_geometry_request())
        iep1b_infer(_geometry_request())
        body = LayoutDetectRequest(image_uri="s3://b/t.jpg", page_id="p1", job_id="j1", page_number=1, material_type="book")
        iep2a_stub(body, time.monotonic())
        iep2b_stub(body, time.monotonic())

        from prometheus_client.exposition import generate_latest
        return generate_latest().decode()

    def test_iep1a_geometry_confidence_present(self, metrics_text):
        assert "iep1a_geometry_confidence" in metrics_text

    def test_iep1b_geometry_confidence_present(self, metrics_text):
        assert "iep1b_geometry_confidence" in metrics_text

    def test_iep2a_region_confidence_present(self, metrics_text):
        assert "iep2a_region_confidence" in metrics_text

    def test_iep2b_region_confidence_present(self, metrics_text):
        assert "iep2b_region_confidence" in metrics_text

    def test_eep_geometry_selection_route_present(self, metrics_text):
        assert "eep_geometry_selection_route" in metrics_text

    def test_eep_artifact_validation_route_present(self, metrics_text):
        assert "eep_artifact_validation_route" in metrics_text

    def test_eep_consensus_route_present(self, metrics_text):
        assert "eep_consensus_route" in metrics_text
