"""
services/retraining_worker/app/task.py
----------------------------------------
Packet 8.5 — Retraining task execution.

Implements the core task lifecycle for a single retraining trigger:

  1. Determine pipeline_type from trigger_type (spec Section 16.3).
  2. Create a RetrainingJob record (status='running').
  3. Link trigger.retraining_job_id → job.job_id.
  4. Stub training run — generates a placeholder mlflow_run_id and
     dataset_version.  Real MLflow integration is wired in Phase 12 when
     IEP1A/B real model weights are introduced.
  5. Run stub offline evaluation — writes gate_results to model_versions in
     the exact format read by promotion_api._check_gates (spec Section 16.2).
     Real per-model inference against held-out datasets is wired in Phase 12.
  6. Create ModelVersion rows (stage='staging') for each target service with
     the computed gate_results.
  7. Mark job completed, trigger completed.

layout_confidence_degradation is a monitoring-only trigger (spec Section 16.3):
no automated retraining job is created; trigger is marked completed immediately.

Gate results format (must match promotion_api._check_gates expectations):
  {
    "geometry_iou":              {"pass": bool, "value": float},
    "split_precision":           {"pass": bool, "value": float},
    "structural_agreement_rate": {"pass": bool, "value": float},
    "golden_dataset":            {"pass": bool, "regressions": int},
    "latency_p95":               {"pass": bool, "value": float},
  }

Exported:
  execute_retraining_task(trigger, db) — callable from the poll loop
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.eep.app.db.models import ModelVersion, RetrainingJob, RetrainingTrigger

logger = logging.getLogger(__name__)

# trigger_type → pipeline_type (spec Section 16.3).
# None means monitoring-only; no automated job is created.
_TRIGGER_PIPELINE: dict[str, str | None] = {
    "escalation_rate_anomaly": "preprocessing",
    "auto_accept_rate_collapse": "preprocessing",
    "structural_agreement_degradation": "preprocessing",
    "drift_alert_persistence": "preprocessing",
    "layout_confidence_degradation": None,
}

# Services retrained for a preprocessing pipeline job (spec Section 16.1).
_PREPROCESSING_SERVICES: tuple[str, ...] = ("iep1a", "iep1b")


# ── Stub helpers ──────────────────────────────────────────────────────────────


def _stub_gate_results() -> dict:
    """
    Placeholder offline evaluation results.

    Format is identical to the gate_results written by the real evaluation
    worker and read by promotion_api._check_gates.  All gates pass with
    conservative but realistic placeholder values.

    Stub — real per-model evaluation against held-out datasets is wired in
    Phase 12 when IEP1A/B model weights are available.
    """
    return {
        "geometry_iou": {"pass": True, "value": 0.84},
        "split_precision": {"pass": True, "value": 0.77},
        "structural_agreement_rate": {"pass": True, "value": 0.71},
        "golden_dataset": {"pass": True, "regressions": 0},
        "latency_p95": {"pass": True, "value": 2.3},
    }


def _stub_mlflow_train(pipeline_type: str, trigger_id: str) -> tuple[str, str]:
    """
    Placeholder MLflow training run.

    Returns (mlflow_run_id, dataset_version).  Real MLflow client integration
    and actual IEP1 model training are wired in Phase 12.
    """
    mlflow_run_id = f"stub-run-{uuid.uuid4().hex[:12]}"
    dataset_version = f"ds-stub-{pipeline_type}-001"
    logger.info(
        "_stub_mlflow_train: pipeline_type=%s trigger_id=%s → run_id=%s (STUB — mlflow not wired)",
        pipeline_type,
        trigger_id,
        mlflow_run_id,
    )
    return mlflow_run_id, dataset_version


# ── Task entry point ──────────────────────────────────────────────────────────


def execute_retraining_task(trigger: RetrainingTrigger, db: Session) -> None:
    """
    Execute a single retraining task for *trigger*.

    The poll loop is responsible for claiming the trigger (status='processing')
    before calling this function.  On success this function sets
    trigger.status='completed'.  On failure the caller is responsible for
    catching the exception, rolling back, and marking the trigger failed.

    Args:
        trigger: ORM row; must already have status='processing'.
        db:      Open SQLAlchemy session owned by the caller.
    """
    now = datetime.now(timezone.utc)
    trigger_type = trigger.trigger_type
    pipeline_type = _TRIGGER_PIPELINE.get(trigger_type)

    # Monitoring-only trigger: mark completed immediately, no job
    if pipeline_type is None:
        logger.info(
            "execute_retraining_task: trigger_type=%s → monitoring-only, no job created",
            trigger_type,
        )
        trigger.status = "completed"
        trigger.resolved_at = now
        trigger.notes = "monitoring-only trigger; no automated retraining job created"
        db.commit()
        return

    # Create retraining job
    job = RetrainingJob(
        job_id=str(uuid.uuid4()),
        trigger_id=trigger.trigger_id,
        pipeline_type=pipeline_type,
        status="running",
        started_at=now,
    )
    db.add(job)

    # Link trigger → job before first commit
    trigger.retraining_job_id = job.job_id
    db.commit()
    db.refresh(job)

    logger.info(
        "execute_retraining_task: created job_id=%s pipeline_type=%s trigger_id=%s",
        job.job_id,
        pipeline_type,
        trigger.trigger_id,
    )

    # Stub training run (Phase 12 replaces with real MLflow training call)
    mlflow_run_id, dataset_version = _stub_mlflow_train(pipeline_type, trigger.trigger_id)
    job.mlflow_run_id = mlflow_run_id
    job.dataset_version = dataset_version

    # Stub offline evaluation — writes gate_results to model_versions
    gate_results = _stub_gate_results()
    services = _PREPROCESSING_SERVICES if pipeline_type == "preprocessing" else ()
    created_version_tags: list[str] = []

    for service_name in services:
        version_tag = f"stub-{service_name}-{uuid.uuid4().hex[:8]}"
        mv = ModelVersion(
            model_id=str(uuid.uuid4()),
            service_name=service_name,
            version_tag=version_tag,
            mlflow_run_id=mlflow_run_id,
            dataset_version=dataset_version,
            stage="staging",
            gate_results=gate_results,
        )
        db.add(mv)
        created_version_tags.append(version_tag)
        logger.info(
            "execute_retraining_task: created ModelVersion service=%s version=%s stage=staging "
            "gate_results written (STUB evaluation)",
            service_name,
            version_tag,
        )

    # Mark job completed
    job.status = "completed"
    job.completed_at = datetime.now(timezone.utc)
    job.result_model_version = ",".join(created_version_tags) if created_version_tags else None
    job.promotion_decision = "pending_gate_review"

    # Mark trigger completed
    trigger.status = "completed"
    trigger.resolved_at = datetime.now(timezone.utc)

    db.commit()

    logger.info(
        "execute_retraining_task: completed job_id=%s versions_created=%s",
        job.job_id,
        created_version_tags,
    )
