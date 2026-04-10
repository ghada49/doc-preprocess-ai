"""
tests/test_p4_db_page_state.py
--------------------------------
Packet 4.2 — page state transition helper tests.

Covers:
  - VALID_TRANSITIONS structure: all 9 states present (ptiff_qa_pending removed)
  - Terminal states map to empty frozensets
  - advance_page_state returns True on success (rows_affected=1)
  - advance_page_state returns False when CAS guard fails (rows_affected=0)
  - advance_page_state raises ValueError for invalid transitions
  - completed_at is included in updates when advancing to terminal state
  - completed_at is NOT included when advancing to non-terminal state
  - Optional fields (acceptance_decision, routing_path, quality_summary,
    output_image_uri, processing_time_ms) are included only when provided
  - All valid transitions from each state are accepted without error

Session is mocked — no live database required.
"""

from __future__ import annotations

import sys
import types
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from services.eep.app.db.models import JobPage
from services.eep.app.db.page_state import (
    TERMINAL_PAGE_STATES,
    VALID_TRANSITIONS,
    advance_page_state,
)
from shared.schemas.eep import TERMINAL_PAGE_STATES as _SHARED_TERMINAL

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> MagicMock:
    s = MagicMock()
    # Default: update() returns 1 (success)
    s.query.return_value.filter.return_value.update.return_value = 1
    return s


def _updates_passed(session: MagicMock) -> dict[str, Any]:
    """Extract the updates dict passed to the last .update() call."""
    return cast(
        dict[str, Any], session.query.return_value.filter.return_value.update.call_args[0][0]
    )


# ── TERMINAL_PAGE_STATES re-export ─────────────────────────────────────────────


class TestTerminalPageStates:
    def test_re_exports_from_shared(self) -> None:
        assert TERMINAL_PAGE_STATES is _SHARED_TERMINAL

    def test_accepted_is_terminal(self) -> None:
        assert "accepted" in TERMINAL_PAGE_STATES

    def test_review_is_terminal(self) -> None:
        assert "review" in TERMINAL_PAGE_STATES

    def test_failed_is_terminal(self) -> None:
        assert "failed" in TERMINAL_PAGE_STATES

    def test_pending_human_correction_is_terminal(self) -> None:
        assert "pending_human_correction" in TERMINAL_PAGE_STATES

    def test_split_is_terminal(self) -> None:
        assert "split" in TERMINAL_PAGE_STATES


# ── VALID_TRANSITIONS structure ────────────────────────────────────────────────


class TestValidTransitions:
    def test_all_9_states_present_as_source(self) -> None:
        expected = {
            "queued",
            "preprocessing",
            "rectification",
            "layout_detection",
            "pending_human_correction",
            "accepted",
            "review",
            "failed",
            "split",
        }
        assert set(VALID_TRANSITIONS.keys()) == expected

    def test_leaf_final_states_have_empty_targets(self) -> None:
        # accepted, review, failed, split are permanently terminal — no transitions.
        # pending_human_correction IS in TERMINAL_PAGE_STATES (worker-terminal)
        # but can re-enter the pipeline after human correction, so it retains
        # valid transitions (spec Section 1.6).
        permanently_terminal = {"accepted", "review", "failed", "split"}
        for state in permanently_terminal:
            assert (
                VALID_TRANSITIONS[state] == frozenset()
            ), f"Leaf-final state {state!r} must have no valid transitions"

    def test_pending_human_correction_has_transitions_despite_being_terminal(self) -> None:
        """Worker-terminal but not leaf-final — human correction re-queues it."""
        assert "pending_human_correction" in TERMINAL_PAGE_STATES
        assert len(VALID_TRANSITIONS["pending_human_correction"]) > 0

    def test_queued_targets(self) -> None:
        assert VALID_TRANSITIONS["queued"] == frozenset({"preprocessing", "failed"})

    def test_preprocessing_targets(self) -> None:
        assert VALID_TRANSITIONS["preprocessing"] == frozenset(
            {
                "rectification",
                "layout_detection",
                "accepted",
                "pending_human_correction",
                "split",
                "failed",
            }
        )

    def test_rectification_targets(self) -> None:
        assert VALID_TRANSITIONS["rectification"] == frozenset(
            {
                "layout_detection",
                "accepted",
                "pending_human_correction",
                "split",
                "failed",
            }
        )

    def test_pending_human_correction_targets(self) -> None:
        # Automation-first: correction → layout_detection (layout) or accepted (preprocess).
        # review = correction-reject. split = human split.
        assert VALID_TRANSITIONS["pending_human_correction"] == frozenset(
            {"layout_detection", "accepted", "review", "split"}
        )

    def test_layout_detection_targets(self) -> None:
        assert VALID_TRANSITIONS["layout_detection"] == frozenset(
            {"accepted", "review", "failed", "pending_human_correction"}
        )

    def test_all_values_are_frozensets(self) -> None:
        for state, targets in VALID_TRANSITIONS.items():
            assert isinstance(
                targets, frozenset
            ), f"VALID_TRANSITIONS[{state!r}] must be a frozenset"


