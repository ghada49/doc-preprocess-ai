"""
services/eep/app/promotion_api.py
-----------------------------------
Packet 8.3 — Model promotion and rollback endpoints.

Implements:
  POST /v1/models/promote   — promote IEP1 staging candidate (admin only)
  POST /v1/models/rollback  — roll back to most recent archived version (admin only)

Both endpoints are restricted to IEP1 services (iep1a, iep1b) per spec
Section 16.5: "IEP2 is excluded from the automated promotion pipeline."

--- POST /v1/models/promote ---

Finds the staging candidate for the requested service and promotes it to
production.  Gate check is enforced by default (force=false).

Gate check (force=false):
  Reads pre-computed gate results from model_versions.gate_results (JSON).
  Written there by the offline evaluation worker (Packet 8.5).
  Any gate with pass=false blocks promotion; returns 409 with the list of
  failed gates.
  If gate_results is absent (evaluation not run): 409.

Force promotion (force=true):
  Skips gate check.  Appended to notes as "[force-promoted by <user_id>]".
  Available for admin override after offline gate review.

On success:
  - staging candidate → stage='production', promoted_at=now()
  - current production (if any) → stage='archived'
  - Redis PUBLISH libraryai:model_reload:{service} (best-effort)
  - MLflow stage transition: logged but not executed (MLflow client not yet
    wired; Packet 8.5 will add mlflow_run_id tracking — stub only)

--- POST /v1/models/rollback ---

Restores the most recently archived version for the requested service to
production.  The currently production model is demoted to archived.

Reason semantics:
  "manual": no window restriction (admin-initiated).
  Any other value (automated Alertmanager path): 409 if current production
  was promoted more than 2 hours ago (spec Section 16.5).

On success:
  - most recent archived → stage='production', promoted_at=now()
  - current production → stage='archived'
  - Redis PUBLISH libraryai:model_reload:{service} (best-effort)

--- Error responses ---
  400 — service not iep1a or iep1b
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role
  404 — no staging candidate (promote) / no archived version (rollback)
  409 — gate check failed (promote) / automated rollback window expired (rollback)

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import ModelVersion
from services.eep.app.db.session import get_session
from services.eep.app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mlops"])

_SUPPORTED_SERVICES = frozenset({"iep1a", "iep1b"})
_ROLLBACK_WINDOW_HOURS = 2


# ── Request / response schemas ─────────────────────────────────────────────────


class PromoteRequest(BaseModel):
    """Request body for POST /v1/models/promote."""

    service: str
    force: bool = False

    @field_validator("service")
    @classmethod
    def _validate_service(cls, v: str) -> str:
        if v not in _SUPPORTED_SERVICES:
            raise ValueError(f"service must be one of {sorted(_SUPPORTED_SERVICES)}")
        return v


class RollbackRequest(BaseModel):
    """Request body for POST /v1/models/rollback."""

    service: str
    reason: str = "manual"

    @field_validator("service")
    @classmethod
    def _validate_service(cls, v: str) -> str:
        if v not in _SUPPORTED_SERVICES:
            raise ValueError(f"service must be one of {sorted(_SUPPORTED_SERVICES)}")
        return v


class ModelVersionRecord(BaseModel):
    """Subset of model_versions fields returned by promote/rollback endpoints."""

    model_id: str
    service_name: str
    version_tag: str
    stage: str
    gate_results: Any
    promoted_at: datetime | None
    notes: str | None
    mlflow_run_id: str | None
    dataset_version: str | None
    created_at: datetime


# ── Helpers ────────────────────────────────────────────────────────────────────


def _to_record(mv: ModelVersion) -> ModelVersionRecord:
    return ModelVersionRecord(
        model_id=mv.model_id,
        service_name=mv.service_name,
        version_tag=mv.version_tag,
        stage=mv.stage,
        gate_results=mv.gate_results,
        promoted_at=mv.promoted_at,
        notes=mv.notes,
        mlflow_run_id=mv.mlflow_run_id,
        dataset_version=mv.dataset_version,
        created_at=mv.created_at,
    )


def _staging_candidate(db: Session, service: str) -> ModelVersion | None:
    """Return the most recently created staging model for *service*, or None."""
    return (
        db.query(ModelVersion)
        .filter(
            ModelVersion.service_name == service,
            ModelVersion.stage == "staging",
        )
        .order_by(ModelVersion.created_at.desc())
        .first()
    )


def _current_production(db: Session, service: str) -> ModelVersion | None:
    """Return the current production model for *service*, or None."""
    return (
        db.query(ModelVersion)
        .filter(
            ModelVersion.service_name == service,
            ModelVersion.stage == "production",
        )
        .first()
    )


def _latest_archived(db: Session, service: str) -> ModelVersion | None:
    """Return the most recently archived model for *service*, or None."""
    return (
        db.query(ModelVersion)
        .filter(
            ModelVersion.service_name == service,
            ModelVersion.stage == "archived",
        )
        .order_by(ModelVersion.promoted_at.desc())
        .first()
    )


def _check_gates(gate_results: Any) -> list[str]:
    """
    Return a list of gate names that failed (pass=false).

    gate_results is a JSON dict written by the offline evaluation worker.
    Each entry is expected to be a dict with at least a "pass" key (bool).
    Any gate where pass=False is a failure.
    """
    if not gate_results or not isinstance(gate_results, dict):
        return ["<all gates — no evaluation results present>"]
    failed: list[str] = []
    for gate_name, result in gate_results.items():
        if isinstance(result, dict) and not result.get("pass", True):
            failed.append(gate_name)
    return failed


def _publish_reload_signal(r: redis_lib.Redis, service: str, version_tag: str) -> None:
    """
    Publish model reload signal to Redis (best-effort).

    Channel: libraryai:model_reload:{service}
    Message: version_tag
    Errors are logged but never raised so that Redis unavailability does not
    fail a promotion or rollback.
    """
    channel = f"libraryai:model_reload:{service}"
    try:
        r.publish(channel, version_tag)
        logger.info("_publish_reload_signal: channel=%s version=%s", channel, version_tag)
    except redis_lib.RedisError as exc:
        logger.error(
            "_publish_reload_signal: failed to publish to %s — %s", channel, exc
        )


def _stub_mlflow_transition(
    model_id: str,
    mlflow_run_id: str | None,
    from_stage: str,
    to_stage: str,
) -> None:
    """
    Log MLflow stage transition intent.

    MLflow client is not installed in the current environment (Packet 8.5 will
    wire up mlflow_run_id tracking and full MLflow integration).  This function
    records the intent for observability and is a no-op stub until then.
    """
    logger.info(
        "_stub_mlflow_transition: model_id=%s mlflow_run_id=%s %s→%s (STUB — mlflow not wired)",
        model_id,
        mlflow_run_id,
        from_stage,
        to_stage,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post(
    "/v1/models/promote",
    response_model=ModelVersionRecord,
    status_code=200,
    summary="Promote IEP1 staging candidate to production",
)
def promote_model(
    body: PromoteRequest,
    db: Session = Depends(get_session),
    r: redis_lib.Redis = Depends(get_redis),
    caller: CurrentUser = Depends(require_admin),
) -> ModelVersionRecord:
    """
    Promote the staging candidate for ``service`` to production.

    With ``force=false`` (default): re-checks all offline evaluation gate
    results stored in ``model_versions.gate_results``; returns 409 if any gate
    fails or if no evaluation results are available.

    With ``force=true``: skips gate check (logged as forced).

    On success: staging→production, current production→archived,
    Redis reload signal published (best-effort).

    **Auth:** admin role required.
    """
    candidate = _staging_candidate(db, body.service)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No staging candidate found for service '{body.service}'",
        )

    if not body.force:
        failed_gates = _check_gates(candidate.gate_results)
        if failed_gates:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Promotion blocked: gate check failed for '{body.service}' "
                    f"candidate {candidate.version_tag!r}. "
                    f"Failed gates: {failed_gates}. "
                    "Use force=true to override after offline review."
                ),
            )

    now = datetime.now(UTC)

    # Demote current production → archived
    current_prod = _current_production(db, body.service)
    if current_prod is not None:
        current_prod.stage = "archived"
        _stub_mlflow_transition(
            current_prod.model_id,
            current_prod.mlflow_run_id,
            "production",
            "archived",
        )

    # Promote staging → production
    force_note = (
        f" [force-promoted by {caller.user_id}]" if body.force else ""
    )
    candidate.stage = "production"
    candidate.promoted_at = now
    candidate.notes = (candidate.notes or "") + force_note if body.force else candidate.notes
    _stub_mlflow_transition(
        candidate.model_id,
        candidate.mlflow_run_id,
        "staging",
        "production",
    )

    db.commit()
    db.refresh(candidate)

    _publish_reload_signal(r, body.service, candidate.version_tag)

    logger.info(
        "promote_model: service=%s version=%s force=%s promoted_by=%s",
        body.service,
        candidate.version_tag,
        body.force,
        caller.user_id,
    )
    return _to_record(candidate)


@router.post(
    "/v1/models/rollback",
    response_model=ModelVersionRecord,
    status_code=200,
    summary="Roll back to the most recently archived model version",
)
def rollback_model(
    body: RollbackRequest,
    db: Session = Depends(get_session),
    r: redis_lib.Redis = Depends(get_redis),
    caller: CurrentUser = Depends(require_admin),
) -> ModelVersionRecord:
    """
    Restore the most recently archived model version for ``service``.

    **Manual rollback** (``reason="manual"``): no window restriction.

    **Automated rollback** (any other ``reason``): 409 if the current
    production model was promoted more than 2 hours ago.

    On success: archived→production, current production→archived,
    Redis reload signal published (best-effort).

    **Auth:** admin role required.
    """
    current_prod = _current_production(db, body.service)
    archived = _latest_archived(db, body.service)

    if archived is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No archived version found for service '{body.service}'",
        )

    # Automated path: enforce 2-hour window
    if body.reason != "manual":
        if current_prod is None or current_prod.promoted_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Automated rollback requires a production model with a known "
                    "promotion timestamp"
                ),
            )
        cutoff = datetime.now(UTC) - timedelta(hours=_ROLLBACK_WINDOW_HOURS)
        promoted_at = current_prod.promoted_at
        # Ensure timezone-aware comparison
        if promoted_at.tzinfo is None:
            promoted_at = promoted_at.replace(tzinfo=UTC)
        if promoted_at < cutoff:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Automated rollback window expired: current production was promoted "
                    f"more than {_ROLLBACK_WINDOW_HOURS}h ago. Use reason='manual' to override."
                ),
            )

    now = datetime.now(UTC)

    # Demote current production → archived
    if current_prod is not None:
        current_prod.stage = "archived"
        _stub_mlflow_transition(
            current_prod.model_id,
            current_prod.mlflow_run_id,
            "production",
            "archived",
        )

    # Restore archived → production
    archived.stage = "production"
    archived.promoted_at = now
    _stub_mlflow_transition(
        archived.model_id,
        archived.mlflow_run_id,
        "archived",
        "production",
    )

    db.commit()
    db.refresh(archived)

    _publish_reload_signal(r, body.service, archived.version_tag)

    logger.info(
        "rollback_model: service=%s restored_version=%s reason=%s by=%s",
        body.service,
        archived.version_tag,
        body.reason,
        caller.user_id,
    )
    return _to_record(archived)
