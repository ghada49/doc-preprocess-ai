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
  trailing_wall_clock_pages_per_hour — terminal completions in the last 24h ÷ 24
                                 wall-clock hours (delivery rate, not peak speed).
  trailing_active_pages_per_hour — terminal completions in the last 24h ÷ aggregate
                                 active processing time (sum of ``JobPage.processing_time_ms``
                                 for those rows, converted to hours).  ``null`` when
                                 ``processing_time_ms`` is missing on any terminal row
                                 in the window, when the summed time is zero, or when
                                 there are no terminal completions in the window.
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
  human_review_throughput_rate — human-corrected pages in the window ÷ window_hours
                                 (same wall-clock convention as delivery rate).
  structural_agreement_rate    — fraction of page_lineage rows created in the
                                 window where structural_agreement IS TRUE.
  window_hours                 — the window used for stage, rescue, and
                                 structural-agreement rates.

Query parameter:
  window_hours (int, default 24, min 1, max 720) — look-back window for
  service-health rates and human review throughput.

Error responses:
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import redis as redis_lib
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import (
    Job,
    JobPage,
    ModelPromotionAudit,
    PageLineage,
    ServiceInvocation,
    ShadowEvaluation,
)
from services.eep.app.db.session import SessionLocal, get_session
from services.eep.app.redis_client import get_redis
from shared.metrics import EEP_AUTO_ACCEPT_RATE, EEP_STRUCTURAL_AGREEMENT_RATE
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


_MS_PER_HOUR = 3_600_000.0


def compute_trailing_page_rates_from_aggregates(
    *,
    hours: int,
    n_terminal_in_window: int,
    n_terminal_with_processing_ms: int,
    sum_processing_time_ms: float,
) -> tuple[float, float | None]:
    """
    Turn trailing-window SQL aggregates into (wall_clock_pph, active_pph).

    ``active_pph`` is ``n_terminal / (sum_processing_time_ms / MS_PER_HOUR)`` when every
    terminal row in the window has ``processing_time_ms`` populated and the sum is
    positive; otherwise ``None`` (caller surfaces as "active time unavailable").
    """
    if hours <= 0:
        return 0.0, None
    wall = round(n_terminal_in_window / float(hours), 4)
    if n_terminal_in_window == 0:
        return wall, None
    if n_terminal_with_processing_ms != n_terminal_in_window:
        return wall, None
    if sum_processing_time_ms <= 0:
        return wall, None
    active_hours = sum_processing_time_ms / _MS_PER_HOUR
    if active_hours <= 0:
        return wall, None
    active = round(n_terminal_in_window / active_hours, 4)
    return wall, active


def _terminal_trailing_window_base_filters(*, now: datetime, hours: int) -> tuple[Any, ...]:
    """SQLAlchemy boolean clauses for terminal ``JobPage`` rows in ``(now-hours, now]``."""
    window_start = now - timedelta(hours=hours)
    return (
        JobPage.status.in_(_TERMINAL_STATES),
        JobPage.status_updated_at.isnot(None),
        JobPage.status_updated_at >= window_start,
        JobPage.status_updated_at <= now,
    )


