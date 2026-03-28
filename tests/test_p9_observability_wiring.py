"""
tests/test_p9_observability_wiring.py
---------------------------------------
Packet 9.5 — Observability wiring tests.

Tests cover:
- drift detected → trigger written (status="pending", correct fields)
- no drift → no trigger (db.add not called)
- cooldown active → trigger NOT written (skipped_cooldown)
- unknown metric (no mapping) → ignored safely, no trigger, no exception
- drift logic failure → observe_and_check returns without raising
- metric → trigger_type mapping correctness (key pairs verified)
- persistence_hours correct per trigger_type
- threshold_value = baseline.mean + threshold_std * baseline.std
- trigger_id is a valid UUID
- multiple observations before drift → trigger written only when drifting
- metric with no baseline → no drift → no trigger

All tests use a mock DB session (same pattern as test_p8_retraining_webhook.py)
and an explicit DriftDetector instance injected via the detector= parameter —
the module singleton is never touched.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from monitoring.drift_detector import Baseline, DriftDetector
from monitoring.drift_observer import (
    _METRIC_TRIGGER_TYPE,
    _TRIGGER_PERSISTENCE,
    observe_and_check,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)

# A baseline where std=0.05, threshold_std=3.0 → tolerance = 0.15
# mean=0.85 → drift fires when window_mean < 0.70 or > 1.00
_METRIC = "iep1a.geometry_confidence"
_MEAN = 0.85
_STD = 0.05
_THRESHOLD_STD = 3.0


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_detector(
    metric: str = _METRIC,
    mean: float = _MEAN,
    std: float = _STD,
    window_size: int = 5,
    threshold_std: float = _THRESHOLD_STD,
    extra_baselines: dict | None = None,
) -> DriftDetector:
    baselines = {metric: Baseline(mean=mean, std=std)}
    if extra_baselines:
        baselines.update(extra_baselines)
    return DriftDetector(baselines=baselines, window_size=window_size, threshold_std=threshold_std)


def _make_session(cooldown_result=None) -> MagicMock:
    """
    Return a mock SQLAlchemy Session.

    cooldown_result=None  → cooldown check returns False (no active cooldown)
    cooldown_result=<obj> → cooldown check returns True (in cooldown)
    """
    session = MagicMock()

    def _query(model):
        q = MagicMock()

        def _filter(*args):
            f = MagicMock()
            f.first.return_value = cooldown_result
            return f

        q.filter = _filter
        return q

    session.query = _query
    session.add = MagicMock()
    session.commit = MagicMock()
    session.refresh = MagicMock()
    return session


def _fill_window_with_drift(detector: DriftDetector, metric: str = _METRIC) -> None:
    """Fill the detector window with a value far below baseline to trigger drift."""
    # window mean = 0.3 → deviation = 0.55 >> 0.15 → drifting
    for _ in range(5):
        detector.observe(metric, 0.3)


# ── Test: drift detected → trigger written ────────────────────────────────────


class TestDriftTriggerWritten:
    def test_trigger_written_when_drifting(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_trigger_status_is_pending(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.status == "pending"

    def test_trigger_id_is_valid_uuid(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        parsed = uuid_mod.UUID(row.trigger_id)  # raises ValueError if invalid
        assert str(parsed) == row.trigger_id

    def test_trigger_type_correct(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.trigger_type == "drift_alert_persistence"

    def test_metric_name_stored(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.metric_name == _METRIC

    def test_metric_value_stored(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.metric_value == pytest.approx(0.3)

    def test_threshold_value_is_baseline_mean_plus_threshold_std_times_std(self):
        # threshold = mean + threshold_std * std = 0.85 + 3.0 * 0.05 = 1.00
        det = _make_detector(mean=0.85, std=0.05, threshold_std=3.0)
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.threshold_value == pytest.approx(0.85 + 3.0 * 0.05)

    def test_persistence_hours_correct_for_drift_alert_persistence(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.persistence_hours == pytest.approx(48.0)

    def test_cooldown_until_is_7_days_from_now(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.cooldown_until == _NOW + timedelta(days=7)

    def test_fired_at_is_now(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.fired_at == _NOW


# ── Test: no drift → no trigger ───────────────────────────────────────────────


class TestNoDrift:
    def test_no_trigger_when_not_drifting(self):
        det = _make_detector()
        db = _make_session()
        # observe at baseline — no drift
        for _ in range(5):
            observe_and_check(_METRIC, _MEAN, db, detector=det)
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_no_trigger_when_window_empty(self):
        det = _make_detector()
        db = _make_session()
        # just one observe at a non-drifting value
        observe_and_check(_METRIC, _MEAN, db, detector=det)
        db.add.assert_not_called()

    def test_no_trigger_before_drift_threshold_crossed(self):
        det = _make_detector()
        db = _make_session()
        # small deviation — within threshold
        for _ in range(5):
            observe_and_check(_METRIC, _MEAN + 0.05, db, detector=det)
        db.add.assert_not_called()


# ── Test: cooldown prevents duplicate trigger ─────────────────────────────────


class TestCooldown:
    def test_no_trigger_when_in_cooldown(self):
        det = _make_detector()
        # db returns a mock row → cooldown is active
        db = _make_session(cooldown_result=MagicMock())
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_trigger_written_when_cooldown_expired(self):
        det = _make_detector()
        # cooldown_result=None → no active cooldown
        db = _make_session(cooldown_result=None)
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(_METRIC, 0.3, db, detector=det)

        db.add.assert_called_once()


# ── Test: unknown metric → ignored safely ─────────────────────────────────────


class TestUnknownMetric:
    def test_unknown_metric_no_exception(self):
        # Metric has a baseline but no trigger_type mapping
        det = DriftDetector(
            baselines={"unmapped.metric": Baseline(mean=0.5, std=0.01)},
            window_size=5,
            threshold_std=3.0,
        )
        db = _make_session()
        # fill window to trigger is_drifting
        for _ in range(5):
            det.observe("unmapped.metric", 5.0)  # far above baseline

        # must not raise
        observe_and_check("unmapped.metric", 5.0, db, detector=det)
        db.add.assert_not_called()

    def test_metric_with_no_baseline_no_exception(self):
        det = DriftDetector(baselines={}, window_size=5)
        db = _make_session()
        # must not raise even though metric is not in baselines
        observe_and_check("some.unknown.metric", 0.5, db, detector=det)
        db.add.assert_not_called()


# ── Test: drift logic failure → request still succeeds ────────────────────────


class TestDriftFailureSafe:
    def test_exception_in_is_drifting_does_not_propagate(self):
        det = _make_detector()
        db = _make_session()
        with patch.object(det, "is_drifting", side_effect=RuntimeError("injected failure")):
            # must not raise
            observe_and_check(_METRIC, 0.3, db, detector=det)
        db.add.assert_not_called()

    def test_exception_in_db_commit_does_not_propagate(self):
        det = _make_detector()
        db = _make_session(cooldown_result=None)
        db.commit.side_effect = Exception("db error")
        _fill_window_with_drift(det)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            # must not raise
            observe_and_check(_METRIC, 0.3, db, detector=det)

    def test_exception_in_cooldown_check_does_not_propagate(self):
        det = _make_detector()
        db = _make_session()
        db.query.side_effect = Exception("db error during cooldown check")
        _fill_window_with_drift(det)

        # must not raise
        observe_and_check(_METRIC, 0.3, db, detector=det)


# ── Test: metric → trigger_type mapping ──────────────────────────────────────


class TestMetricTriggerMapping:
    def test_iep1a_metrics_map_to_drift_alert_persistence(self):
        for metric in [
            "iep1a.geometry_confidence",
            "iep1a.split_detection_rate",
            "iep1a.tta_structural_agreement_rate",
            "iep1a.tta_prediction_variance",
        ]:
            assert _METRIC_TRIGGER_TYPE[metric] == "drift_alert_persistence", metric

    def test_iep1b_metrics_map_to_drift_alert_persistence(self):
        for metric in [
            "iep1b.geometry_confidence",
            "iep1b.split_detection_rate",
            "iep1b.tta_structural_agreement_rate",
            "iep1b.tta_prediction_variance",
        ]:
            assert _METRIC_TRIGGER_TYPE[metric] == "drift_alert_persistence", metric

    def test_iep1c_metrics_map_to_drift_alert_persistence(self):
        for metric in ["iep1c.blur_score", "iep1c.border_score", "iep1c.foreground_coverage"]:
            assert _METRIC_TRIGGER_TYPE[metric] == "drift_alert_persistence", metric

    def test_iep1d_metric_maps_to_drift_alert_persistence(self):
        assert _METRIC_TRIGGER_TYPE["iep1d.rectification_confidence"] == "drift_alert_persistence"

    def test_iep2a_metrics_map_to_layout_confidence_degradation(self):
        for metric in [
            "iep2a.mean_page_confidence",
            "iep2a.region_count",
            "iep2a.class_fraction.text_block",
            "iep2a.class_fraction.title",
            "iep2a.class_fraction.table",
            "iep2a.class_fraction.image",
            "iep2a.class_fraction.caption",
        ]:
            assert _METRIC_TRIGGER_TYPE[metric] == "layout_confidence_degradation", metric

    def test_iep2b_metrics_map_to_layout_confidence_degradation(self):
        for metric in [
            "iep2b.mean_page_confidence",
            "iep2b.region_count",
            "iep2b.class_fraction.text_block",
        ]:
            assert _METRIC_TRIGGER_TYPE[metric] == "layout_confidence_degradation", metric

    def test_eep_structural_agreement_maps_to_structural_agreement_degradation(self):
        assert (
            _METRIC_TRIGGER_TYPE["eep.structural_agreement_rate"]
            == "structural_agreement_degradation"
        )

    def test_eep_layout_consensus_maps_to_layout_confidence_degradation(self):
        assert (
            _METRIC_TRIGGER_TYPE["eep.layout_consensus_confidence"]
            == "layout_confidence_degradation"
        )

    def test_eep_route_fractions_map_to_drift_alert_persistence(self):
        for metric in [
            "eep.geometry_selection_route.accepted_fraction",
            "eep.geometry_selection_route.review_fraction",
            "eep.artifact_validation_route.valid_fraction",
        ]:
            assert _METRIC_TRIGGER_TYPE[metric] == "drift_alert_persistence", metric


# ── Test: persistence_hours per trigger_type ──────────────────────────────────


class TestPersistenceHours:
    def test_drift_alert_persistence_is_48h(self):
        assert _TRIGGER_PERSISTENCE["drift_alert_persistence"] == pytest.approx(48.0)

    def test_structural_agreement_degradation_is_48h(self):
        assert _TRIGGER_PERSISTENCE["structural_agreement_degradation"] == pytest.approx(48.0)

    def test_layout_confidence_degradation_is_48h(self):
        assert _TRIGGER_PERSISTENCE["layout_confidence_degradation"] == pytest.approx(48.0)

    def test_layout_trigger_written_with_correct_persistence(self):
        metric = "iep2a.mean_page_confidence"
        det = DriftDetector(
            baselines={metric: Baseline(mean=0.82, std=0.08)},
            window_size=5,
            threshold_std=3.0,
        )
        db = _make_session(cooldown_result=None)
        # fill window well below baseline → drift
        for _ in range(5):
            det.observe(metric, 0.3)

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(metric, 0.3, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.trigger_type == "layout_confidence_degradation"
        assert row.persistence_hours == pytest.approx(48.0)

    def test_structural_trigger_written_with_correct_persistence(self):
        metric = "eep.structural_agreement_rate"
        det = DriftDetector(
            baselines={metric: Baseline(mean=0.88, std=0.05)},
            window_size=5,
            threshold_std=3.0,
        )
        db = _make_session(cooldown_result=None)
        for _ in range(5):
            det.observe(metric, 0.4)  # far below baseline → drift

        with patch("monitoring.drift_observer.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            observe_and_check(metric, 0.4, db, detector=det)

        row = db.add.call_args[0][0]
        assert row.trigger_type == "structural_agreement_degradation"
        assert row.persistence_hours == pytest.approx(48.0)


# ── Test: drift hook wired into invoke_geometry_services ─────────────────────


class TestGeometryInvocationDriftHook:
    """
    Prove that observe_and_check is called during a real invoke_geometry_services
    execution with the correct metric keys and values extracted from the
    GeometryResponse objects.
    """

    _VALID_RESPONSE = {
        "page_count": 1,
        "pages": [
            {
                "region_id": "page_0",
                "geometry_type": "bbox",
                "corners": None,
                "bbox": [10, 10, 800, 750],
                "confidence": 0.92,
                "page_area_fraction": 0.78,
            }
        ],
        "split_required": False,
        "split_x": None,
        "geometry_confidence": 0.92,
        "tta_structural_agreement_rate": 0.90,
        "tta_prediction_variance": 0.03,
        "tta_passes": 3,
        "uncertainty_flags": [],
        "warnings": [],
        "processing_time_ms": 110.0,
    }

    def _make_backend(self, response_dict: dict | None = None) -> MagicMock:
        from unittest.mock import AsyncMock
        backend = MagicMock()
        backend.call = AsyncMock(return_value=response_dict or self._VALID_RESPONSE)
        return backend

    def _make_cbs(self):
        from services.eep_worker.app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        cfg = CircuitBreakerConfig(failure_threshold=5, reset_timeout_seconds=30.0)
        return CircuitBreaker(cfg), CircuitBreaker(cfg)

    @pytest.mark.asyncio
    async def test_observe_and_check_called_for_iep1a_metrics(self):
        cb_a, cb_b = self._make_cbs()
        backend = self._make_backend()
        db = MagicMock()

        with patch(
            "services.eep_worker.app.geometry_invocation.observe_and_check"
        ) as mock_observe:
            from services.eep_worker.app.geometry_invocation import invoke_geometry_services

            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://bucket/img.jpg",
                material_type="book",
                proxy_width=800,
                proxy_height=760,
                iep1a_endpoint="http://iep1a/v1/geometry",
                iep1b_endpoint="http://iep1b/v1/geometry",
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=db,
            )

        called_metrics = [c.args[0] for c in mock_observe.call_args_list]

        # IEP1A metrics must be observed
        assert "iep1a.geometry_confidence" in called_metrics
        assert "iep1a.tta_structural_agreement_rate" in called_metrics
        assert "iep1a.tta_prediction_variance" in called_metrics
        assert "iep1a.split_detection_rate" in called_metrics

    @pytest.mark.asyncio
    async def test_observe_and_check_called_for_iep1b_metrics(self):
        cb_a, cb_b = self._make_cbs()
        backend = self._make_backend()
        db = MagicMock()

        with patch(
            "services.eep_worker.app.geometry_invocation.observe_and_check"
        ) as mock_observe:
            from services.eep_worker.app.geometry_invocation import invoke_geometry_services

            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://bucket/img.jpg",
                material_type="book",
                proxy_width=800,
                proxy_height=760,
                iep1a_endpoint="http://iep1a/v1/geometry",
                iep1b_endpoint="http://iep1b/v1/geometry",
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=db,
            )

        called_metrics = [c.args[0] for c in mock_observe.call_args_list]
        assert "iep1b.geometry_confidence" in called_metrics
        assert "iep1b.tta_structural_agreement_rate" in called_metrics
        assert "iep1b.tta_prediction_variance" in called_metrics
        assert "iep1b.split_detection_rate" in called_metrics

    @pytest.mark.asyncio
    async def test_observe_and_check_called_for_eep_route_fraction(self):
        cb_a, cb_b = self._make_cbs()
        backend = self._make_backend()
        db = MagicMock()

        with patch(
            "services.eep_worker.app.geometry_invocation.observe_and_check"
        ) as mock_observe:
            from services.eep_worker.app.geometry_invocation import invoke_geometry_services

            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://bucket/img.jpg",
                material_type="book",
                proxy_width=800,
                proxy_height=760,
                iep1a_endpoint="http://iep1a/v1/geometry",
                iep1b_endpoint="http://iep1b/v1/geometry",
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=db,
            )

        called_metrics = [c.args[0] for c in mock_observe.call_args_list]
        assert "eep.geometry_selection_route.accepted_fraction" in called_metrics

    @pytest.mark.asyncio
    async def test_correct_values_passed_to_observe(self):
        """Values extracted from GeometryResponse are fed into observe_and_check."""
        cb_a, cb_b = self._make_cbs()
        backend = self._make_backend()
        db = MagicMock()

        with patch(
            "services.eep_worker.app.geometry_invocation.observe_and_check"
        ) as mock_observe:
            from services.eep_worker.app.geometry_invocation import invoke_geometry_services

            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://bucket/img.jpg",
                material_type="book",
                proxy_width=800,
                proxy_height=760,
                iep1a_endpoint="http://iep1a/v1/geometry",
                iep1b_endpoint="http://iep1b/v1/geometry",
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=db,
            )

        # Build a {metric: value} dict from calls
        observed = {c.args[0]: c.args[1] for c in mock_observe.call_args_list}

        assert observed["iep1a.geometry_confidence"] == pytest.approx(0.92)
        assert observed["iep1a.tta_structural_agreement_rate"] == pytest.approx(0.90)
        assert observed["iep1a.tta_prediction_variance"] == pytest.approx(0.03)
        assert observed["iep1a.split_detection_rate"] == pytest.approx(0.0)  # split_required=False

    @pytest.mark.asyncio
    async def test_iep1a_metrics_not_observed_when_iep1a_fails(self):
        """When IEP1A fails (response=None), its metrics must not be observed."""
        from unittest.mock import AsyncMock
        from shared.gpu.backend import BackendError, BackendErrorKind

        cb_a, cb_b = self._make_cbs()
        backend = MagicMock()

        call_count = {"n": 0}

        async def _side_effect(url, payload):
            call_count["n"] += 1
            if "iep1a" in url:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "iep1a down")
            return self._VALID_RESPONSE

        backend.call = AsyncMock(side_effect=_side_effect)
        db = MagicMock()

        with patch(
            "services.eep_worker.app.geometry_invocation.observe_and_check"
        ) as mock_observe:
            from services.eep_worker.app.geometry_invocation import invoke_geometry_services

            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://bucket/img.jpg",
                material_type="book",
                proxy_width=800,
                proxy_height=760,
                iep1a_endpoint="http://iep1a/v1/geometry",
                iep1b_endpoint="http://iep1b/v1/geometry",
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=db,
            )

        called_metrics = [c.args[0] for c in mock_observe.call_args_list]
        # IEP1A metrics must NOT be observed (response was None)
        assert "iep1a.geometry_confidence" not in called_metrics
        assert "iep1a.tta_structural_agreement_rate" not in called_metrics
        # IEP1B metrics must still be observed
        assert "iep1b.geometry_confidence" in called_metrics
