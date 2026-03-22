"""
services/eep/app/correction/ptiff_qa.py
----------------------------------------
Packet 5.0a — PTIFF QA workflow: job-level gate endpoints and gate-release logic.

Implements the PTIFF QA checkpoint defined in spec Section 3.1.

Endpoints:
  GET  /v1/jobs/{job_id}/ptiff-qa
      Return job-level QA status with per-page entries.
  POST /v1/jobs/{job_id}/ptiff-qa/approve-all
      Approve all pages in ptiff_qa_pending; trigger gate release when ready.
  POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve
      Approve a single page in ptiff_qa_pending; trigger gate release when ready.
  POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit
      Transition a ptiff_qa_pending page to pending_human_correction.

PTIFF QA semantics (non-negotiable):
  - ptiff_qa_pending is NOT a terminal state; the job remains 'running'.
  - Approval records intent only; NO state transition on per-page approval.
  - Gate releases only when ALL conditions are satisfied:
      * No ptiff_qa_pending page is unapproved (ptiff_qa_approved == False).
      * No page is in pending_human_correction.
  - Gate release is executed in a single DB transaction and is idempotent:
      if no ptiff_qa_pending pages remain, _check_and_release_ptiff_qa
      returns False without performing any updates.
  - All state transitions use advance_page_state() (shared/state_machine.py).
  - No transition to layout_detection or accepted is permitted outside gate release.

Gate release targets (spec Section 3.1):
  pipeline_mode == 'preprocess' → accepted
  pipeline_mode == 'layout'     → layout_detection

Error responses:
  404 — job not found (all endpoints)
  409 — page not in ptiff_qa_pending state (approve / edit endpoints)

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from shared.state_machine import validate_transition

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Response models ─────────────────────────────────────────────────────────────


class PtiffQaPageEntry(BaseModel):
    """Per-page entry in the PTIFF QA status response."""

    page_number: int
    sub_page_index: int | None
    current_state: str
    approval_status: str  # "approved" | "pending"
    needs_correction: bool


class PtiffQaStatusResponse(BaseModel):
    """Job-level PTIFF QA status response for GET /v1/jobs/{job_id}/ptiff-qa."""

    job_id: str
    ptiff_qa_mode: str
    total_pages: int
    pages_pending: int
    pages_approved: int
    pages_in_correction: int
    is_gate_ready: bool
    pages: list[PtiffQaPageEntry]


class ApprovePageResponse(BaseModel):
    """Response for POST …/ptiff-qa/approve (single page)."""

    page_number: int
    approved: bool
    gate_released: bool


class ApproveAllResponse(BaseModel):
    """Response for POST /v1/jobs/{job_id}/ptiff-qa/approve-all."""

    approved_count: int
    gate_released: bool


class EditPageResponse(BaseModel):
    """Response for POST …/ptiff-qa/edit."""

    page_number: int
    new_state: str


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


def _leaf_pages(db: Session, job_id: str) -> list[JobPage]:
    """Return all non-split leaf pages for the job."""
    return db.query(JobPage).filter(JobPage.job_id == job_id, JobPage.status != "split").all()


def _is_gate_satisfied(pages: list[JobPage]) -> bool:
    """
    Return True when all PTIFF QA gate conditions are met.

    Gate is satisfied only if:
      - No ptiff_qa_pending page has ptiff_qa_approved == False.
      - No page is in pending_human_correction.

    Args:
        pages: Leaf pages for the job (split-parent records excluded).

    Returns:
        True when gate conditions are met, False otherwise.
    """
    for p in pages:
        if p.status == "ptiff_qa_pending" and not p.ptiff_qa_approved:
            return False
        if p.status == "pending_human_correction":
            return False
    return True


def _check_and_release_ptiff_qa(db: Session, job: Job, pages: list[JobPage]) -> bool:
    """
    Evaluate gate conditions and release the PTIFF QA gate if satisfied.

    Gate release transitions all ptiff_qa_pending pages atomically:
      - pipeline_mode == 'preprocess' → accepted
      - pipeline_mode == 'layout'     → layout_detection

    This function is idempotent: if there are no ptiff_qa_pending pages,
    it returns False without performing any DB updates.

    Must be called within an open session transaction. The caller is
    responsible for committing (or rolling back) after this call.

    Args:
        db:    SQLAlchemy session (caller owns transaction and commit).
        job:   The Job ORM record (provides pipeline_mode).
        pages: Current leaf pages for the job (pre-fetched, post-flush).

    Returns:
        True if the gate was released (pages transitioned), False otherwise.
    """
    pages_to_release = [p for p in pages if p.status == "ptiff_qa_pending"]

    # Idempotent: nothing to release when no QA pages remain.
    if not pages_to_release:
        return False

    if not _is_gate_satisfied(pages):
        return False

    target_state = "accepted" if job.pipeline_mode == "preprocess" else "layout_detection"

    # Validate transition before touching the DB (fail-fast on programming errors).
    validate_transition("ptiff_qa_pending", target_state)

    for page in pages_to_release:
        advanced = advance_page_state(
            db,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state=target_state,
        )
        if not advanced:
            logger.warning(
                "PTIFF QA gate release CAS miss: job=%s page_id=%s "
                "(concurrent update or already transitioned)",
                job.job_id,
                page.page_id,
            )

    logger.info(
        "PTIFF QA gate released: job=%s pipeline_mode=%s target=%s page_count=%d",
        job.job_id,
        job.pipeline_mode,
        target_state,
        len(pages_to_release),
    )
    return True


# ── Endpoints ───────────────────────────────────────────────────────────────────


@router.get(
    "/v1/jobs/{job_id}/ptiff-qa",
    response_model=PtiffQaStatusResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Get PTIFF QA status for a job",
)
def get_ptiff_qa_status(
    job_id: str,
    db: Session = Depends(get_session),
) -> PtiffQaStatusResponse:
    """
    Return the current PTIFF QA gate status for a job.

    Includes job-level aggregate counts and a per-page list with approval
    and correction status. Read-only; no state changes.

    **Error responses**

    - ``404`` — job not found
    """
    job = _fetch_job_or_404(db, job_id)
    pages = _leaf_pages(db, job_id)

    pages_pending = sum(
        1 for p in pages if p.status == "ptiff_qa_pending" and not p.ptiff_qa_approved
    )
    pages_approved = sum(1 for p in pages if p.status == "ptiff_qa_pending" and p.ptiff_qa_approved)
    pages_in_correction = sum(1 for p in pages if p.status == "pending_human_correction")

    # Gate is ready when at least one QA page exists and all conditions are met.
    qa_pages_exist = any(p.status == "ptiff_qa_pending" for p in pages)
    is_gate_ready = qa_pages_exist and pages_pending == 0 and pages_in_correction == 0

    page_entries = [
        PtiffQaPageEntry(
            page_number=p.page_number,
            sub_page_index=p.sub_page_index,
            current_state=p.status,
            approval_status="approved" if p.ptiff_qa_approved else "pending",
            needs_correction=(p.status == "pending_human_correction"),
        )
        for p in pages
    ]

    return PtiffQaStatusResponse(
        job_id=job_id,
        ptiff_qa_mode=job.ptiff_qa_mode,
        total_pages=len(pages),
        pages_pending=pages_pending,
        pages_approved=pages_approved,
        pages_in_correction=pages_in_correction,
        is_gate_ready=is_gate_ready,
        pages=page_entries,
    )


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve",
    response_model=ApprovePageResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Approve a single PTIFF QA page",
)
def approve_page(
    job_id: str,
    page_number: int,
    db: Session = Depends(get_session),
) -> ApprovePageResponse:
    """
    Record approval intent for a page in ptiff_qa_pending.

    Approval does NOT change the page state immediately. If all gate
    conditions are met after this approval, the gate is released
    (all ptiff_qa_pending pages transition in a single transaction).

    **Error responses**

    - ``404`` — job not found
    - ``409`` — page is not in 'ptiff_qa_pending' state
    """
    job = _fetch_job_or_404(db, job_id)

    qa_pages: list[JobPage] = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.status == "ptiff_qa_pending",
        )
        .all()
    )

    if not qa_pages:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Page {page_number} of job {job_id!r} is not in " "'ptiff_qa_pending' state."),
        )

    for page in qa_pages:
        page.ptiff_qa_approved = True

    # Flush approval so _leaf_pages re-fetch sees the updated flags.
    db.flush()

    all_pages = _leaf_pages(db, job_id)
    gate_released = _check_and_release_ptiff_qa(db, job, all_pages)

    db.commit()

    logger.info(
        "PTIFF QA approve: job=%s page=%d gate_released=%s",
        job_id,
        page_number,
        gate_released,
    )
    return ApprovePageResponse(
        page_number=page_number,
        approved=True,
        gate_released=gate_released,
    )


@router.post(
    "/v1/jobs/{job_id}/ptiff-qa/approve-all",
    response_model=ApproveAllResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Approve all pending PTIFF QA pages",
)
def approve_all(
    job_id: str,
    db: Session = Depends(get_session),
) -> ApproveAllResponse:
    """
    Approve all pages currently in ptiff_qa_pending for this job.

    Only records approval for pages currently in ptiff_qa_pending.
    Already-approved pages are idempotently skipped. If all gate conditions
    are satisfied after recording approvals, the gate is released.

    Calling this endpoint when no ptiff_qa_pending pages exist is a no-op:
    approved_count=0 and gate_released=False (idempotent).

    **Error responses**

    - ``404`` — job not found
    """
    job = _fetch_job_or_404(db, job_id)

    qa_pages: list[JobPage] = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.status == "ptiff_qa_pending",
        )
        .all()
    )

    approved_count = 0
    for page in qa_pages:
        if not page.ptiff_qa_approved:
            page.ptiff_qa_approved = True
            approved_count += 1

    db.flush()

    all_pages = _leaf_pages(db, job_id)
    gate_released = _check_and_release_ptiff_qa(db, job, all_pages)

    db.commit()

    logger.info(
        "PTIFF QA approve-all: job=%s approved_count=%d gate_released=%s",
        job_id,
        approved_count,
        gate_released,
    )
    return ApproveAllResponse(approved_count=approved_count, gate_released=gate_released)


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit",
    response_model=EditPageResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Send a PTIFF QA page to human correction",
)
def edit_page(
    job_id: str,
    page_number: int,
    db: Session = Depends(get_session),
) -> EditPageResponse:
    """
    Transition a ptiff_qa_pending page to pending_human_correction.

    Uses the state machine validator. Clears any prior approval flag so the
    page must be re-approved after correction is submitted.

    After human correction (handled in later packets), the page returns to
    ptiff_qa_pending and must be approved again before the gate can release.

    **Error responses**

    - ``404`` — job not found
    - ``409`` — page is not in 'ptiff_qa_pending' state
    """
    _fetch_job_or_404(db, job_id)

    qa_pages: list[JobPage] = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.status == "ptiff_qa_pending",
        )
        .all()
    )

    if not qa_pages:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Page {page_number} of job {job_id!r} is not in " "'ptiff_qa_pending' state."),
        )

    for page in qa_pages:
        advanced = advance_page_state(
            db,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state="pending_human_correction",
        )
        if not advanced:
            logger.warning(
                "PTIFF QA edit CAS miss: job=%s page_id=%s page_number=%d",
                job_id,
                page.page_id,
                page_number,
            )
        # Clear approval flag: page must be re-approved after correction.
        page.ptiff_qa_approved = False

    db.commit()

    logger.info(
        "PTIFF QA edit: job=%s page=%d → pending_human_correction",
        job_id,
        page_number,
    )
    return EditPageResponse(
        page_number=page_number,
        new_state="pending_human_correction",
    )
