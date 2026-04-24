"""
services/eep/app/jobs/status.py
--------------------------------
GET /v1/jobs/{job_id} — return the current status of a processing job.

Job status derivation
---------------------
Status is derived live from leaf page states on every request.

  Leaf pages:  all job_pages rows except split parents that have child rows.
               Split-parent records (status='split') are excluded once their
               child sub-pages (sub_page_index IS NOT NULL) exist. A split
               parent without children remains visible as an anomalous
               in-progress leaf.

  Derivation (exact, deterministic — spec Section 9.1 / 13):

    queued:  all leaf pages are in 'queued' state (no processing started)
    running: at least one leaf page is in a non-worker-terminal state:
             {'queued', 'preprocessing', 'rectification', 'layout_detection',
              'semantic_norm', 'pending_human_correction', 'split'}
    done:    all leaf pages are worker-terminal AND at least one is not 'failed'
    failed:  all leaf pages are worker-terminal AND all are 'failed'

pending_human_correction rule
-----------------------------
'pending_human_correction' is in the non-terminal set, so a job with any leaf
page requiring human review remains 'running', never 'done'.
(spec Section 9.11)

Counter fields
--------------
accepted_count, review_count, failed_count, and
pending_human_correction_count are derived live from current leaf page states.
This avoids exposing stale denormalized job counters after out-of-band page
transitions such as human correction actions.

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

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.models import Job, JobPage
from services.eep.app.db.session import get_session
from services.eep.app.jobs.summary import (
    derive_job_status as _derive_job_status,
    leaf_pages_from_pages,
    summarize_leaf_pages,
)
from shared.schemas.eep import (
    JobStatus,
    JobStatusResponse,
    JobStatusSummary,
    PageStatus,
    QualitySummary,
)

logger = logging.getLogger(__name__)
router = APIRouter()

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
    user: CurrentUser = Depends(require_user),
) -> JobStatusResponse:
    """
    Return the current status of a processing job.

    Job-level status is derived live from leaf page states on every request
    (see module docstring for derivation rules).

    Counter fields (accepted_count, review_count, failed_count,
    pending_human_correction_count) are derived live from current leaf pages.

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

    assert_job_ownership(job, user)

    all_pages: list[JobPage] = (
        db.query(JobPage)
        .filter(JobPage.job_id == job_id)
        .order_by(JobPage.page_number, JobPage.sub_page_index.asc().nullsfirst())
        .all()
    )

    leaf_pages = leaf_pages_from_pages(all_pages)
    derived_status: JobStatus = _derive_job_status(leaf_pages)
    counts = summarize_leaf_pages(leaf_pages)

    summary = JobStatusSummary(
        job_id=job.job_id,
        collection_id=job.collection_id,
        material_type=job.material_type,  # type: ignore[arg-type]
        pipeline_mode=job.pipeline_mode,  # type: ignore[arg-type]
        policy_version=job.policy_version,
        shadow_mode=job.shadow_mode,
        created_by=job.created_by,
        status=derived_status,
        page_count=len(leaf_pages),
        accepted_count=counts.accepted_count,
        review_count=counts.review_count,
        failed_count=counts.failed_count,
        pending_human_correction_count=counts.pending_human_correction_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
        reading_direction=job.reading_direction,  # type: ignore[arg-type]
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
                input_image_uri=p.input_image_uri,
                output_image_uri=p.output_image_uri,
                output_layout_uri=p.output_layout_uri,
                quality_summary=quality_summary,
                review_reasons=p.review_reasons,
                acceptance_decision=p.acceptance_decision,  # type: ignore[arg-type]
                processing_time_ms=p.processing_time_ms,
                reading_order=p.reading_order,
            )
        )

    return JobStatusResponse(summary=summary, pages=page_statuses)
