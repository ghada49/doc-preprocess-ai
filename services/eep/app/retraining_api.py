"""
services/eep/app/retraining_api.py
------------------------------------
Retraining status endpoint for frontend MLOps pages.

Implements:
  GET /v1/retraining/status  — Admin-only summary of retraining pipeline state.

Data sources:
  - retraining_jobs     — active / queued / recently completed retraining runs
  - retraining_triggers — trigger cooldown state by type

Response is shaped for frontend usefulness, not raw DB dumping. The top-level
summary gives counts at a glance; detailed lists power expanded views.

Auth: admin only.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import RetrainingJob, RetrainingTrigger
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mlops"])

# How many completed jobs to include in "recently_completed" list.
_RECENT_COMPLETED_LIMIT = 10

# Jobs completed within this window are considered "recent".
_RECENT_COMPLETED_HOURS = 72


# ── Response schemas ──────────────────────────────────────────────────────────


class RetrainingJobSummary(BaseModel):
    """
    Summary of a single retraining job record.

    Fields:
        job_id           — retraining job identifier
        pipeline_type    — layout_detection | doclayout_yolo | rectification | preprocessing
        status           — pending | running | completed | failed
        trigger_id       — originating trigger (None for manually triggered jobs)
        dataset_version  — dataset version used (None when not set)
        mlflow_run_id    — MLflow run identifier (None when not started)
        result_mAP       — best validation mAP (None when not completed)
        promotion_decision — auto | manual | rejected | None
        started_at       — when the job started (None if not started)
        completed_at     — when the job completed (None if not done)
        error_message    — error detail (None when not failed)
        created_at       — record creation timestamp
    """

    job_id: str
    pipeline_type: str
    status: str
    trigger_id: str | None
    dataset_version: str | None
    mlflow_run_id: str | None
    result_map: float | None = Field(alias="result_mAP")
    promotion_decision: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    created_at: datetime


class TriggerCooldownEntry(BaseModel):
    """
    Cooldown state for one trigger type.

    Fields:
        trigger_type   — trigger type identifier
        in_cooldown    — whether this type is currently blocked by cooldown
        cooldown_until — when the cooldown expires (None if not in cooldown)
        last_fired_at  — timestamp of the most recent trigger of this type
        last_status    — status of the most recent trigger record
    """

    trigger_type: str
    in_cooldown: bool
    cooldown_until: datetime | None
    last_fired_at: datetime | None
    last_status: str | None


class RetrainingStatusSummary(BaseModel):
    """
    Top-level counts for the retraining status page.

    Fields:
        active_count     — jobs in 'running' status
        queued_count     — jobs in 'pending' status
        completed_count  — jobs completed in the last 72 hours
        failed_count     — jobs in 'failed' status (all time, for alerting)
        total_triggers   — all-time recorded triggers
        pending_triggers — triggers in 'pending' status (not yet processed)
    """

    active_count: int
    queued_count: int
    completed_count: int
    failed_count: int
    total_triggers: int
    pending_triggers: int


class RetrainingStatusResponse(BaseModel):
    """
    Full retraining status response for GET /v1/retraining/status.

    Returned data is shaped for the frontend Retraining dashboard page.

    Fields:
        summary           — aggregate counts at a glance
        active_jobs       — jobs currently running
        queued_jobs       — jobs waiting to be picked up
        recently_completed — up to 10 jobs completed in the last 72 hours
        trigger_cooldowns — cooldown state per trigger type
        as_of             — server timestamp when this snapshot was taken
    """

    summary: RetrainingStatusSummary
    active_jobs: list[RetrainingJobSummary]
    queued_jobs: list[RetrainingJobSummary]
    recently_completed: list[RetrainingJobSummary]
    trigger_cooldowns: list[TriggerCooldownEntry]
    as_of: datetime


# ── Helpers ───────────────────────────────────────────────────────────────────


def _job_to_summary(rj: RetrainingJob) -> RetrainingJobSummary:
    return RetrainingJobSummary(
        job_id=rj.job_id,
        pipeline_type=rj.pipeline_type,
        status=rj.status,
        trigger_id=rj.trigger_id,
        dataset_version=rj.dataset_version,
        mlflow_run_id=rj.mlflow_run_id,
        result_mAP=rj.result_mAP,
        promotion_decision=rj.promotion_decision,
        started_at=rj.started_at,
        completed_at=rj.completed_at,
        error_message=rj.error_message,
        created_at=rj.created_at,
    )


def _build_trigger_cooldowns(db: Session, now: datetime) -> list[TriggerCooldownEntry]:
    """
    Build per-trigger-type cooldown summary from the most recent trigger row
    of each type. Returns one entry per known trigger type.
    """
    from services.eep.app.retraining_webhook import _TRIGGER_PERSISTENCE

    entries: list[TriggerCooldownEntry] = []

    for trigger_type in sorted(_TRIGGER_PERSISTENCE.keys()):
        latest: RetrainingTrigger | None = (
            db.query(RetrainingTrigger)
            .filter(RetrainingTrigger.trigger_type == trigger_type)
            .order_by(RetrainingTrigger.fired_at.desc())
            .first()
        )

        if latest is None:
            entries.append(
                TriggerCooldownEntry(
                    trigger_type=trigger_type,
                    in_cooldown=False,
                    cooldown_until=None,
                    last_fired_at=None,
                    last_status=None,
                )
            )
            continue

        cooldown_until = latest.cooldown_until
        in_cooldown = (
            cooldown_until is not None
            and (
                cooldown_until.replace(tzinfo=timezone.utc)
                if cooldown_until.tzinfo is None
                else cooldown_until
            )
            > now
        )

        entries.append(
            TriggerCooldownEntry(
                trigger_type=trigger_type,
                in_cooldown=in_cooldown,
                cooldown_until=cooldown_until,
                last_fired_at=latest.fired_at,
                last_status=latest.status,
            )
        )

    return entries


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get(
    "/v1/retraining/status",
    response_model=RetrainingStatusResponse,
    status_code=200,
    summary="Get retraining pipeline status",
)
def get_retraining_status(
    db: Session = Depends(get_session),
    _caller: CurrentUser = Depends(require_admin),
) -> RetrainingStatusResponse:
    """
    Return current retraining pipeline status for the MLOps dashboard.

    Includes:
    - Active (running) and queued (pending) retraining jobs
    - Recently completed jobs (last 72 hours, up to 10)
    - Cooldown state for each known trigger type
    - Summary counts for at-a-glance status

    **Auth:** admin role required.
    """
    now = datetime.now(timezone.utc)
    recent_threshold = now - timedelta(hours=_RECENT_COMPLETED_HOURS)

    # Active jobs
    active_jobs: list[RetrainingJob] = (
        db.query(RetrainingJob)
        .filter(RetrainingJob.status == "running")
        .order_by(RetrainingJob.started_at.desc())
        .all()
    )

    # Queued (pending) jobs
    queued_jobs: list[RetrainingJob] = (
        db.query(RetrainingJob)
        .filter(RetrainingJob.status == "pending")
        .order_by(RetrainingJob.created_at.asc())
        .all()
    )

    # Recently completed
    recently_completed: list[RetrainingJob] = (
        db.query(RetrainingJob)
        .filter(
            RetrainingJob.status == "completed",
            RetrainingJob.completed_at >= recent_threshold,
        )
        .order_by(RetrainingJob.completed_at.desc())
        .limit(_RECENT_COMPLETED_LIMIT)
        .all()
    )

    # Summary counts
    active_count = len(active_jobs)
    queued_count = len(queued_jobs)
    completed_count = len(recently_completed)
    failed_count: int = db.query(RetrainingJob).filter(RetrainingJob.status == "failed").count()
    total_triggers: int = db.query(RetrainingTrigger).count()
    pending_triggers: int = (
        db.query(RetrainingTrigger).filter(RetrainingTrigger.status == "pending").count()
    )

    summary = RetrainingStatusSummary(
        active_count=active_count,
        queued_count=queued_count,
        completed_count=completed_count,
        failed_count=failed_count,
        total_triggers=total_triggers,
        pending_triggers=pending_triggers,
    )

    trigger_cooldowns = _build_trigger_cooldowns(db, now)

    logger.info(
        "get_retraining_status: active=%d queued=%d completed_recent=%d failed=%d",
        active_count,
        queued_count,
        completed_count,
        failed_count,
    )

    return RetrainingStatusResponse(
        summary=summary,
        active_jobs=[_job_to_summary(j) for j in active_jobs],
        queued_jobs=[_job_to_summary(j) for j in queued_jobs],
        recently_completed=[_job_to_summary(j) for j in recently_completed],
        trigger_cooldowns=trigger_cooldowns,
        as_of=now,
    )
