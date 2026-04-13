"""
services/eep/app/admin/dashboard.py
-------------------------------------
Packet 7.4 — Admin dashboard endpoints.

Implements:
  GET /v1/admin/dashboard-summary
  GET /v1/admin/service-health

Both endpoints require `require_admin`.  Non-admin callers receive 403.

--- dashboard-summary ---

Returns a live snapshot of system-wide KPIs.  All counts and rates are
computed on-request from the authoritative tables; no caching layer is
introduced.

Fields (spec Section 11.4):
  throughput_pages_per_hour    — pages that reached a terminal state in the
                                 rolling last 60 minutes.
  auto_accept_rate             — fraction of all-time terminal pages where
                                 acceptance_decision = 'accepted'.
  structural_agreement_rate    — fraction of all-time page_lineage rows where
                                 structural_agreement IS TRUE (out of those
                                 where structural_agreement IS NOT NULL).
  pending_corrections_count    — count of job_pages in pending_human_correction.
  active_jobs_count            — count of jobs with status = 'running'.
  active_workers_count         — length of Redis page-task processing list
                                 (libraryai:page_tasks:processing); proxy for
                                 tasks currently claimed by worker processes.
  shadow_evaluations_count     — count of jobs with shadow_mode = True.

--- service-health ---

Returns per-pipeline-stage success rates over a configurable time window.
Rates are computed from the service_invocations table keyed by service_name
patterns matching the IEP naming convention used throughout the spec:
  preprocessing stage  — service_name ILIKE 'iep1a%' OR 'iep1b%' OR 'iep1c%'
  rectification stage  — service_name ILIKE 'iep1d%'
  layout stage         — service_name ILIKE 'iep2%'

Fields (spec Section 11.4):
  preprocessing_success_rate   — fraction of preprocessing-stage invocations
                                 with status = 'success' in the window.
  rectification_success_rate   — fraction of rectification invocations with
                                 status = 'success' in the window.
  layout_success_rate          — fraction of layout-stage invocations with
                                 status = 'success' in the window.
  human_review_throughput_rate — human-corrected pages per hour over the window
                                 (page_lineage.human_correction_timestamp).
  structural_agreement_rate    — fraction of page_lineage rows created in the
                                 window where structural_agreement IS TRUE.
  window_hours                 — the window used for all rate computations.

Query parameter:
  window_hours (int, default 24, min 1, max 720) — look-back window for
  service-health rates.

Error responses:
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import cast

import redis as redis_lib
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import Job, JobPage, PageLineage, ServiceInvocation
from services.eep.app.db.session import get_session
from services.eep.app.redis_client import get_redis
from shared.schemas.queue import QUEUE_PAGE_TASKS_PROCESSING

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

_TERMINAL_STATES = ("accepted", "review", "failed")


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _safe_rate(numerator: int, denominator: int) -> float:
    """Return numerator/denominator rounded to 4 decimal places; 0.0 when denominator is 0."""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


# ── Response schemas ─────────────────────────────────────────────────────────────


class DashboardSummaryResponse(BaseModel):
    """
    System-wide KPI snapshot.

    Fields:
        throughput_pages_per_hour    — pages completing (reaching a terminal state)
                                       in the last 60 minutes
        auto_accept_rate             — fraction of terminal pages automatically accepted
        structural_agreement_rate    — fraction of pages where IEP1A and IEP1B agreed
        pending_corrections_count    — pages currently awaiting human correction
        active_jobs_count            — jobs currently in 'running' state
        active_workers_count         — tasks currently claimed from the processing queue
        shadow_evaluations_count     — jobs submitted with shadow_mode enabled
    """

    throughput_pages_per_hour: float
    auto_accept_rate: float
    structural_agreement_rate: float
    pending_corrections_count: int
    active_jobs_count: int
    active_workers_count: int
    shadow_evaluations_count: int


class ServiceHealthResponse(BaseModel):
    """
    Per-pipeline-stage success rates over a rolling time window.

    Fields:
        preprocessing_success_rate   — IEP1A/IEP1B/IEP1C invocation success rate
        rectification_success_rate   — IEP1D invocation success rate
        layout_success_rate          — IEP2A/IEP2B invocation success rate
        human_review_throughput_rate — human-corrected pages per hour
        structural_agreement_rate    — IEP1A/IEP1B geometric agreement rate
        window_hours                 — look-back window used for all rates
    """

    preprocessing_success_rate: float
    rectification_success_rate: float
    layout_success_rate: float
    human_review_throughput_rate: float
    structural_agreement_rate: float
    window_hours: int


# ── Endpoints ────────────────────────────────────────────────────────────────────


@router.get(
    "/v1/admin/dashboard-summary",
    response_model=DashboardSummaryResponse,
    status_code=200,
    summary="Admin dashboard KPI snapshot",
)
def get_dashboard_summary(
    db: Session = Depends(get_session),
    r: redis_lib.Redis = Depends(get_redis),
    _user: CurrentUser = Depends(require_admin),
) -> DashboardSummaryResponse:
    """
    Return a live system-wide KPI snapshot.

    All values are computed on-request from the database and Redis.
    No caching; suitable for polling on a 30–60 second interval.

    **Auth:** admin role required (403 for non-admin callers).
    """
    now = datetime.now(tz=UTC)
    one_hour_ago = now - timedelta(hours=1)

    # throughput: terminal pages completed in the rolling last hour
    throughput_pages_per_hour: float = float(
        db.query(func.count(JobPage.page_id))
        .filter(
            JobPage.status.in_(_TERMINAL_STATES),
            JobPage.status_updated_at >= one_hour_ago,
        )
        .scalar()
        or 0
    )

    # auto_accept_rate: accepted / all terminal (all-time)
    total_terminal: int = (
        db.query(func.count(JobPage.page_id)).filter(JobPage.status.in_(_TERMINAL_STATES)).scalar()
        or 0
    )
    total_accepted: int = (
        db.query(func.count(JobPage.page_id))
        .filter(JobPage.acceptance_decision == "accepted")
        .scalar()
        or 0
    )
    auto_accept_rate = _safe_rate(total_accepted, total_terminal)

    # structural_agreement_rate: from page_lineage (all-time)
    total_with_agreement: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(PageLineage.structural_agreement.isnot(None))
        .scalar()
        or 0
    )
    total_agreed: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(PageLineage.structural_agreement.is_(True))
        .scalar()
        or 0
    )
    structural_agreement_rate = _safe_rate(total_agreed, total_with_agreement)

    # pending corrections
    pending_corrections_count: int = (
        db.query(func.count(JobPage.page_id))
        .filter(JobPage.status == "pending_human_correction")
        .scalar()
        or 0
    )

    # active jobs
    active_jobs_count: int = (
        db.query(func.count(Job.job_id)).filter(Job.status == "running").scalar() or 0
    )

    # active workers: tasks currently held in the processing list
    active_workers_count: int = cast(int, r.llen(QUEUE_PAGE_TASKS_PROCESSING))

    # shadow evaluations: jobs submitted with shadow_mode
    shadow_evaluations_count: int = (
        db.query(func.count(Job.job_id)).filter(Job.shadow_mode.is_(True)).scalar() or 0
    )

    logger.debug(
        "dashboard-summary: throughput=%.1f auto_accept=%.4f pending_corrections=%d "
        "active_jobs=%d active_workers=%d shadow=%d",
        throughput_pages_per_hour,
        auto_accept_rate,
        pending_corrections_count,
        active_jobs_count,
        active_workers_count,
        shadow_evaluations_count,
    )
    return DashboardSummaryResponse(
        throughput_pages_per_hour=throughput_pages_per_hour,
        auto_accept_rate=auto_accept_rate,
        structural_agreement_rate=structural_agreement_rate,
        pending_corrections_count=pending_corrections_count,
        active_jobs_count=active_jobs_count,
        active_workers_count=active_workers_count,
        shadow_evaluations_count=shadow_evaluations_count,
    )


@router.get(
    "/v1/admin/service-health",
    response_model=ServiceHealthResponse,
    status_code=200,
    summary="Admin service health rates",
)
def get_service_health(
    window_hours: int = Query(
        default=24,
        ge=1,
        le=720,
        description="Look-back window in hours for all rate computations (default 24, max 720).",
    ),
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> ServiceHealthResponse:
    """
    Return per-pipeline-stage success rates over a rolling time window.

    Rates are derived from the ``service_invocations`` table.  When no
    invocations exist for a stage within the window, the rate is ``0.0``
    rather than ``null`` to keep the response shape stable.

    ``window_hours`` is echoed back in the response so callers always know
    which window produced the numbers they are seeing.

    **Auth:** admin role required (403 for non-admin callers).
    """
    now = datetime.now(tz=UTC)
    window_start = now - timedelta(hours=window_hours)

    def _stage_rate(name_filters: list[str]) -> float:
        """
        Compute success rate for service invocations matching any of the given
        ILIKE patterns within the current window.
        """
        q = db.query(ServiceInvocation).filter(
            or_(*[ServiceInvocation.service_name.ilike(p) for p in name_filters]),
            ServiceInvocation.invoked_at >= window_start,
        )
        total: int = q.with_entities(func.count(ServiceInvocation.id)).scalar() or 0
        success: int = (
            q.filter(ServiceInvocation.status == "success")
            .with_entities(func.count(ServiceInvocation.id))
            .scalar()
            or 0
        )
        return _safe_rate(success, total)

    # Preprocessing stage: IEP1A geometry, IEP1B geometry, IEP1C preprocessing
    preprocessing_success_rate = _stage_rate(["iep1a%", "iep1b%", "iep1c%"])

    # Rectification stage: IEP1D
    rectification_success_rate = _stage_rate(["iep1d%"])

    # Layout stage: IEP2A / IEP2B
    layout_success_rate = _stage_rate(["iep2%"])

    # Human review throughput: human-corrected pages per hour in the window
    human_reviewed: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.human_corrected.is_(True),
            PageLineage.human_correction_timestamp >= window_start,
        )
        .scalar()
        or 0
    )
    human_review_throughput_rate = round(human_reviewed / window_hours, 4)

    # Structural agreement rate: page_lineage rows created within the window
    total_with_agreement_window: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.structural_agreement.isnot(None),
            PageLineage.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    agreed_window: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.structural_agreement.is_(True),
            PageLineage.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    structural_agreement_rate = _safe_rate(agreed_window, total_with_agreement_window)

    logger.debug(
        "service-health: window=%dh preprocessing=%.4f rectification=%.4f "
        "layout=%.4f human_throughput=%.4f structural=%.4f",
        window_hours,
        preprocessing_success_rate,
        rectification_success_rate,
        layout_success_rate,
        human_review_throughput_rate,
        structural_agreement_rate,
    )
    return ServiceHealthResponse(
        preprocessing_success_rate=preprocessing_success_rate,
        rectification_success_rate=rectification_success_rate,
        layout_success_rate=layout_success_rate,
        human_review_throughput_rate=human_review_throughput_rate,
        structural_agreement_rate=structural_agreement_rate,
        window_hours=window_hours,
    )
