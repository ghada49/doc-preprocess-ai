"""
services/eep_worker/app/layout_completion.py
---------------------------------------------
Post-layout-detection bookkeeping for the EEP worker task runner.

Provides finalize_layout_page() — the single function the task runner must
call after transitioning a child page out of layout_detection. This ensures
the split parent is closed to 'split' once all its children are
worker-terminal (accepted / pending_human_correction / review / failed).

Usage in the task runner (when implemented):

    # After layout inference result is recorded and advance_page_state() has
    # transitioned the page to accepted / review / failed:
    page.status = new_terminal_state  # mirror into ORM
    finalize_layout_page(session, page)
    session.commit()

This module contains NO layout inference logic. It only handles the
post-inference DB bookkeeping that must follow every layout_detection
→ {accepted | review | failed} transition.

Exported:
    finalize_layout_page  — call after every layout_detection → terminal transition
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.eep.app.correction._split_parent import _maybe_close_split_parent
from services.eep.app.db.models import JobPage

logger = logging.getLogger(__name__)

__all__ = ["finalize_layout_page"]


def finalize_layout_page(session: Session, page: JobPage) -> None:
    """
    Post-transition bookkeeping for a page that has just left layout_detection.

    Must be called AFTER the page's state has been transitioned from
    layout_detection to a worker-terminal state (accepted / review / failed)
    and the new status has been reflected in ``page.status``.

    For split children (sub_page_index is not None), attempts to close the
    split parent to 'split' if all siblings are now worker-terminal.

    The caller is responsible for committing the session after this call.

    Args:
        session: Open SQLAlchemy session (caller owns the transaction).
        page:    The JobPage that just completed layout detection. Its
                 ``status`` field must already reflect the new terminal state.
    """
    if page.sub_page_index is None:
        # Not a split child; no parent to close.
        return

    closed = _maybe_close_split_parent(session, page.job_id, page.page_number)
    if closed:
        logger.info(
            "layout_completion: split parent closed: job=%s page=%d",
            page.job_id,
            page.page_number,
        )
    else:
        logger.debug(
            "layout_completion: split parent not yet closeable: job=%s page=%d sub=%d",
            page.job_id,
            page.page_number,
            page.sub_page_index,
        )
