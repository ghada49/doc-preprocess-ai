"""
services/eep/app/models_api.py
--------------------------------
Model evaluation endpoints for the frontend MLOps pages.

Implements:
  GET  /v1/models/evaluation  — Return evaluation info for model versions (admin only)
  POST /v1/models/evaluate    — Trigger an offline evaluation run (admin only)

--- GET /v1/models/evaluation ---

Returns model version records with gate results, shaped for:
  - Model evaluation page
  - Shadow / candidate model widgets
  - Comparison cards

Query params:
  candidate_tag  — filter to a specific version tag; omit for most recent rows
  service        — optional filter by service name (iep1a, iep1b, etc.)
  stage          — optional filter by stage
                   (experimental | staging | shadow | production | archived)
  limit          — max rows to return (default 20, max 100)

Response includes gate pass/fail summary per version.

--- POST /v1/models/evaluate ---

Triggers an offline evaluation run for a candidate model version.

Accepts:
  candidate_tag  — version_tag of the model to evaluate
  service        — service name (iep1a, iep1b, ...)

Validates that the candidate exists in model_versions.
Creates a retraining_jobs record with status='pending' representing the
evaluation task.

TODO (Packet 8.5): Wire this to the actual evaluation worker queue instead of
creating a retraining_jobs record. The retraining_jobs pipeline_type should be
mapped to a dedicated 'evaluation' task type or an existing pipeline_type that
the worker recognises as an offline eval run. The DB record contract below is
stable and the frontend contract is preserved.

Auth: admin only for both endpoints.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import ModelVersion, RetrainingTrigger
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mlops"])

_DEFAULT_EVALUATION_LIMIT = 20
_MAX_EVALUATION_LIMIT = 100

# Services that have formal model versions managed by the promotion pipeline.
# IEP2 is excluded per spec Section 16.5; it can still appear in model_versions
# for informational purposes but is not subject to the promotion gate.
_EVALUATION_SERVICES = frozenset({"iep1a", "iep1b", "iep1c", "iep1d", "iep2a", "iep2b"})


# ── Response schemas ──────────────────────────────────────────────────────────


class GateSummary(BaseModel):
    """
    Compact pass/fail summary derived from model_versions.gate_results JSONB.

    Fields:
        total_gates  — total number of gates evaluated
        passed_gates — number of gates that passed
        failed_gates — number of gates that failed
        all_pass     — True only when total_gates > 0 and failed_gates == 0
        failed_names — names of gates that failed (for display in comparison cards)
    """

    total_gates: int
    passed_gates: int
    failed_gates: int
    all_pass: bool
    failed_names: list[str]


class ModelEvaluationRecord(BaseModel):
    """
    Model evaluation record for frontend display.

    Shaped for use in:
      - model evaluation page
      - shadow / candidate model comparison cards
      - promotion eligibility widget

    Fields:
        model_id        — unique model version identifier
        service_name    — iep1a | iep1b | iep1c | iep1d | iep2a | iep2b
        version_tag     — human-readable version identifier
        stage           — experimental | staging | shadow | production | archived
        dataset_version — dataset version used for training (nullable)
        mlflow_run_id   — MLflow run identifier (nullable)
        gate_results    — raw gate results JSONB (nullable; written by eval worker)
        gate_summary    — computed pass/fail summary (nullable when no gate_results)
        promoted_at     — when this version was promoted to production (nullable)
        notes           — freeform notes (nullable)
        created_at      — when this model_version record was created
    """

    model_id: str
    service_name: str
    version_tag: str
    stage: str
    dataset_version: str | None
    mlflow_run_id: str | None
    gate_results: Any  # raw JSONB; can be dict or None
    gate_summary: GateSummary | None
    promoted_at: datetime | None
    notes: str | None
    created_at: datetime


class ModelEvaluationResponse(BaseModel):
    """
    Response for GET /v1/models/evaluation.

    Fields:
        total   — total records matching the filter (ignoring limit)
        records — model evaluation records
    """

    total: int
    records: list[ModelEvaluationRecord]


class EvaluateRequest(BaseModel):
    """
    Request body for POST /v1/models/evaluate.

    Fields:
        candidate_tag — version_tag of the model to evaluate
        service       — service name (iep1a, iep1b, ...)
    """

    candidate_tag: str
    service: str


class EvaluateResponse(BaseModel):
    """
    Response for POST /v1/models/evaluate.

    Fields:
        evaluation_job_id — internal job/task ID created for this evaluation run
        model_id          — model_versions.model_id being evaluated
        service_name      — service being evaluated
        version_tag       — version tag being evaluated
        status            — initial status ('pending')
        message           — human-readable status message for the UI
    """

    evaluation_job_id: str
    model_id: str
    service_name: str
    version_tag: str
    status: str
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_gate_summary(gate_results: Any) -> GateSummary | None:
    """
    Compute a GateSummary from model_versions.gate_results JSONB.

    Returns None when gate_results is absent or not a dict.
    """
    if not gate_results or not isinstance(gate_results, dict):
        return None

    total = len(gate_results)
    failed_names = [
        name
        for name, result in gate_results.items()
        if isinstance(result, dict) and not result.get("pass", True)
    ]
    passed = total - len(failed_names)

    return GateSummary(
        total_gates=total,
        passed_gates=passed,
        failed_gates=len(failed_names),
        all_pass=(total > 0 and len(failed_names) == 0),
        failed_names=failed_names,
    )


def _mv_to_record(mv: ModelVersion) -> ModelEvaluationRecord:
    gate_summary = _build_gate_summary(mv.gate_results)
    return ModelEvaluationRecord(
        model_id=mv.model_id,
        service_name=mv.service_name,
        version_tag=mv.version_tag,
        stage=mv.stage,
        dataset_version=mv.dataset_version,
        mlflow_run_id=mv.mlflow_run_id,
        gate_results=mv.gate_results,
        gate_summary=gate_summary,
        promoted_at=mv.promoted_at,
        notes=mv.notes,
        created_at=mv.created_at,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get(
    "/v1/models/evaluation",
    response_model=ModelEvaluationResponse,
    status_code=200,
    summary="Get model evaluation info",
)
def get_model_evaluation(
    candidate_tag: str | None = Query(
        default=None,
        description=(
            "Filter to a specific version tag. "
            "When omitted, returns the most recently created model versions."
        ),
    ),
    service: str | None = Query(
        default=None,
        description="Filter by service name (iep1a, iep1b, iep1c, iep1d, iep2a, iep2b).",
    ),
    stage: str | None = Query(
        default=None,
        description=("Filter by stage: experimental | staging | shadow | production | archived."),
    ),
    limit: int = Query(
        default=_DEFAULT_EVALUATION_LIMIT,
        ge=1,
        le=_MAX_EVALUATION_LIMIT,
        description="Maximum records to return.",
    ),
    db: Session = Depends(get_session),
    _caller: CurrentUser = Depends(require_admin),
) -> ModelEvaluationResponse:
    """
    Return model version records with gate results for the evaluation dashboard.

    When ``candidate_tag`` is provided, results are filtered to that version tag
    (may match across multiple services).  When omitted, the most recently
    created model version records are returned, ordered by ``created_at DESC``.

    **Auth:** admin role required.
    """
    q = db.query(ModelVersion)

    if candidate_tag is not None:
        q = q.filter(ModelVersion.version_tag == candidate_tag)

    if service is not None:
        q = q.filter(ModelVersion.service_name == service)

    if stage is not None:
        q = q.filter(ModelVersion.stage == stage)

    total: int = q.count()
    rows: list[ModelVersion] = q.order_by(ModelVersion.created_at.desc()).limit(limit).all()

    logger.debug(
        "get_model_evaluation: candidate_tag=%r service=%r stage=%r total=%d",
        candidate_tag,
        service,
        stage,
        total,
    )

    return ModelEvaluationResponse(
        total=total,
        records=[_mv_to_record(mv) for mv in rows],
    )


@router.post(
    "/v1/models/evaluate",
    response_model=EvaluateResponse,
    status_code=202,
    summary="Trigger offline evaluation for a candidate model",
)
def trigger_model_evaluate(
    body: EvaluateRequest,
    db: Session = Depends(get_session),
    caller: CurrentUser = Depends(require_admin),
) -> EvaluateResponse:
    """
    Trigger an offline evaluation run for a candidate model version.

    Validates that the candidate exists in ``model_versions``.  Creates a
    ``retraining_jobs`` record with ``status='pending'`` representing the
    evaluation task.

    The evaluation worker (Packet 8.5) picks up pending retraining_jobs
    records and executes the evaluation, writing results back to
    ``model_versions.gate_results``.

    TODO (Packet 8.5): Map pipeline_type to a dedicated evaluation task type
    that the worker recognises as an offline eval run (not a full retraining
    cycle).  The current implementation reuses the retraining_jobs table with
    the candidate's service-appropriate pipeline_type.  The DB record contract
    below is stable.

    **Auth:** admin role required.

    **Error responses**

    - ``404`` — no model version found for the given service + candidate_tag
    - ``409`` — an evaluation is already pending/running for this candidate
    """
    # Validate candidate exists
    candidate: ModelVersion | None = (
        db.query(ModelVersion)
        .filter(
            ModelVersion.service_name == body.service,
            ModelVersion.version_tag == body.candidate_tag,
        )
        .order_by(ModelVersion.created_at.desc())
        .first()
    )

    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No model version found for service={body.service!r} "
                f"tag={body.candidate_tag!r}."
            ),
        )

    # Guard against duplicate evaluation requests
    existing: RetrainingTrigger | None = (
        db.query(RetrainingTrigger)
        .filter(
            RetrainingTrigger.trigger_type == "manual_evaluation",
            RetrainingTrigger.notes == candidate.model_id,
            RetrainingTrigger.status.in_(["pending", "processing"]),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An evaluation is already {existing.status} for model_id="
                f"{candidate.model_id!r} (trigger_id={existing.trigger_id!r})."
            ),
        )

    # Create a RetrainingTrigger that the retraining worker will pick up.
    # trigger_type="manual_evaluation" causes execute_retraining_task to skip
    # training and run evaluation only, writing gate_results back to this model_id
    # (stored in notes).  metric_* fields are required by the schema; zeros are
    # used as sentinels for manually triggered evaluations.
    eval_trigger = RetrainingTrigger(
        trigger_id=str(uuid.uuid4()),
        trigger_type="manual_evaluation",
        metric_name="manual",
        metric_value=0.0,
        threshold_value=0.0,
        persistence_hours=0.0,
        status="pending",
        notes=candidate.model_id,
    )
    db.add(eval_trigger)
    db.commit()
    db.refresh(eval_trigger)

    logger.info(
        "trigger_model_evaluate: queued eval trigger_id=%s model_id=%s service=%s tag=%s by=%s",
        eval_trigger.trigger_id,
        candidate.model_id,
        body.service,
        body.candidate_tag,
        caller.user_id,
    )

    return EvaluateResponse(
        evaluation_job_id=eval_trigger.trigger_id,
        model_id=candidate.model_id,
        service_name=body.service,
        version_tag=body.candidate_tag,
        status="pending",
        message=(
            f"Evaluation queued for {body.service} v{body.candidate_tag}. "
            "Results will be written to model_versions.gate_results when complete."
        ),
    )
