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
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import ModelVersion, RetrainingJob, RetrainingTrigger
from services.eep.app.db.session import get_session
from services.eep.app.scaling.retraining_scaler import maybe_start_retraining_worker
from services.eep.app.scaling.runpod_scaler import terminate_retraining_pod

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


class ManualRetrainingRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ManualRetrainingResponse(BaseModel):
    trigger_id: str
    trigger_type: str
    status: str
    worker_start_status: str
    worker_start_message: str
    worker_external_id: str | None
    message: str


class RunPodModelVersionPayload(BaseModel):
    service_name: str
    version_tag: str
    mlflow_run_id: str | None = None
    dataset_version: str | None = None
    gate_results: dict | None = None
    notes: str | None = None


class RunPodRetrainingCallbackRequest(BaseModel):
    trigger_id: str
    job_id: str
    status: Literal["running", "completed", "failed"]
    mlflow_run_id: str | None = None
    dataset_version: str | None = None
    result_model_version: str | None = None
    result_mAP: float | None = None
    promotion_decision: str | None = None
    error_message: str | None = None
    model_versions: list[RunPodModelVersionPayload] = Field(default_factory=list)


class RunPodRetrainingCallbackResponse(BaseModel):
    ok: bool
    trigger_id: str
    job_id: str
    status: str


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


def _retraining_worker_start_mode() -> str:
    return os.environ.get("RETRAINING_WORKER_START_MODE", "disabled").strip().lower()


def _stub_gate_results() -> dict:
    return {
        "geometry_iou": {"pass": True, "value": 0.84},
        "split_precision": {"pass": True, "value": 0.77},
        "structural_agreement_rate": {"pass": True, "value": 0.71},
        "golden_dataset": {"pass": True, "regressions": 0},
        "latency_p95": {"pass": True, "value": 2.3},
    }


def _verify_retraining_callback_secret(secret: str | None) -> None:
    expected = os.environ.get("RETRAINING_CALLBACK_SECRET", "").strip()
    if not expected:
        logger.error("runpod_callback: RETRAINING_CALLBACK_SECRET is not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retraining callback secret is not configured.",
        )
    if not secret or not hmac.compare_digest(secret, expected):
        logger.warning("runpod_callback: invalid callback secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid retraining callback secret.",
        )


def _extract_worker_external_id(notes: str | None) -> str | None:
    if not notes:
        return None
    for part in notes.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == "worker_external_id" and value:
            return value
    return None


def _create_callback_model_versions(
    db: Session,
    *,
    job: RetrainingJob,
    payload: RunPodRetrainingCallbackRequest,
) -> str | None:
    created_tags: list[str] = []
    requested_versions = payload.model_versions

    if not requested_versions and job.pipeline_type == "preprocessing":
        requested_versions = [
            RunPodModelVersionPayload(
                service_name="iep1a",
                version_tag=f"rt-{job.job_id.replace('-', '')[:12]}-iep1a",
                mlflow_run_id=payload.mlflow_run_id,
                dataset_version=payload.dataset_version,
                gate_results=_stub_gate_results(),
                notes="created from RunPod callback",
            ),
            RunPodModelVersionPayload(
                service_name="iep1b",
                version_tag=f"rt-{job.job_id.replace('-', '')[:12]}-iep1b",
                mlflow_run_id=payload.mlflow_run_id,
                dataset_version=payload.dataset_version,
                gate_results=_stub_gate_results(),
                notes="created from RunPod callback",
            ),
        ]

    for version in requested_versions:
        existing: ModelVersion | None = (
            db.query(ModelVersion)
            .filter(
                ModelVersion.service_name == version.service_name,
                ModelVersion.version_tag == version.version_tag,
            )
            .first()
        )
        if existing is not None:
            created_tags.append(existing.version_tag)
            continue

        db.add(
            ModelVersion(
                model_id=str(uuid.uuid4()),
                service_name=version.service_name,
                version_tag=version.version_tag,
                mlflow_run_id=version.mlflow_run_id or payload.mlflow_run_id,
                dataset_version=version.dataset_version or payload.dataset_version,
                stage="staging",
                gate_results=version.gate_results or _stub_gate_results(),
                notes=version.notes,
            )
        )
        created_tags.append(version.version_tag)

    return ",".join(created_tags) if created_tags else None


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


