"""
services/eep/app/correction/apply.py
--------------------------------------
Packet 5.2 / 5.3 — Single-page and split correction apply path.

Implements:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction

Applies human correction inputs for pages in pending_human_correction.
Dispatches to one of two paths depending on whether split_x is supplied.

──────────────────────────────────────────────────────────────────────────
Packet 5.2  (split_x is None — single-page path)
──────────────────────────────────────────────────────────────────────────
  - The correction is authoritative; processing is not re-run.
  - The existing artifact is copied to a derived corrected URI via the
    storage backend and the URI is recorded in lineage.
  - State: pending_human_correction → ptiff_qa_pending.
  - If ptiff_qa_mode == 'auto_continue', _check_and_release_ptiff_qa is
    called immediately after the transition.

──────────────────────────────────────────────────────────────────────────
Packet 5.3  (split_x is not None — split correction path)
──────────────────────────────────────────────────────────────────────────
  - Two child sub-pages (sub_page_index 0 and 1) are created or reused
    (idempotent via UNIQUE job_id / page_number / sub_page_index).
  - A corrected artifact is copied to a child-specific URI for each child.
  - Each child transitions: pending_human_correction → ptiff_qa_pending.
  - In auto_continue mode, children are released directly past the PTIFF QA
    gate (accepted or layout_detection) because the gate is blocked while the
    parent is still in pending_human_correction.
  - In auto_continue + layout mode, children released to layout_detection are
    enqueued to Redis for downstream layout detection processing.
  - The parent transitions to 'split' only when both children reach a
    worker-terminal state (accepted / pending_human_correction / review /
    failed). This occurs synchronously only for preprocess + auto_continue.
  - Parent lineage is NOT modified; it remains the retained lineage record
    for the original OTIFF.

──────────────────────────────────────────────────────────────────────────
Shared invariants (non-negotiable)
──────────────────────────────────────────────────────────────────────────
  - Only pages in pending_human_correction are accepted (409 otherwise).
  - All state transitions use advance_page_state() (state machine is never
    bypassed).
  - Lineage rows must exist; missing rows are data-integrity failures (500).
  - Storage writes happen before DB commit; the DB never points to a URI
    that was not written.

Error responses:
  404 — job not found / page not found
  409 — page not in pending_human_correction state
  422 — invalid request body (Pydantic validation)
  500 — data-integrity failure: lineage row missing or page has no source
        artifact URI

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import redis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.correction.ptiff_qa import (
    _WORKER_TERMINAL_STATES,
    _check_and_release_ptiff_qa,
)
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from services.eep.app.queue import enqueue_page_task
from services.eep.app.redis_client import get_redis
from shared.io.storage import get_backend
from shared.schemas.queue import PageTask

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request schema ──────────────────────────────────────────────────────────────


class CorrectionApplyRequest(BaseModel):
    """
    Request body for POST /v1/jobs/{job_id}/pages/{page_number}/correction.

    Fields:
        crop_box     — [x_min, y_min, x_max, y_max]; exactly 4 non-negative integers
                       with x_min < x_max and y_min < y_max.
        deskew_angle — rotation correction angle in degrees.
        split_x      — horizontal split coordinate. When non-null, triggers the
                       split correction path (Packet 5.3); creates two child pages.
        notes        — optional reviewer notes; stored in lineage.reviewer_notes.
    """

    crop_box: list[int]
    deskew_angle: float
    split_x: int | None = None
    notes: str | None = None

    @field_validator("crop_box")
    @classmethod
    def validate_crop_box(cls, v: list[int]) -> list[int]:
        if len(v) != 4:
            raise ValueError(f"crop_box must have exactly 4 integers; got {len(v)}")
        x_min, y_min, x_max, y_max = v
        if any(val < 0 for val in v):
            raise ValueError("crop_box values must be non-negative")
        if x_min >= x_max:
            raise ValueError("crop_box x_min must be less than x_max")
        if y_min >= y_max:
            raise ValueError("crop_box y_min must be less than y_max")
        return v


# ── Response schema ─────────────────────────────────────────────────────────────


class CorrectionApplyResponse(BaseModel):
    status: str = "ok"


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


def _derive_corrected_uri(base_uri: str | None) -> str | None:
    """
    Return a derived artifact URI for the single-page corrected output.

    Inserts '_corrected' before the file extension if one exists, otherwise
    appends it. Returns None when base_uri is None.

    Examples:
        's3://bucket/norm.tiff' → 's3://bucket/norm_corrected.tiff'
        's3://bucket/page'      → 's3://bucket/page_corrected'
    """
    if base_uri is None:
        return None
    dot = base_uri.rfind(".")
    slash = base_uri.rfind("/")
    if dot > slash:
        return base_uri[:dot] + "_corrected" + base_uri[dot:]
    return base_uri + "_corrected"


def _derive_child_corrected_uri(
    source_uri: str,
    job_id: str,
    page_number: int,
    sub_page_index: int,
) -> str:
    """
    Return the corrected artifact URI for a split child page.

    Format (spec Section 8.6):
        s3://{bucket}/jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff
        file://jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff  (local/CI)
    """
    parsed = urlparse(source_uri)
    if parsed.scheme == "s3":
        return (
            f"s3://{parsed.netloc}/jobs/{job_id}/corrected/" f"{page_number}_{sub_page_index}.tiff"
        )
    # file:// fallback for local development and CI
    return f"file://jobs/{job_id}/corrected/{page_number}_{sub_page_index}.tiff"


def _leaf_pages(db: Session, job_id: str) -> list[JobPage]:
    """Return all non-split leaf pages for the job."""
    return db.query(JobPage).filter(JobPage.job_id == job_id, JobPage.status != "split").all()


# ── Split correction path (Packet 5.3) ──────────────────────────────────────────


def _apply_split_correction(
    db: Session,
    job: Job,
    parent: JobPage,
    body: CorrectionApplyRequest,
    r: redis.Redis,
) -> None:
    """
    Execute the split correction path (Packet 5.3).

    Creates two child sub-pages (left: sub_page_index=0, right: sub_page_index=1),
    writes corrected artifacts for each child via the storage backend, records
    lineage, and transitions children through ptiff_qa_pending.

    In auto_continue mode, children are immediately released to their target state
    (accepted or layout_detection). Children released to layout_detection are
    enqueued to Redis for downstream processing.

    The parent page transitions to 'split' via _close_parent_if_children_terminal
    when both children reach a worker-terminal state. This occurs synchronously
    only for preprocess + auto_continue. In all other modes the parent stays in
    pending_human_correction until a later child-completion path closes it.

    The caller is responsible for db.commit() after this function returns.
    """
    assert body.split_x is not None  # guarded by caller

    now = datetime.now(tz=UTC)

    # Step A — Fetch parent lineage row
    # Data-integrity requirement: parent lineage must exist.
    parent_lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == parent.job_id,
            PageLineage.page_number == parent.page_number,
            PageLineage.sub_page_index == None,  # noqa: E711
        )
        .first()
    )
    if parent_lineage is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: no lineage row for job {parent.job_id!r} "
                f"page {parent.page_number}."
            ),
        )

    # Step B — Validate source URI
    # Data-integrity requirement: parent must have a source artifact URI.
    if parent.output_image_uri is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Data-integrity failure: page {parent.page_number} of job "
                f"{parent.job_id!r} has no source artifact URI. "
                "Cannot create corrected child artifacts."
            ),
        )

    correction_fields: dict[str, Any] = {
        "crop_box": body.crop_box,
        "deskew_angle": body.deskew_angle,
        "split_x": body.split_x,
    }

    children: list[JobPage] = []

    # Step C — For each side: create/reuse child page + lineage, write artifact,
    #           transition to ptiff_qa_pending.
    for sub_idx in (0, 1):
        child_uri = _derive_child_corrected_uri(
            parent.output_image_uri, parent.job_id, parent.page_number, sub_idx
        )

        # Create or reuse child JobPage (idempotent).
        child: JobPage | None = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == parent.job_id,
                JobPage.page_number == parent.page_number,
                JobPage.sub_page_index == sub_idx,
            )
            .first()
        )
        if child is None:
            child = JobPage(
                page_id=str(uuid.uuid4()),
                job_id=parent.job_id,
                page_number=parent.page_number,
                sub_page_index=sub_idx,
                status="pending_human_correction",
                input_image_uri=parent.input_image_uri,
                output_image_uri=None,
                ptiff_qa_approved=False,
            )
            db.add(child)

        # Copy parent artifact bytes to child corrected URI.
        src_data = get_backend(parent.output_image_uri).get_bytes(parent.output_image_uri)
        get_backend(child_uri).put_bytes(child_uri, src_data)

        # Mirror corrected URI onto the child page row for fast lookups.
        # page_lineage remains authoritative; job_pages.output_image_uri is a convenience copy.
        child.output_image_uri = child_uri
        child.ptiff_qa_approved = False

        # Create or update child lineage row (idempotent).
        child_lineage: PageLineage | None = (
            db.query(PageLineage)
            .filter(
                PageLineage.job_id == parent.job_id,
                PageLineage.page_number == parent.page_number,
                PageLineage.sub_page_index == sub_idx,
            )
            .first()
        )
        if child_lineage is None:
            child_lineage = PageLineage(
                lineage_id=str(uuid.uuid4()),
                job_id=parent.job_id,
                page_number=parent.page_number,
                sub_page_index=sub_idx,
                correlation_id=parent_lineage.correlation_id,
                input_image_uri=parent_lineage.input_image_uri,
                input_image_hash=parent_lineage.input_image_hash,
                otiff_uri=parent_lineage.otiff_uri,
                material_type=parent_lineage.material_type,
                routing_path=parent_lineage.routing_path,
                policy_version=parent_lineage.policy_version,
                parent_page_id=parent.page_id,
                split_source=True,
                human_corrected=True,
                human_correction_timestamp=now,
                human_correction_fields=correction_fields,
                output_image_uri=child_uri,
                reviewer_notes=body.notes,
            )
            db.add(child_lineage)
        else:
            # Idempotent update for repeated calls.
            child_lineage.human_corrected = True
            child_lineage.human_correction_timestamp = now
            child_lineage.human_correction_fields = correction_fields
            child_lineage.output_image_uri = child_uri
            if body.notes is not None:
                child_lineage.reviewer_notes = body.notes

        # Transition child: pending_human_correction → ptiff_qa_pending.
        # Track status in memory because advance_page_state uses a bulk UPDATE
        # and does not refresh the ORM object automatically.
        if child.status == "pending_human_correction":
            advanced = advance_page_state(
                db,
                child.page_id,
                from_state="pending_human_correction",
                to_state="ptiff_qa_pending",
            )
            if advanced:
                child.status = "ptiff_qa_pending"
            else:
                logger.warning(
                    "Split correction child CAS miss: job=%s page_id=%s sub_page_index=%d "
                    "(concurrent update or state already changed)",
                    parent.job_id,
                    child.page_id,
                    sub_idx,
                )

        children.append(child)

    db.flush()

    # Step D — auto_continue: release children directly past the PTIFF QA gate.
    # Note: _check_and_release_ptiff_qa is intentionally NOT used here. The parent
    # is still in pending_human_correction, which would block _is_gate_satisfied.
    # Instead, children are released directly per spec Section 8.6, step 8.
    if job.ptiff_qa_mode == "auto_continue":
        target = "accepted" if job.pipeline_mode == "preprocess" else "layout_detection"
        for child in children:
            if child.status == "ptiff_qa_pending":
                advanced = advance_page_state(
                    db,
                    child.page_id,
                    from_state="ptiff_qa_pending",
                    to_state=target,
                )
                if advanced:
                    child.status = target
                    # Enqueue children released to layout_detection for downstream processing.
                    if target == "layout_detection":
                        enqueue_page_task(
                            r,
                            PageTask(
                                task_id=str(uuid.uuid4()),
                                job_id=child.job_id,
                                page_id=child.page_id,
                                page_number=child.page_number,
                                sub_page_index=child.sub_page_index,
                                retry_count=0,
                            ),
                        )
                else:
                    logger.warning(
                        "Split correction auto-release CAS miss: job=%s page_id=%s",
                        parent.job_id,
                        child.page_id,
                    )
        db.flush()

    # Step E — Transition parent to 'split' when both children are worker-terminal.
    # Uses pre-loaded children with up-to-date in-memory statuses (manually
    # tracked after each advance_page_state call above).
    # Worker-terminal: accepted, pending_human_correction, review, failed.
    # Synchronous only for preprocess + auto_continue; other modes rely on
    # _maybe_close_split_parent invoked from later child-completion paths
    # (e.g. ptiff_qa.py approve endpoints).
    if all(c.status in _WORKER_TERMINAL_STATES for c in children):
        advanced = advance_page_state(
            db,
            parent.page_id,
            from_state="pending_human_correction",
            to_state="split",
        )
        if not advanced:
            logger.warning(
                "Split correction parent-to-split CAS miss: job=%s page_id=%s",
                parent.job_id,
                parent.page_id,
            )

    logger.info(
        "Split correction applied: job=%s page=%d split_x=%d " "ptiff_qa_mode=%s pipeline_mode=%s",
        parent.job_id,
        parent.page_number,
        body.split_x,
        job.ptiff_qa_mode,
        job.pipeline_mode,
    )


# ── Endpoint ────────────────────────────────────────────────────────────────────


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/correction",
    response_model=CorrectionApplyResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction"],
    summary="Apply human correction to a single page",
)
def apply_correction(
    job_id: str,
    page_number: int,
    body: CorrectionApplyRequest,
    db: Session = Depends(get_session),
    r: redis.Redis = Depends(get_redis),
    user: CurrentUser = Depends(require_user),
) -> CorrectionApplyResponse:
    """
    Apply human correction inputs for a page in pending_human_correction.

    Dispatches to one of two paths depending on ``split_x``:

    **Single-page path (split_x is None — Packet 5.2):**
      Processing is not re-run. The existing artifact is copied to a derived
      corrected URI and recorded in lineage. The page transitions to
      ptiff_qa_pending. In auto_continue mode, the PTIFF QA gate is checked
      and may release immediately.

    **Split correction path (split_x is not None — Packet 5.3):**
      Two child sub-pages are created (or reused for idempotency). Corrected
      artifacts are written for each child. Children transition to
      ptiff_qa_pending. The parent stays in pending_human_correction until both
      children reach worker-terminal states. In auto_continue mode, children
      are released directly to their target state within this request.
      Children released to layout_detection are enqueued to Redis.

    **Error responses**

    - ``404`` — job or page not found
    - ``409`` — page is not in 'pending_human_correction' state
    - ``422`` — invalid body
    - ``500`` — data-integrity failure: lineage row missing or page has no
                source artifact URI
    """
    # Step 1 — Load job and page
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    page: JobPage | None = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.sub_page_index == None,  # non-split pages only  # noqa: E711
        )
        .first()
    )

    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {page_number} of job {job_id!r} not found.",
        )

    if page.status != "pending_human_correction":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Page {page_number} of job {job_id!r} is in state "
                f"{page.status!r}, not 'pending_human_correction'."
            ),
        )

    if body.split_x is not None:
        # ── Packet 5.3: split correction path ────────────────────────────────
        _apply_split_correction(db, job, page, body, r)

    else:
        # ── Packet 5.2: single-page correction path ───────────────────────────

        # Step 2 — Fetch required lineage row
        # Data-integrity requirement: every page in pending_human_correction must
        # have a lineage row.
        lineage: PageLineage | None = (
            db.query(PageLineage)
            .filter(
                PageLineage.job_id == job_id,
                PageLineage.page_number == page_number,
                PageLineage.sub_page_index == None,  # noqa: E711
            )
            .first()
        )

        if lineage is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Data-integrity failure: no lineage row for job {job_id!r} "
                    f"page {page_number}. "
                    "Page cannot be corrected without an existing lineage record."
                ),
            )

        # Step 3 — Derive corrected URI and copy artifact through storage backend
        # Data-integrity requirement: page must have a source artifact URI.
        if page.output_image_uri is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Data-integrity failure: page {page_number} of job {job_id!r} "
                    "has no source artifact URI. Cannot create corrected artifact."
                ),
            )

        corrected_uri = _derive_corrected_uri(page.output_image_uri)
        assert corrected_uri is not None  # guaranteed: page.output_image_uri is non-None above
        src_data = get_backend(page.output_image_uri).get_bytes(page.output_image_uri)
        get_backend(corrected_uri).put_bytes(corrected_uri, src_data)

        # Step 4 — Update lineage with correction fields
        now = datetime.now(tz=UTC)
        correction_fields: dict[str, Any] = {
            "crop_box": body.crop_box,
            "deskew_angle": body.deskew_angle,
        }

        lineage.human_corrected = True
        lineage.human_correction_timestamp = now
        lineage.human_correction_fields = correction_fields
        # page_lineage.output_image_uri is the authoritative durable artifact path.
        lineage.output_image_uri = corrected_uri
        if body.notes is not None:
            lineage.reviewer_notes = body.notes

        # Mirror corrected URI onto the page row for fast lookups.
        # page_lineage remains authoritative; job_pages.output_image_uri is a convenience copy.
        page.output_image_uri = corrected_uri

        # Step 5 — Clear approval flag: corrected page must be re-approved
        page.ptiff_qa_approved = False

        # Step 6 — State transition: pending_human_correction → ptiff_qa_pending
        advanced = advance_page_state(
            db,
            page.page_id,
            from_state="pending_human_correction",
            to_state="ptiff_qa_pending",
        )

        if not advanced:
            logger.warning(
                "Correction apply CAS miss: job=%s page_id=%s page_number=%d "
                "(concurrent update or state already changed)",
                job_id,
                page.page_id,
                page_number,
            )

        db.flush()

        # Step 7 — PTIFF QA behavior
        if job.ptiff_qa_mode == "auto_continue":
            all_pages = _leaf_pages(db, job_id)
            _check_and_release_ptiff_qa(db, job, all_pages)

        logger.info(
            "Correction applied: job=%s page=%d ptiff_qa_mode=%s",
            job_id,
            page_number,
            job.ptiff_qa_mode,
        )

    db.commit()
    return CorrectionApplyResponse()
