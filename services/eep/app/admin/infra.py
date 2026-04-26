"""
services/eep/app/admin/infra.py
--------------------------------
Infrastructure status endpoints for the admin dashboard grading command center.

Implements:
  GET /v1/admin/queue-status        — Redis queue depths and worker slot state
  GET /v1/admin/service-inventory   — Static service catalog with DB-derived health signals
  GET /v1/admin/deployment-status   — Deployment metadata, env flags, migration version

All endpoints require admin role.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import cast

import redis as redis_lib
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import ServiceInvocation
from services.eep.app.db.session import get_session
from services.eep.app.redis_client import get_redis
from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    QUEUE_SHADOW_TASKS,
    QUEUE_SHADOW_TASKS_PROCESSING,
    WORKER_SLOTS_KEY,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

_DEFAULT_WORKER_CONCURRENCY = 20


# ── Queue Status ──────────────────────────────────────────────────────────────


class QueueStatusResponse(BaseModel):
    """Live Redis queue depth snapshot."""

    page_tasks_queued: int
    page_tasks_processing: int
    page_tasks_dead_letter: int
    shadow_tasks_queued: int
    shadow_tasks_processing: int
    worker_slots_available: int | None
    worker_slots_max: int
    as_of: datetime


@router.get(
    "/v1/admin/queue-status",
    response_model=QueueStatusResponse,
    status_code=200,
    summary="Redis queue depth snapshot",
)
def get_queue_status(
    r: redis_lib.Redis = Depends(get_redis),
    _user: CurrentUser = Depends(require_admin),
) -> QueueStatusResponse:
    """
    Return live Redis queue depths and worker concurrency state.

    All counts come directly from Redis LLEN and GET calls — no DB queries.
    Poll this on a 10–30 second interval to monitor processing backpressure.

    **Auth:** admin role required.
    """
    page_tasks_queued = cast(int, r.llen(QUEUE_PAGE_TASKS))
    page_tasks_processing = cast(int, r.llen(QUEUE_PAGE_TASKS_PROCESSING))
    page_tasks_dead_letter = cast(int, r.llen(QUEUE_DEAD_LETTER))
    shadow_tasks_queued = cast(int, r.llen(QUEUE_SHADOW_TASKS))
    shadow_tasks_processing = cast(int, r.llen(QUEUE_SHADOW_TASKS_PROCESSING))

    # Worker slots: Redis STRING counter, None if key does not exist yet
    slots_raw = r.get(WORKER_SLOTS_KEY)
    worker_slots_available: int | None = int(slots_raw) if slots_raw is not None else None

    max_concurrency = int(
        os.environ.get("MAX_CONCURRENT_PAGES", str(_DEFAULT_WORKER_CONCURRENCY))
    )

    return QueueStatusResponse(
        page_tasks_queued=page_tasks_queued,
        page_tasks_processing=page_tasks_processing,
        page_tasks_dead_letter=page_tasks_dead_letter,
        shadow_tasks_queued=shadow_tasks_queued,
        shadow_tasks_processing=shadow_tasks_processing,
        worker_slots_available=worker_slots_available,
        worker_slots_max=max_concurrency,
        as_of=datetime.now(timezone.utc),
    )


# ── Service Inventory ─────────────────────────────────────────────────────────


# Static catalog — reflects docker-compose / ECS task definitions exactly.
# Health signal is derived from recent service_invocations rather than live
# network probes, so this works even when IEP services are not co-located.
_SERVICE_CATALOG = [
    {
        "service_name": "eep",
        "role": "Central Orchestrator / API Gateway",
        "deployment_type": "Fargate",
        "port": 8888,
        "invocation_pattern": None,  # EEP itself — always shown as healthy
        "model_applicable": False,
    },
    {
        "service_name": "eep_worker",
        "role": "Page Processing Worker",
        "deployment_type": "Fargate",
        "port": 9100,
        "invocation_pattern": None,
        "model_applicable": False,
    },
    {
        "service_name": "eep_recovery",
        "role": "Hung-task Recovery / Reconciler",
        "deployment_type": "Fargate",
        "port": 9101,
        "invocation_pattern": None,
        "model_applicable": False,
    },
    {
        "service_name": "iep0",
        "role": "Material-type Classification (YOLOv8)",
        "deployment_type": "Fargate",
        "port": 8006,
        "invocation_pattern": "iep0%",
        "model_applicable": True,
    },
    {
        "service_name": "iep1a",
        "role": "Geometry Detection — YOLOv8-seg (primary)",
        "deployment_type": "EC2 GPU",
        "port": 8001,
        "invocation_pattern": "iep1a%",
        "model_applicable": True,
    },
    {
        "service_name": "iep1b",
        "role": "Geometry Detection — YOLOv8-pose (challenger)",
        "deployment_type": "EC2 GPU",
        "port": 8002,
        "invocation_pattern": "iep1b%",
        "model_applicable": True,
    },
    {
        "service_name": "iep1d",
        "role": "Document Rectification — UVDoc",
        "deployment_type": "EC2 GPU",
        "port": 8003,
        "invocation_pattern": "iep1d%",
        "model_applicable": True,
    },
    {
        "service_name": "iep1e",
        "role": "Semantic Normalisation / Orientation (PaddleOCR)",
        "deployment_type": "Fargate",
        "port": 8007,
        "invocation_pattern": "iep1e%",
        "model_applicable": False,
    },
    {
        "service_name": "iep2a",
        "role": "Layout Detection — Detectron2",
        "deployment_type": "EC2 GPU",
        "port": 8004,
        "invocation_pattern": "iep2a%",
        "model_applicable": True,
    },
    {
        "service_name": "iep2b",
        "role": "Layout Detection — DocLayout-YOLO",
        "deployment_type": "EC2 GPU",
        "port": 8005,
        "invocation_pattern": "iep2b%",
        "model_applicable": True,
    },
    {
        "service_name": "shadow_worker",
        "role": "Shadow Evaluation Worker",
        "deployment_type": "Fargate",
        "port": 9102,
        "invocation_pattern": None,
        "model_applicable": False,
    },
    {
        "service_name": "retraining_worker",
        "role": "MLOps Retraining + Golden Eval (stub mode)",
        "deployment_type": "Fargate (one-shot)",
        "port": 9104,
        "invocation_pattern": None,
        "model_applicable": False,
    },
    {
        "service_name": "dataset_builder",
        "role": "YOLO Dataset Export (profile-gated, on-demand)",
        "deployment_type": "One-shot / disabled",
        "port": None,
        "invocation_pattern": None,
        "model_applicable": False,
    },
    # Artifact cleanup is not a deployed service.
    # Temporary cleanup is handled by S3 Lifecycle. DB-referenced artifacts are retained.
]


class ServiceHealthSignal(BaseModel):
    """DB-derived health signal for an IEP service over the last 24h."""

    total_invocations: int
    success_count: int
    error_count: int
    success_rate: float | None
    last_invoked_at: datetime | None
    p95_latency_ms: float | None


class ServiceInventoryItem(BaseModel):
    service_name: str
    role: str
    deployment_type: str
    port: int | None
    model_applicable: bool
    health_signal: ServiceHealthSignal | None


class ServiceInventoryResponse(BaseModel):
    items: list[ServiceInventoryItem]
    window_hours: int
    as_of: datetime


@router.get(
    "/v1/admin/service-inventory",
    response_model=ServiceInventoryResponse,
    status_code=200,
    summary="Service catalog with DB-derived health signals",
)
def get_service_inventory(
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> ServiceInventoryResponse:
    """
    Return the static service catalog annotated with health signals from
    the ``service_invocations`` table over the last 24 hours.

    Services without invocation records (e.g. workers)
    return ``health_signal: null``.

    **Auth:** admin role required.
    """
    window_hours = 24
    window_start = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    items: list[ServiceInventoryItem] = []
    for svc in _SERVICE_CATALOG:
        pattern = svc["invocation_pattern"]
        signal: ServiceHealthSignal | None = None

        if pattern:
            q = db.query(ServiceInvocation).filter(
                ServiceInvocation.service_name.ilike(str(pattern)),
                ServiceInvocation.invoked_at >= window_start,
            )
            total = q.with_entities(func.count(ServiceInvocation.id)).scalar() or 0
            success = (
                q.filter(ServiceInvocation.status == "success")
                .with_entities(func.count(ServiceInvocation.id))
                .scalar()
                or 0
            )
            errors = total - success

            last_row = (
                q.order_by(ServiceInvocation.invoked_at.desc())
                .first()
            )
            last_invoked_at = last_row.invoked_at if last_row else None

            # P95 latency from the processing_time_ms column (stored as int ms)
            p95: float | None = None
            if total > 0:
                p95_raw = (
                    db.query(
                        func.percentile_cont(0.95).within_group(
                            ServiceInvocation.processing_time_ms.asc()
                        )
                    )
                    .filter(
                        ServiceInvocation.service_name.ilike(str(pattern)),
                        ServiceInvocation.invoked_at >= window_start,
                        ServiceInvocation.processing_time_ms.isnot(None),
                    )
                    .scalar()
                )
                p95 = float(p95_raw) if p95_raw is not None else None

            success_rate = round(success / total, 4) if total > 0 else None
            signal = ServiceHealthSignal(
                total_invocations=total,
                success_count=success,
                error_count=errors,
                success_rate=success_rate,
                last_invoked_at=last_invoked_at,
                p95_latency_ms=p95,
            )

        items.append(
            ServiceInventoryItem(
                service_name=str(svc["service_name"]),
                role=str(svc["role"]),
                deployment_type=str(svc["deployment_type"]),
                port=svc["port"],  # type: ignore[arg-type]
                model_applicable=bool(svc["model_applicable"]),
                health_signal=signal,
            )
        )

    return ServiceInventoryResponse(
        items=items,
        window_hours=window_hours,
        as_of=datetime.now(timezone.utc),
    )


# ── Deployment Status ─────────────────────────────────────────────────────────


class FeatureFlags(BaseModel):
    """Live/stub mode flags read from environment variables."""

    retraining_mode: str  # "live" | "stub"
    golden_eval_mode: str  # "live" | "stub"
    artifact_cleanup: str  # always "disabled" — not implemented


class DeploymentStatusResponse(BaseModel):
    image_tag: str | None
    git_sha: str | None
    ecs_cluster: str | None
    ecs_service: str | None
    alembic_version: str | None
    feature_flags: FeatureFlags
    s3_bucket: str | None
    redis_url_configured: bool
    as_of: datetime


@router.get(
    "/v1/admin/deployment-status",
    response_model=DeploymentStatusResponse,
    status_code=200,
    summary="Deployment metadata and feature flag state",
)
def get_deployment_status(
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> DeploymentStatusResponse:
    """
    Return deployment metadata sourced from environment variables and the DB.

    **Feature flags:**
    - ``retraining_mode``: ``live`` only when ``LIBRARYAI_RETRAINING_TRAIN=live``;
      otherwise ``stub``.
    - ``golden_eval_mode``: ``live`` only when ``LIBRARYAI_RETRAINING_GOLDEN_EVAL=live``;
      otherwise ``stub``.
    - ``artifact_cleanup``: always ``disabled`` — safe retention/deletion logic
      is not yet implemented.

    **Auth:** admin role required.
    """
    # Migration version from alembic_version table
    alembic_version: str | None = None
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
        alembic_version = str(row[0]) if row else None
    except Exception:
        alembic_version = None

    retraining_mode = (
        "live"
        if os.environ.get("LIBRARYAI_RETRAINING_TRAIN", "stub").lower() == "live"
        else "stub"
    )
    golden_eval_mode = (
        "live"
        if os.environ.get("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub").lower() == "live"
        else "stub"
    )

    redis_url = os.environ.get("REDIS_URL", "")

    return DeploymentStatusResponse(
        image_tag=os.environ.get("LIBRARYAI_IMAGE_TAG") or os.environ.get("IMAGE_TAG"),
        git_sha=os.environ.get("GIT_SHA") or os.environ.get("COMMIT_SHA"),
        ecs_cluster=os.environ.get("ECS_CLUSTER"),
        ecs_service=os.environ.get("ECS_SERVICE"),
        alembic_version=alembic_version,
        feature_flags=FeatureFlags(
            retraining_mode=retraining_mode,
            golden_eval_mode=golden_eval_mode,
            artifact_cleanup="disabled",
        ),
        s3_bucket=os.environ.get("S3_BUCKET_NAME"),
        redis_url_configured=bool(redis_url),
        as_of=datetime.now(timezone.utc),
    )
