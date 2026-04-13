"""
tests/test_p1_state_machine.py
-------------------------------
Packet 1.3a validator tests for shared.state_machine:
  - ALLOWED_TRANSITIONS coverage (all PageStates as keys)
  - validate_transition: every valid pair passes
  - validate_transition: every invalid pair raises InvalidTransitionError
  - validate_transition: unknown state raises ValueError
  - Leaf-final states have no outgoing transitions
  - is_worker_terminal delegates to TERMINAL_PAGE_STATES
  - ptiff_qa_pending is NOT worker-terminal
  - is_leaf_final covers accepted / review / failed only
  - allowed_next returns correct frozensets

Definition of done:
  - all page transitions are centrally validated
  - ptiff_qa_pending transitions are explicitly defined
  - terminal-state automation stop rules are enforced in one shared module
"""

import pytest

from shared.schemas.eep import TERMINAL_PAGE_STATES
from shared.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    allowed_next,
    is_leaf_final,
    is_worker_terminal,
    validate_transition,
)

# ── Constants ──────────────────────────────────────────────────────────────────

ALL_PAGE_STATES = frozenset(
    {
        "queued",
        "preprocessing",
        "rectification",
        "ptiff_qa_pending",
        "layout_detection",
        "pending_human_correction",
        "accepted",
        "review",
        "failed",
        "split",
    }
)

# All valid (from, to) pairs derived from spec Section 8 + Section 9
VALID_TRANSITIONS = [
    ("queued", "preprocessing"),
    ("queued", "failed"),
    ("preprocessing", "rectification"),
    ("preprocessing", "ptiff_qa_pending"),
    ("preprocessing", "pending_human_correction"),
    ("preprocessing", "split"),
    ("preprocessing", "failed"),
    ("rectification", "ptiff_qa_pending"),
    ("rectification", "pending_human_correction"),
    ("rectification", "split"),
    ("rectification", "failed"),
    ("ptiff_qa_pending", "accepted"),
    ("ptiff_qa_pending", "layout_detection"),
    ("ptiff_qa_pending", "pending_human_correction"),
    ("layout_detection", "accepted"),
    ("layout_detection", "review"),
    ("layout_detection", "failed"),
    ("pending_human_correction", "ptiff_qa_pending"),
    ("pending_human_correction", "review"),
    ("pending_human_correction", "split"),
]

# A representative sample of explicitly prohibited transitions
INVALID_TRANSITIONS = [
    # Backwards / skipping transitions
    ("preprocessing", "queued"),
    ("rectification", "queued"),
    ("rectification", "preprocessing"),
    ("ptiff_qa_pending", "queued"),
    ("ptiff_qa_pending", "preprocessing"),
    ("ptiff_qa_pending", "rectification"),
    ("ptiff_qa_pending", "failed"),
    ("layout_detection", "queued"),
    ("layout_detection", "preprocessing"),
    ("layout_detection", "rectification"),
    ("layout_detection", "ptiff_qa_pending"),
    ("layout_detection", "pending_human_correction"),
    ("layout_detection", "split"),
    # Transitions out of leaf-final states
    ("accepted", "queued"),
    ("accepted", "preprocessing"),
    ("accepted", "ptiff_qa_pending"),
    ("accepted", "pending_human_correction"),
    ("accepted", "review"),
    ("accepted", "failed"),
    ("accepted", "split"),
    ("review", "queued"),
    ("review", "accepted"),
    ("review", "pending_human_correction"),
    ("failed", "queued"),
    ("failed", "preprocessing"),
    ("failed", "pending_human_correction"),
    # Transitions out of routing-terminal state
    ("split", "queued"),
    ("split", "accepted"),
    ("split", "review"),
    # No direct queued → ptiff_qa_pending / layout_detection / etc.
    ("queued", "ptiff_qa_pending"),
    ("queued", "layout_detection"),
    ("queued", "pending_human_correction"),
    ("queued", "review"),
    ("queued", "split"),
    # No direct preprocessing → review / accepted / layout_detection
    ("preprocessing", "review"),
    ("preprocessing", "accepted"),
    ("preprocessing", "layout_detection"),
    # No pending_human_correction → accepted / layout_detection / failed
    ("pending_human_correction", "accepted"),
    ("pending_human_correction", "layout_detection"),
    ("pending_human_correction", "failed"),
    # Identical state self-transition
    ("queued", "queued"),
    ("preprocessing", "preprocessing"),
    ("ptiff_qa_pending", "ptiff_qa_pending"),
]


# ── ALLOWED_TRANSITIONS structure ──────────────────────────────────────────────


