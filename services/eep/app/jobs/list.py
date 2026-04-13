"""
services/eep/app/jobs/list.py
-------------------------------
Packet 7.3 — GET /v1/jobs: paginated, filtered job list.

Implements:
  GET /v1/jobs

Auth / scoping:
  - require_user: JWT bearer required.
  - Non-admin: results are scoped to jobs.created_by = current_user.user_id.
  - Admin: all jobs are visible (no created_by filter applied).

Query parameters:
  search       — case-insensitive substring match against job_id OR collection_id.
  status       — exact match against jobs.status ('queued'|'running'|'done'|'failed').
  pipeline_mode — exact match ('preprocess'|'layout').
  created_by   — restrict to a specific owner. Admin-only; silently ignored for
                 non-admin callers (their results are already owner-scoped).
  from_date    — lower bound on created_at (inclusive, UTC datetime).
  to_date      — upper bound on created_at (inclusive, UTC datetime).
  page         — 1-indexed page number (default 1).
  page_size    — items per page (default 50, max 200).

Sorting: created_at DESC (newest first).

Response:
  JobListResponse — {total, page, page_size, items: list[JobStatusSummary]}

Error responses:
  401 — missing or invalid bearer token
  403 — token present but insufficient role (require_user accepts any role)

Notes:
  - job.status is the worker-maintained denormalised status field; no leaf-page
    aggregation is performed for the list view (that is reserved for GET /v1/jobs/{id}).
  - No schema migration needed: all columns referenced exist from Phase 1.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_user
from services.eep.app.db.models import Job, User
from services.eep.app.db.session import get_session
from shared.schemas.eep import JobStatusSummary

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Response schema ─────────────────────────────────────────────────────────────


class JobListSummary(JobStatusSummary):
    """
    Extends JobStatusSummary with the display username for the job owner.

    Added field:
        created_by_username — username of the job creator (from users.username);
                              None when created_by is null or the user record no
                              longer exists (e.g. deactivated account).
    """

    created_by_username: str | None = None


class JobListResponse(BaseModel):
    """
    Paginated job list response for GET /v1/jobs.

    Fields:
        total      — total matching jobs (ignoring page/page_size)
        page       — current 1-indexed page
        page_size  — maximum items returned per page
        items      — job summaries for this page (each includes created_by_username)
    """

    total: int
    page: int
    page_size: int
    items: list[JobListSummary]


# ── Endpoint ────────────────────────────────────────────────────────────────────


@router.get(
    "/v1/jobs",
    response_model=JobListResponse,
    status_code=200,
    tags=["jobs"],
    summary="List jobs",
)
def list_jobs(
    search: str | None = Query(
        default=None,
        description="Case-insensitive substring match against job_id or collection_id.",
    ),
    status: str | None = Query(
        default=None,
        description="Filter by job status: queued | running | done | failed.",
    ),
    pipeline_mode: str | None = Query(
        default=None,
        description="Filter by pipeline mode: preprocess | layout.",
    ),
    created_by: str | None = Query(
        default=None,
        description="Filter by owner user_id. Admin-only; ignored for non-admin callers.",
    ),
    from_date: datetime | None = Query(
        default=None,
        description="Lower bound on created_at (inclusive, ISO 8601 UTC datetime).",
    ),
    to_date: datetime | None = Query(
        default=None,
        description="Upper bound on created_at (inclusive, ISO 8601 UTC datetime).",
    ),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page."),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> JobListResponse:
    """
    Return a paginated list of jobs visible to the caller.

    Non-admin users see only jobs they created. Admin users see all jobs.
    Results are sorted by created_at descending (newest first).

    **Filters**

    - ``search`` — case-insensitive substring on job_id or collection_id
    - ``status`` — queued / running / done / failed
    - ``pipeline_mode`` — preprocess / layout
    - ``created_by`` — admin only; silently ignored for non-admin callers
    - ``from_date`` / ``to_date`` — created_at range (ISO 8601 UTC)

    **Pagination**

    - ``page`` — 1-indexed (default 1)
    - ``page_size`` — items per page, max 200 (default 50)
    """
    q = db.query(Job, User).outerjoin(User, User.user_id == Job.created_by)

    # ── Ownership scoping ────────────────────────────────────────────────────────
    if user.role != "admin":
        # Non-admin: restrict to caller's own jobs.
        q = q.filter(Job.created_by == user.user_id)
    elif created_by is not None:
        # Admin may further narrow to a specific owner.
        q = q.filter(Job.created_by == created_by)

    # ── Filters ──────────────────────────────────────────────────────────────────
    if search is not None:
        pattern = f"%{search}%"
        q = q.filter(Job.job_id.ilike(pattern) | Job.collection_id.ilike(pattern))

    if status is not None:
        q = q.filter(Job.status == status)

    if pipeline_mode is not None:
        q = q.filter(Job.pipeline_mode == pipeline_mode)

    if from_date is not None:
        q = q.filter(Job.created_at >= from_date)

    if to_date is not None:
        q = q.filter(Job.created_at <= to_date)

    # ── Count ────────────────────────────────────────────────────────────────────
    total: int = q.with_entities(sqlfunc.count(Job.job_id)).scalar() or 0

    # ── Sort and paginate ─────────────────────────────────────────────────────────
    offset = (page - 1) * page_size
    rows: list[tuple[Job, User | None]] = cast(
        list[tuple[Job, User | None]],
        q.order_by(Job.created_at.desc()).offset(offset).limit(page_size).all(),
    )

    items = [
        JobListSummary(
            job_id=job.job_id,
            collection_id=job.collection_id,
            material_type=job.material_type,  # type: ignore[arg-type]
            pipeline_mode=job.pipeline_mode,  # type: ignore[arg-type]
            ptiff_qa_mode=job.ptiff_qa_mode,  # type: ignore[arg-type]
            policy_version=job.policy_version,
            shadow_mode=job.shadow_mode,
            created_by=job.created_by,
            created_by_username=u.username if u is not None else None,
            status=job.status,  # type: ignore[arg-type]
            page_count=job.page_count,
            accepted_count=job.accepted_count,
            review_count=job.review_count,
            failed_count=job.failed_count,
            pending_human_correction_count=job.pending_human_correction_count,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )
        for job, u in rows
    ]

    logger.debug(
        "list_jobs: user=%s role=%s total=%d page=%d page_size=%d",
        user.user_id,
        user.role,
        total,
        page,
        page_size,
    )
    return JobListResponse(total=total, page=page, page_size=page_size, items=items)
