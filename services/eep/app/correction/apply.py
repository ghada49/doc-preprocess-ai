"""
services/eep/app/correction/apply.py
--------------------------------------
Packet 5.2 — Single-page correction apply path.

Implements:
  POST /v1/jobs/{job_id}/pages/{page_number}/correction

Applies human correction inputs for non-split pages, updates DB state,
writes lineage, and routes the page back to ptiff_qa_pending.

Semantics (non-negotiable):
  - Only pages in pending_human_correction are accepted (409 otherwise).
  - split_x is not supported in Packet 5.2; requests containing it are rejected (422).
  - Correction fields are persisted to the existing page_lineage row via
    human_corrected / human_correction_fields / human_correction_timestamp.
  - The source artifact (page.output_image_uri) is copied to a derived corrected URI
    via the storage backend. If the source URI is absent, the request fails (500).
  - The corrected URI is written to lineage.output_image_uri.
  - The lineage row must exist; if it is missing, the request fails (500).
  - ptiff_qa_approved is cleared so the corrected page must be re-approved.
  - All state transitions use advance_page_state() (state machine is never bypassed).
  - If ptiff_qa_mode == 'auto_continue', _check_and_release_ptiff_qa is called
    immediately after the pending_human_correction → ptiff_qa_pending transition.
  - Split-page correction (Packet 5.3) is NOT handled here; sub_page_index is
    always treated as NULL (non-split pages only).

Error responses:
  404 — job not found / page not found
  409 — page not in pending_human_correction state
  422 — invalid request body (Pydantic validation) or split_x provided
  500 — data-integrity failure: lineage row missing or page has no source artifact URI

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from services.eep.app.correction.ptiff_qa import _check_and_release_ptiff_qa
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from shared.io.storage import get_backend

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
        split_x      — horizontal split coordinate; rejected in Packet 5.2
                       (use split correction flow instead).
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
    Return a derived artifact URI representing the human-corrected output.

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


def _leaf_pages(db: Session, job_id: str) -> list[JobPage]:
    """Return all non-split leaf pages for the job."""
    return db.query(JobPage).filter(JobPage.job_id == job_id, JobPage.status != "split").all()


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
) -> CorrectionApplyResponse:
    """
    Apply human correction inputs for a non-split page in pending_human_correction.

    The correction is treated as authoritative. Processing is not re-run; the
    existing artifact is copied to a derived corrected URI via the storage backend
    and the URI is recorded in lineage. The page transitions to ptiff_qa_pending
    and must be re-approved before the PTIFF QA gate releases.

    When ptiff_qa_mode is 'auto_continue', the gate release check is triggered
    immediately after the transition.

    **Error responses**

    - ``404`` — job or page not found
    - ``409`` — page is not in 'pending_human_correction' state
    - ``422`` — split_x provided or invalid body
    - ``500`` — data-integrity failure: lineage row missing or page has no source artifact URI
    """
    # Step 0 — Reject split_x: not supported in Packet 5.2
    if body.split_x is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="split_x not supported in Packet 5.2 (use split correction flow)",
        )

    # Step 1 — Load job and page
    job = _fetch_job_or_404(db, job_id)

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

    # Step 2 — Fetch required lineage row
    # Data-integrity requirement: every page in pending_human_correction must have a lineage row.
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
                f"Data-integrity failure: no lineage row for job {job_id!r} page {page_number}. "
                "Page cannot be corrected without an existing lineage record."
            ),
        )

    # Step 3 — Derive corrected URI and copy artifact through storage backend
    # Data-integrity requirement: page must have a source artifact URI before correction.
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

    db.commit()

    logger.info(
        "Correction applied: job=%s page=%d ptiff_qa_mode=%s",
        job_id,
        page_number,
        job.ptiff_qa_mode,
    )

    return CorrectionApplyResponse()
