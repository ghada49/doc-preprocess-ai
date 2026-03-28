"""
services/eep/app/lineage_api.py
---------------------------------
Packet 7.5 — Lineage endpoint.

Implements:
  GET /v1/lineage/{job_id}/{page_number}

Returns the full audit trail for a single page (spec Section 11.4):
  - All page_lineage rows for the given (job_id, page_number) pair.
    Unsplit pages produce one row; split pages produce two rows
    (sub_page_index 0 and 1).
  - For each lineage row: all service_invocations joined on lineage_id.
  - All quality_gate_log rows for (job_id, page_number).

Auth:
  - require_admin (403 for non-admin callers, 401 for missing/invalid token).

Error responses:
  401 — missing or invalid bearer token
  403 — caller does not have the 'admin' role
  404 — no lineage records found for (job_id, page_number)

Exported:
  router — FastAPI APIRouter (mounted in main.py)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.eep.app.auth import CurrentUser, require_admin
from services.eep.app.db.models import PageLineage, QualityGateLog, ServiceInvocation
from services.eep.app.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["lineage"])


# ── Response schemas ──────────────────────────────────────────────────────────


class ServiceInvocationRecord(BaseModel):
    """One service_invocations row, embedded inside a LineageRecord."""

    id: int
    lineage_id: str
    service_name: str
    service_version: str | None
    model_version: str | None
    model_source: str | None
    invoked_at: datetime
    completed_at: datetime | None
    processing_time_ms: float | None
    status: str
    error_message: str | None
    metrics: Any
    config_snapshot: Any


class QualityGateRecord(BaseModel):
    """One quality_gate_log row."""

    gate_id: str
    job_id: str
    page_number: int
    gate_type: str
    iep1a_geometry: Any
    iep1b_geometry: Any
    structural_agreement: bool | None
    selected_model: str | None
    selection_reason: str | None
    sanity_check_results: Any
    split_confidence: Any
    tta_variance: Any
    artifact_validation_score: float | None
    route_decision: str
    review_reason: str | None
    processing_time_ms: float | None
    created_at: datetime


class LineageRecord(BaseModel):
    """
    One page_lineage row, with all service_invocations for that row embedded.

    Sub-page index is None for unsplit pages; 0 (left) or 1 (right) for split
    children.
    """

    lineage_id: str
    job_id: str
    page_number: int
    sub_page_index: int | None
    correlation_id: str
    input_image_uri: str
    input_image_hash: str | None
    otiff_uri: str
    reference_ptiff_uri: str | None
    ptiff_ssim: float | None
    iep1a_used: bool
    iep1b_used: bool
    selected_geometry_model: str | None
    structural_agreement: bool | None
    iep1d_used: bool
    material_type: str
    routing_path: str | None
    policy_version: str
    acceptance_decision: str | None
    acceptance_reason: str | None
    gate_results: Any
    total_processing_ms: float | None
    shadow_eval_id: str | None
    cleanup_retry_count: int
    preprocessed_artifact_state: str
    layout_artifact_state: str
    output_image_uri: str | None
    parent_page_id: str | None
    split_source: bool
    human_corrected: bool
    human_correction_timestamp: datetime | None
    human_correction_fields: Any
    reviewed_by: str | None
    reviewed_at: datetime | None
    reviewer_notes: str | None
    created_at: datetime
    completed_at: datetime | None
    service_invocations: list[ServiceInvocationRecord]


class LineageResponse(BaseModel):
    """
    Full audit trail for one (job_id, page_number) pair.

    Fields:
        job_id          — parent job identifier
        page_number     — 1-indexed page number
        lineage         — one record for unsplit pages; two (sub_page_index 0
                          and 1) for split pages; each includes its service
                          invocations
        quality_gates   — all quality gate decisions for this page, ordered by
                          created_at ascending
    """

    job_id: str
    page_number: int
    lineage: list[LineageRecord]
    quality_gates: list[QualityGateRecord]


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get(
    "/v1/lineage/{job_id}/{page_number}",
    response_model=LineageResponse,
    status_code=200,
    summary="Full page audit trail",
)
def get_lineage(
    job_id: str,
    page_number: int,
    db: Session = Depends(get_session),
    _user: CurrentUser = Depends(require_admin),
) -> LineageResponse:
    """
    Return the complete audit trail for a single page.

    Includes all ``page_lineage`` rows for ``(job_id, page_number)`` —
    typically one for an unsplit page, two for a split page — with each
    row's ``service_invocations`` embedded.  All ``quality_gate_log``
    entries for the same page are returned in the ``quality_gates`` list.

    **Auth:** admin role required (403 for non-admin callers).
    """
    # ── page_lineage rows ─────────────────────────────────────────────────────
    lineage_rows: list[PageLineage] = (
        db.query(PageLineage)
        .filter(
            PageLineage.job_id == job_id,
            PageLineage.page_number == page_number,
        )
        .order_by(PageLineage.sub_page_index.asc().nullsfirst())
        .all()
    )

    if not lineage_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No lineage found for job_id={job_id!r} page_number={page_number}",
        )

    # ── service_invocations per lineage row ───────────────────────────────────
    lineage_ids = [row.lineage_id for row in lineage_rows]
    all_invocations: list[ServiceInvocation] = (
        db.query(ServiceInvocation)
        .filter(ServiceInvocation.lineage_id.in_(lineage_ids))
        .order_by(ServiceInvocation.invoked_at.asc())
        .all()
    )

    invocations_by_lineage: dict[str, list[ServiceInvocation]] = {
        lid: [] for lid in lineage_ids
    }
    for inv in all_invocations:
        invocations_by_lineage[inv.lineage_id].append(inv)

    # ── quality_gate_log rows ─────────────────────────────────────────────────
    gate_rows: list[QualityGateLog] = (
        db.query(QualityGateLog)
        .filter(
            QualityGateLog.job_id == job_id,
            QualityGateLog.page_number == page_number,
        )
        .order_by(QualityGateLog.created_at.asc())
        .all()
    )

    # ── Assemble response ─────────────────────────────────────────────────────
    lineage_records = [
        LineageRecord(
            lineage_id=row.lineage_id,
            job_id=row.job_id,
            page_number=row.page_number,
            sub_page_index=row.sub_page_index,
            correlation_id=row.correlation_id,
            input_image_uri=row.input_image_uri,
            input_image_hash=row.input_image_hash,
            otiff_uri=row.otiff_uri,
            reference_ptiff_uri=row.reference_ptiff_uri,
            ptiff_ssim=row.ptiff_ssim,
            iep1a_used=row.iep1a_used,
            iep1b_used=row.iep1b_used,
            selected_geometry_model=row.selected_geometry_model,
            structural_agreement=row.structural_agreement,
            iep1d_used=row.iep1d_used,
            material_type=row.material_type,
            routing_path=row.routing_path,
            policy_version=row.policy_version,
            acceptance_decision=row.acceptance_decision,
            acceptance_reason=row.acceptance_reason,
            gate_results=row.gate_results,
            total_processing_ms=row.total_processing_ms,
            shadow_eval_id=row.shadow_eval_id,
            cleanup_retry_count=row.cleanup_retry_count,
            preprocessed_artifact_state=row.preprocessed_artifact_state,
            layout_artifact_state=row.layout_artifact_state,
            output_image_uri=row.output_image_uri,
            parent_page_id=row.parent_page_id,
            split_source=row.split_source,
            human_corrected=row.human_corrected,
            human_correction_timestamp=row.human_correction_timestamp,
            human_correction_fields=row.human_correction_fields,
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
            reviewer_notes=row.reviewer_notes,
            created_at=row.created_at,
            completed_at=row.completed_at,
            service_invocations=[
                ServiceInvocationRecord(
                    id=inv.id,
                    lineage_id=inv.lineage_id,
                    service_name=inv.service_name,
                    service_version=inv.service_version,
                    model_version=inv.model_version,
                    model_source=inv.model_source,
                    invoked_at=inv.invoked_at,
                    completed_at=inv.completed_at,
                    processing_time_ms=inv.processing_time_ms,
                    status=inv.status,
                    error_message=inv.error_message,
                    metrics=inv.metrics,
                    config_snapshot=inv.config_snapshot,
                )
                for inv in invocations_by_lineage[row.lineage_id]
            ],
        )
        for row in lineage_rows
    ]

    quality_gate_records = [
        QualityGateRecord(
            gate_id=g.gate_id,
            job_id=g.job_id,
            page_number=g.page_number,
            gate_type=g.gate_type,
            iep1a_geometry=g.iep1a_geometry,
            iep1b_geometry=g.iep1b_geometry,
            structural_agreement=g.structural_agreement,
            selected_model=g.selected_model,
            selection_reason=g.selection_reason,
            sanity_check_results=g.sanity_check_results,
            split_confidence=g.split_confidence,
            tta_variance=g.tta_variance,
            artifact_validation_score=g.artifact_validation_score,
            route_decision=g.route_decision,
            review_reason=g.review_reason,
            processing_time_ms=g.processing_time_ms,
            created_at=g.created_at,
        )
        for g in gate_rows
    ]

    logger.debug(
        "get_lineage: job=%s page=%d lineage_rows=%d invocations=%d gates=%d",
        job_id,
        page_number,
        len(lineage_records),
        len(all_invocations),
        len(quality_gate_records),
    )
    return LineageResponse(
        job_id=job_id,
        page_number=page_number,
        lineage=lineage_records,
        quality_gates=quality_gate_records,
    )
