"""
tests/test_p6_layout_completion.py
----------------------------------
Packet 6.x — Layout completion bookkeeping tests.

Covers:
  - finalize_layout_page() invokes split-parent finalization for split children
  - _maybe_close_split_parent() closes the parent to "split" when all children
    are worker-terminal after layout completion
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from services.eep.app.correction.ptiff_qa import _maybe_close_split_parent
from services.eep_worker.app.layout_completion import finalize_layout_page


def _make_page(
    page_id: str,
    *,
    job_id: str = "job-001",
    page_number: int = 3,
    sub_page_index: int | None,
    status: str,
) -> MagicMock:
    page = MagicMock()
    page.page_id = page_id
    page.job_id = job_id
    page.page_number = page_number
    page.sub_page_index = sub_page_index
    page.status = status
    return page


def _make_parent_close_session(parent: Any, children: list[Any]) -> MagicMock:
    session = MagicMock()
    call_index = {"value": 0}

    def query_se(*args: Any, **kwargs: Any) -> MagicMock:
        chain = MagicMock()
        chain.filter.return_value = chain

        idx = call_index["value"]
        call_index["value"] += 1

        if idx == 0:
            chain.first.return_value = parent
            chain.all.return_value = []
        else:
            chain.first.return_value = None
            chain.all.return_value = children
        return chain

    session.query.side_effect = query_se
    return session


class TestFinalizeLayoutPage:
    def test_finalize_layout_page_checks_parent_for_split_child(self) -> None:
        session = MagicMock()
        child = _make_page(
            "child-1",
            sub_page_index=1,
            status="accepted",
        )

        with patch(
            "services.eep_worker.app.layout_completion._maybe_close_split_parent",
            return_value=True,
        ) as mock_close:
            finalize_layout_page(session, child)

        mock_close.assert_called_once_with(session, "job-001", 3)

    def test_finalize_layout_page_skips_unsplit_page(self) -> None:
        session = MagicMock()
        page = _make_page(
            "parent-1",
            sub_page_index=None,
            status="accepted",
        )

        with patch(
            "services.eep_worker.app.layout_completion._maybe_close_split_parent"
        ) as mock_close:
            finalize_layout_page(session, page)

        mock_close.assert_not_called()


class TestMaybeCloseSplitParent:
    def test_parent_transitions_to_split_when_children_are_terminal(self) -> None:
        parent = _make_page(
            "parent-1",
            sub_page_index=None,
            status="pending_human_correction",
        )
        child_0 = _make_page(
            "child-0",
            sub_page_index=0,
            status="accepted",
        )
        child_1 = _make_page(
            "child-1",
            sub_page_index=1,
            status="review",
        )
        session = _make_parent_close_session(parent, [child_0, child_1])

        with patch(
            "services.eep.app.correction.ptiff_qa.advance_page_state",
            return_value=True,
        ) as mock_advance:
            closed = _maybe_close_split_parent(session, "job-001", 3)

        assert closed is True
        mock_advance.assert_called_once_with(
            session,
            "parent-1",
            from_state="pending_human_correction",
            to_state="split",
        )
