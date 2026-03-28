"""
services/eep/app/correction/queue.py
--------------------------------------
Packet 5.1 — Correction queue read endpoints.

Provides two read-only endpoints for the human correction workflow:

  GET /v1/correction-queue
      List all pages currently in pending_human_correction, with optional
      filtering by job_id, material_type, and review_reason.
      Sorted by waiting duration (oldest first). Paginated via offset/limit.

  GET /v1/correction-queue/{job_id}/{page_number}
      Return the full correction workspace for a single page.
      Optional query param sub_page_index selects a split sub-page.
      When multiple sub-pages are pending and sub_page_index is not provided,
      returns 422 so the client can re-request with an explicit sub-page.

Both endpoints are read-only. No state transitions occur here.

Error responses:
  404 — job not found / page not found
  409 — page exists but is not in pending_human_correction
  422 — multiple sub-pages pending and sub_page_index not specified

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.correction.workspace_assembly import (
    PageNotInCorrectionError,
    assemble_correction_workspace,
)
from services.eep.app.correction.workspace_schema import CorrectionWorkspaceResponse
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Response models ─────────────────────────────────────────────────────────────


class CorrectionQueueEntry(BaseModel):
    """
    Single entry in the correction queue list.

    Fields:
        job_id            — parent job identifier
        page_number       — 1-indexed page number
        sub_page_index    — 0/1 for split children; None for unsplit pages
        material_type     — job material type (book | newspaper | archival_document)
        pipeline_mode     — preprocess | layout
        review_reasons    — reason codes that caused the correction routing
        waiting_since     — when the page entered pending_human_correction
                            (job_pages.status_updated_at; None if not recorded)
        output_image_uri  — best available preprocessing output artifact URI
    """

    job_id: str
    page_number: int
    sub_page_index: int | None
    material_type: str
    pipeline_mode: str
    review_reasons: list[str]
    waiting_since: datetime | None
    output_image_uri: str | None


class CorrectionQueueResponse(BaseModel):
    """
    Paginated correction queue list response.

    Fields:
        total   — total matching pages (ignoring offset/limit)
        offset  — number of items skipped
        limit   — maximum items returned
        items   — page entries for this page of results
    """

    total: int
    offset: int
    limit: int
    items: list[CorrectionQueueEntry]


# ── Endpoints ───────────────────────────────────────────────────────────────────


@router.get(
    "/v1/correction-queue",
    response_model=CorrectionQueueResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction-queue"],
    summary="List pages pending human correction",
)
def list_correction_queue(
    job_id: str | None = Query(default=None, description="Filter by job ID"),
    material_type: str | None = Query(default=None, description="Filter by material type"),
    review_reason: str | None = Query(default=None, description="Filter by review reason code"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum items to return"),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> CorrectionQueueResponse:
    """
    Return a paginated list of pages currently in pending_human_correction.

    Results are sorted oldest-first by waiting duration
    (job_pages.status_updated_at ASC, NULLs last).

    **Filters**

    - ``job_id`` — restrict to a specific job
    - ``material_type`` — one of book / newspaper / archival_document
    - ``review_reason`` — pages whose review_reasons array contains this code

    **Auth:** enforced in Phase 7 (Packet 7.1) — not yet active.
    """
    q = (
        db.query(JobPage, Job)
        .join(Job, Job.job_id == JobPage.job_id)
        .filter(JobPage.status == "pending_human_correction")
    )

    # Scope to the authenticated user's own jobs unless they are admin.
    if user.role != "admin":
        q = q.filter(Job.created_by == user.user_id)

    if job_id is not None:
        q = q.filter(JobPage.job_id == job_id)
    if material_type is not None:
        q = q.filter(Job.material_type == material_type)
    if review_reason is not None:
        # JSONB array containment: review_reasons @> '["reason"]'
        q = q.filter(JobPage.review_reasons.contains([review_reason]))

    total: int = q.with_entities(func.count(JobPage.page_id)).scalar() or 0

    rows: list[Any] = q.order_by(JobPage.status_updated_at.asc()).offset(offset).limit(limit).all()

    items = [
        CorrectionQueueEntry(
            job_id=page.job_id,
            page_number=page.page_number,
            sub_page_index=page.sub_page_index,
            material_type=job.material_type,
            pipeline_mode=job.pipeline_mode,
            review_reasons=list(page.review_reasons) if page.review_reasons else [],
            waiting_since=page.status_updated_at,
            output_image_uri=page.output_image_uri,
        )
        for page, job in rows
    ]

    return CorrectionQueueResponse(total=total, offset=offset, limit=limit, items=items)


@router.get(
    "/v1/correction-queue/{job_id}/{page_number}",
    response_model=CorrectionWorkspaceResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction-queue"],
    summary="Get correction workspace for a page",
)
def get_correction_workspace(
    job_id: str,
    page_number: int,
    sub_page_index: int | None = Query(
        default=None,
        description=(
            "Sub-page index (0 or 1) for split pages. "
            "Required when multiple sub-pages are pending correction."
        ),
    ),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> CorrectionWorkspaceResponse:
    """
    Return the full correction workspace for a page in pending_human_correction.

    When ``sub_page_index`` is not provided and exactly one sub-page exists for
    the given page_number, it is selected automatically. When multiple sub-pages
    are simultaneously pending and ``sub_page_index`` is not specified, 422 is
    returned so the caller can re-request with an explicit index.

    **Error responses**

    - ``404`` — job not found or page not found
    - ``409`` — page exists but is not in 'pending_human_correction'
    - ``422`` — multiple sub-pages pending; re-request with sub_page_index
    """
    # Ownership check: look up job first, return 404 if missing, 403 if not owned.
    _job: Job | None = db.get(Job, job_id)
    if _job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    assert_job_ownership(_job, user)

    resolved_sub: int | None = sub_page_index

    if resolved_sub is None:
        # Determine sub_page_index from the set of pending sub-pages.
        pending: list[JobPage] = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == job_id,
                JobPage.page_number == page_number,
                JobPage.status == "pending_human_correction",
            )
            .all()
        )
        if len(pending) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Page {page_number} of job {job_id!r} has "
                    f"{len(pending)} sub-pages in pending_human_correction. "
                    "Specify sub_page_index to select one."
                ),
            )
        if len(pending) == 1:
            # Carry the resolved sub_page_index into the assembly call.
            resolved_sub = pending[0].sub_page_index
        # If pending is empty, let assemble_correction_workspace raise the error.

    try:
        return assemble_correction_workspace(db, job_id, page_number, resolved_sub)
    except PageNotInCorrectionError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