class TestAllowedTransitionsStructure:
    def test_all_page_states_are_keys(self) -> None:
        assert set(ALLOWED_TRANSITIONS.keys()) == ALL_PAGE_STATES

    def test_values_are_frozensets(self) -> None:
        for state, nexts in ALLOWED_TRANSITIONS.items():
            assert isinstance(nexts, frozenset), f"{state}: expected frozenset"

    def test_all_destination_states_are_known(self) -> None:
        for state, nexts in ALLOWED_TRANSITIONS.items():
            for dest in nexts:
                assert dest in ALL_PAGE_STATES, f"Unknown destination '{dest}' from '{state}'"

    def test_leaf_final_states_have_no_transitions(self) -> None:
        for state in ("accepted", "review", "failed"):
            assert (
                ALLOWED_TRANSITIONS[state] == frozenset()
            ), f"Leaf-final state '{state}' must have no outgoing transitions"

    def test_split_has_no_transitions(self) -> None:
        assert ALLOWED_TRANSITIONS["split"] == frozenset()

    def test_queued_has_exactly_two_transitions(self) -> None:
        assert ALLOWED_TRANSITIONS["queued"] == frozenset({"preprocessing", "failed"})

    def test_ptiff_qa_pending_transitions_are_defined(self) -> None:
        # Spec DoD: ptiff_qa_pending transitions explicitly defined
        expected = frozenset({"accepted", "layout_detection", "pending_human_correction"})
        assert ALLOWED_TRANSITIONS["ptiff_qa_pending"] == expected

    def test_pending_human_correction_transitions(self) -> None:
        expected = frozenset({"ptiff_qa_pending", "review", "split"})
        assert ALLOWED_TRANSITIONS["pending_human_correction"] == expected

    def test_layout_detection_transitions(self) -> None:
        expected = frozenset({"accepted", "review", "failed"})
        assert ALLOWED_TRANSITIONS["layout_detection"] == expected


# ── validate_transition — valid pairs ─────────────────────────────────────────


class TestValidTransitions:
    @pytest.mark.parametrize("from_state,to_state", VALID_TRANSITIONS)
    def test_valid_pair_does_not_raise(self, from_state: str, to_state: str) -> None:
        validate_transition(from_state, to_state)  # must not raise

    def test_preprocessing_to_ptiff_qa_pending(self) -> None:
        validate_transition("preprocessing", "ptiff_qa_pending")

    def test_rectification_to_ptiff_qa_pending(self) -> None:
        validate_transition("rectification", "ptiff_qa_pending")

    def test_ptiff_qa_pending_to_accepted(self) -> None:
        validate_transition("ptiff_qa_pending", "accepted")

    def test_ptiff_qa_pending_to_layout_detection(self) -> None:
        validate_transition("ptiff_qa_pending", "layout_detection")

    def test_ptiff_qa_pending_to_pending_human_correction(self) -> None:
        # Reviewer sends page back from PTIFF QA to correction
        validate_transition("ptiff_qa_pending", "pending_human_correction")

    def test_pending_human_correction_to_ptiff_qa_pending(self) -> None:
        # Correction submitted
        validate_transition("pending_human_correction", "ptiff_qa_pending")

    def test_pending_human_correction_to_review(self) -> None:
        # Correction rejected
        validate_transition("pending_human_correction", "review")

    def test_pending_human_correction_to_split(self) -> None:
        # Human-submitted split: parent transitions after children reach terminal
        validate_transition("pending_human_correction", "split")

    def test_preprocessing_to_split(self) -> None:
        # Automated split: parent created children and enqueued them
        validate_transition("preprocessing", "split")

    def test_rectification_to_split(self) -> None:
        # Rectification path completed, parent was a spread
        validate_transition("rectification", "split")


# ── validate_transition — invalid pairs ───────────────────────────────────────


class TestInvalidTransitions:
    @pytest.mark.parametrize("from_state,to_state", INVALID_TRANSITIONS)
    def test_invalid_pair_raises(self, from_state: str, to_state: str) -> None:
        with pytest.raises(InvalidTransitionError):
            validate_transition(from_state, to_state)

    def test_accepted_to_any_state_raises(self) -> None:
        for state in ALL_PAGE_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("accepted", state)

    def test_review_to_any_state_raises(self) -> None:
        for state in ALL_PAGE_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("review", state)

    def test_failed_to_any_state_raises(self) -> None:
        for state in ALL_PAGE_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("failed", state)

    def test_split_to_any_state_raises(self) -> None:
        for state in ALL_PAGE_STATES:
            with pytest.raises(InvalidTransitionError):
                validate_transition("split", state)

    def test_error_message_includes_states(self) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition("queued", "layout_detection")
        msg = str(exc_info.value)
        assert "queued" in msg
        assert "layout_detection" in msg


# ── validate_transition — unknown states ──────────────────────────────────────


class TestUnknownStates:
    def test_unknown_from_state_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown page state"):
            validate_transition("not_a_state", "queued")

    def test_unknown_to_state_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown page state"):
            validate_transition("queued", "not_a_state")

    def test_both_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown page state"):
            validate_transition("ghost", "phantom")


# ── is_worker_terminal ────────────────────────────────────────────────────────


