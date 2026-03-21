"""
services/eep/app/db/quality_gate.py
--------------------------------------
Quality gate log helpers for the EEP processing pipeline.

Quality gate log rows are IMMUTABLE once written (spec Section 13).
This module only provides the write path; audit reads are out of scope
for the worker (handled by Phase 7 reporting endpoints).

Gate types (spec Section 13 — quality_gate_log.gate_type CHECK):
    geometry_selection                 — initial geometry cascade (Step 3)
    geometry_selection_post_rectification — second-pass geometry (Step 6.5)
    artifact_validation                — first artifact quality check (Step 5)
    artifact_validation_final          — post-rectification validation (Step 7)
    layout                             — layout consensus gate (Step 13)

Route decisions (spec Section 13 — quality_gate_log.route_decision CHECK):
    accepted               — artifact valid, proceeds downstream
    rectification          — artifact invalid, IEP1D fallback triggered
    pending_human_correction — failures requiring human review
    review                 — layout consensus failures or permanent issues

Exported:
    VALID_GATE_TYPES      — frozenset of allowed gate_type values
    VALID_ROUTE_DECISIONS — frozenset of allowed route_decision values
    log_gate              — insert an immutable quality_gate_log row
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from services.eep.app.db.models import QualityGateLog

__all__ = [
    "VALID_GATE_TYPES",
    "VALID_ROUTE_DECISIONS",
    "log_gate",
]

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_GATE_TYPES: frozenset[str] = frozenset(
    {
        "geometry_selection",
        "geometry_selection_post_rectification",
        "artifact_validation",
        "artifact_validation_final",
        "layout",
    }
)

VALID_ROUTE_DECISIONS: frozenset[str] = frozenset(
    {
        "accepted",
        "rectification",
        "pending_human_correction",
        "review",
    }
)


# ── Write helper ───────────────────────────────────────────────────────────────


def log_gate(
    session: Session,
    *,
    gate_id: str,
    job_id: str,
    page_number: int,
    gate_type: str,
    route_decision: str,
    iep1a_geometry: dict[str, Any] | None = None,
    iep1b_geometry: dict[str, Any] | None = None,
    structural_agreement: bool | None = None,
    selected_model: str | None = None,
    selection_reason: str | None = None,
    sanity_check_results: dict[str, Any] | None = None,
    split_confidence: dict[str, Any] | None = None,
    tta_variance: dict[str, Any] | None = None,
    artifact_validation_score: float | None = None,
    review_reason: str | None = None,
    processing_time_ms: float | None = None,
) -> QualityGateLog:
    """
    Insert an immutable quality gate decision record.

    Args:
        session:                  SQLAlchemy session (caller owns commit/rollback).
        gate_id:                  UUID string (unique primary key, caller-supplied).
        job_id:                   Parent job identifier.
        page_number:              1-indexed page number.
        gate_type:                One of VALID_GATE_TYPES.
        route_decision:           One of VALID_ROUTE_DECISIONS.
        iep1a_geometry:           Raw IEP1A GeometryResponse as a dict (optional).
        iep1b_geometry:           Raw IEP1B GeometryResponse as a dict (optional).
        structural_agreement:     True when both models agreed; None if only one
                                  model was invoked.
        selected_model:           'iep1a' | 'iep1b' | None.
        selection_reason:         Human-readable reason for model selection.
        sanity_check_results:     Per-filter sanity check pass/fail dict (JSONB).
        split_confidence:         Split detection confidence values (JSONB).
        tta_variance:             TTA variance per prediction head (JSONB).
        artifact_validation_score: Normalized [0, 1] quality score.
        review_reason:            Explanation when route_decision='review'.
        processing_time_ms:       Total time for this gate evaluation (ms).

    Returns:
        The newly created QualityGateLog ORM instance added to *session*
        (not yet committed).

    Raises:
        ValueError if gate_type is not in VALID_GATE_TYPES.
        ValueError if route_decision is not in VALID_ROUTE_DECISIONS.
    """
    if gate_type not in VALID_GATE_TYPES:
        raise ValueError(
            f"Invalid gate_type: {gate_type!r}. " f"Valid values: {sorted(VALID_GATE_TYPES)}"
        )
    if route_decision not in VALID_ROUTE_DECISIONS:
        raise ValueError(
            f"Invalid route_decision: {route_decision!r}. "
            f"Valid values: {sorted(VALID_ROUTE_DECISIONS)}"
        )

    record = QualityGateLog(
        gate_id=gate_id,
        job_id=job_id,
        page_number=page_number,
        gate_type=gate_type,
        route_decision=route_decision,
        iep1a_geometry=iep1a_geometry,
        iep1b_geometry=iep1b_geometry,
        structural_agreement=structural_agreement,
        selected_model=selected_model,
        selection_reason=selection_reason,
        sanity_check_results=sanity_check_results,
        split_confidence=split_confidence,
        tta_variance=tta_variance,
        artifact_validation_score=artifact_validation_score,
        review_reason=review_reason,
        processing_time_ms=processing_time_ms,
    )
    session.add(record)
    return record
