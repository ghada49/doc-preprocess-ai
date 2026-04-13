"""
services/eep/app/correction/_split_parent.py
---------------------------------------------
Pure SQLAlchemy helpers for closing a split parent page once all its
children reach a worker-terminal state.

Extracted from ptiff_qa.py so that callers that do not need the FastAPI
router (EEP worker, unit tests) can import without pulling in auth.py
and its OAuth2PasswordRequestForm route, which requires python-multipart
at import time.

Exported:
    _WORKER_TERMINAL_STATES             — frozenset of worker-terminal status strings
    _close_parent_if_children_terminal  — core transition logic (pre-loaded children)
    _maybe_close_split_parent           — DB-querying entry point
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.eep.app.db.models import JobPage
from services.eep.app.db.page_state import advance_page_state

logger = logging.getLogger(__name__)

# Worker-terminal states: states in which a split child is considered "done"
# for the purpose of closing the split parent (spec Section 8.6).
_WORKER_TERMINAL_STATES: frozenset[str] = frozenset(
    {"accepted", "pending_human_correction", "review", "failed"}
)


def _close_parent_if_children_terminal(
    db: Session,
    parent: JobPage,
    children: list[JobPage],
) -> bool:
    """
    Core logic: transition parent to 'split' if all children are worker-terminal.

    Uses pre-loaded JobPage objects; performs no DB queries. This function is
    called from _apply_split_correction (apply.py) where children are already
    in memory with their current statuses.

    Args:
        db:       SQLAlchemy session (caller owns transaction and commit).
        parent:   The parent JobPage (must be in pending_human_correction).
        children: Pre-loaded child JobPage objects with current statuses.

    Returns:
        True if the parent was transitioned to 'split', False otherwise.
    """
    if not children:
        return False
    if not all(c.status in _WORKER_TERMINAL_STATES for c in children):
        return False

    advanced = advance_page_state(
        db,
        parent.page_id,
        from_state="pending_human_correction",
        to_state="split",
    )
    if advanced:
        logger.info(
            "Split parent closed: job=%s page=%d → split",
            parent.job_id,
            parent.page_number,
        )
    else:
        logger.warning(
            "Split parent close CAS miss: job=%s page_id=%s page=%d",
            parent.job_id,
            parent.page_id,
            parent.page_number,
        )
    return bool(advanced)


def _maybe_close_split_parent(
    db: Session,
    job_id: str,
    page_number: int,
) -> bool:
    """
    Query DB for parent and children, close parent if all children are worker-terminal.

    Used from PTIFF QA approve endpoints after gate release, where pre-loaded
    child objects may not be available.  The parent must be in
    pending_human_correction; if it is not (already closed or never existed),
    this function is a no-op.

    Relies on the session identity map reflecting current statuses — callers
    must ensure that any preceding advance_page_state calls have been mirrored
    onto the in-memory ORM objects (page.status = new_state) before calling
    this function, so that identity-map lookups return accurate states.

    Args:
        db:          SQLAlchemy session (caller owns transaction and commit).
        job_id:      Job identifier.
        page_number: Page number of the split parent.

    Returns:
        True if the parent was transitioned to 'split', False otherwise.
    """
    parent: JobPage | None = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.sub_page_index == None,  # noqa: E711
            JobPage.status == "pending_human_correction",
        )
        .first()
    )
    if parent is None:
        return False

    children: list[JobPage] = (
        db.query(JobPage)
        .filter(
            JobPage.job_id == job_id,
            JobPage.page_number == page_number,
            JobPage.sub_page_index.isnot(None),
        )
        .all()
    )

    return _close_parent_if_children_terminal(db, parent, children)
