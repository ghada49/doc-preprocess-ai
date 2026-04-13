"""
monitoring/drift_observer.py
-----------------------------
Packet 9.5 — Observability wiring: runtime metrics → DriftDetector →
retraining_triggers.

This module is the single integration point for drift detection.
Any production code path (EEP task execution, IEP response aggregation)
that collects a monitored metric value calls::

    from monitoring.drift_observer import observe_and_check
    observe_and_check(metric="iep1a.geometry_confidence", value=conf, db=db)

The call:
  1. Feeds ``value`` into the module-level ``DriftDetector`` sliding window.
  2. Checks ``is_drifting(metric)``.
  3. If drifting AND the trigger_type is not in cooldown: writes a row to
     ``retraining_triggers`` with ``status='pending'``.

All drift logic is wrapped in a top-level ``try/except`` so that any
internal failure (corrupt baselines, DB error) never breaks the caller's
request flow.

Metric → trigger_type mapping (spec Section 16.3 + 16.4)
---------------------------------------------------------

IEP1A / IEP1B / IEP1C / IEP1D metrics → ``drift_alert_persistence``
  - "Any IEP1 drift detector alert firing continuously" (spec Section 16.3)

IEP2A / IEP2B metrics → ``layout_confidence_degradation``
  - "Median IEP2A/IEP2B confidence drops >15% from baseline"

``eep.structural_agreement_rate`` → ``structural_agreement_degradation``
  - "IEP1A/IEP1B structural agreement rate drops >20% from baseline"

``eep.layout_consensus_confidence`` → ``layout_confidence_degradation``

EEP geometry_selection_route / artifact_validation_route fractions
  → ``drift_alert_persistence`` (IEP1 routing-quality signals)

Persistence hours (spec Section 16.3 / retraining_webhook.py Packet 8.4)
-------------------------------------------------------------------------
  drift_alert_persistence          → 48 h
  structural_agreement_degradation → 48 h
  layout_confidence_degradation    → 48 h

Cooldown: 7 days per trigger_type (same as Packet 8.4).

Exported:
  observe_and_check  — feed a metric observation; write trigger if drifting
  get_detector       — return the module-level DriftDetector singleton
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from monitoring.drift_detector import DriftDetector
from services.eep.app.db.models import RetrainingTrigger

logger = logging.getLogger(__name__)

__all__ = ["observe_and_check", "get_detector"]

# ── Constants ─────────────────────────────────────────────────────────────────

_COOLDOWN_DAYS = 7

_BASELINES_PATH = Path(__file__).parent / "baselines.json"

# trigger_type → persistence_hours (spec Section 16.3)
_TRIGGER_PERSISTENCE: dict[str, float] = {
    "escalation_rate_anomaly": 24.0,
    "auto_accept_rate_collapse": 24.0,
    "structural_agreement_degradation": 48.0,
    "drift_alert_persistence": 48.0,
    "layout_confidence_degradation": 48.0,
}

# metric key → trigger_type (spec Section 16.3 + 16.4)
_METRIC_TRIGGER_TYPE: dict[str, str] = {
    # ── IEP1A — preprocessing geometry model ──────────────────────────────────
    "iep1a.geometry_confidence": "drift_alert_persistence",
    "iep1a.split_detection_rate": "drift_alert_persistence",
    "iep1a.tta_structural_agreement_rate": "drift_alert_persistence",
    "iep1a.tta_prediction_variance": "drift_alert_persistence",
    # ── IEP1B — preprocessing geometry model ──────────────────────────────────
    "iep1b.geometry_confidence": "drift_alert_persistence",
    "iep1b.split_detection_rate": "drift_alert_persistence",
    "iep1b.tta_structural_agreement_rate": "drift_alert_persistence",
    "iep1b.tta_prediction_variance": "drift_alert_persistence",
    # ── IEP1C — normalization shared module ───────────────────────────────────
    "iep1c.blur_score": "drift_alert_persistence",
    "iep1c.border_score": "drift_alert_persistence",
    "iep1c.foreground_coverage": "drift_alert_persistence",
    # ── IEP1D — rectification fallback ────────────────────────────────────────
    "iep1d.rectification_confidence": "drift_alert_persistence",
    # ── IEP2A — layout detector (primary) ─────────────────────────────────────
    "iep2a.mean_page_confidence": "layout_confidence_degradation",
    "iep2a.region_count": "layout_confidence_degradation",
    "iep2a.class_fraction.text_block": "layout_confidence_degradation",
    "iep2a.class_fraction.title": "layout_confidence_degradation",
    "iep2a.class_fraction.table": "layout_confidence_degradation",
    "iep2a.class_fraction.image": "layout_confidence_degradation",
    "iep2a.class_fraction.caption": "layout_confidence_degradation",
    # ── IEP2B — layout detector (secondary) ───────────────────────────────────
    "iep2b.mean_page_confidence": "layout_confidence_degradation",
    "iep2b.region_count": "layout_confidence_degradation",
    "iep2b.class_fraction.text_block": "layout_confidence_degradation",
    "iep2b.class_fraction.title": "layout_confidence_degradation",
    "iep2b.class_fraction.table": "layout_confidence_degradation",
    "iep2b.class_fraction.image": "layout_confidence_degradation",
    "iep2b.class_fraction.caption": "layout_confidence_degradation",
    # ── EEP — structural agreement (spec Section 16.3) ────────────────────────
    "eep.structural_agreement_rate": "structural_agreement_degradation",
    # ── EEP — layout consensus signal ─────────────────────────────────────────
    "eep.layout_consensus_confidence": "layout_confidence_degradation",
    # ── EEP — IEP1 routing-quality signals ────────────────────────────────────
    "eep.geometry_selection_route.accepted_fraction": "drift_alert_persistence",
    "eep.geometry_selection_route.review_fraction": "drift_alert_persistence",
    "eep.geometry_selection_route.structural_disagreement_fraction": "drift_alert_persistence",
    "eep.geometry_selection_route.sanity_failed_fraction": "drift_alert_persistence",
    "eep.geometry_selection_route.split_confidence_low_fraction": "drift_alert_persistence",
    "eep.geometry_selection_route.tta_variance_high_fraction": "drift_alert_persistence",
    # ── EEP — artifact validation signals ────────────────────────────────────
    "eep.artifact_validation_route.valid_fraction": "drift_alert_persistence",
    "eep.artifact_validation_route.invalid_fraction": "drift_alert_persistence",
    "eep.artifact_validation_route.rectification_triggered_fraction": "drift_alert_persistence",
}

# ── Singleton detector ────────────────────────────────────────────────────────

_detector: DriftDetector | None = None


def get_detector() -> DriftDetector:
    """
    Return the module-level DriftDetector singleton.

    Loaded lazily on first call from ``monitoring/baselines.json``.
    Falls back to an empty-baselines detector (no drift ever fires) if
    the file is absent — this prevents import-time failures in environments
    that do not have the baselines file (e.g. minimal CI configurations).
    """
    global _detector
    if _detector is None:
        try:
            _detector = DriftDetector.load(_BASELINES_PATH)
            logger.info(
                "drift_observer: loaded %d baselines from %s",
                len(_detector._baselines),
                _BASELINES_PATH,
            )
        except FileNotFoundError:
            logger.warning(
                "drift_observer: %s not found — using empty baselines (no drift detection)",
                _BASELINES_PATH,
            )
            _detector = DriftDetector(baselines={})
    return _detector


# ── Public API ────────────────────────────────────────────────────────────────


def observe_and_check(
    metric: str,
    value: float,
    db: Session,
    *,
    detector: DriftDetector | None = None,
) -> None:
    """
    Feed a metric observation into the drift detector and write a retraining
    trigger if drift is detected.

    This function is the single EEP-level integration point for drift
    detection.  Call it once per metric observation at a centralized
    aggregation point (e.g. after receiving an IEP response, or at the end
    of a gate decision).

    The call is entirely best-effort: any internal error (corrupt baselines,
    DB failure) is caught, logged, and silently suppressed so that the
    caller's request flow is never interrupted.

    Args:
        metric   — dot-notation metric key (e.g. ``"iep1a.geometry_confidence"``).
        value    — observed scalar value to record.
        db       — SQLAlchemy Session used for the cooldown check and trigger
                   insert.  The function commits autonomously if a trigger is
                   written.
        detector — Optional DriftDetector override.  When None the module-level
                   singleton is used.  Pass an explicit detector in tests to
                   avoid touching the module singleton.
    """
    try:
        _detector_instance = detector if detector is not None else get_detector()
        _detector_instance.observe(metric, value)

        if not _detector_instance.is_drifting(metric):
            return

        trigger_type = _METRIC_TRIGGER_TYPE.get(metric)
        if trigger_type is None:
            logger.debug(
                "drift_observer: metric %r is drifting but has no trigger_type mapping — ignored",
                metric,
            )
            return

        _maybe_write_trigger(
            metric=metric,
            value=value,
            trigger_type=trigger_type,
            detector=_detector_instance,
            db=db,
        )
    except Exception:
        logger.exception(
            "drift_observer: unhandled error for metric=%r value=%r — suppressed",
            metric,
            value,
        )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_in_cooldown(db: Session, trigger_type: str, now: datetime) -> bool:
    """Return True if *trigger_type* has an active cooldown_until in the future."""
    return (
        db.query(RetrainingTrigger)
        .filter(
            RetrainingTrigger.trigger_type == trigger_type,
            RetrainingTrigger.cooldown_until > now,
        )
        .first()
    ) is not None


def _maybe_write_trigger(
    metric: str,
    value: float,
    trigger_type: str,
    detector: DriftDetector,
    db: Session,
) -> None:
    """
    Write a ``RetrainingTrigger`` row if *trigger_type* is not in cooldown.

    threshold_value is ``baseline.mean + threshold_std × baseline.std``
    (upper drift boundary).  Falls back to 0.0 if the metric has no
    baseline entry.
    """
    now = datetime.now(UTC)

    if _is_in_cooldown(db, trigger_type, now):
        logger.debug(
            "drift_observer: trigger_type=%s in cooldown — skipping trigger write",
            trigger_type,
        )
        return

    baseline = detector._baselines.get(metric)
    if baseline is not None:
        threshold_value = baseline.mean + detector._threshold_std * baseline.std
    else:
        threshold_value = 0.0

    persistence_hours = _TRIGGER_PERSISTENCE.get(trigger_type, 48.0)
    cooldown_until = now + timedelta(days=_COOLDOWN_DAYS)

    row = RetrainingTrigger(
        trigger_id=str(uuid.uuid4()),
        trigger_type=trigger_type,
        metric_name=metric,
        metric_value=float(value),
        threshold_value=threshold_value,
        persistence_hours=persistence_hours,
        fired_at=now,
        cooldown_until=cooldown_until,
        status="pending",
    )
    db.add(row)
    db.commit()

    logger.info(
        "drift_observer: trigger recorded trigger_type=%s metric=%s value=%.4f threshold=%.4f",
        trigger_type,
        metric,
        value,
        threshold_value,
    )