# ── advance_page_state — return value ─────────────────────────────────────────


class TestAdvancePageStateReturnValue:
    def test_returns_true_when_update_affects_one_row(self, session: MagicMock) -> None:
        session.query.return_value.filter.return_value.update.return_value = 1
        result = advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert result is True

    def test_returns_false_when_update_affects_zero_rows(self, session: MagicMock) -> None:
        session.query.return_value.filter.return_value.update.return_value = 0
        result = advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert result is False

    def test_returns_false_for_concurrent_update(self, session: MagicMock) -> None:
        """CAS guard: row was already advanced by another worker."""
        session.query.return_value.filter.return_value.update.return_value = 0
        result = advance_page_state(session, "pg-1", "preprocessing", "layout_detection")
        assert result is False


class TestAdvancePageStateLayoutCompletionHook:
    @staticmethod
    def _page(*, sub_page_index: int | None = 0) -> JobPage:
        return JobPage(
            page_id="pg-1",
            job_id="job-1",
            page_number=5,
            sub_page_index=sub_page_index,
            status="accepted",
            input_image_uri="s3://bucket/input.tiff",
        )

    @pytest.mark.parametrize("to_state", ["accepted", "review", "failed"])
    def test_calls_finalize_after_successful_layout_terminal_transition(
        self,
        session: MagicMock,
        to_state: str,
    ) -> None:
        page = self._page()
        session.get.return_value = page
        fake_module = types.ModuleType("services.eep_worker.app.layout_completion")
        mock_finalize = MagicMock()
        setattr(fake_module, "finalize_layout_page", mock_finalize)

        with patch.dict(sys.modules, {"services.eep_worker.app.layout_completion": fake_module}):
            result = advance_page_state(session, "pg-1", "layout_detection", to_state)

        assert result is True
        session.get.assert_called_once_with(JobPage, "pg-1")
        mock_finalize.assert_called_once_with(session, page)

    def test_skips_finalize_when_layout_transition_cas_misses(self, session: MagicMock) -> None:
        session.query.return_value.filter.return_value.update.return_value = 0
        session.get.return_value = self._page()
        fake_module = types.ModuleType("services.eep_worker.app.layout_completion")
        mock_finalize = MagicMock()
        setattr(fake_module, "finalize_layout_page", mock_finalize)

        with patch.dict(sys.modules, {"services.eep_worker.app.layout_completion": fake_module}):
            result = advance_page_state(session, "pg-1", "layout_detection", "accepted")

        assert result is False
        session.get.assert_not_called()
        mock_finalize.assert_not_called()

    def test_skips_finalize_for_non_layout_transition(self, session: MagicMock) -> None:
        session.get.return_value = self._page()
        fake_module = types.ModuleType("services.eep_worker.app.layout_completion")
        mock_finalize = MagicMock()
        setattr(fake_module, "finalize_layout_page", mock_finalize)

        with patch.dict(sys.modules, {"services.eep_worker.app.layout_completion": fake_module}):
            advance_page_state(session, "pg-1", "queued", "preprocessing")

        session.get.assert_not_called()
        mock_finalize.assert_not_called()


# ── advance_page_state — invalid transitions ───────────────────────────────────


