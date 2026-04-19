"""
services/eep/app/correction/ptiff_qa_viewer.py
-----------------------------------------------
Packet 5.0b — PTIFF QA Viewer: navigation-aware image browsing endpoint.

Implements:
  GET  /v1/jobs/{job_id}/ptiff-qa/viewer
       Return a single page's PTIFF output image (as a presigned URL), quality
       metrics, QA approval state, and prev/next page pointers for carousel
       navigation.

  POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/flag
       Flag any page (including accepted pages) for human correction.
       Transitions: ptiff_qa_pending | accepted → pending_human_correction.
       This is the primary "send to review" action from the viewer in
       auto_continue mode, where pages have already passed through the
       automated pipeline and landed in accepted.

Ordering:
  Pages are sorted by (page_number ASC, sub_page_index ASC NULLS FIRST).
  Split children (sub_page_index 0 and 1) immediately follow the parent
  page_number. The parent row itself (sub_page_index IS NULL) is excluded by
  _leaf_pages, so children appear in sequence after any preceding whole page.

Image delivery:
  Prefers output_image_uri (PTIFF).  Falls back to input_image_uri (OTIFF)
  when the preprocessing output is not yet available.  The presigned URL TTL
  matches ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS (default 300 s).  When object
  storage is unavailable, preview_url is null and preview_unavailable_reason
  contains the error detail.

Auth:
  require_user — ownership-scoped for regular users (assert_job_ownership);
  admin users may view any job.

Error responses:
  404 — job not found
  404 — no pages found for this job
  404 — requested page_number / sub_page_index not found

Exported:
  router — FastAPI APIRouter (mount in main.py)
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session
from shared.io.storage import rewrite_presigned_url_for_public_endpoint

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration (mirrors artifacts_api.py — single source of truth is env)
# ---------------------------------------------------------------------------

_BUCKET: str = os.environ.get("S3_BUCKET_NAME", "libraryai")
_READ_EXPIRES_IN: int = int(os.environ.get("ARTIFACT_READ_PRESIGN_EXPIRES_SECONDS", "300"))


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ViewerPageRef(BaseModel):
    """Minimal page pointer used for prev / next navigation links."""

    page_number: int
    sub_page_index: int | None


class ViewerQualitySummary(BaseModel):
    """
    Selected quality-gate metrics surfaced for the reviewer.

    All values are pass-through from job_pages.quality_summary (JSONB).
    Missing keys are returned as None rather than raising an error.
    """

    blur_score: float | None = None
    skew_angle_deg: float | None = None
    border_fraction: float | None = None
    coverage_fraction: float | None = None
    overall_passed: bool | None = None


class ViewerCurrentPage(BaseModel):
    """Full detail for the page currently shown in the viewer."""

    page_number: int
    sub_page_index: int | None
    status: str
    ptiff_qa_approved: bool

    # Image URIs (s3://)
    output_image_uri: str | None  # PTIFF — preferred
    input_image_uri: str | None   # OTIFF — fallback

    # Ready-to-use presigned URL for <img src=...> display (may be null on
    # storage failure; check preview_unavailable_reason for details)
    preview_url: str | None
    preview_uri_used: str | None        # which URI was presigned
    preview_expires_in: int             # seconds until preview_url expires
    preview_unavailable_reason: str | None

    quality_summary: ViewerQualitySummary | None
    review_reasons: list[str] | None
    routing_path: str | None
    processing_time_ms: float | None

    # Convenience flags for the UI
    can_approve: bool             # True when status == ptiff_qa_pending and not approved
    can_send_to_correction: bool  # True when page can be flagged for human correction
                                  # (ptiff_qa_pending OR accepted)


class ViewerNavigation(BaseModel):
    """Ordered navigation context relative to the current page."""

    current_index: int   # 0-based position in the full ordered page list
    total_pages: int
    prev: ViewerPageRef | None
    next: ViewerPageRef | None


class ViewerJobSummary(BaseModel):
    """Aggregate job-level counts shown in the viewer header / progress bar."""

    job_id: str
    ptiff_qa_mode: str
    pipeline_mode: str
    total_pages: int
    pages_pending_qa: int     # ptiff_qa_pending AND not approved
    pages_approved: int       # ptiff_qa_pending AND approved
    pages_in_correction: int  # pending_human_correction
    pages_accepted: int       # accepted
    pages_failed: int         # failed
    is_gate_ready: bool


class PtiffQaViewerResponse(BaseModel):
    """
    Full response for GET /v1/jobs/{job_id}/ptiff-qa/viewer.

    Contains everything a carousel UI needs to render the current page,
    navigate to adjacent pages, and take QA actions.
    """

    job_summary: ViewerJobSummary
    current_page: ViewerCurrentPage
    navigation: ViewerNavigation


class FlagPageResponse(BaseModel):
    """Response for POST …/ptiff-qa/flag."""

    page_number: int
    sub_page_index: int | None
    previous_state: str
    new_state: str   # always "pending_human_correction"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_job_or_404(db: Session, job_id: str) -> Job:
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    return job


def _leaf_pages_ordered(db: Session, job_id: str) -> list[JobPage]:
    """
    Return all non-split leaf pages sorted by (page_number, sub_page_index).

    Split-parent rows (status == 'split') are excluded; their children appear
    in ascending sub_page_index order after any preceding whole-page rows.
    None sub_page_index sorts before 0 (NULLS FIRST semantics).
    """
    rows: list[JobPage] = (
        db.query(JobPage)
        .filter(JobPage.job_id == job_id, JobPage.status != "split")
        .all()
    )
    rows.sort(key=lambda p: (p.page_number, p.sub_page_index if p.sub_page_index is not None else -1))
    return rows


def _presign_uri(uri: str) -> tuple[str | None, str | None]:
    """
    Generate a presigned GET URL for *uri*.

    Returns (presigned_url, None) on success, (None, error_reason) on failure.
    Only s3:// URIs are presigned; file:// URIs are returned as-is for local dev.
    """
    parsed = urlparse(uri)

    if parsed.scheme == "file":
        # Local development: return the file:// URI unchanged (no presigning).
        return uri, None

    if parsed.scheme != "s3":
        return None, f"Unsupported URI scheme {parsed.scheme!r}; only s3:// is presigned."

    uri_bucket = parsed.netloc
    if uri_bucket != _BUCKET:
        return None, (
            f"URI bucket {uri_bucket!r} does not match configured bucket {_BUCKET!r}."
        )

    key = parsed.path.lstrip("/")
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY") or os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY") or os.environ.get("S3_SECRET_ACCESS_KEY"),
        )
        url: str = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": _BUCKET, "Key": key},
            ExpiresIn=_READ_EXPIRES_IN,
        )
        url = rewrite_presigned_url_for_public_endpoint(url)
        return url, None
    except Exception as exc:
        logger.warning("ptiff_qa_viewer: presign failed uri=%s — %s", uri, exc)
        return None, f"Storage unavailable: {exc}"


def _make_preview(page: JobPage) -> tuple[str | None, str | None, str | None]:
    """
    Return (preview_url, uri_used, unavailable_reason) for *page*.

    Prefers output_image_uri (PTIFF).  Falls back to input_image_uri (OTIFF).
    Returns (None, None, reason) when neither URI can be presigned.
    """
    for candidate_uri in [page.output_image_uri, page.input_image_uri]:
        if not candidate_uri:
            continue
        url, err = _presign_uri(candidate_uri)
        if url:
            return url, candidate_uri, None
        # Log and try next candidate.
        logger.debug("ptiff_qa_viewer: presign failed for %s — %s; trying fallback", candidate_uri, err)

    return None, None, "No presignable image URI available for this page."


def _build_quality_summary(raw: dict | None) -> ViewerQualitySummary | None:
    """Extract known quality fields from the JSONB quality_summary dict."""
    if not raw:
        return None
    return ViewerQualitySummary(
        blur_score=raw.get("blur_score"),
        skew_angle_deg=raw.get("skew_angle_deg"),
        border_fraction=raw.get("border_fraction"),
        coverage_fraction=raw.get("coverage_fraction"),
        overall_passed=raw.get("overall_passed"),
    )


def _build_job_summary(job: Job, pages: list[JobPage]) -> ViewerJobSummary:
    pages_pending_qa = sum(
        1 for p in pages if p.status == "ptiff_qa_pending" and not p.ptiff_qa_approved
    )
    pages_approved = sum(
        1 for p in pages if p.status == "ptiff_qa_pending" and p.ptiff_qa_approved
    )
    pages_in_correction = sum(1 for p in pages if p.status == "pending_human_correction")
    pages_accepted = sum(1 for p in pages if p.status == "accepted")
    pages_failed = sum(1 for p in pages if p.status == "failed")

    qa_pages_exist = any(p.status == "ptiff_qa_pending" for p in pages)
    is_gate_ready = qa_pages_exist and pages_pending_qa == 0 and pages_in_correction == 0

    return ViewerJobSummary(
        job_id=job.job_id,
        ptiff_qa_mode=job.ptiff_qa_mode,
        pipeline_mode=job.pipeline_mode,
        total_pages=len(pages),
        pages_pending_qa=pages_pending_qa,
        pages_approved=pages_approved,
        pages_in_correction=pages_in_correction,
        pages_accepted=pages_accepted,
        pages_failed=pages_failed,
        is_gate_ready=is_gate_ready,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/v1/jobs/{job_id}/ptiff-qa/viewer",
    response_model=PtiffQaViewerResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="PTIFF QA image viewer — carousel navigation with presigned image URL",
)
def ptiff_qa_viewer(
    job_id: str,
    page_number: int | None = Query(
        default=None,
        description=(
            "1-indexed page number to view. Defaults to the first page in the "
            "ordered list when omitted."
        ),
    ),
    sub_page_index: int | None = Query(
        default=None,
        description=(
            "Sub-page index (0 = left, 1 = right) for split pages. "
            "Omit for whole (unsplit) pages."
        ),
    ),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> PtiffQaViewerResponse:
    """
    Return the PTIFF output image for a single page together with carousel
    navigation context (prev / next page pointers) and job-level QA counts.

    **Navigation**

    Pages are ordered by ``(page_number ASC, sub_page_index ASC NULLS FIRST)``.
    Omit ``page_number`` to start at the first page.  Use the ``prev`` and
    ``next`` fields in the response to walk forward or backward through the
    collection.

    **Image display**

    ``current_page.preview_url`` is a short-lived presigned S3 GET URL ready
    for use as an ``<img src>`` value.  It expires in
    ``current_page.preview_expires_in`` seconds (default 300 s).  When the
    PTIFF (``output_image_uri``) is not yet available, the OTIFF
    (``input_image_uri``) is presigned as a fallback; ``preview_uri_used``
    identifies which was used.

    **QA actions available from the viewer**

    - Approve: ``POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/approve``
    - Send to correction: ``POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/edit``
    - Approve all: ``POST /v1/jobs/{job_id}/ptiff-qa/approve-all``

    **Error responses**

    - ``404`` — job not found
    - ``404`` — job has no processable pages
    - ``404`` — requested page_number / sub_page_index not found in this job
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    pages = _leaf_pages_ordered(db, job_id)
    if not pages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} has no processable pages.",
        )

    # ── Resolve current page ────────────────────────────────────────────────
    if page_number is None:
        # Default to first page in ordered list.
        current_idx = 0
    else:
        # Find matching page.
        current_idx = next(
            (
                i
                for i, p in enumerate(pages)
                if p.page_number == page_number and p.sub_page_index == sub_page_index
            ),
            None,
        )
        if current_idx is None:
            sub_desc = f" sub {sub_page_index}" if sub_page_index is not None else ""
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Page {page_number}{sub_desc} not found in job {job_id!r}. "
                    "Check page_number and sub_page_index."
                ),
            )

    page = pages[current_idx]

    # ── Presign image ───────────────────────────────────────────────────────
    preview_url, preview_uri_used, preview_unavailable_reason = _make_preview(page)

    # ── Navigation pointers ─────────────────────────────────────────────────
    prev_ref: ViewerPageRef | None = None
    next_ref: ViewerPageRef | None = None

    if current_idx > 0:
        prev_page = pages[current_idx - 1]
        prev_ref = ViewerPageRef(
            page_number=prev_page.page_number,
            sub_page_index=prev_page.sub_page_index,
        )
    if current_idx < len(pages) - 1:
        next_page = pages[current_idx + 1]
        next_ref = ViewerPageRef(
            page_number=next_page.page_number,
            sub_page_index=next_page.sub_page_index,
        )

    # ── Build response ──────────────────────────────────────────────────────
    is_qa_pending = page.status == "ptiff_qa_pending"
    # accepted pages can be flagged for re-correction via the /flag endpoint
    # (accepted → pending_human_correction transition, user-initiated only)
    can_flag = page.status in ("ptiff_qa_pending", "accepted")

    current_page_detail = ViewerCurrentPage(
        page_number=page.page_number,
        sub_page_index=page.sub_page_index,
        status=page.status,
        ptiff_qa_approved=page.ptiff_qa_approved,
        output_image_uri=page.output_image_uri,
        input_image_uri=page.input_image_uri,
        preview_url=preview_url,
        preview_uri_used=preview_uri_used,
        preview_expires_in=_READ_EXPIRES_IN,
        preview_unavailable_reason=preview_unavailable_reason,
        quality_summary=_build_quality_summary(page.quality_summary),
        review_reasons=page.review_reasons if isinstance(page.review_reasons, list) else None,
        routing_path=page.routing_path,
        processing_time_ms=page.processing_time_ms,
        can_approve=is_qa_pending and not page.ptiff_qa_approved,
        can_send_to_correction=can_flag,
    )

    navigation = ViewerNavigation(
        current_index=current_idx,
        total_pages=len(pages),
        prev=prev_ref,
        next=next_ref,
    )

    job_summary = _build_job_summary(job, pages)

    logger.info(
        "ptiff_qa_viewer: user=%s job=%s page=%s sub=%s idx=%d/%d",
        user.user_id,
        job_id,
        page.page_number,
        page.sub_page_index,
        current_idx,
        len(pages),
    )

    return PtiffQaViewerResponse(
        job_summary=job_summary,
        current_page=current_page_detail,
        navigation=navigation,
    )


