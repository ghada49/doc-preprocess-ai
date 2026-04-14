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
  - is_leaf_final covers accepted / review / failed only
  - allowed_next returns correct frozensets

Definition of done:
  - all page transitions are centrally validated
  - automation-first: preprocessing/rectification route directly to layout_detection/accepted
  - pending_human_correction resumes via layout_detection or accepted after correction
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
    ("preprocessing", "layout_detection"),
    ("preprocessing", "accepted"),
    ("preprocessing", "pending_human_correction"),
    ("preprocessing", "split"),
    ("preprocessing", "failed"),
    ("rectification", "layout_detection"),
    ("rectification", "accepted"),
    ("rectification", "pending_human_correction"),
    ("rectification", "split"),
    ("rectification", "failed"),
    ("layout_detection", "accepted"),
    ("layout_detection", "review"),
    ("layout_detection", "failed"),
    ("layout_detection", "pending_human_correction"),
    ("pending_human_correction", "layout_detection"),
    ("pending_human_correction", "accepted"),
    ("pending_human_correction", "review"),
    ("pending_human_correction", "split"),
]

# A representative sample of explicitly prohibited transitions
INVALID_TRANSITIONS = [
    # Backwards / skipping transitions
    ("preprocessing", "queued"),
    ("rectification", "queued"),
    ("rectification", "preprocessing"),
    ("layout_detection", "queued"),
    ("layout_detection", "preprocessing"),
    ("layout_detection", "rectification"),
    ("layout_detection", "split"),
    # Transitions out of accepted (only pending_human_correction is allowed now;
    # all others remain invalid)
    ("accepted", "queued"),
    ("accepted", "preprocessing"),
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
    # No direct queued → layout_detection / etc.
    ("queued", "layout_detection"),
    ("queued", "pending_human_correction"),
    ("queued", "review"),
    ("queued", "split"),
    # No direct preprocessing → review
    ("preprocessing", "review"),
    # No pending_human_correction → failed
    ("pending_human_correction", "failed"),
    # Identical state self-transition
    ("queued", "queued"),
    ("preprocessing", "preprocessing"),
    ("layout_detection", "layout_detection"),
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
        # "accepted" now allows reviewer-initiated flagging to pending_human_correction.
        # Only "review" and "failed" are truly leaf-final with no outgoing transitions.
        for state in ("review", "failed"):
            assert (
                ALLOWED_TRANSITIONS[state] == frozenset()
            ), f"Leaf-final state '{state}' must have no outgoing transitions"
        # accepted has exactly one outgoing transition (user-initiated re-correction)
        assert ALLOWED_TRANSITIONS["accepted"] == frozenset({"pending_human_correction"})

    def test_split_has_no_transitions(self) -> None:
        assert ALLOWED_TRANSITIONS["split"] == frozenset()

    def test_queued_has_exactly_two_transitions(self) -> None:
        assert ALLOWED_TRANSITIONS["queued"] == frozenset({"preprocessing", "failed"})

    def test_pending_human_correction_transitions(self) -> None:
        expected = frozenset({"layout_detection", "accepted", "review", "split"})
        assert ALLOWED_TRANSITIONS["pending_human_correction"] == expected

    def test_layout_detection_transitions(self) -> None:
        expected = frozenset({"accepted", "review", "failed", "pending_human_correction"})
        assert ALLOWED_TRANSITIONS["layout_detection"] == expected

    def test_preprocessing_direct_to_layout_detection(self) -> None:
        assert "layout_detection" in ALLOWED_TRANSITIONS["preprocessing"]

    def test_preprocessing_direct_to_accepted(self) -> None:
        assert "accepted" in ALLOWED_TRANSITIONS["preprocessing"]

    def test_rectification_direct_to_layout_detection(self) -> None:
        assert "layout_detection" in ALLOWED_TRANSITIONS["rectification"]

    def test_rectification_direct_to_accepted(self) -> None:
        assert "accepted" in ALLOWED_TRANSITIONS["rectification"]


# ── validate_transition — valid pairs ─────────────────────────────────────────


class TestValidTransitions:
    @pytest.mark.parametrize("from_state,to_state", VALID_TRANSITIONS)
    def test_valid_pair_does_not_raise(self, from_state: str, to_state: str) -> None:
        validate_transition(from_state, to_state)  # must not raise

    def test_preprocessing_to_layout_detection(self) -> None:
        validate_transition("preprocessing", "layout_detection")

    def test_preprocessing_to_accepted(self) -> None:
        validate_transition("preprocessing", "accepted")

    def test_rectification_to_layout_detection(self) -> None:
        validate_transition("rectification", "layout_detection")

    def test_rectification_to_accepted(self) -> None:
        validate_transition("rectification", "accepted")

    def test_layout_detection_to_pending_human_correction(self) -> None:
        # User explicitly sends page to review
        validate_transition("layout_detection", "pending_human_correction")

    def test_pending_human_correction_to_layout_detection(self) -> None:
        # Correction submitted, pipeline_mode=layout → resume IEP2
        validate_transition("pending_human_correction", "layout_detection")

    def test_pending_human_correction_to_accepted(self) -> None:
        # Correction submitted, pipeline_mode=preprocess → direct accept
        validate_transition("pending_human_correction", "accepted")

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

    def test_accepted_to_any_invalid_state_raises(self) -> None:
        # accepted → pending_human_correction is now valid (reviewer flag action).
        # All other transitions from accepted are still invalid.
        invalid_targets = ALL_PAGE_STATES - {"pending_human_correction"}
        for state in invalid_targets:
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

    def test_queued_is_not_terminal(self) -> None:
        assert is_worker_terminal("queued") is False

    def test_preprocessing_is_not_terminal(self) -> None:
        assert is_worker_terminal("preprocessing") is False

    def test_rectification_is_not_terminal(self) -> None:
        assert is_worker_terminal("rectification") is False

    def test_layout_detection_is_not_terminal(self) -> None:
        assert is_worker_terminal("layout_detection") is False

    def test_delegates_to_worker_stop_states(self) -> None:
        # is_worker_terminal checks _WORKER_STOP_STATES, which is intentionally broader
        # than TERMINAL_PAGE_STATES (job-accounting states).
        # Worker-stop = automated worker ceases; includes human-gate states
        # (pending_human_correction, ptiff_qa_pending) that are not job-complete.
        worker_stop_expected = frozenset(
            {
                "accepted",
                "review",
                "failed",
                "split",
                "pending_human_correction",
                "ptiff_qa_pending",
            }
        )
        for state in ALL_PAGE_STATES:
            assert is_worker_terminal(state) == (state in worker_stop_expected), (
                f"is_worker_terminal({state!r}) mismatch"
            )

    def test_all_terminal_states_covered(self) -> None:
        terminal_count = sum(1 for s in ALL_PAGE_STATES if is_worker_terminal(s))
        assert terminal_count == 6  # accepted, ptiff_qa_pending, pending_human_correction, review, failed, split

    def test_non_terminal_count(self) -> None:
        non_terminal = [s for s in ALL_PAGE_STATES if not is_worker_terminal(s)]
        assert set(non_terminal) == {
            "queued",
            "preprocessing",
            "rectification",
            "layout_detection",
        }


# ── is_leaf_final ─────────────────────────────────────────────────────────────


class TestIsLeafFinal:
    def test_accepted_is_not_leaf_final(self) -> None:
        # accepted allows one user-initiated outgoing transition (flagging for re-correction).
        assert is_leaf_final("accepted") is False

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

    def test_queued_is_not_leaf_final(self) -> None:
        assert is_leaf_final("queued") is False

    def test_preprocessing_is_not_leaf_final(self) -> None:
        assert is_leaf_final("preprocessing") is False

    def test_rectification_is_not_leaf_final(self) -> None:
        assert is_leaf_final("rectification") is False

    def test_layout_detection_is_not_leaf_final(self) -> None:
        assert is_leaf_final("layout_detection") is False

    def test_exactly_two_leaf_final_states(self) -> None:
        # "accepted" was removed from leaf-final: reviewers can flag it for re-correction.
        leaf_final = [s for s in ALL_PAGE_STATES if is_leaf_final(s)]
        assert sorted(leaf_final) == ["failed", "review"]


# ── allowed_next ──────────────────────────────────────────────────────────────


class TestAllowedNext:
    def test_queued_allowed_next(self) -> None:
        assert allowed_next("queued") == frozenset({"preprocessing", "failed"})

    def test_pending_human_correction_allowed_next(self) -> None:
        result = allowed_next("pending_human_correction")
        assert result == frozenset({"layout_detection", "accepted", "review", "split"})

    def test_layout_detection_allowed_next(self) -> None:
        result = allowed_next("layout_detection")
        assert result == frozenset({"accepted", "review", "failed", "pending_human_correction"})

    def test_accepted_allowed_next_has_flag_transition(self) -> None:
        # accepted → pending_human_correction is the reviewer flag action.
        assert allowed_next("accepted") == frozenset({"pending_human_correction"})

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