class TestAdvancePageStateInvalidTransitions:
    def test_raises_value_error_for_invalid_transition(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="Invalid state transition"):
            advance_page_state(session, "pg-1", "queued", "accepted")

    def test_raises_value_error_terminal_to_any(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            advance_page_state(session, "pg-1", "accepted", "queued")

    def test_raises_value_error_terminal_to_same(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            advance_page_state(session, "pg-1", "failed", "failed")

    def test_raises_value_error_unknown_from_state(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            advance_page_state(session, "pg-1", "nonexistent", "preprocessing")

    def test_error_message_includes_states(self, session: MagicMock) -> None:
        with pytest.raises(ValueError, match="queued.*accepted"):
            advance_page_state(session, "pg-1", "queued", "accepted")

    def test_no_db_call_on_invalid_transition(self, session: MagicMock) -> None:
        with pytest.raises(ValueError):
            advance_page_state(session, "pg-1", "queued", "review")
        session.query.assert_not_called()


# ── advance_page_state — updates dict contents ────────────────────────────────


class TestAdvancePageStateUpdateContents:
    def test_status_always_set_to_to_state(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert _updates_passed(session)["status"] == "preprocessing"

    def test_status_updated_at_always_set(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert "status_updated_at" in _updates_passed(session)

    def test_completed_at_set_for_terminal_state(self, session: MagicMock) -> None:
        # accepted is terminal
        advance_page_state(session, "pg-1", "layout_detection", "accepted")
        assert "completed_at" in _updates_passed(session)

    def test_completed_at_not_set_for_non_terminal(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert "completed_at" not in _updates_passed(session)

    def test_completed_at_set_for_failed(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "preprocessing", "failed")
        assert "completed_at" in _updates_passed(session)

    def test_acceptance_decision_included_when_provided(self, session: MagicMock) -> None:
        advance_page_state(
            session,
            "pg-1",
            "layout_detection",
            "accepted",
            acceptance_decision="accepted",
        )
        assert _updates_passed(session)["acceptance_decision"] == "accepted"

    def test_acceptance_decision_excluded_when_none(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert "acceptance_decision" not in _updates_passed(session)

    def test_routing_path_included_when_provided(self, session: MagicMock) -> None:
        advance_page_state(
            session,
            "pg-1",
            "queued",
            "preprocessing",
            routing_path="standard",
        )
        assert _updates_passed(session)["routing_path"] == "standard"

    def test_routing_path_excluded_when_none(self, session: MagicMock) -> None:
        advance_page_state(session, "pg-1", "queued", "preprocessing")
        assert "routing_path" not in _updates_passed(session)

    def test_quality_summary_included_when_provided(self, session: MagicMock) -> None:
        qs = {"score": 0.95}
        advance_page_state(session, "pg-1", "queued", "preprocessing", quality_summary=qs)
        assert _updates_passed(session)["quality_summary"] == qs

    def test_output_image_uri_included_when_provided(self, session: MagicMock) -> None:
        advance_page_state(
            session,
            "pg-1",
            "queued",
            "preprocessing",
            output_image_uri="s3://bucket/out.ptiff",
        )
        assert _updates_passed(session)["output_image_uri"] == "s3://bucket/out.ptiff"

    def test_processing_time_ms_included_when_provided(self, session: MagicMock) -> None:
        advance_page_state(
            session,
            "pg-1",
            "queued",
            "preprocessing",
            processing_time_ms=123.4,
        )
        assert _updates_passed(session)["processing_time_ms"] == 123.4


# ── advance_page_state — all valid transitions accepted ───────────────────────


class TestAllValidTransitionsAccepted:
    """Smoke test: every entry in VALID_TRANSITIONS invokes without ValueError."""

    @pytest.mark.parametrize(
        "from_state,to_state",
        [(src, tgt) for src, targets in VALID_TRANSITIONS.items() for tgt in targets],
    )
    def test_valid_transition_does_not_raise(
        self,
        session: MagicMock,
        from_state: str,
        to_state: str,
    ) -> None:
        advance_page_state(session, "pg-1", from_state, to_state)
