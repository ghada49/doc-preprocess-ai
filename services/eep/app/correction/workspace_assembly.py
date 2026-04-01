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
  - best_output_uri:    job_pages.output_image_uri ?? page_lineage.output_image_uri
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
)
from services.eep.app.db.models import Job, JobPage, PageLineage, QualityGateLog, ServiceInvocation

__all__ = [
    "PageNotInCorrectionError",
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
) -> tuple[list[int] | None, float | None, int | None]:
    """
    Extract crop, deskew, and any stored split boundary from human correction data.

    Coerces crop_box values to int and split_x to int to ensure consistent types.
    deskew_angle is returned as-is (float or None).
    """
    raw_crop = hcf.get("crop_box")
    deskew_angle: float | None = hcf.get("deskew_angle")
    raw_split = hcf.get("split_x")

    crop_box: list[int] | None = [int(v) for v in raw_crop] if raw_crop is not None else None
    split_x: int | None = int(raw_split) if raw_split is not None else None

    return crop_box, deskew_angle, split_x


def _params_from_gate(
    gate: QualityGateLog,
) -> tuple[list[int] | None, float | None, int | None]:
    """
    Derive current workspace defaults from the geometry gate log.

    crop_box: derived from the selected model's pages[0].bbox (x_min, y_min, x_max, y_max).
    split_x:  derived from the selected model's top-level split_x field as
              backend context for structure suggestions or fallback behavior.
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

    # Derive crop_box from first detected page's bbox
    crop_box: list[int] | None = None
    pages = geo_jsonb.get("pages", [])
    if pages:
        bbox = pages[0].get("bbox")
        if bbox is not None and len(bbox) == 4:
            crop_box = [int(v) for v in bbox]

    # Derive split_x from top-level geometry split_x
    raw_split_x = geo_jsonb.get("split_x")
    split_x: int | None = int(raw_split_x) if raw_split_x is not None else None

    return crop_box, None, split_x


def _suggested_page_structure(
    *,
    current_split_x: int | None,
    child_pages: list[JobPage],
) -> str:
    """Derive the default single-page vs spread choice for the workspace UI."""
    if child_pages or current_split_x is not None:
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

    # best_output_uri: best available derived artifact.
    # job_pages.output_image_uri is the authoritative current output; fall back
    # to page_lineage.output_image_uri if the page record has not been updated yet.
    best_output_uri: str | None = page.output_image_uri or (
        lineage.output_image_uri if lineage is not None else None
    )

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
        iep1c_normalized=best_output_uri,
        iep1d_rectified=iep1d_rectified_uri,
    )

    # ── Step 8: Current correction parameters ─────────────────────────────────
    # Priority: prior human correction data > geometry-derived defaults
    current_crop_box: list[int] | None = None
    current_deskew_angle: float | None = None
    current_split_x: int | None = None

    if lineage is not None and lineage.human_correction_fields:
        current_crop_box, current_deskew_angle, current_split_x = _params_from_human_correction(
            lineage.human_correction_fields
        )
    elif gate is not None:
        current_crop_box, current_deskew_angle, current_split_x = _params_from_gate(gate)

    suggested_page_structure = _suggested_page_structure(
        current_split_x=current_split_x,
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
        best_output_uri=best_output_uri,
        branch_outputs=branch_outputs,
        suggested_page_structure=suggested_page_structure,  # type: ignore[arg-type]
        child_pages=child_page_summaries,
        current_crop_box=current_crop_box,
        current_deskew_angle=current_deskew_angle,
        current_split_x=current_split_x,
    )