def trailing_terminal_page_rate_metrics(
    db: Session,
    *,
    hours: int = 24,
    now: datetime | None = None,
) -> tuple[float, float | None]:
    """
    Delivery rate (wall-clock) and active processing rate for terminal ``JobPage`` rows.

    The wall-clock rate divides completions in ``[now - hours, now]`` by *hours*.

    The active rate divides the same completion count by the sum of
    ``processing_time_ms`` for those rows (worker-task wall time, aggregated across
    parallelism).  Returns ``(wall, None)`` when active time cannot be computed
    reliably (see ``compute_trailing_page_rates_from_aggregates``).
    """
    if hours <= 0:
        return 0.0, None
    now = now or datetime.now(timezone.utc)
    base = _terminal_trailing_window_base_filters(now=now, hours=hours)
    n_all = int(db.query(func.count()).select_from(JobPage).filter(*base).scalar() or 0)
    n_with_ms = int(
        db.query(func.count())
        .select_from(JobPage)
        .filter(*base, JobPage.processing_time_ms.isnot(None))
        .scalar()
        or 0
    )
    sum_row = (
        db.query(func.coalesce(func.sum(JobPage.processing_time_ms), 0.0))
        .select_from(JobPage)
        .filter(*base, JobPage.processing_time_ms.isnot(None))
        .scalar()
    )
    sum_ms = float(sum_row or 0.0)
    return compute_trailing_page_rates_from_aggregates(
        hours=hours,
        n_terminal_in_window=n_all,
        n_terminal_with_processing_ms=n_with_ms,
        sum_processing_time_ms=sum_ms,
    )


def _dashboard_acceptance_rates(db: Session) -> tuple[float, float]:
    """Return the same all-time rates shown on the admin overview page."""
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
    return auto_accept_rate, structural_agreement_rate


def update_dashboard_rate_metrics(db: Session) -> tuple[float, float]:
    """
    Refresh Prometheus gauges that mirror the admin overview rate tiles.

    These gauges are observability-only; routing decisions continue to use the
    persisted page and lineage records directly.
    """
    auto_accept_rate, structural_agreement_rate = _dashboard_acceptance_rates(db)
    EEP_AUTO_ACCEPT_RATE.set(auto_accept_rate)
    EEP_STRUCTURAL_AGREEMENT_RATE.set(structural_agreement_rate)
    return auto_accept_rate, structural_agreement_rate


def refresh_dashboard_rate_metrics() -> None:
    """Refresh DB-backed EEP gauges before Prometheus scrapes /metrics."""
    db = SessionLocal()
    try:
        update_dashboard_rate_metrics(db)
    except Exception:
        logger.exception("dashboard metrics refresh failed")
    finally:
        db.close()


# ── Response schemas ─────────────────────────────────────────────────────────────


