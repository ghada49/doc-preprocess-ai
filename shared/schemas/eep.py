"""
shared.schemas.eep
------------------
EEP public schemas and terminal page state constants.

All components must import TERMINAL_PAGE_STATES from this module.
It must never be redefined inline elsewhere. (spec Section 9.1 / 12.1)

Exported:
    TERMINAL_PAGE_STATES   — frozenset[str] of worker-terminal page states
    PageState              — Literal union of all valid page states
    MaterialType           — Literal union of valid material types
    PipelineMode           — Literal union of valid pipeline modes
    JobStatus              — Literal union of valid job-level statuses
    PageInput              — single page submitted in a JobCreateRequest
    JobCreateRequest       — request to POST /v1/jobs
    JobCreateResponse      — response to POST /v1/jobs (HTTP 201)
    QualitySummary         — per-page quality metrics summary (stored as JSONB)
    PageStatus             — per-page status within a JobStatusResponse
    JobStatusSummary       — compact job row used in paginated list responses
    JobStatusResponse      — full job status response for GET /v1/jobs/{job_id}
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# ── Type aliases ───────────────────────────────────────────────────────────────

MaterialType = Literal["book", "newspaper", "archival_document", "microfilm"]
PipelineMode = Literal["preprocess", "layout"]
JobStatus = Literal["queued", "running", "done", "failed"]

# All valid page states (spec Section 9.1 + DB CHECK constraint in Section 13).
PageState = Literal[
    "queued",
    "preprocessing",
    "rectification",
    "ptiff_qa_pending",        # PTIFF QA checkpoint (spec Section 3.1 / 8.5)
    "layout_detection",
    "semantic_norm",           # post-human-correction iep1e pass before layout_detection
    "pending_human_correction",
    "accepted",
    "review",
    "failed",
    "split",
]

# ── Terminal page state constant ───────────────────────────────────────────────

# Job/page terminal states for completion accounting.
# Automated worker-stop semantics are defined separately in shared.state_machine.
TERMINAL_PAGE_STATES: frozenset[str] = frozenset(
    {
        "accepted",
        "review",
        "failed",
        "split",
    }
)


# ── PageInput ──────────────────────────────────────────────────────────────────


class PageInput(BaseModel):
    """
    A single page submitted as part of a JobCreateRequest.

    Fields:
        page_number         — 1-indexed page number assigned by the caller (>= 1);
                              must be unique within the job
        input_uri           — storage URI of the raw OTIFF input
        reference_ptiff_uri — optional reference PTIFF for offline evaluation only;
                              stored in page_lineage but MUST NOT influence any
                              routing decision during live pipeline processing
    """

    page_number: Annotated[int, Field(ge=1)]
    input_uri: str
    reference_ptiff_uri: str | None = None


# ── JobCreateRequest ───────────────────────────────────────────────────────────


class JobCreateRequest(BaseModel):
    """
    Request body for POST /v1/jobs.

    Fields:
        collection_id  — collection identifier
        material_type  — one of book, newspaper, archival_document
        pages          — 1 to 1000 PageInput entries
        pipeline_mode  — "preprocess" (preprocessing only) or "layout" (+ layout detection)
        ptiff_qa_mode  — "auto_continue" (default): pipeline runs fully automatic;
                         "manual": pipeline stops at ptiff_qa_pending for human
                         review before layout detection begins. Use "manual" only
                         when the librarian must approve each PTIFF before layout runs.
        policy_version — policy version string to pin processing rules
        shadow_mode    — True to enable async candidate model shadow evaluation

    Validator:
        1 <= len(pages) <= 1000
    """

    collection_id: str
    material_type: MaterialType = "book"
    pages: list[PageInput]
    pipeline_mode: PipelineMode = "layout"
    ptiff_qa_mode: Literal["auto_continue", "manual"] = "auto_continue"
    policy_version: str
    shadow_mode: bool = False

    @model_validator(mode="after")
    def pages_count_in_bounds(self) -> JobCreateRequest:
        n = len(self.pages)
        if not (1 <= n <= 1000):
            raise ValueError(f"pages must contain 1–1000 entries; got {n}")
        return self


# ── JobCreateResponse ──────────────────────────────────────────────────────────


class JobCreateResponse(BaseModel):
    """
    Response body for POST /v1/jobs (HTTP 201).

    Fields:
        job_id      — UUID4 job identifier assigned by EEP
        status      — always "queued" immediately after creation
        page_count  — number of pages accepted (equals len(JobCreateRequest.pages))
        created_at  — timestamp when the job record was created
    """

    job_id: str
    status: Literal["queued"]
    page_count: int
    created_at: datetime


# ── QualitySummary ─────────────────────────────────────────────────────────────


class QualitySummary(BaseModel):
    """
    Per-page quality metrics summary stored as JSONB in job_pages.quality_summary.

    All fields are optional; they are populated only after preprocessing completes.
    Presence of None values does not indicate failure — it indicates the metric
    has not yet been computed (page may be queued or in an early processing state).

    Fields:
        blur_score         — sharpness signal [0, 1]; higher is better
        border_score       — border accuracy signal [0, 1]; higher is better
        skew_residual      — residual skew after normalization (>= 0); lower is better
        foreground_coverage — fraction of image covered by content [0, 1]
    """

    blur_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    border_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    skew_residual: Annotated[float, Field(ge=0.0)] | None = None
    foreground_coverage: Annotated[float, Field(ge=0.0, le=1.0)] | None = None


# ── PageStatus ─────────────────────────────────────────────────────────────────


class PageStatus(BaseModel):
    """
    Status of a single page within a job, as returned in JobStatusResponse.

    Fields:
        page_number         — 1-indexed page number
        sub_page_index      — 0 (left) or 1 (right) for split child pages; None otherwise
        status              — current page state (one of PageState values)
        routing_path        — human-readable routing path label (e.g. "preprocessing_only")
        input_image_uri     — URI of the original uploaded artifact for fallback display
        output_image_uri    — URI of the preprocessed output artifact; None while processing
        output_layout_uri   — URI of the layout JSON artifact; None if not yet produced
        quality_summary     — quality metrics summary; None while processing
        review_reasons      — list of review/correction reason codes; None if not in review
        acceptance_decision — leaf-final outcome (accepted/review/failed);
                              None while in pending_human_correction or non-terminal states
        processing_time_ms  — total elapsed processing time in ms; None while processing
    """

    page_number: int
    sub_page_index: int | None = None
    status: PageState
    routing_path: str | None = None
    input_image_uri: str | None = None
    output_image_uri: str | None = None
    output_layout_uri: str | None = None
    quality_summary: QualitySummary | None = None
    review_reasons: list[str] | None = None
    acceptance_decision: Literal["accepted", "review", "failed"] | None = None
    processing_time_ms: float | None = None
    reading_order: int | None = None


# ── JobStatusSummary ───────────────────────────────────────────────────────────


class JobStatusSummary(BaseModel):
    """
    Compact job row used in paginated list responses (GET /v1/jobs).

    Contains all job configuration, status, and outcome counts.
    Does not include per-page detail (use JobStatusResponse for that).

    Fields:
        job_id                       — job identifier
        collection_id                — collection identifier
        material_type                — book, newspaper, or archival_document
        pipeline_mode                — preprocess or layout
        policy_version               — policy version pinned at job creation
        shadow_mode                  — True if shadow evaluation is enabled
        created_by                   — user identifier from JWT sub; None if unavailable
        status                       — derived job status: queued/running/done/failed
        page_count                   — total pages submitted (immutable after creation)
        accepted_count               — leaf pages that reached accepted
        review_count                 — leaf pages that reached review
        failed_count                 — leaf pages that reached failed
        pending_human_correction_count — leaf pages currently in pending_human_correction
        created_at                   — job creation timestamp
        updated_at                   — last modification timestamp
        completed_at                 — timestamp when all leaf pages reached terminal state;
                                       None while job is queued or running
    """

    job_id: str
    collection_id: str
    material_type: MaterialType
    pipeline_mode: PipelineMode
    policy_version: str
    shadow_mode: bool
    created_by: str | None = None
    status: JobStatus
    page_count: int
    accepted_count: int
    review_count: int
    failed_count: int
    pending_human_correction_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    reading_direction: Literal["ltr", "rtl", "unresolved"] | None = None


# ── JobStatusResponse ──────────────────────────────────────────────────────────


class JobStatusResponse(BaseModel):
    """
    Full job status response for GET /v1/jobs/{job_id}.

    Combines the job-level summary with per-page status list.

    Fields:
        summary — job-level configuration, status, and outcome counts
        pages   — per-page status for every page submitted in the job
    """

    summary: JobStatusSummary
    pages: list[PageStatus]
