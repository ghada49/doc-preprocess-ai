"""
services/eep/app/correction/send_to_review.py
----------------------------------------------
Explicit user-initiated send-to-review action.

Implements:
  POST /v1/jobs/{job_id}/pages/{page_number}/send-to-review

Allows a user to explicitly send a page that is currently in layout_detection
to pending_human_correction.  This is the only way a page enters human review
in the automation-first model (the pipeline never routes there automatically
during layout detection).

Semantics:
  - Only pages in layout_detection are accepted (409 otherwise).
  - State transition: layout_detection → pending_human_correction.
  - review_reasons is set to ["user_requested_review"] on the page row.
  - All stale layout state (output_layout_uri, layout_consensus_result,
    gate_results[layout_*]) is invalidated so a fresh IEP2 run will follow
    the correction.
  - All state transitions use advance_page_state() (state machine never bypassed).

Error responses:
  404 — job not found / page not found
  409 — page is not in layout_detection state
  500 — data-integrity failure: lineage row missing

Auth:
  Enforced in Phase 7 (Packet 7.1) — not yet active.

Exported:
  router — FastAPI APIRouter (mount at app level in main.py)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


class SendToReviewResponse(BaseModel):
    status: str = "ok"


@router.post(
    "/v1/jobs/{job_id}/pages/{page_number}/send-to-review",
    response_model=SendToReviewResponse,
    status_code=status.HTTP_200_OK,
    tags=["correction"],
    summary="Send a page to human review",
)
def send_to_review(
    job_id: str,
    page_number: int,
    sub_page_index: int | None = Query(default=None),
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> SendToReviewResponse:
    """
    Explicitly send a page in layout_detection to pending_human_correction.

    This is the only way a page enters human review in the automation-first model.
    The automated pipeline never routes a page to pending_human_correction during
    layout detection — only through preprocessing failures or this endpoint.

    Stale layout state is invalidated so a fresh IEP2 run follows the correction.

    **Error responses**

    - ``404`` — job or page not found
    - ``409`` — page is not in 'layout_detection' state
    """
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    assert_job_ownership(job, user)

    if sub_page_index is not None:
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
                detail=f"Page {page_number} sub {sub_page_index} of job {job_id!r} not found.",
            )
    else:
        page = (
            db.query(JobPage)
            .filter(
                JobPage.job_id == job_id,
                JobPage.page_number == page_number,
                JobPage.sub_page_index == None,  # noqa: E711
            )
            .first()
        )
        if page is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Page {page_number} of job {job_id!r} not found.",
            )

    if page.status != "layout_detection":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Page {page_number} of job {job_id!r} is in state "
                f"{page.status!r}, not 'layout_detection'. "
                "Only pages in layout_detection can be sent to review."
            ),
        )

    # Invalidate stale layout state before transitioning.
    page.output_layout_uri = None
    page.layout_consensus_result = None

    lineage: PageLineage | None = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == job_id,
            PageLineage.page_number == page_number,
            PageLineage.sub_page_index == page.sub_page_index,
        )
        .first()
    )
    if lineage is not None:
        gate_results = dict(lineage.gate_results or {})
        gate_results.pop("downsample", None)
        gate_results.pop("layout_input", None)
        gate_results.pop("layout_adjudication", None)
        lineage.gate_results = gate_results or None
        lineage.layout_artifact_state = "pending"

    advanced = advance_page_state(
        db,
        page.page_id,
        from_state="layout_detection",
        to_state="pending_human_correction",
    )
    if not advanced:
        logger.warning(
            "send_to_review: CAS miss job=%s page_id=%s page_number=%d",
            job_id,
            page.page_id,
            page_number,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Page state changed concurrently; please retry.",
        )

    page.status = "pending_human_correction"
    page.review_reasons = ["user_requested_review"]

    db.commit()

    logger.info(
        "send_to_review: page sent to human review job=%s page=%d sub=%s",
        job_id,
        page_number,
        sub_page_index,
    )
    return SendToReviewResponse()