class TestIsWorkerTerminal:
    def test_accepted_is_terminal(self) -> None:
        assert is_worker_terminal("accepted") is True

    def test_pending_human_correction_is_terminal(self) -> None:
        assert is_worker_terminal("pending_human_correction") is True

    def test_review_is_terminal(self) -> None:
        assert is_worker_terminal("review") is True

    def test_failed_is_terminal(self) -> None:
        assert is_worker_terminal("failed") is True

    def test_split_is_terminal(self) -> None:
        assert is_worker_terminal("split") is True

    def test_ptiff_qa_pending_is_not_terminal(self) -> None:
        # Critical spec invariant (spec Section 9.1 + Section 3.1)
        assert is_worker_terminal("ptiff_qa_pending") is False

    def test_queued_is_not_terminal(self) -> None:
        assert is_worker_terminal("queued") is False

    def test_preprocessing_is_not_terminal(self) -> None:
        assert is_worker_terminal("preprocessing") is False

    def test_rectification_is_not_terminal(self) -> None:
        assert is_worker_terminal("rectification") is False

    def test_layout_detection_is_not_terminal(self) -> None:
        assert is_worker_terminal("layout_detection") is False

    def test_delegates_to_terminal_page_states(self) -> None:
        # Must use the same set defined in shared.schemas.eep
        for state in ALL_PAGE_STATES:
            assert is_worker_terminal(state) == (state in TERMINAL_PAGE_STATES)

    def test_all_terminal_states_covered(self) -> None:
        terminal_count = sum(1 for s in ALL_PAGE_STATES if is_worker_terminal(s))
        assert terminal_count == 5  # accepted, pending_human_correction, review, failed, split

    def test_non_terminal_count(self) -> None:
        non_terminal = [s for s in ALL_PAGE_STATES if not is_worker_terminal(s)]
        assert set(non_terminal) == {
            "queued",
            "preprocessing",
            "rectification",
            "ptiff_qa_pending",
            "layout_detection",
        }


# ── is_leaf_final ─────────────────────────────────────────────────────────────


class TestIsLeafFinal:
    def test_accepted_is_leaf_final(self) -> None:
        assert is_leaf_final("accepted") is True

    def test_review_is_leaf_final(self) -> None:
        assert is_leaf_final("review") is True

    def test_failed_is_leaf_final(self) -> None:
        assert is_leaf_final("failed") is True

    def test_pending_human_correction_is_not_leaf_final(self) -> None:
        # Worker-terminal but human can resume it
        assert is_leaf_final("pending_human_correction") is False

    def test_split_is_not_leaf_final(self) -> None:
        # Routing-terminal for parent, not a page outcome
        assert is_leaf_final("split") is False

    def test_ptiff_qa_pending_is_not_leaf_final(self) -> None:
        assert is_leaf_final("ptiff_qa_pending") is False

    def test_queued_is_not_leaf_final(self) -> None:
        assert is_leaf_final("queued") is False

    def test_preprocessing_is_not_leaf_final(self) -> None:
        assert is_leaf_final("preprocessing") is False

    def test_rectification_is_not_leaf_final(self) -> None:
        assert is_leaf_final("rectification") is False

    def test_layout_detection_is_not_leaf_final(self) -> None:
        assert is_leaf_final("layout_detection") is False

    def test_exactly_three_leaf_final_states(self) -> None:
        leaf_final = [s for s in ALL_PAGE_STATES if is_leaf_final(s)]
        assert sorted(leaf_final) == ["accepted", "failed", "review"]


# ── allowed_next ──────────────────────────────────────────────────────────────


class TestAllowedNext:
    def test_queued_allowed_next(self) -> None:
        assert allowed_next("queued") == frozenset({"preprocessing", "failed"})

    def test_ptiff_qa_pending_allowed_next(self) -> None:
        result = allowed_next("ptiff_qa_pending")
        assert result == frozenset({"accepted", "layout_detection", "pending_human_correction"})

    def test_accepted_allowed_next_is_empty(self) -> None:
        assert allowed_next("accepted") == frozenset()

    def test_review_allowed_next_is_empty(self) -> None:
        assert allowed_next("review") == frozenset()

    def test_failed_allowed_next_is_empty(self) -> None:
        assert allowed_next("failed") == frozenset()

    def test_split_allowed_next_is_empty(self) -> None:
        assert allowed_next("split") == frozenset()

    def test_returns_frozenset(self) -> None:
        result = allowed_next("preprocessing")
        assert isinstance(result, frozenset)

    def test_unknown_state_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown page state"):
            allowed_next("ghost_state")

    def test_all_states_queryable(self) -> None:
        for state in ALL_PAGE_STATES:
            result = allowed_next(state)
            assert isinstance(result, frozenset)


# ── InvalidTransitionError ────────────────────────────────────────────────────


class TestInvalidTransitionError:
    def test_is_exception(self) -> None:
        err = InvalidTransitionError("queued", "review")
        assert isinstance(err, Exception)

    def test_stores_current(self) -> None:
        err = InvalidTransitionError("queued", "review")
        assert err.current == "queued"

    def test_stores_next_state(self) -> None:
        err = InvalidTransitionError("queued", "review")
        assert err.next_state == "review"

    def test_message_contains_states(self) -> None:
        err = InvalidTransitionError("accepted", "queued")
        assert "accepted" in str(err)
        assert "queued" in str(err)

    def test_raised_by_validate_transition(self) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_transition("accepted", "queued")
        assert exc_info.value.current == "accepted"
        assert exc_info.value.next_state == "queued"
