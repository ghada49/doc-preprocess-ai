"""
services/eep/app/db/page_state.py
-----------------------------------
Page state transition helpers for the EEP processing pipeline.

Implements the compare-and-swap (CAS) state transition pattern for job_pages
rows, enforcing the state machine from spec Section 1.6.

State machine (valid transitions):
  queued               → preprocessing
  preprocessing        → rectification | ptiff_qa_pending |
                         pending_human_correction | split | failed
  rectification        → ptiff_qa_pending | pending_human_correction | failed
  ptiff_qa_pending     → accepted | layout_detection | pending_human_correction
  pending_human_correction → ptiff_qa_pending | layout_detection |
                             accepted | review
  layout_detection     → accepted | review | failed
  split, accepted, review, failed → (terminal — no further transitions)

ptiff_qa_pending is NOT in TERMINAL_PAGE_STATES (spec Section 9.1 / 12.1).

TERMINAL_PAGE_STATES is re-exported from shared.schemas.eep — the canonical
definition.  No other module may redefine it inline (spec Section 12.1).

Exported:
    VALID_TRANSITIONS     — dict[str, frozenset[str]] of allowed transitions
    TERMINAL_PAGE_STATES  — re-exported from shared.schemas.eep
    advance_page_state    — CAS UPDATE on job_pages; returns bool
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from services.eep.app.db.models import JobPage
from shared.schemas.eep import TERMINAL_PAGE_STATES  # noqa: F401 — re-exported

__all__ = [
    "VALID_TRANSITIONS",
    "TERMINAL_PAGE_STATES",
    "advance_page_state",
]

# ── State machine ──────────────────────────────────────────────────────────────

# Valid target states for each source state (spec Section 1.6).
# Terminal states map to empty frozensets — no further transitions allowed.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"preprocessing"}),
    "preprocessing": frozenset(
        {
            "rectification",
            "ptiff_qa_pending",
            "pending_human_correction",
            "split",
            "failed",
        }
    ),
    "rectification": frozenset(
        {
            "ptiff_qa_pending",
            "pending_human_correction",
            "failed",
        }
    ),
    "ptiff_qa_pending": frozenset(
        {
            "accepted",
            "layout_detection",
            "pending_human_correction",
        }
    ),
    "pending_human_correction": frozenset(
        {
            "ptiff_qa_pending",
            "layout_detection",
            "accepted",
            "review",
        }
    ),
    "layout_detection": frozenset({"accepted", "review", "failed"}),
    # Terminal states — no further transitions.
    "split": frozenset(),
    "accepted": frozenset(),
    "review": frozenset(),
    "failed": frozenset(),
}


# ── Core API ───────────────────────────────────────────────────────────────────


def advance_page_state(
    session: Session,
    page_id: str,
    from_state: str,
    to_state: str,
    *,
    acceptance_decision: str | None = None,
    routing_path: str | None = None,
    quality_summary: dict[str, Any] | None = None,
    output_image_uri: str | None = None,
    processing_time_ms: float | None = None,
) -> bool:
    """
    Advance a job_pages row from *from_state* to *to_state* atomically.

    Uses a compare-and-swap pattern: ``UPDATE … WHERE status = from_state``
    ensures that concurrent workers cannot double-advance the same page.

    Args:
        session:             SQLAlchemy session (caller owns commit/rollback).
        page_id:             Primary key of the job_pages row.
        from_state:          Expected current state (CAS guard).
        to_state:            Target state after this transition.
        acceptance_decision: 'accepted' | 'review' | 'failed' — set when page
                             reaches a leaf-final state.
        routing_path:        Human-readable routing label for audit.
        quality_summary:     Quality metric dict written to JSONB column.
        output_image_uri:    S3 URI of the processed artifact.
        processing_time_ms:  Total processing time for the page.

    Returns:
        True  if the UPDATE affected exactly one row (transition succeeded).
        False if the row does not exist, is not in from_state (concurrent
              update already advanced it), or no matching row is found.

    Raises:
        ValueError if (from_state, to_state) is not in VALID_TRANSITIONS —
        catches programming errors at call time.

    Side-effects:
        Sets ``status_updated_at`` to current UTC time on every successful
        advance.  Sets ``completed_at`` when ``to_state`` is in
        TERMINAL_PAGE_STATES.
    """
    if to_state not in VALID_TRANSITIONS.get(from_state, frozenset()):
        raise ValueError(
            f"Invalid state transition: {from_state!r} → {to_state!r}. "
            f"Valid targets from {from_state!r}: "
            f"{sorted(VALID_TRANSITIONS.get(from_state, frozenset()))}"
        )

    now = datetime.now(tz=UTC)

    updates: dict[str, Any] = {
        "status": to_state,
        "status_updated_at": now,
    }

    if acceptance_decision is not None:
        updates["acceptance_decision"] = acceptance_decision
    if routing_path is not None:
        updates["routing_path"] = routing_path
    if quality_summary is not None:
        updates["quality_summary"] = quality_summary
    if output_image_uri is not None:
        updates["output_image_uri"] = output_image_uri
    if processing_time_ms is not None:
        updates["processing_time_ms"] = processing_time_ms

    if to_state in TERMINAL_PAGE_STATES:
        updates["completed_at"] = now

    rows_affected: int = (
        session.query(JobPage)
        .filter(JobPage.page_id == page_id, JobPage.status == from_state)
        .update(updates, synchronize_session="fetch")  # type: ignore[arg-type]
    )
    return rows_affected > 0
