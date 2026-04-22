"""
services/eep/app/correction/workspace_schema.py
------------------------------------------------
Packet 5.0 — Correction workspace response schema.

Defines the read-side schema for GET /v1/correction-queue/{job_id}/{page_number}.
Matches spec Section 11.3 correction workspace response exactly.

No write or apply logic lives here. The apply path comes in Packets 5.2/5.3/5.4.

Schema fields:
  job_id               — parent job identifier
  page_number          — 1-indexed page number
  sub_page_index       — 0/1 for split children; None for unsplit pages
  material_type        — job material type (book | newspaper | archival_document)
  pipeline_mode        — preprocess | layout | layout_with_ocr; frontend suppresses
                         layout fields for preprocess-only jobs (spec Section 11.3)
  review_reasons       — list of reason codes that caused the correction routing
  original_otiff_uri   — original raw OTIFF input (always included when available)
  best_output_uri      — best available derived preprocessing artifact
  branch_outputs       — per-branch geometry summaries and artifact URIs
  suggested_page_structure — default structural choice for the reviewer UI
  child_pages         — existing split child references for Page 0 / Page 1 navigation
  current_crop_box     — [x_min, y_min, x_max, y_max] current crop bounds; None when
                         no geometry or prior correction data is available
  current_deskew_angle — deskew angle in degrees; None when unavailable
  current_split_x      — internal split boundary context; retained for backend
                         compatibility and advanced fallback, not normal reviewer entry

Availability rules (spec Section 11.3):
  - original_otiff_uri: from page_lineage.otiff_uri; always included when available
  - best_output_uri: best available derived artifact (IEP1C output or IEP1D output)
  - branch_outputs.iep1c_normalized: URI of the final IEP1C normalized artifact
  - branch_outputs.iep1d_rectified: URI of IEP1D rectified artifact; null when not used
  - branch_outputs.iep1a_geometry: compact geometry summary from IEP1A; null when not used
  - branch_outputs.iep1b_geometry: compact geometry summary from IEP1B; null when not used

Exported:
  GeometrySummary              — compact per-branch geometry summary
  BranchOutputs                — all branch artifact references and geometry summaries
  CorrectionWorkspaceResponse  — top-level workspace response model
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from shared.schemas.eep import MaterialType, PageState, PipelineMode
from shared.schemas.layout import LayoutArtifactRole

PageStructure = Literal["single", "spread"]
SelectionMode = Literal["rect", "quad"]
QuadPoint = tuple[float, float]

# ── Branch-level geometry summary ──────────────────────────────────────────────


class GeometrySummary(BaseModel):
    """
    Compact geometry summary for one branch (IEP1A or IEP1B) in the workspace.

    Derived from quality_gate_log.iep1a_geometry / iep1b_geometry JSONB.
    Contains only the fields needed for human review comparison.

    Fields:
        page_count          — number of pages detected by this model (1 or 2)
        split_required      — True when the model detected a two-page spread
        geometry_confidence — min confidence across all detected instances [0, 1]
    """

    page_count: Annotated[int, Field(ge=1, le=2)]
    split_required: bool
    geometry_confidence: Annotated[float, Field(ge=0.0, le=1.0)]


# ── Branch outputs ─────────────────────────────────────────────────────────────


class BranchOutputs(BaseModel):
    """
    Per-branch artifact references and geometry summaries.

    Each field is nullable: presence reflects whether the corresponding
    branch was actually invoked during processing.

    Availability rules (spec Section 11.3):
      - iep1a_geometry: present when IEP1A was invoked for this page
      - iep1b_geometry: present when IEP1B was invoked for this page
      - iep1c_normalized: URI of the final IEP1C normalized artifact; None
                          when preprocessing did not produce an output artifact
      - iep1d_rectified: URI of the IEP1D rectified artifact; None when IEP1D
                         was not invoked or the URI is not stored
    """

    iep1a_geometry: GeometrySummary | None = None
    iep1b_geometry: GeometrySummary | None = None
    iep1c_normalized: str | None = None
    iep1d_rectified: str | None = None


class ChildPageSummary(BaseModel):
    """
    Minimal child-page reference used by the correction workspace UI.

    Returned for both split parents and split children so the frontend can
    render Page 0 / Page 1 navigation after a reviewer-driven spread decision
    without reconstructing lineage locally.
    """

    sub_page_index: Annotated[int, Field(ge=0, le=1)]
    status: PageState
    output_image_uri: str | None = None


# ── Top-level workspace response ───────────────────────────────────────────────


class CorrectionWorkspaceResponse(BaseModel):
    """
    Read-side response for GET /v1/correction-queue/{job_id}/{page_number}.

    Returned for pages currently in status='pending_human_correction'.
    Contains all data required for a human reviewer to inspect, compare,
    and submit corrections without any ad-hoc frontend data reconstruction.

    Source image references follow availability rules (spec Section 11.3):
      - original_otiff_uri: always included when available (raw OTIFF input)
      - best_output_uri: best available derived artifact
      - branch_outputs: each available branch artifact included individually

    Current correction parameters represent the last-known state for the
    correction form initial values and structure defaults:
      - After a prior human correction: from page_lineage.human_correction_fields
      - For a fresh correction routing: derived from quality gate geometry data
      - suggested_page_structure: the structural choice the UI should default to
      - current_deskew_angle: None when no prior correction exists (the IEP1C
        normalization deskew angle is not stored in the current DB schema)

    Fields:
        job_id               — parent job identifier
        page_number          — 1-indexed page number (>= 1)
        sub_page_index       — 0 (left child) or 1 (right child); None for unsplit pages
        material_type        — job material type
        pipeline_mode        — preprocess | layout | layout_with_ocr; frontend suppresses
                               layout-related fields in preprocess-only mode (spec Section 11.3)
        review_reasons       — list of reason codes from preprocessing quality gates
        original_otiff_uri   — URI of the original raw OTIFF input; None when unavailable
        best_output_uri      — URI of best available derived artifact; None when unavailable
        branch_outputs       — per-branch artifact references and geometry summaries
        suggested_page_structure — default reviewer choice: single page vs spread
        child_pages         — existing child review units for Page 0 / Page 1 navigation
        current_crop_box     — [x_min, y_min, x_max, y_max] current crop bounds in pixels
        current_deskew_angle — deskew angle in degrees; None when unavailable
        current_split_x      — internal split boundary context; None for single-page
    """

    job_id: str
    page_number: Annotated[int, Field(ge=1)]
    sub_page_index: int | None = None
    material_type: MaterialType
    pipeline_mode: PipelineMode
    review_reasons: list[str]
    original_otiff_uri: str | None = None
    parent_source_uri: str | None = None
    current_output_uri: str | None = None
    current_output_role: LayoutArtifactRole | None = None
    current_layout_uri: str | None = None
    best_output_uri: str | None = None
    branch_outputs: BranchOutputs
    suggested_page_structure: PageStructure = "single"
    child_pages: list[ChildPageSummary] = Field(default_factory=list)
    current_selection_mode: SelectionMode = "rect"
    current_quad_points: Annotated[list[QuadPoint], Field(min_length=4, max_length=4)] | None = None
    current_crop_box: Annotated[list[int], Field(min_length=4, max_length=4)] | None = None
    current_deskew_angle: float | None = None
    current_split_x: int | None = None
    page_image_width: int | None = None
    page_image_height: int | None = None
