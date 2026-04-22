"""
services/eep/app/correction/reject.py
----------------------------------------
Packet 5.4 — Correction reject path.

Implements:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction-reject

A reviewer rejects a page that is in pending_human_correction, permanently
routing it to the 'review' terminal state rather than submitting a correction.

Semantics (non-negotiable):
  - Only pages in pending_human_correction are accepted (409 otherwise).
  - State transition: pending_human_correction → review (leaf-final).
  - review_reasons is set to ["human_correction_rejected"] on the page row.
  - page_lineage.human_corrected is set to False (page was not corrected).
  - Optional reviewer notes are stored in page_lineage.reviewer_notes.
  - Lineage row must exist; if missing, the request fails with 500.
  - 'review' is a permanent terminal state; no further transitions are possible.
  - All state transitions use advance_page_state() (state machine never bypassed).

Error responses:
  404 — job not found / page not found
  409 — page not in pending_human_correction state
  500 — data-integrity failure: lineage row missing

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.lineage import update_lineage_completion
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from services.eep.app.jobs.summary import derive_job_status, leaf_pages_from_pages

logger = logging.getLogger(__name__)
router = APIRouter()

_REJECT_REASON = "human_correction_rejected"


def _sync_job_summary(db: Session, job: Job) -> None:
    pages = db.query(JobPage).filter_by(job_id=job.job_id).all()
    leaf_pages = leaf_pages_from_pages(pages)
    now = datetime.now(timezone.utc)

    job.accepted_count = sum(1 for page in leaf_pages if page.status == "accepted")
    job.review_count = sum(1 for page in leaf_pages if page.status == "review")
    job.failed_count = sum(1 for page in leaf_pages if page.status == "failed")
    job.pending_human_correction_count = sum(
        1 for page in leaf_pages if page.status == "pending_human_correction"
    )
    job.status = derive_job_status(leaf_pages)
    if job.status in {"done", "failed"}:
        job.completed_at = job.completed_at or now
    else:
        job.completed_at = None


# ── Request / Response schemas ───────────────────────────────────────────────────


class CorrectionRejectRequest(BaseModel):
    """
    Optional request body for POST …/correction-reject.

    Fields:
        notes — optional reviewer notes explaining why the page was rejected.
                Stored in page_lineage.reviewer_notes.
    """

    notes: str | None = None


class CorrectionRejectResponse(BaseModel):
    """Response for POST …/correction-reject."""

    page_number: int
    new_state: str = "review"


# ── Internal helpers ────────────────────────────────────────────────────────────


def _fetch_job_or_404(db: Session, job_id: str) -> Job:
    """Return the Job row or raise HTTP 404."""
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    return job


# ── Endpoint ────────────────────────────────────────────────────────────────────


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/correction-reject",
    response_model=CorrectionRejectResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction"],
    summary="Reject a page from the human correction queue",
)
def reject_correction(
    job_id: str,
    page_number: int,
    body: CorrectionRejectRequest = CorrectionRejectRequest(),
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> CorrectionRejectResponse:
    """
    Permanently route a page in pending_human_correction to the 'review' state.

    The reviewer declines to submit a correction. The page is marked with
    review_reasons=["human_correction_rejected"] and transitions to 'review',
    a leaf-final terminal state from which no further pipeline processing occurs.

    **Error responses**

    - ``404`` — job or page not found
    - ``409`` — page is not in 'pending_human_correction' state
    - ``500`` — data-integrity failure: lineage row missing
    """
    # Step 1 — Load job and page
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    page: JobPage | None = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.sub_page_index == sub_page_index,
        )
        .first()
    )

    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Page {page_number} sub_page_index {sub_page_index!r} "
                f"of job {job_id!r} not found."
            ),
        )

    if page.status != "pending_human_correction":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Page {page_number} of job {job_id!r} is in state "
                f"{page.status!r}, not 'pending_human_correction'."
            ),
        )

    # Step 2 — Fetch required lineage row
    # Data-integrity requirement: every page in pending_human_correction must
    # have a lineage row.
    lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == job_id,
            PageLineage.page_number == page_number,
            PageLineage.sub_page_index == sub_page_index,
        )
        .first()
    )

    if lineage is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: no lineage row for job {job_id!r} "
                f"page {page_number} sub_page_index {sub_page_index!r}. "
                "Page cannot be rejected without an existing lineage record."
            ),
        )

    # Step 3 — State transition: pending_human_correction → review
    advanced = advance_page_state(
        db,
        page.page_id,
        from_state="pending_human_correction",
        to_state="review",
        acceptance_decision="review",
        routing_path=_REJECT_REASON,
    )

    if not advanced:
        logger.warning(
            "Correction reject CAS miss: job=%s page_id=%s page_number=%d "
            "(concurrent update or state already changed)",
            job_id,
            page.page_id,
            page_number,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Page state changed concurrently; please retry.",
        )

    # Step 4 — Record rejection on the page row
    page.status = "review"
    page.acceptance_decision = "review"
    page.routing_path = _REJECT_REASON
    page.review_reasons = [_REJECT_REASON]

    # Step 5 — Update lineage: mark page as NOT human-corrected, store notes
    lineage.human_corrected = False
    if body.notes is not None:
        lineage.reviewer_notes = body.notes
    update_lineage_completion(
        db,
        lineage.lineage_id,
        acceptance_decision="review",
        acceptance_reason=_REJECT_REASON,
        routing_path=_REJECT_REASON,
        total_processing_ms=page.processing_time_ms,
        output_image_uri=page.output_image_uri,
    )

    _sync_job_summary(db, job)
    db.flush()
    db.commit()

    logger.info(
        "Correction rejected: job=%s page=%d sub_page_index=%s → review",
        job_id,
        page_number,
        sub_page_index,
    )
    return CorrectionRejectResponse(page_number=page_number)
