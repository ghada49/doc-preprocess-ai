"""
services/eep/app/jobs/status.py
--------------------------------
GET /v1/jobs/{job_id} — return the current status of a processing job.

Job status derivation
---------------------
Status is derived live from leaf page states on every request.

  Leaf pages:  all job_pages rows where status != 'split'.
               Split-parent records (status='split') are excluded; their
               child sub-pages (sub_page_index IS NOT NULL) are included.

  Derivation (exact, deterministic — spec Section 9.1 / 13):

    queued:  all leaf pages are in 'queued' state (no processing started)
    running: at least one leaf page is in a non-worker-terminal state:
             {'queued', 'preprocessing', 'rectification',
              'ptiff_qa_pending', 'layout_detection'}
    done:    all leaf pages are worker-terminal AND at least one is not 'failed'
    failed:  all leaf pages are worker-terminal AND all are 'failed'

ptiff_qa_pending rule
----------------------
'ptiff_qa_pending' is in the non-terminal set, so a job with any leaf page
in 'ptiff_qa_pending' must remain 'running', never 'done'.
(spec Section 3.1 PTIFF-stage QA checkpoint, Section 9.1)

Counter fields
--------------
accepted_count, review_count, failed_count, and
pending_human_correction_count are read directly from the jobs row.
Workers maintain these counters when they update page states.
This endpoint does not recompute them.

Error responses
---------------
    404 — job_id not found
    500 — unexpected database error

Auth
----
Enforced in Phase 7 (Packet 7.1) — not yet active.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from shared.schemas.eep import (
    JobStatus,
    JobStatusResponse,
    JobStatusSummary,
    PageStatus,
    QualitySummary,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Non-terminal page states: a job remains 'running' if any leaf page is in
# one of these states.  Crucially includes 'ptiff_qa_pending'.
_NON_TERMINAL: frozenset[str] = frozenset(
    {
        "queued",
        "preprocessing",
        "rectification",
        "ptiff_qa_pending",
        "layout_detection",
    }
)


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def _derive_job_status(leaf_pages: list[JobPage]) -> JobStatus:
    """
    Derive the job-level status from leaf page states.

    This is the single authoritative implementation of the derivation rule.
    Call it only after filtering split-parent records from ``leaf_pages``.

    Args:
        leaf_pages: Pages with status != 'split' for this job.

    Returns:
        One of 'queued' | 'running' | 'done' | 'failed'.
    """
    if not leaf_pages or all(p.status == "queued" for p in leaf_pages):
        return "queued"

    if any(p.status in _NON_TERMINAL for p in leaf_pages):
        return "running"

    # All leaf pages are worker-terminal.
    if all(p.status == "failed" for p in leaf_pages):
        return "failed"

    return "done"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    status_code=status.HTTP_200_OK,
    tags=["jobs"],
    summary="Get job status",
)
def get_job_status(
    job_id: str,
    db: Session = Depends(get_session),
) -> JobStatusResponse:
    """
    Return the current status of a processing job.

    Job-level status is derived live from leaf page states on every request
    (see module docstring for derivation rules).

    Counter fields (accepted_count, review_count, failed_count,
    pending_human_correction_count) are read from the stored job row and
    maintained by workers.

    **Auth:** enforced in Phase 7 (Packet 7.1) — not yet active.

    **Error responses**

    - ``404`` — job not found
    """
    job: Job | None = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )

    all_pages: list[JobPage] = db.query(JobPage).filter(JobPage.job_id == job_id).all()

    # Leaf pages exclude split-parent records.
    leaf_pages = [p for p in all_pages if p.status != "split"]

    derived_status: JobStatus = _derive_job_status(leaf_pages)

    summary = JobStatusSummary(
        job_id=job.job_id,
        collection_id=job.collection_id,
        material_type=job.material_type,  # type: ignore[arg-type]
        pipeline_mode=job.pipeline_mode,  # type: ignore[arg-type]
        ptiff_qa_mode=job.ptiff_qa_mode,  # type: ignore[arg-type]
        policy_version=job.policy_version,
        shadow_mode=job.shadow_mode,
        created_by=job.created_by,
        status=derived_status,
        page_count=job.page_count,
        accepted_count=job.accepted_count,
        review_count=job.review_count,
        failed_count=job.failed_count,
        pending_human_correction_count=job.pending_human_correction_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )

    page_statuses: list[PageStatus] = []
    for p in all_pages:
        quality_summary = (
            QualitySummary.model_validate(p.quality_summary)
            if p.quality_summary is not None
            else None
        )
        page_statuses.append(
            PageStatus(
                page_number=p.page_number,
                sub_page_index=p.sub_page_index,
                status=p.status,  # type: ignore[arg-type]
                routing_path=p.routing_path,
                output_image_uri=p.output_image_uri,
                output_layout_uri=p.output_layout_uri,
                quality_summary=quality_summary,
                review_reasons=p.review_reasons,
                acceptance_decision=p.acceptance_decision,  # type: ignore[arg-type]
                processing_time_ms=p.processing_time_ms,
            )
        )

    return JobStatusResponse(summary=summary, pages=page_statuses)
