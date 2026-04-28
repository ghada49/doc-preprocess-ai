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
      returns an empty list without performing any updates.
  - All state transitions use advance_page_state() (shared/state_machine.py).
  - No transition to layout_detection or accepted is permitted outside gate release.

Gate release targets (spec Section 3.1):
  pipeline_mode == 'preprocess' → accepted
  pipeline_mode == 'layout'     → layout_detection

Post-release side effects (executed within the same DB transaction, before commit):
  - layout mode: each released page is enqueued to Redis for layout detection.
  - Any released page that is a split child (sub_page_index is not None) triggers
    _maybe_close_split_parent, which closes the parent to 'split' if all siblings
    are now worker-terminal.

Error responses:
  404 — job not found (all endpoints)
  409 — page not in ptiff_qa_pending state (approve / edit endpoints)

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router                      — FastAPI APIRouter (mount at app level in main.py)
  _WORKER_TERMINAL_STATES     — frozenset used by apply.py split path (Step E)
  _maybe_close_split_parent   — DB-querying helper used by approve endpoints and
                                 any caller that does not have pre-loaded children
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import redis
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.correction._split_parent import _maybe_close_split_parent
from services.eep.app.db.lineage import update_lineage_completion
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from services.eep.app.jobs.summary import derive_job_status, leaf_pages_from_pages
from services.eep.app.queue import enqueue_page_task
from services.eep.app.redis_client import get_redis
from services.eep.app.scaling.normal_scaler import maybe_trigger_scale_up
from shared.schemas.eep import TERMINAL_PAGE_STATES
from shared.schemas.queue import PageTask
from shared.state_machine import validate_transition

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Exported constants ───────────────────────────────────────────────────────────
_WORKER_TERMINAL_STATES = TERMINAL_PAGE_STATES


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
      - No page is in pending_human_correction, EXCEPT split parents.

    A split parent is a page with sub_page_index IS None whose page_number
    also appears among the children (sub_page_index IS NOT None) in the list.
    Split parents must not block the gate. In the reviewer-driven split flow,
    child pages are the real review units, and any parent row that still
    appears in pending_human_correction should be ignored by gate evaluation
    once matching children exist.

    Args:
        pages: Leaf pages for the job (may include split-parent records).

    Returns:
        True when gate conditions are met, False otherwise.
    """
    # Page numbers that have at least one child (sub_page_index IS NOT None).
    # These correspond to parent rows that should not block child-page PTIFF QA.
    child_page_numbers: frozenset[int] = frozenset(
        p.page_number for p in pages if p.sub_page_index is not None
    )

    for p in pages:
        if p.status == "ptiff_qa_pending" and not p.ptiff_qa_approved:
            return False
        if p.status == "pending_human_correction":
            # Split parent: managed by _maybe_close_split_parent; must not
            # block the PTIFF QA gate for its children.
            if p.sub_page_index is None and p.page_number in child_page_numbers:
                continue
            return False
    return True


def _check_and_release_ptiff_qa(db: Session, job: Job, pages: list[JobPage]) -> list[JobPage]:
    """
    Evaluate gate conditions and release the PTIFF QA gate if satisfied.

    Gate release transitions all ptiff_qa_pending pages atomically:
      - pipeline_mode == 'preprocess' → accepted
      - pipeline_mode == 'layout'     → layout_detection

    Released pages have their in-memory status updated (page.status = target_state)
    so that subsequent identity-map lookups (e.g. in _maybe_close_split_parent)
    reflect the new states without needing a DB re-query.

    This function is idempotent: if there are no ptiff_qa_pending pages,
    it returns an empty list without performing any DB updates.

    Must be called within an open session transaction. The caller is
    responsible for committing (or rolling back) after this call.

    Args:
        db:    SQLAlchemy session (caller owns transaction and commit).
        job:   The Job ORM record (provides pipeline_mode).
        pages: Current leaf pages for the job (pre-fetched, post-flush).

    Returns:
        List of pages that were released (transitioned). Empty if no release.
    """
    pages_to_release = [p for p in pages if p.status == "ptiff_qa_pending"]

    # Idempotent: nothing to release when no QA pages remain.
    if not pages_to_release:
        return []

    if not _is_gate_satisfied(pages):
        return []

    target_state = "accepted" if job.pipeline_mode == "preprocess" else "layout_detection"

    # Validate transition before touching the DB (fail-fast on programming errors).
    validate_transition("ptiff_qa_pending", target_state)

    released_pages: list[JobPage] = []
    for page in pages_to_release:
        transition_kwargs = {}
        if target_state == "accepted":
            transition_kwargs = {
                "acceptance_decision": "accepted",
                "routing_path": "ptiff_qa_approved",
            }
        advanced = advance_page_state(
            db,
            page.page_id,
            from_state="ptiff_qa_pending",
            to_state=target_state,
            **transition_kwargs,
        )
        if not advanced:
            logger.warning(
                "PTIFF QA gate release CAS miss: job=%s page_id=%s "
                "(concurrent update or already transitioned)",
                job.job_id,
                page.page_id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Page state changed concurrently during PTIFF QA release; please retry.",
            )

        # Mirror new state onto the ORM object so identity-map lookups
        # (e.g. in _maybe_close_split_parent) see the updated status.
        page.status = target_state
        if target_state == "accepted":
            page.acceptance_decision = "accepted"
            page.routing_path = "ptiff_qa_approved"
            lineage = (
                db.query(PageLineage)
                .filter(
                    PageLineage.job_id == page.job_id,
                    PageLineage.page_number == page.page_number,
                    PageLineage.sub_page_index == page.sub_page_index,
                )
                .first()
            )
            if lineage is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        f"Data-integrity failure: no lineage row for job {page.job_id!r} "
                        f"page {page.page_number} sub_page_index {page.sub_page_index!r}."
                    ),
                )
            update_lineage_completion(
                db,
                lineage.lineage_id,
                acceptance_decision="accepted",
                acceptance_reason="PTIFF QA approved",
                routing_path="ptiff_qa_approved",
                total_processing_ms=page.processing_time_ms,
                output_image_uri=page.output_image_uri,
            )
        released_pages.append(page)

    logger.info(
        "PTIFF QA gate released: job=%s pipeline_mode=%s target=%s page_count=%d",
        job.job_id,
        job.pipeline_mode,
        target_state,
        len(released_pages),
    )
    return released_pages


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
    user: CurrentUser = Depends(require_user),
) -> PtiffQaStatusResponse:
    """
    Return the current PTIFF QA gate status for a job.

    Includes job-level aggregate counts and a per-page list with approval
    and correction status. Read-only; no state changes.

    **Error responses**

    - ``404`` — job not found
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)
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
    background_tasks: BackgroundTasks,
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    user: CurrentUser = Depends(require_user),
) -> ApprovePageResponse:
    """
    Record approval intent for a page in ptiff_qa_pending.

    When ``sub_page_index`` is provided, only that specific child sub-page is
    approved. When omitted, all pages with the given page_number are approved
    (original behavior).

    Approval does NOT change the page state immediately. If all gate
    conditions are met after this approval, the gate is released
    (all ptiff_qa_pending pages transition in a single transaction).

    When the gate releases to layout_detection, released pages are enqueued
    to Redis for downstream layout detection processing.

    If any released page is a split child, the split parent is closed to
    'split' if all its children are now worker-terminal.

    **Error responses**

    - ``404`` — job not found
    - ``409`` — page is not in 'ptiff_qa_pending' state
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    query = db.query(JobPage).filter(
        JobPage.job_id == job_id,
        JobPage.page_number == page_number,
        JobPage.status == "ptiff_qa_pending",
    )
    if sub_page_index is not None:
        query = query.filter(JobPage.sub_page_index == sub_page_index)
    qa_pages: list[JobPage] = query.all()

    if not qa_pages:
        page_desc = (
            f"Page {page_number} sub {sub_page_index}"
            if sub_page_index is not None
            else f"Page {page_number}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{page_desc} of job {job_id!r} is not in 'ptiff_qa_pending' state.",
        )

    for page in qa_pages:
        page.ptiff_qa_approved = True

    # Flush approval so _leaf_pages re-fetch sees the updated flags.
    db.flush()

    all_pages = _leaf_pages(db, job_id)
    released_pages = _check_and_release_ptiff_qa(db, job, all_pages)
    gate_released = bool(released_pages)
    tasks_to_enqueue: list[PageTask] = []

    if gate_released:
        # Enqueue released pages for layout detection when pipeline_mode == 'layout'.
        if job.pipeline_mode != "preprocess":
            for page in released_pages:
                tasks_to_enqueue.append(
                    PageTask(
                        task_id=str(uuid.uuid4()),
                        job_id=page.job_id,
                        page_id=page.page_id,
                        page_number=page.page_number,
                        sub_page_index=page.sub_page_index,
                        retry_count=0,
                    ),
                )

        # Close any split parent whose children are all now worker-terminal.
        for pn in {p.page_number for p in released_pages if p.sub_page_index is not None}:
            _maybe_close_split_parent(db, job_id, pn)
        _sync_job_summary(db, job)

    db.commit()
    enqueued_ok = False
    for task in tasks_to_enqueue:
        try:
            enqueue_page_task(r, task)
            enqueued_ok = True
        except redis.RedisError:
            logger.exception(
                "PTIFF QA approve: DB committed but enqueue failed job=%s page_id=%s",
                job_id,
                task.page_id,
            )

    if enqueued_ok:
        background_tasks.add_task(maybe_trigger_scale_up, r)

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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    user: CurrentUser = Depends(require_user),
) -> ApproveAllResponse:
    """
    Approve all pages currently in ptiff_qa_pending for this job.

    Only records approval for pages currently in ptiff_qa_pending.
    Already-approved pages are idempotently skipped. If all gate conditions
    are satisfied after recording approvals, the gate is released.

    When the gate releases to layout_detection, released pages are enqueued
    to Redis for downstream layout detection processing.

    If any released page is a split child, the split parent is closed to
    'split' if all its children are now worker-terminal.

    Calling this endpoint when no ptiff_qa_pending pages exist is a no-op:
    approved_count=0 and gate_released=False (idempotent).

    **Error responses**

    - ``404`` — job not found
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

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
    released_pages = _check_and_release_ptiff_qa(db, job, all_pages)
    gate_released = bool(released_pages)
    tasks_to_enqueue: list[PageTask] = []

    if gate_released:
        # Enqueue released pages for layout detection when pipeline_mode == 'layout'.
        if job.pipeline_mode != "preprocess":
            for page in released_pages:
                tasks_to_enqueue.append(
                    PageTask(
                        task_id=str(uuid.uuid4()),
                        job_id=page.job_id,
                        page_id=page.page_id,
                        page_number=page.page_number,
                        sub_page_index=page.sub_page_index,
                        retry_count=0,
                    ),
                )

        # Close any split parent whose children are all now worker-terminal.
        for pn in {p.page_number for p in released_pages if p.sub_page_index is not None}:
            _maybe_close_split_parent(db, job_id, pn)
        _sync_job_summary(db, job)

    db.commit()
    enqueued_ok = False
    for task in tasks_to_enqueue:
        try:
            enqueue_page_task(r, task)
            enqueued_ok = True
        except redis.RedisError:
            logger.exception(
                "PTIFF QA approve-all: DB committed but enqueue failed job=%s page_id=%s",
                job_id,
                task.page_id,
            )

    if enqueued_ok:
        background_tasks.add_task(maybe_trigger_scale_up, r)

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
@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit-and-return",
    response_model=EditPageResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Send a PTIFF QA page to human correction (alias for /edit)",
    include_in_schema=True,
)
def edit_page(
    job_id: str,
    page_number: int,
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> EditPageResponse:
    """
    Transition a ptiff_qa_pending page to pending_human_correction.

    When ``sub_page_index`` is provided, only that specific child sub-page is
    sent to correction. When omitted, all pages with the given page_number are
    transitioned (original behavior).

    Uses the state machine validator. Clears any prior approval flag so the
    page must be re-approved after correction is submitted.

    After human correction (handled in later packets), the page returns to
    ptiff_qa_pending and must be approved again before the gate can release.

    **Error responses**

    - ``404`` — job not found
    - ``409`` — page is not in 'ptiff_qa_pending' state
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    query = db.query(JobPage).filter(
        JobPage.job_id == job_id,
        JobPage.page_number == page_number,
        JobPage.status == "ptiff_qa_pending",
    )
    if sub_page_index is not None:
        query = query.filter(JobPage.sub_page_index == sub_page_index)
    qa_pages: list[JobPage] = query.all()

    if not qa_pages:
        page_desc = (
            f"Page {page_number} sub {sub_page_index}"
            if sub_page_index is not None
            else f"Page {page_number}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{page_desc} of job {job_id!r} is not in 'ptiff_qa_pending' state.",
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
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Page state changed concurrently; please retry.",
            )
        page.status = "pending_human_correction"
        # Clear approval flag: page must be re-approved after correction.
        page.ptiff_qa_approved = False

    _sync_job_summary(db, job)
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
