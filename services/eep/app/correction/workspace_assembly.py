"""
services/eep/app/correction/workspace_assembly.py
---------------------------------------------------
Packet 5.0 — Correction workspace data assembly.

Assembles a CorrectionWorkspaceResponse from DB records for a page currently
in status='pending_human_correction'. Read-only: no state transitions or writes.

Assembly sources (in query order):
  1. jobs             — material_type, pipeline_mode
  2. job_pages        — page metadata, status guard, review_reasons, output_image_uri
  3. page_lineage     — otiff_uri, output_image_uri, iep1d_used, human_correction_fields
  4. quality_gate_log — iep1a_geometry/iep1b_geometry JSONB, selected_model, split_x
  5. service_invocations — iep1d rectified_image_uri (from metrics when stored)

Source image availability rules (spec Section 11.3):
  - original_otiff_uri: page_lineage.otiff_uri (always when available)
  - best_output_uri:    current workspace preview source for correction
  - iep1c_normalized:   same as best_output_uri (the final IEP1C output in all paths)
  - iep1d_rectified:    from service_invocations.metrics['rectified_image_uri'] when available

Current workspace default priority:
  1. page_lineage.human_correction_fields (set by prior human correction, if any)
  2. quality_gate_log selected geometry data -> current_crop_box and internal split context
  3. None (deskew_angle is always None for a fresh correction — see known limitation)

The correction UI now treats split as a structural choice. current_split_x is
still assembled as backend context for suggestion and fallback purposes, but it
is not the primary reviewer control in the normal workspace flow.

Known limitation: current_deskew_angle is None for pages with no prior human
correction because the IEP1C normalization deskew angle is not persisted in the
DB. The quality_summary JSONB stores skew_residual (not the applied angle).
Adding a dedicated column is out of Packet 5.0 scope and must not be done here.

Known limitation: iep1d_rectified is None in the current implementation because
rescue_step._call_iep1d() stores metrics=None. This assembly returns the correct
None value; the field will be populated once the rescue_step is updated to write
the rectified URI into service_invocations.metrics.

Exported:
  PageNotInCorrectionError      — raised when the page is not in pending_human_correction
  assemble_correction_workspace — main assembly entry point
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from services.eep.app.correction.workspace_schema import (
    BranchOutputs,
    ChildPageSummary,
    CorrectionWorkspaceResponse,
    GeometrySummary,
    QuadPoint,
    SelectionMode,
)
from services.eep.app.db.models import Job, JobPage, PageLineage, QualityGateLog, ServiceInvocation
from shared.schemas.layout import LayoutArtifactRole

__all__ = [
    "PageNotInCorrectionError",
    "_full_res_dims_from_lineage",
    "assemble_correction_workspace",
]

_GEOMETRY_GATE_TYPES = frozenset(
    {
        "geometry_selection",
        "geometry_selection_post_rectification",
    }
)


# ── Exception ─────────────────────────────────────────────────────────────────


class PageNotInCorrectionError(Exception):
    """
    Raised when the requested page cannot be served as a correction workspace.

    Covers:
      - job not found
      - page record not found
      - page status is not 'pending_human_correction'
    """


# ── DB read helpers ────────────────────────────────────────────────────────────


def _fetch_job(session: Session, job_id: str) -> Job | None:
    return session.query(Job).filter(Job.job_id == job_id).first()


def _fetch_page(
    session: Session,
    job_id: str,
    page_number: int,
    sub_page_index: int | None,
) -> JobPage | None:
    q = session.query(JobPage).filter(
        JobPage.job_id == job_id,
        JobPage.page_number == page_number,
    )
    if sub_page_index is None:
        q = q.filter(JobPage.sub_page_index.is_(None))
    else:
        q = q.filter(JobPage.sub_page_index == sub_page_index)
    return q.first()


def _fetch_lineage(
    session: Session,
    job_id: str,
    page_number: int,
    sub_page_index: int | None,
) -> PageLineage | None:
    q = session.query(PageLineage).filter(
        PageLineage.job_id == job_id,
        PageLineage.page_number == page_number,
    )
    if sub_page_index is None:
        q = q.filter(PageLineage.sub_page_index.is_(None))
    else:
        q = q.filter(PageLineage.sub_page_index == sub_page_index)
    return q.first()


def _fetch_latest_geometry_gate(
    session: Session,
    job_id: str,
    page_number: int,
) -> QualityGateLog | None:
    """
    Return the most recent geometry gate log for this page (either first or
    post-rectification pass). The post-rectification pass takes precedence
    when present (ordering by created_at desc).
    """
    return (
        session.query(QualityGateLog)
        .filter(
            QualityGateLog.job_id == job_id,
            QualityGateLog.page_number == page_number,
            QualityGateLog.gate_type.in_(list(_GEOMETRY_GATE_TYPES)),
        )
        .order_by(desc(QualityGateLog.created_at))
        .first()
    )


def _fetch_child_pages(
    session: Session,
    job_id: str,
    page_number: int,
) -> list[JobPage]:
    """Return any existing split children for this page, ordered left to right."""
    return (
        session.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.sub_page_index.isnot(None),
        )
        .order_by(JobPage.sub_page_index.asc())
        .all()
    )


def _fetch_iep1d_rectified_uri(session: Session, lineage_id: str) -> str | None:
    """
    Attempt to retrieve the IEP1D rectified artifact URI from service_invocations.

    Looks for a successful IEP1D invocation with a 'rectified_image_uri' key
    in the metrics JSONB field. Returns None when not available.

    Note: the current rescue_step._call_iep1d() implementation stores
    metrics=None, so this returns None until that is updated. The assembly
    logic is correct and forward-compatible once metrics are populated.
    """
    inv = (
        session.query(ServiceInvocation)
        .filter(
            ServiceInvocation.lineage_id == lineage_id,
            ServiceInvocation.service_name == "iep1d",
            ServiceInvocation.status == "success",
        )
        .order_by(desc(ServiceInvocation.invoked_at))
        .first()
    )
    if inv is None or inv.metrics is None:
        return None
    value = inv.metrics.get("rectified_image_uri")
    return str(value) if value is not None else None


# ── Internal helpers ───────────────────────────────────────────────────────────


def _geometry_summary_from_jsonb(geo: dict[str, Any]) -> GeometrySummary | None:
    """
    Build a GeometrySummary from a quality_gate_log geometry JSONB dict.

    Returns None when the dict is missing required keys or has unexpected types
    (defensive: malformed gate JSONB must not crash the workspace assembly).
    """
    try:
        return GeometrySummary(
            page_count=geo["page_count"],
            split_required=geo["split_required"],
            geometry_confidence=geo["geometry_confidence"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _params_from_human_correction(
    hcf: dict[str, Any],
) -> tuple[list[int] | None, float | None, int | None, SelectionMode, list[QuadPoint] | None]:
    """
    Extract crop, deskew, and any stored split boundary from human correction data.

    Coerces crop_box values to int and split_x to int to ensure consistent types.
    deskew_angle is returned as-is (float or None).
    """
    raw_crop = hcf.get("crop_box")
    deskew_angle: float | None = hcf.get("deskew_angle")
    raw_split = hcf.get("split_x")
    raw_selection_mode = hcf.get("selection_mode")
    raw_quad = hcf.get("quad_points")

    crop_box: list[int] | None = [int(v) for v in raw_crop] if raw_crop is not None else None
    split_x: int | None = int(raw_split) if raw_split is not None else None
    selection_mode: SelectionMode = "quad" if raw_selection_mode == "quad" else "rect"
    quad_points: list[QuadPoint] | None = None
    if raw_quad is not None:
        quad_points = [(float(point[0]), float(point[1])) for point in raw_quad]
        selection_mode = "quad"

    return crop_box, deskew_angle, split_x, selection_mode, quad_points


def _correction_source_uri(correction_fields: dict[str, Any]) -> str | None:
    """Return the reviewer-selected source artifact recorded in correction fields."""
    raw_source_uri = correction_fields.get("source_artifact_uri")
    return raw_source_uri if isinstance(raw_source_uri, str) and raw_source_uri else None


def _quad_points_from_crop_box(crop_box: list[int] | None) -> list[QuadPoint] | None:
    """Return a rectangular quad from an axis-aligned crop box."""
    if crop_box is None:
        return None
    x1, y1, x2, y2 = crop_box
    return [
        (float(x1), float(y1)),
        (float(x2), float(y1)),
        (float(x2), float(y2)),
        (float(x1), float(y2)),
    ]


def _resolve_current_output_role(
    *,
    page: JobPage,
    lineage: PageLineage | None,
    original_otiff_uri: str | None,
    current_output_uri: str | None,
) -> LayoutArtifactRole | None:
    """Classify the current workspace artifact for reviewer-facing labels."""
    if current_output_uri is None:
        return None
    if page.sub_page_index is not None or (lineage is not None and lineage.split_source):
        return "split_child"
    if lineage is not None and lineage.human_corrected:
        return "human_corrected"
    if original_otiff_uri is not None and current_output_uri != original_otiff_uri:
        return "normalized_output"
    return "original_upload"


def _params_from_gate(
    gate: QualityGateLog,
    page_index: int = 0,
) -> tuple[list[int] | None, float | None, int | None]:
    """
    Derive current workspace defaults from the geometry gate log.

    crop_box:    derived from the selected model's pages[0].bbox (x_min, y_min, x_max, y_max).
    split_x:     always None.  Gate JSONB stores model outputs in proxy-image pixel space;
                 the proxy dimensions are not persisted in the gate log so the value cannot
                 be scaled to full-resolution safely.
    deskew_angle: always None — the IEP1C normalization deskew angle is not stored
                  in the DB (see module-level known-limitation note).

    When the selected_model field is None (page routed before model selection),
    falls back to iep1a_geometry then iep1b_geometry.
    """
    geo_jsonb: dict[str, Any] | None = None

    if gate.selected_model == "iep1a":
        geo_jsonb = gate.iep1a_geometry
    elif gate.selected_model == "iep1b":
        geo_jsonb = gate.iep1b_geometry

    # Fallback when no model was selected (routing happened before selection)
    if geo_jsonb is None:
        geo_jsonb = gate.iep1a_geometry or gate.iep1b_geometry

    if geo_jsonb is None:
        return None, None, None

    # Derive crop_box from the selected detected page's bbox. Split children
    # use sub_page_index so Page 1 does not inherit Page 0's region.
    crop_box: list[int] | None = None
    pages = geo_jsonb.get("pages", [])
    selected_page = pages[page_index] if 0 <= page_index < len(pages) else None
    if selected_page:
        bbox = selected_page.get("bbox")
        if bbox is not None and len(bbox) == 4:
            crop_box = [int(v) for v in bbox]

    # split_x from gate JSONB is in proxy-image pixel space.  The proxy
    # dimensions required to scale it to full-resolution are not stored in the
    # gate log, so the value cannot be used safely here.
    return crop_box, None, None


def _default_child_crop_box_from_dims(
    image_width: int | None,
    image_height: int | None,
    sub_page_index: int,
) -> list[int] | None:
    """Return a conservative half-page crop when no child-specific geometry exists."""
    if image_width is None or image_height is None or image_width < 2 or image_height < 1:
        return None
    midpoint = image_width // 2
    if sub_page_index == 0:
        return [0, 0, midpoint, image_height]
    return [midpoint, 0, image_width, image_height]


def _full_res_dims_from_lineage(
    lineage: "PageLineage | None",
) -> tuple[int | None, int | None]:
    """
    Read actual full-resolution image dimensions from the downsample gate stored
    in page_lineage.gate_results["downsample"]["original_width/height"].

    The worker writes these values via run_downsample_step() before geometry
    inference.  They are in full-resolution pixel space and safe to use directly.

    Returns (width, height) when both fields are present and positive;
    (None, None) otherwise.
    """
    if lineage is None:
        return None, None
    gate_results = lineage.gate_results
    if not isinstance(gate_results, dict):
        return None, None
    downsample = gate_results.get("downsample")
    if not isinstance(downsample, dict):
        return None, None
    raw_w = downsample.get("original_width")
    raw_h = downsample.get("original_height")
    if raw_w is None or raw_h is None:
        return None, None
    try:
        w, h = int(raw_w), int(raw_h)
    except (TypeError, ValueError):
        return None, None
    if w <= 0 or h <= 0:
        return None, None
    return w, h


def _suggested_page_structure(
    *,
    child_pages: list[JobPage],
) -> str:
    """
    Derive the default single-page vs spread choice for the workspace UI.

    Only existing child pages (a DB fact) drive a "spread" suggestion.
    Model-derived split_x is not used here: it is in proxy-image space and
    cannot safely represent a structural decision without reviewer input.
    """
    if child_pages:
        return "spread"
    return "single"


# ── Main assembly entry point ──────────────────────────────────────────────────


def assemble_correction_workspace(
    session: Session,
    job_id: str,
    page_number: int,
    sub_page_index: int | None = None,
) -> CorrectionWorkspaceResponse:
    """
    Assemble a CorrectionWorkspaceResponse for a page in pending_human_correction.

    Queries job, job_pages, page_lineage, quality_gate_log, and service_invocations
    to produce a complete workspace payload. Supports both:
      - Original-only scenario: only otiff_uri available; no derived artifact yet
      - Original + branch artifacts: normalized PTIFF and optional IEP1D output

    Args:
        session:        SQLAlchemy session (caller owns lifecycle).
        job_id:         Parent job identifier.
        page_number:    1-indexed page number.
        sub_page_index: 0 or 1 for split children; None for unsplit pages.

    Returns:
        CorrectionWorkspaceResponse — fully assembled workspace payload.

    Raises:
        PageNotInCorrectionError when:
          - the job does not exist
          - the page record does not exist
          - the page status is not 'pending_human_correction'
    """
    # ── Step 1: Fetch and validate job ────────────────────────────────────────
    job = _fetch_job(session, job_id)
    if job is None:
        raise PageNotInCorrectionError(f"Job not found: {job_id!r}")

    # ── Step 2: Fetch and guard page status ────────────────────────────────────
    page = _fetch_page(session, job_id, page_number, sub_page_index)
    if page is None:
        raise PageNotInCorrectionError(
            f"Page not found: job={job_id!r} page={page_number} sub={sub_page_index}"
        )
    if page.status != "pending_human_correction":
        raise PageNotInCorrectionError(
            f"Page status is {page.status!r}, not 'pending_human_correction'"
        )

    # ── Step 3: Fetch lineage (may be None for pages with no lineage record) ──
    lineage = _fetch_lineage(session, job_id, page_number, sub_page_index)

    # ── Step 4: Fetch most recent geometry gate log ────────────────────────────
    gate = _fetch_latest_geometry_gate(session, job_id, page_number)
    child_pages = _fetch_child_pages(session, job_id, page_number)

    # ── Step 5: Source image references ────────────────────────────────────────
    # original_otiff_uri: from page_lineage.otiff_uri; fall back to input_image_uri
    # when lineage is absent (edge case: lineage not yet created).
    original_otiff_uri: str | None = (
        lineage.otiff_uri if lineage is not None else page.input_image_uri
    )
    parent_source_uri: str | None = None
    if page.sub_page_index is not None:
        parent_source_uri = (
            (lineage.otiff_uri if lineage is not None else None)
            or page.input_image_uri
        )

    correction_fields = (
        lineage.human_correction_fields
        if lineage is not None and isinstance(lineage.human_correction_fields, dict)
        else {}
    )
    prior_source_uri = _correction_source_uri(correction_fields)
    # current_output_uri: the authoritative current artifact for correction/UI.
    # Child pages without a materialized child TIFF preview the shared parent
    # source artifact recorded in their correction fields.
    current_output_uri: str | None = page.output_image_uri or (
        lineage.output_image_uri if lineage is not None else None
    )
    if current_output_uri is None and page.sub_page_index is not None:
        current_output_uri = prior_source_uri
    current_output_role = _resolve_current_output_role(
        page=page,
        lineage=lineage,
        original_otiff_uri=original_otiff_uri,
        current_output_uri=current_output_uri,
    )
    current_layout_uri: str | None = page.output_layout_uri
    normalized_output_uri: str | None = None
    if prior_source_uri is not None and prior_source_uri != original_otiff_uri:
        if current_output_uri is None or prior_source_uri != current_output_uri:
            normalized_output_uri = prior_source_uri

    # ── Step 6: IEP1D rectified URI ────────────────────────────────────────────
    iep1d_rectified_uri: str | None = None
    if lineage is not None and lineage.iep1d_used:
        iep1d_rectified_uri = _fetch_iep1d_rectified_uri(session, lineage.lineage_id)

    # ── Step 7: Branch geometry summaries ─────────────────────────────────────
    iep1a_geo: GeometrySummary | None = None
    iep1b_geo: GeometrySummary | None = None
    if gate is not None:
        if gate.iep1a_geometry:
            iep1a_geo = _geometry_summary_from_jsonb(gate.iep1a_geometry)
        if gate.iep1b_geometry:
            iep1b_geo = _geometry_summary_from_jsonb(gate.iep1b_geometry)

    # iep1c_normalized = the final preprocessing output (IEP1C always produces it)
    branch_outputs = BranchOutputs(
        iep1a_geometry=iep1a_geo,
        iep1b_geometry=iep1b_geo,
        iep1c_normalized=normalized_output_uri,
        iep1d_rectified=iep1d_rectified_uri,
    )

    # Dimensions are needed both for frontend scaling and for a conservative
    # child-region fallback when a split child has no saved human correction.
    page_image_width: int | None = None
    page_image_height: int | None = None
    raw_img_width = correction_fields.get("image_width")
    if raw_img_width is not None:
        page_image_width = int(raw_img_width)
    raw_img_height = correction_fields.get("image_height")
    if raw_img_height is not None:
        page_image_height = int(raw_img_height)
    if page_image_width is None or page_image_height is None:
        lineage_w, lineage_h = _full_res_dims_from_lineage(lineage)
        if page_image_width is None:
            page_image_width = lineage_w
        if page_image_height is None:
            page_image_height = lineage_h

    # ── Step 8: Current correction parameters ─────────────────────────────────
    # Priority: prior human correction data > geometry-derived defaults
    current_crop_box: list[int] | None = None
    current_deskew_angle: float | None = None
    current_split_x: int | None = None
    current_selection_mode: SelectionMode = "rect"
    current_quad_points: list[QuadPoint] | None = None

    if lineage is not None and lineage.human_correction_fields:
        (
            current_crop_box,
            current_deskew_angle,
            current_split_x,
            current_selection_mode,
            current_quad_points,
        ) = _params_from_human_correction(lineage.human_correction_fields)
    elif gate is not None:
        gate_page_index = page.sub_page_index if page.sub_page_index is not None else 0
        current_crop_box, current_deskew_angle, current_split_x = _params_from_gate(
            gate,
            page_index=gate_page_index,
        )

    if page.sub_page_index is not None:
        current_selection_mode = "quad"
        if current_crop_box is None:
            current_crop_box = _default_child_crop_box_from_dims(
                page_image_width,
                page_image_height,
                page.sub_page_index,
            )
        if current_quad_points is None:
            current_quad_points = _quad_points_from_crop_box(current_crop_box)

    suggested_page_structure = _suggested_page_structure(
        child_pages=child_pages,
    )
    child_page_summaries = [
        ChildPageSummary(
            sub_page_index=int(child.sub_page_index),
            status=child.status,  # type: ignore[arg-type]
            output_image_uri=child.output_image_uri,
        )
        for child in child_pages
        if child.sub_page_index is not None
    ]

    # ── Step 9: Assemble and return ────────────────────────────────────────────
    return CorrectionWorkspaceResponse(
        job_id=job_id,
        page_number=page_number,
        sub_page_index=sub_page_index,
        material_type=job.material_type,  # type: ignore[arg-type]
        pipeline_mode=job.pipeline_mode,  # type: ignore[arg-type]
        review_reasons=list(page.review_reasons) if page.review_reasons else [],
        original_otiff_uri=original_otiff_uri,
        parent_source_uri=parent_source_uri,
        current_output_uri=current_output_uri,
        current_output_role=current_output_role,
        current_layout_uri=current_layout_uri,
        best_output_uri=current_output_uri,
        branch_outputs=branch_outputs,
        suggested_page_structure=suggested_page_structure,  # type: ignore[arg-type]
        child_pages=child_page_summaries,
        current_selection_mode=current_selection_mode,
        current_quad_points=current_quad_points,
        current_crop_box=current_crop_box,
        current_deskew_angle=current_deskew_angle,
        current_split_x=current_split_x,
        page_image_width=page_image_width,
        page_image_height=page_image_height,
    )
