"""
services/eep/app/jobs/summary.py
--------------------------------
Shared helpers for deriving live job summaries from page rows.

These helpers are intentionally read/write agnostic:
  - read paths use them to avoid exposing stale denormalized counters
  - write paths may use them to resync jobs after page-state transitions
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from services.eep.app.db.models import Job, JobPage
from shared.schemas.eep import JobStatus

_NON_TERMINAL: frozenset[str] = frozenset(
    {
        "queued",
        "preprocessing",
        "rectification",
        "layout_detection",
        "semantic_norm",
        "ptiff_qa_pending",
        "pending_human_correction",
        "split",
    }
)


@dataclass(frozen=True)
class JobPageCounts:
    accepted_count: int
    review_count: int
    failed_count: int
    pending_human_correction_count: int


def leaf_pages_from_pages(pages: list[JobPage]) -> list[JobPage]:
    """Return only leaf pages for job-level status/count derivation.

    A normal split parent is excluded once child rows exist. If a split parent
    has no children, keep it visible as an anomalous in-progress leaf instead
    of deriving an empty/queued job that disappears from the UI.
    """
    page_numbers_with_children = {
        page.page_number for page in pages if page.sub_page_index is not None
    }
    return [
        page
        for page in pages
        if page.status != "split" or page.page_number not in page_numbers_with_children
    ]


def derive_job_status(leaf_pages: list[JobPage]) -> JobStatus:
    """Derive the authoritative job status from leaf page states."""
    if not leaf_pages or all(page.status == "queued" for page in leaf_pages):
        return "queued"

    if any(page.status in _NON_TERMINAL for page in leaf_pages):
        return "running"

    if all(page.status == "failed" for page in leaf_pages):
        return "failed"

    return "done"


def summarize_leaf_pages(leaf_pages: list[JobPage]) -> JobPageCounts:
    """Count live leaf-page states for UI/job summary use."""
    return JobPageCounts(
        accepted_count=sum(1 for page in leaf_pages if page.status == "accepted"),
        review_count=sum(1 for page in leaf_pages if page.status == "review"),
        failed_count=sum(1 for page in leaf_pages if page.status == "failed"),
        pending_human_correction_count=sum(
            1 for page in leaf_pages if page.status == "pending_human_correction"
        ),
    )


def sync_job_summary(session: Session, job: Job) -> None:
    """Refresh denormalized job counters/status from current leaf page states."""
    pages = session.query(JobPage).filter(JobPage.job_id == job.job_id).all()
    leaf_pages = leaf_pages_from_pages(pages)
    counts = summarize_leaf_pages(leaf_pages)
    now = datetime.now(UTC)

    job.accepted_count = counts.accepted_count
    job.review_count = counts.review_count
    job.failed_count = counts.failed_count
    job.pending_human_correction_count = counts.pending_human_correction_count
    job.status = derive_job_status(leaf_pages)
    if job.status in {"done", "failed"}:
        job.completed_at = job.completed_at or now
    else:
        job.completed_at = None
