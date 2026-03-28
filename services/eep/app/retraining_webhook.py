"""
services/eep/app/retraining_webhook.py
----------------------------------------
Packet 8.4 — Retraining webhook receiver.

Implements:
  POST /v1/retraining/webhook  — Alertmanager webhook receiver (no auth)

Receives standard Alertmanager v4 webhook payloads, extracts trigger events
from alert labels, and records them in `retraining_triggers` (spec Section
16.3).

Trigger type → persistence_hours (spec Section 16.3):
  escalation_rate_anomaly          → 24 h
  auto_accept_rate_collapse        → 24 h
  structural_agreement_degradation → 48 h
  drift_alert_persistence          → 48 h
  layout_confidence_degradation    → 48 h

Cooldown: 7 days per trigger type.  If a trigger of the same type already has
an active ``cooldown_until`` timestamp in the future, the incoming alert is
acknowledged with 200 but not re-recorded.

Only ``status="firing"`` alerts are processed.  ``status="resolved"`` alerts
are silently acknowledged and ignored.

Trigger records are written with ``status='pending'``.  The retraining worker
(Packet 8.5) is responsible for picking up pending triggers and enqueuing
retraining jobs.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.eep.app.db.models import RetrainingTrigger
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mlops"])

_COOLDOWN_DAYS = 7

# trigger_type → persistence_hours (spec Section 16.3)
_TRIGGER_PERSISTENCE: dict[str, float] = {
    "escalation_rate_anomaly": 24.0,
    "auto_accept_rate_collapse": 24.0,
    "structural_agreement_degradation": 48.0,
    "drift_alert_persistence": 48.0,
    "layout_confidence_degradation": 48.0,
}


# ── Alertmanager payload schemas ─────────────────────────────────────────────


class _AlertLabels(BaseModel):
    """Alert labels from an Alertmanager notification. Extra keys are ignored."""

    model_config = {"extra": "ignore"}

    trigger_type: str | None = None
    metric_name: str | None = None
    # Alertmanager label values are always strings; parsed to float downstream
    metric_value: str | None = None
    threshold_value: str | None = None


class _Alert(BaseModel):
    """Single alert entry in an Alertmanager webhook payload."""

    model_config = {"extra": "ignore"}

    status: str = "firing"
    labels: _AlertLabels = Field(default_factory=_AlertLabels)
    startsAt: str | None = None


class AlertmanagerPayload(BaseModel):
    """Standard Alertmanager webhook body (v4 format)."""

    model_config = {"extra": "ignore"}

    status: str = "firing"
    alerts: list[_Alert] = Field(default_factory=list)


# ── Response schemas ──────────────────────────────────────────────────────────


class TriggerResult(BaseModel):
    """Per-alert processing outcome."""

    trigger_id: str | None
    trigger_type: str
    # "recorded" | "skipped_cooldown" | "skipped_unknown" | "skipped_resolved"
    status: str


class WebhookResponse(BaseModel):
    """Summary returned to Alertmanager after processing a webhook call."""

    processed: int
    results: list[TriggerResult]


# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _parse_float(value: str | None, default: float = 0.0) -> float:
    """Parse a string label value to float; return *default* on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_fired_at(starts_at: str | None, fallback: datetime) -> datetime:
    """Parse an Alertmanager ISO-8601 startsAt string; return *fallback* on failure."""
    if not starts_at:
        return fallback
    try:
        return datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError:
        return fallback


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post(
    "/v1/retraining/webhook",
    response_model=WebhookResponse,
    status_code=200,
    summary="Alertmanager retraining webhook receiver",
)
def retraining_webhook(
    body: AlertmanagerPayload,
    db: Session = Depends(get_session),
) -> WebhookResponse:
    """
    Receive Alertmanager webhook notifications and record retraining triggers.

    Each alert in the payload is processed independently:

    - ``status="resolved"`` alerts are acknowledged and ignored.
    - Alerts with an unknown or absent ``trigger_type`` label are skipped.
    - Alerts whose trigger type is within its 7-day cooldown are skipped.
    - All other firing alerts are recorded in ``retraining_triggers`` with
      ``status='pending'`` and ``cooldown_until = now + 7 days``.

    Always returns 200 so Alertmanager does not retry the delivery.

    **Auth:** none — internal Alertmanager endpoint.
    """
    now = datetime.now(UTC)
    results: list[TriggerResult] = []

    for alert in body.alerts:
        if alert.status != "firing":
            results.append(
                TriggerResult(
                    trigger_id=None,
                    trigger_type=alert.labels.trigger_type or "unknown",
                    status="skipped_resolved",
                )
            )
            continue

        trigger_type = alert.labels.trigger_type
        if not trigger_type or trigger_type not in _TRIGGER_PERSISTENCE:
            logger.warning(
                "retraining_webhook: unknown trigger_type=%r — skipped", trigger_type
            )
            results.append(
                TriggerResult(
                    trigger_id=None,
                    trigger_type=trigger_type or "unknown",
                    status="skipped_unknown",
                )
            )
            continue

        if _is_in_cooldown(db, trigger_type, now):
            logger.info(
                "retraining_webhook: trigger_type=%s active cooldown — skipped", trigger_type
            )
            results.append(
                TriggerResult(
                    trigger_id=None,
                    trigger_type=trigger_type,
                    status="skipped_cooldown",
                )
            )
            continue

        fired_at = _parse_fired_at(alert.startsAt, now)
        cooldown_until = now + timedelta(days=_COOLDOWN_DAYS)

        row = RetrainingTrigger(
            trigger_id=str(uuid.uuid4()),
            trigger_type=trigger_type,
            metric_name=alert.labels.metric_name or trigger_type,
            metric_value=_parse_float(alert.labels.metric_value),
            threshold_value=_parse_float(alert.labels.threshold_value),
            persistence_hours=_TRIGGER_PERSISTENCE[trigger_type],
            fired_at=fired_at,
            cooldown_until=cooldown_until,
            status="pending",
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        logger.info(
            "retraining_webhook: recorded trigger_id=%s trigger_type=%s persistence_h=%.0f",
            row.trigger_id,
            trigger_type,
            row.persistence_hours,
        )
        results.append(
            TriggerResult(
                trigger_id=row.trigger_id,
                trigger_type=trigger_type,
                status="recorded",
            )
        )

    return WebhookResponse(processed=len(body.alerts), results=results)