# ---------------------------------------------------------------------------
# Flag endpoint — send any page to human correction from the viewer
# ---------------------------------------------------------------------------


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/flag",
    response_model=FlagPageResponse,
    status_code=status.HTTP_200_OK,
    tags=["ptiff-qa"],
    summary="Flag a page for human correction from the PTIFF QA viewer",
)
def flag_page_for_correction(
    job_id: str,
    page_number: int,
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> FlagPageResponse:
    """
    Transition a page to ``pending_human_correction`` from the PTIFF QA viewer.

    Supported source states:
      - ``accepted``         — reviewer flags an already-accepted page for re-correction
      - ``ptiff_qa_pending`` — reviewer sends a queued QA page to correction

    This is the primary "send to review" action in ``auto_continue`` mode, where
    the automated pipeline runs to completion (pages reach ``accepted``) before the
    librarian reviews outputs in the viewer.

    After human correction is submitted (via the correction queue endpoints), the
    page resumes the normal pipeline:
      - ``pipeline_mode="layout"``     → transitions to ``layout_detection``
      - ``pipeline_mode="preprocess"`` → transitions directly to ``accepted``

    When ``sub_page_index`` is provided, only that specific child sub-page is
    flagged. When omitted, all pages with the given page_number are flagged.

    **Error responses**

    - ``404`` — job not found
    - ``409`` — page is not in a flaggable state (must be accepted or ptiff_qa_pending)
    """
    job = _fetch_job_or_404(db, job_id)
    assert_job_ownership(job, user)

    _FLAGGABLE_STATES = {"accepted", "ptiff_qa_pending"}

    query = db.query(JobPage).filter(
        JobPage.job_id == job_id,
        JobPage.page_number == page_number,
        JobPage.status.in_(_FLAGGABLE_STATES),
    )
    if sub_page_index is not None:
        query = query.filter(JobPage.sub_page_index == sub_page_index)
    target_pages: list[JobPage] = query.all()

    if not target_pages:
        sub_desc = f" sub {sub_page_index}" if sub_page_index is not None else ""
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Page {page_number}{sub_desc} of job {job_id!r} is not in a flaggable state. "
                f"Only pages in {sorted(_FLAGGABLE_STATES)} can be sent to human correction "
                "from the viewer."
            ),
        )

    previous_state = target_pages[0].status  # all matched rows share the same state

    for page in target_pages:
        advanced = advance_page_state(
            db,
            page.page_id,
            from_state=page.status,
            to_state="pending_human_correction",
        )
        if not advanced:
            logger.warning(
                "ptiff_qa_viewer flag: CAS miss job=%s page_id=%s from=%s",
                job_id,
                page.page_id,
                page.status,
            )
        # Clear any prior QA approval so re-correction starts fresh.
        page.ptiff_qa_approved = False

    db.commit()

    logger.info(
        "ptiff_qa_viewer flag: user=%s job=%s page=%d sub=%s %s → pending_human_correction",
        user.user_id,
        job_id,
        page_number,
        sub_page_index,
        previous_state,
    )

    return FlagPageResponse(
        page_number=page_number,
        sub_page_index=sub_page_index,
        previous_state=previous_state,
        new_state="pending_human_correction",
    )