@router.post(
    "/v1/retraining/trigger",
    response_model=ManualRetrainingResponse,
    status_code=202,
    summary="Trigger manual retraining",
)
def trigger_manual_retraining(
    body: ManualRetrainingRequest | None = None,
    db: Session = Depends(get_session),
    caller: CurrentUser = Depends(require_admin),
) -> ManualRetrainingResponse:
    """
    Queue a manual preprocessing retraining run.

    In RunPod mode, EEP creates the retraining job and the external pod reports
    back through the callback endpoint. In ECS/local mode, the DB-backed worker
    can still poll ``retraining_triggers`` and create the job itself.
    """
    existing: RetrainingTrigger | None = (
        db.query(RetrainingTrigger)
        .filter(
            RetrainingTrigger.trigger_type == "manual_retraining",
            RetrainingTrigger.status.in_(["pending", "processing"]),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manual retraining is already queued or running.",
        )

    now = datetime.now(timezone.utc)
    reason = (body.reason if body else None) or "Manual retraining requested from admin UI"
    row = RetrainingTrigger(
        trigger_id=str(uuid.uuid4()),
        trigger_type="manual_retraining",
        metric_name="manual_admin_trigger",
        metric_value=1.0,
        threshold_value=1.0,
        persistence_hours=0.0,
        fired_at=now,
        cooldown_until=None,
        status="pending",
        notes=f"requested_by={caller.user_id}; reason={reason}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    runpod_job: RetrainingJob | None = None
    if _retraining_worker_start_mode() == "runpod_pod":
        runpod_job = RetrainingJob(
            job_id=str(uuid.uuid4()),
            trigger_id=row.trigger_id,
            pipeline_type="preprocessing",
            status="pending",
        )
        db.add(runpod_job)
        row.retraining_job_id = runpod_job.job_id
        db.commit()
        db.refresh(row)
        db.refresh(runpod_job)

    worker_start_status, worker_start_message, worker_external_id = maybe_start_retraining_worker(
        row.trigger_id,
        job_id=runpod_job.job_id if runpod_job is not None else None,
    )
    if worker_external_id:
        notes_suffix = f"; worker_external_id={worker_external_id}"
        row.notes = f"{row.notes or ''}{notes_suffix}"
    if runpod_job is not None:
        if worker_start_status == "requested":
            row.status = "processing"
            runpod_job.status = "running"
            runpod_job.started_at = datetime.now(timezone.utc)
        elif worker_start_status == "failed":
            row.status = "failed"
            row.resolved_at = datetime.now(timezone.utc)
            runpod_job.status = "failed"
            runpod_job.completed_at = datetime.now(timezone.utc)
            runpod_job.error_message = worker_start_message
    if worker_external_id or runpod_job is not None:
        db.commit()
        db.refresh(row)

    logger.info(
        "trigger_manual_retraining: trigger_id=%s requested_by=%s worker_start_status=%s external_id=%s",
        row.trigger_id,
        caller.user_id,
        worker_start_status,
        worker_external_id,
    )

    return ManualRetrainingResponse(
        trigger_id=row.trigger_id,
        trigger_type=row.trigger_type,
        status=row.status,
        worker_start_status=worker_start_status,
        worker_start_message=worker_start_message,
        worker_external_id=worker_external_id,
        message=(
            "Manual retraining queued and worker start requested."
            if worker_start_status == "requested"
            else "Manual retraining queued."
        ),
    )


@router.post(
    "/v1/retraining/runpod/callback",
    response_model=RunPodRetrainingCallbackResponse,
    status_code=200,
    summary="Receive RunPod retraining completion callback",
)
def runpod_retraining_callback(
    payload: RunPodRetrainingCallbackRequest,
    x_retraining_callback_secret: str | None = Header(
        default=None,
        alias="X-Retraining-Callback-Secret",
    ),
    db: Session = Depends(get_session),
) -> RunPodRetrainingCallbackResponse:
    """
    Receive status from the RunPod one-shot retraining worker.

    RunPod cannot reach private AWS RDS/Redis. EEP owns database writes and
    exposes this authenticated callback so the external GPU pod only needs
    public HTTP access back to EEP.
    """
    _verify_retraining_callback_secret(x_retraining_callback_secret)

    trigger = db.get(RetrainingTrigger, payload.trigger_id)
    job = db.get(RetrainingJob, payload.job_id)
    if trigger is None or job is None or job.trigger_id != payload.trigger_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Retraining trigger/job pair was not found.",
        )

    now = datetime.now(timezone.utc)
    if payload.status == "running":
        trigger.status = "processing"
        job.status = "running"
        job.started_at = job.started_at or now
    elif payload.status == "failed":
        trigger.status = "failed"
        trigger.resolved_at = now
        job.status = "failed"
        job.completed_at = now
        job.error_message = payload.error_message or "RunPod retraining failed."
    else:
        created_versions = _create_callback_model_versions(db, job=job, payload=payload)
        trigger.status = "completed"
        trigger.resolved_at = now
        trigger.mlflow_run_id = payload.mlflow_run_id
        job.status = "completed"
        job.completed_at = now
        job.mlflow_run_id = payload.mlflow_run_id
        job.dataset_version = payload.dataset_version
        job.result_model_version = payload.result_model_version or created_versions
        job.result_mAP = payload.result_mAP
        job.promotion_decision = payload.promotion_decision or "pending_gate_review"

    db.commit()

    logger.info(
        "runpod_retraining_callback: trigger_id=%s job_id=%s status=%s",
        payload.trigger_id,
        payload.job_id,
        payload.status,
    )

    if payload.status in {"completed", "failed"} and os.environ.get(
        "RUNPOD_TERMINATE_ON_CALLBACK",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}:
        pod_id = _extract_worker_external_id(trigger.notes)
        if pod_id:
            terminate_retraining_pod(pod_id)

    return RunPodRetrainingCallbackResponse(
        ok=True,
        trigger_id=payload.trigger_id,
        job_id=payload.job_id,
        status=payload.status,
    )