class DashboardSummaryResponse(BaseModel):
    """
    System-wide KPI snapshot.

    Fields:
        trailing_wall_clock_pages_per_hour — terminal completions in last 24h ÷ 24
        trailing_active_pages_per_hour — same count ÷ aggregate ``processing_time_ms``
                                           hours, or null when unavailable
        auto_accept_rate             — fraction of terminal pages automatically accepted
        structural_agreement_rate    — fraction of pages where IEP1A and IEP1B agreed
        pending_corrections_count    — pages currently awaiting human correction
        active_jobs_count            — jobs currently in 'running' state
        active_workers_count         — tasks currently claimed from the processing queue
        shadow_evaluations_count     — jobs submitted with shadow_mode enabled
    """

    trailing_wall_clock_pages_per_hour: float
    trailing_active_pages_per_hour: float | None = None
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
        human_review_throughput_rate — human-corrected pages in the window ÷ window_hours
        structural_agreement_rate    — IEP1A/IEP1B geometric agreement rate
        rescue_rate                  — fraction of first-pass failures that entered
                                       IEP1D rescue (vs. skipped by policy). 0.0 when
                                       no failures occurred in the window.
        policy_skips_count           — count of pages skipped to pending_human_correction
                                       by the disabled_direct_review policy in the window.
        window_hours                 — look-back window used for the windowed rates
    """

    preprocessing_success_rate: float
    rectification_success_rate: float
    layout_success_rate: float
    human_review_throughput_rate: float
    structural_agreement_rate: float
    rescue_rate: float
    policy_skips_count: int
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
    wall_pph, active_pph = trailing_terminal_page_rate_metrics(db, hours=24)

    auto_accept_rate, structural_agreement_rate = update_dashboard_rate_metrics(db)

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
        "dashboard-summary: wall_pph=%.2f active_pph=%s auto_accept=%.4f pending_corrections=%d "
        "active_jobs=%d active_workers=%d shadow=%d",
        wall_pph,
        active_pph,
        auto_accept_rate,
        pending_corrections_count,
        active_jobs_count,
        active_workers_count,
        shadow_evaluations_count,
    )
    return DashboardSummaryResponse(
        trailing_wall_clock_pages_per_hour=wall_pph,
        trailing_active_pages_per_hour=active_pph,
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

    ``window_hours`` is echoed back in the response so callers know which
    window produced the stage, rescue, and structural-agreement rates.

    **Auth:** admin role required (403 for non-admin callers).
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    def _stage_rate(name_filters: list[str]) -> float:
        """
        Compute success rate for service invocations matching any of the given
        ILIKE patterns within the current window.
        """
        base_filters = (
            or_(*[ServiceInvocation.service_name.ilike(p) for p in name_filters]),
            ServiceInvocation.invoked_at >= window_start,
        )
        total: int = (
            db.query(func.count(ServiceInvocation.id)).filter(*base_filters).scalar() or 0
        )
        success: int = (
            db.query(func.count(ServiceInvocation.id))
            .filter(
                *base_filters,
                ServiceInvocation.status == "success",
            )
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

    human_corrections_window: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.human_corrected.is_(True),
            PageLineage.human_correction_timestamp.isnot(None),
            PageLineage.human_correction_timestamp >= window_start,
        )
        .scalar()
        or 0
    )
    human_review_throughput_rate = round(
        human_corrections_window / float(window_hours), 4
    )

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

    # Rescue rate: of first-pass failures, what fraction entered IEP1D rescue
    # (vs. being skipped to pending_human_correction by disabled_direct_review policy).
    rescue_attempted: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.iep1d_used.is_(True),
            PageLineage.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    policy_skips_count: int = (
        db.query(func.count(PageLineage.lineage_id))
        .filter(
            PageLineage.acceptance_reason == "rectification_policy_disabled",
            PageLineage.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    rescue_rate = _safe_rate(rescue_attempted, rescue_attempted + policy_skips_count)

    logger.debug(
        "service-health: window=%dh preprocessing=%.4f rectification=%.4f "
        "layout=%.4f human_throughput=%.4f structural=%.4f rescue=%.4f policy_skips=%d",
        window_hours,
        preprocessing_success_rate,
        rectification_success_rate,
        layout_success_rate,
        human_review_throughput_rate,
        structural_agreement_rate,
        rescue_rate,
        policy_skips_count,
    )
    return ServiceHealthResponse(
        preprocessing_success_rate=preprocessing_success_rate,
        rectification_success_rate=rectification_success_rate,
        layout_success_rate=layout_success_rate,
        human_review_throughput_rate=human_review_throughput_rate,
        structural_agreement_rate=structural_agreement_rate,
        rescue_rate=rescue_rate,
        policy_skips_count=policy_skips_count,
        window_hours=window_hours,
    )


# ── Model gate comparison list ────────────────────────────────────────────────


class ModelGateComparisonRecord(BaseModel):
    """Single model gate comparison row for the admin list endpoint."""

    eval_id: str
    job_id: str
    page_id: str
    page_status: str
    confidence_delta: float | None
    status: str
    created_at: datetime
    completed_at: datetime | None


class ModelGateComparisonsResponse(BaseModel):
    """Paginated list of model gate comparison records."""

    total: int
    limit: int
    offset: int
    items: list[ModelGateComparisonRecord]


@router.get(
    "/v1/admin/model-gate-comparisons",
    response_model=ModelGateComparisonsResponse,
    status_code=200,
    summary="List offline model gate comparison records",
)
def list_model_gate_comparisons(
    job_id: str | None = Query(default=None, description="Filter by job_id."),
    status: str | None = Query(
        default=None,
        description="Filter by status: pending | completed | failed | no_shadow_model.",
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Max records to return."),
    offset: int = Query(default=0, ge=0, description="Number of records to skip."),
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> ModelGateComparisonsResponse:
    """
    Return a paginated list of offline model gate comparison records.

    Each record represents the shadow worker comparing the geometry IoU gate
    score of the current ``shadow``-stage model against the current
    ``production``-stage model.  ``confidence_delta`` is a model-level metric
    from offline evaluation — no candidate inference runs on live pages.

    Filterable by ``job_id`` and/or ``status``.  Results are ordered by
    ``created_at DESC`` so the most recent records appear first.

    **Auth:** admin role required.
    """
    q = db.query(ShadowEvaluation)
    if job_id is not None:
        q = q.filter(ShadowEvaluation.job_id == job_id)
    if status is not None:
        q = q.filter(ShadowEvaluation.status == status)

    total: int = q.count()
    rows: list[ShadowEvaluation] = (
        q.order_by(ShadowEvaluation.created_at.desc()).offset(offset).limit(limit).all()
    )

    logger.debug(
        "list_model_gate_comparisons: job_id=%r status=%r total=%d offset=%d limit=%d",
        job_id,
        status,
        total,
        offset,
        limit,
    )
    return ModelGateComparisonsResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            ModelGateComparisonRecord(
                eval_id=row.eval_id,
                job_id=row.job_id,
                page_id=row.page_id,
                page_status=row.page_status,
                confidence_delta=row.confidence_delta,
                status=row.status,
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ],
    )


# ── Promotion audit list ──────────────────────────────────────────────────────


class PromotionAuditRecord(BaseModel):
    """Single row from the model_promotion_audit table."""

    audit_id: str
    action: str
    service_name: str
    candidate_model_id: str
    previous_model_id: str | None
    promoted_by_user_id: str
    forced: bool
    failed_gates_bypassed: list[str] | None
    reason: str | None
    notes: str | None
    created_at: datetime


class PromotionAuditResponse(BaseModel):
    """Paginated list of promotion audit records."""

    total: int
    limit: int
    offset: int
    items: list[PromotionAuditRecord]


@router.get(
    "/v1/admin/promotion-audit",
    response_model=PromotionAuditResponse,
    status_code=200,
    summary="List model promotion and rollback audit records",
)
def list_promotion_audit(
    service: str | None = Query(default=None, description="Filter by service name (iep1a, iep1b)."),
    action: str | None = Query(default=None, description="Filter by action: promote | rollback."),
    model_id: str | None = Query(default=None, description="Filter by candidate_model_id."),
    limit: int = Query(default=50, ge=1, le=200, description="Max records to return."),
    offset: int = Query(default=0, ge=0, description="Number of records to skip."),
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> PromotionAuditResponse:
    """
    Return a paginated, reverse-chronological list of promotion and rollback
    audit events.

    Each record captures who performed the action, whether gate checks were
    bypassed, and which model was displaced.

    **Auth:** admin role required.
    """
    q = db.query(ModelPromotionAudit)
    if service is not None:
        q = q.filter(ModelPromotionAudit.service_name == service)
    if action is not None:
        q = q.filter(ModelPromotionAudit.action == action)
    if model_id is not None:
        q = q.filter(ModelPromotionAudit.candidate_model_id == model_id)

    total: int = q.count()
    rows: list[ModelPromotionAudit] = (
        q.order_by(ModelPromotionAudit.created_at.desc()).offset(offset).limit(limit).all()
    )

    logger.debug(
        "list_promotion_audit: service=%r action=%r total=%d offset=%d limit=%d",
        service,
        action,
        total,
        offset,
        limit,
    )
    return PromotionAuditResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            PromotionAuditRecord(
                audit_id=row.audit_id,
                action=row.action,
                service_name=row.service_name,
                candidate_model_id=row.candidate_model_id,
                previous_model_id=row.previous_model_id,
                promoted_by_user_id=row.promoted_by_user_id,
                forced=row.forced,
                failed_gates_bypassed=row.failed_gates_bypassed,
                reason=row.reason,
                notes=row.notes,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )
