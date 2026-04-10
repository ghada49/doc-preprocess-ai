"""
services/eep/app/db/lineage.py
--------------------------------
Page lineage record management for the EEP processing pipeline.

Implements the DB-first write protocol (spec Section 7.2):
  1. ``create_lineage()`` records write intent BEFORE any S3 write.
     Both artifact states begin as 'pending'.
  2. ``confirm_preprocessed_artifact()`` / ``confirm_layout_artifact()``
     set state to 'confirmed' immediately after a successful S3 write.
  3. ``mark_artifact_recovery_failed()`` sets state to 'recovery_failed' after
     3+ retries with age > 3× grace period (triggered by recovery service).

Artifact state lifecycle:
  pending → confirmed            (artifact written and durable)
  pending → recovery_failed      (write exhausted all retries)

All columns mirror the page_lineage table definition in the migration
(spec Section 13).  ptiff_ssim is NEVER written here — it is an offline
evaluation metric that MUST NOT influence routing (spec Section 12.3).

Exported:
    create_lineage               — insert new lineage row (artifact state=pending)
    confirm_preprocessed_artifact — set preprocessed_artifact_state='confirmed'
    confirm_layout_artifact       — set layout_artifact_state='confirmed'
    mark_artifact_recovery_failed — set artifact_state='recovery_failed' + incr retry count
    update_geometry_result        — record geometry invocation outcomes
    update_lineage_completion     — record final acceptance decision
    record_human_correction       — record human-corrected fields
    get_lineage                   — fetch a lineage row by primary key
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from services.eep.app.db.models import PageLineage

__all__ = [
    "create_lineage",
    "confirm_preprocessed_artifact",
    "confirm_layout_artifact",
    "mark_artifact_recovery_failed",
    "update_geometry_result",
    "update_lineage_completion",
    "record_human_correction",
    "get_lineage",
]

ArtifactType = Literal["preprocessed", "layout"]


# ── Write helpers ──────────────────────────────────────────────────────────────


def create_lineage(
    session: Session,
    *,
    lineage_id: str,
    job_id: str,
    page_number: int,
    correlation_id: str,
    input_image_uri: str,
    otiff_uri: str,
    material_type: str,
    policy_version: str,
    sub_page_index: int | None = None,
    input_image_hash: str | None = None,
    parent_page_id: str | None = None,
    split_source: bool = False,
) -> PageLineage:
    """
    Insert a new page_lineage row with both artifact states set to 'pending'.

    Must be called BEFORE any S3 write (DB-first protocol, spec Section 7.2).
    After the S3 write completes call ``confirm_preprocessed_artifact()`` or
    ``confirm_layout_artifact()`` as appropriate.

    Args:
        session:          SQLAlchemy session (caller owns commit/rollback).
        lineage_id:       UUID string (unique primary key).
        job_id:           Parent job identifier.
        page_number:      1-indexed page number.
        correlation_id:   Trace/correlation identifier for this processing run.
        input_image_uri:  Source image URI (never mutated after creation).
        otiff_uri:        Output TIFF URI for this page.
        material_type:    One of the four material type values.
        policy_version:   Policy ConfigMap version in effect at processing time.
        sub_page_index:   0 (left) or 1 (right) for split children; None for
                          unsplit pages.
        input_image_hash: Hash of the input image for deduplication.
        parent_page_id:   page_id of the split parent; None for top-level pages.
        split_source:     True when this record is for a split-child page.

    Returns:
        The newly created PageLineage ORM instance added to *session*
        (not yet committed).
    """
    record = PageLineage(
        lineage_id=lineage_id,
        job_id=job_id,
        page_number=page_number,
        sub_page_index=sub_page_index,
        correlation_id=correlation_id,
        input_image_uri=input_image_uri,
        input_image_hash=input_image_hash,
        otiff_uri=otiff_uri,
        material_type=material_type,
        policy_version=policy_version,
        parent_page_id=parent_page_id,
        split_source=split_source,
        preprocessed_artifact_state="pending",
        layout_artifact_state="pending",
    )
    session.add(record)
    return record


def confirm_preprocessed_artifact(session: Session, lineage_id: str) -> None:
    """
    Mark the preprocessed PTIFF artifact as successfully written to S3.

    Sets ``preprocessed_artifact_state = 'confirmed'``.
    Must be called immediately after the S3 write succeeds (spec Section 7.2).
    """
    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        {"preprocessed_artifact_state": "confirmed"},
        synchronize_session="fetch",
    )


def confirm_layout_artifact(session: Session, lineage_id: str) -> None:
    """
    Mark the layout JSON artifact as successfully written to S3.

    Sets ``layout_artifact_state = 'confirmed'``.
    """
    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        {"layout_artifact_state": "confirmed"},
        synchronize_session="fetch",
    )


def mark_artifact_recovery_failed(
    session: Session,
    lineage_id: str,
    artifact_type: ArtifactType,
) -> None:
    """
    Mark an artifact as permanently unrecoverable and increment the retry count.

    Sets ``<artifact_type>_artifact_state = 'recovery_failed'`` and
    atomically increments ``cleanup_retry_count`` by 1.

    Called by the recovery service after exhausting retries (spec Section 7.2:
    3+ retries, age > 3× grace period).

    Args:
        artifact_type: ``'preprocessed'`` or ``'layout'``.
    """
    column = f"{artifact_type}_artifact_state"
    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        {
            column: "recovery_failed",
            "cleanup_retry_count": PageLineage.cleanup_retry_count + 1,
        },
        synchronize_session="fetch",
    )


def update_geometry_result(
    session: Session,
    lineage_id: str,
    *,
    iep1a_used: bool,
    iep1b_used: bool,
    selected_geometry_model: str | None,
    structural_agreement: bool | None,
    iep1d_used: bool = False,
) -> None:
    """
    Record which geometry services were invoked and which model was selected.

    Called after the geometry cascade completes (spec Section 3, Step 3).

    Args:
        iep1a_used:              True if IEP1A was called.
        iep1b_used:              True if IEP1B was called.
        selected_geometry_model: 'iep1a' | 'iep1b' | None.
        structural_agreement:    True when both models agreed on the geometry;
                                 None when only one model was used.
        iep1d_used:              True if IEP1D rectification was triggered.
    """
    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        {
            "iep1a_used": iep1a_used,
            "iep1b_used": iep1b_used,
            "selected_geometry_model": selected_geometry_model,
            "structural_agreement": structural_agreement,
            "iep1d_used": iep1d_used,
        },
        synchronize_session="fetch",
    )


def update_lineage_completion(
    session: Session,
    lineage_id: str,
    *,
    acceptance_decision: str,
    acceptance_reason: str | None,
    routing_path: str | None,
    total_processing_ms: float | None,
    output_image_uri: str | None = None,
    gate_results: dict[str, Any] | None = None,
) -> None:
    """
    Record the final disposition of a page processing run.

    Called when the page reaches a leaf-final or worker-terminal state.
    Sets ``acceptance_decision``, ``acceptance_reason``, ``routing_path``,
    ``total_processing_ms``, and ``completed_at``.

    Args:
        acceptance_decision: 'accepted' | 'review' | 'failed' |
                             'pending_human_correction'.
        acceptance_reason:   Human-readable explanation of the decision.
        routing_path:        Final routing label (e.g. 'preprocessing_only').
        total_processing_ms: Wall-clock time from task start to this call.
        output_image_uri:    S3 URI of the output artifact (if produced).
        gate_results:        Consolidated gate decision summary (JSONB).
    """
    now = datetime.now(timezone.utc)

    updates: dict[str, Any] = {
        "acceptance_decision": acceptance_decision,
        "acceptance_reason": acceptance_reason,
        "routing_path": routing_path,
        "total_processing_ms": total_processing_ms,
        "completed_at": now,
    }

    if output_image_uri is not None:
        updates["output_image_uri"] = output_image_uri
    if gate_results is not None:
        updates["gate_results"] = gate_results

    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        updates, synchronize_session="fetch"  # type: ignore[arg-type]
    )


def record_human_correction(
    session: Session,
    lineage_id: str,
    *,
    correction_fields: dict[str, Any],
    reviewed_by: str | None = None,
    reviewer_notes: str | None = None,
) -> None:
    """
    Record that a human correction was applied to this page.

    Sets ``human_corrected = True``, ``human_correction_timestamp`` to now,
    and ``human_correction_fields`` to the provided dict.

    correction_fields keys (spec Section 13):
        crop_box     — [x_min, y_min, x_max, y_max] bounding box, or None
        deskew_angle — rotation in degrees, or None
        split_x      — horizontal split pixel coordinate, or None

    Args:
        correction_fields: Dict containing the human-provided corrections.
        reviewed_by:       Reviewer user_id (optional).
        reviewer_notes:    Free-text notes from the reviewer (optional).
    """
    now = datetime.now(timezone.utc)

    updates: dict[str, Any] = {
        "human_corrected": True,
        "human_correction_timestamp": now,
        "human_correction_fields": correction_fields,
    }

    if reviewed_by is not None:
        updates["reviewed_by"] = reviewed_by
    if reviewer_notes is not None:
        updates["reviewer_notes"] = reviewer_notes

    session.query(PageLineage).filter(PageLineage.lineage_id == lineage_id).update(
        updates, synchronize_session="fetch"  # type: ignore[arg-type]
    )


# ── Read helpers ───────────────────────────────────────────────────────────────


def get_lineage(session: Session, lineage_id: str) -> PageLineage | None:
    """
    Fetch a page_lineage row by its primary key.

    Returns:
        The PageLineage ORM instance, or None if not found.
    """
    return session.get(PageLineage, lineage_id)
