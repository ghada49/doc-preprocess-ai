"""
Job action endpoints: cancel and remove.

The existing job schema has no dedicated "canceled" state, so cancellation
marks unfinished pages as failed with a job_canceled review reason and then
resyncs the job summary. Removal deletes the job and dependent audit rows after
the normal ownership/admin guard.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, assert_job_ownership, require_user
from services.eep.app.db.models import (
    Job,
    JobPage,
    PageLineage,
    QualityGateLog,
    ServiceInvocation,
    ShadowEvaluation,
    SloAuditSample,
    TaskRetryState,
)
from services.eep.app.db.session import get_session
from services.eep.app.jobs.summary import sync_job_summary

router = APIRouter(tags=["jobs"])

_CANCELABLE_PAGE_STATES = {
    "queued",
    "preprocessing",
    "rectification",
    "ptiff_qa_pending",
    "layout_detection",
    "semantic_norm",
    "pending_human_correction",
    "split",
}


class JobActionResponse(BaseModel):
    job_id: str
    status: str
    affected_pages: int


@router.post(
    "/v1/jobs/{job_id}/cancel",
    response_model=JobActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel a processing job",
)
def cancel_job(
    job_id: str,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> JobActionResponse:
    job = _get_authorized_job(db, job_id, user)
    now = datetime.now(timezone.utc)

    pages = db.query(JobPage).filter(JobPage.job_id == job_id).all()
    page_numbers_with_children = {
        page.page_number for page in pages if page.sub_page_index is not None
    }
    affected = 0
    for page in pages:
        if page.status == "split" and page.page_number in page_numbers_with_children:
            continue
        if page.status not in _CANCELABLE_PAGE_STATES:
            continue
        page.status = "failed"
        page.acceptance_decision = "failed"
        page.review_reasons = ["job_canceled"]
        page.status_updated_at = now
        page.completed_at = now
        affected += 1

    sync_job_summary(db, job)
    if affected == 0 and job.status not in {"done", "failed"}:
        job.status = "failed"
        job.completed_at = job.completed_at or now

    db.commit()
    db.refresh(job)
    return JobActionResponse(
        job_id=job.job_id,
        status=job.status,
        affected_pages=affected,
    )


@router.delete(
    "/v1/jobs/{job_id}",
    response_model=JobActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove a job",
)
def delete_job(
    job_id: str,
    db: Session = Depends(get_session),
    user: CurrentUser = Depends(require_user),
) -> JobActionResponse:
    job = _get_authorized_job(db, job_id, user)
    page_ids = [
        row[0]
        for row in db.query(JobPage.page_id).filter(JobPage.job_id == job_id).all()
    ]
    lineage_ids = [
        row[0]
        for row in db.query(PageLineage.lineage_id)
        .filter(PageLineage.job_id == job_id)
        .all()
    ]

    if lineage_ids:
        db.execute(
            delete(ServiceInvocation).where(ServiceInvocation.lineage_id.in_(lineage_ids))
        )
    if page_ids:
        db.execute(delete(TaskRetryState).where(TaskRetryState.page_id.in_(page_ids)))
    db.execute(delete(TaskRetryState).where(TaskRetryState.job_id == job_id))

    db.execute(delete(QualityGateLog).where(QualityGateLog.job_id == job_id))
    db.execute(delete(SloAuditSample).where(SloAuditSample.job_id == job_id))
    db.execute(delete(ShadowEvaluation).where(ShadowEvaluation.job_id == job_id))
    db.execute(delete(PageLineage).where(PageLineage.job_id == job_id))
    affected_pages = (
        db.query(JobPage).filter(JobPage.job_id == job_id).delete(synchronize_session=False)
    )
    db.delete(job)
    db.commit()

    return JobActionResponse(
        job_id=job_id,
        status="deleted",
        affected_pages=affected_pages,
    )


def _get_authorized_job(db: Session, job_id: str, user: CurrentUser) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )
    assert_job_ownership(job, user)
    return job
