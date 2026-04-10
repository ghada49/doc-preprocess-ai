"""
services/eep/app/db/page_state.py
-----------------------------------
Page state transition helpers for the EEP processing pipeline.

Implements the compare-and-swap (CAS) state transition pattern for job_pages
rows, enforcing the state machine from spec Section 1.6 and Section 9.

The authoritative transition map lives in shared/state_machine.py.
VALID_TRANSITIONS here is a direct alias of ALLOWED_TRANSITIONS from that
module — no local copy is maintained.  All validation is delegated to
validate_transition() from shared.state_machine so the two cannot diverge.

State machine (valid transitions):
  queued               → preprocessing | failed
  preprocessing        → rectification | layout_detection | accepted |
                         pending_human_correction | split | failed
  rectification        → layout_detection | accepted |
                         pending_human_correction | split | failed
  layout_detection     → accepted | review | failed | pending_human_correction
  pending_human_correction → layout_detection | accepted | review | split
  split, accepted, review, failed → (terminal — no further transitions)

TERMINAL_PAGE_STATES is re-exported from shared.schemas.eep — the canonical
definition.  No other module may redefine it inline (spec Section 12.1).

Exported:
    VALID_TRANSITIONS     — alias of ALLOWED_TRANSITIONS from shared.state_machine
    TERMINAL_PAGE_STATES  — re-exported from shared.schemas.eep
    advance_page_state    — CAS UPDATE on job_pages; returns bool
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from services.eep.app.db.models import JobPage
from shared.schemas.eep import TERMINAL_PAGE_STATES  # noqa: F401 — re-exported
from shared.state_machine import ALLOWED_TRANSITIONS, InvalidTransitionError, validate_transition

__all__ = [
    "VALID_TRANSITIONS",
    "TERMINAL_PAGE_STATES",
    "advance_page_state",
]

# ── State machine ──────────────────────────────────────────────────────────────

# VALID_TRANSITIONS is the authoritative transition map.
# It is an alias of ALLOWED_TRANSITIONS from shared.state_machine — do NOT
# redefine it here.  Any change to the state machine must be made in
# shared/state_machine.py and will be reflected here automatically.
VALID_TRANSITIONS: dict[str, frozenset[str]] = ALLOWED_TRANSITIONS
_LAYOUT_TERMINAL_STATES: frozenset[str] = frozenset({"accepted", "review", "failed"})


def _finalize_layout_transition(
    session: Session,
    page_id: str,
    from_state: str,
    to_state: str,
) -> None:
    """
    Run post-layout bookkeeping after a successful layout terminal transition.

    This is intentionally local to the centralized CAS transition helper so the
    runtime hook fires in the same DB session/transaction without requiring the
    still-unimplemented task runner to duplicate the call at multiple sites.
    """
    if from_state != "layout_detection" or to_state not in _LAYOUT_TERMINAL_STATES:
        return

    page = session.get(JobPage, page_id)
    if not isinstance(page, JobPage):
        return

    # Local import avoids a module import cycle:
    # page_state -> layout_completion -> ptiff_qa -> page_state.
    from services.eep_worker.app.layout_completion import finalize_layout_page

    finalize_layout_page(session, page)


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

    Transition validation is delegated to validate_transition() from
    shared.state_machine (the authoritative source).  A ValueError is raised
    for any invalid (from_state, to_state) pair so that programming errors
    are caught at call time, not silently written to the DB.

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
    try:
        validate_transition(from_state, to_state)
    except (ValueError, InvalidTransitionError) as exc:
        raise ValueError(
            f"Invalid state transition: {from_state!r} → {to_state!r}. "
            f"Valid targets from {from_state!r}: "
            f"{sorted(VALID_TRANSITIONS.get(from_state, frozenset()))}"
        ) from exc

    now = datetime.now(timezone.utc)

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
    if rows_affected > 0:
        _finalize_layout_transition(session, page_id, from_state, to_state)
    return rows_affected > 0
