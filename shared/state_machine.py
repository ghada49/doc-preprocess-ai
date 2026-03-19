"""
shared.state_machine
--------------------
Authoritative page-state transition contract for all LibraryAI services.

All components (EEP API, worker, watchdog, recovery) must import from this
module to validate or enforce state transitions. Never duplicate or weaken the
transition rules inline. (spec Section 9, Section 19.5)

Exported:
    ALLOWED_TRANSITIONS    — complete map of every valid (from, to) pair
    InvalidTransitionError — raised when a requested transition is not allowed
    validate_transition    — guard used before every DB state update
    is_worker_terminal     — True when automated worker must stop for this state
    is_leaf_final          — True for permanent, non-revisitable terminal outcomes
    allowed_next           — frozenset of states reachable from a given state
"""

from __future__ import annotations

from shared.schemas.eep import TERMINAL_PAGE_STATES

# ── Transition map ─────────────────────────────────────────────────────────────
#
# Source: spec Section 8 (process_page algorithm), Section 9.1 (terminal states),
# Section 3.1 (PTIFF QA checkpoint), Section 9.11 (pending_human_correction),
# Section 5 (human correction workflow).
#
# Every valid (from_state → to_state) pair is listed here.
# States not present as source keys have no outgoing transitions (leaf-final /
# routing-terminal).  The presence of both "accepted" and "split" as keys with
# empty frozensets is intentional: it allows `allowed_next` callers to query any
# state without a KeyError while the empty set encodes finality.

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # ── Non-terminal active states ────────────────────────────────────────────
    "queued": frozenset(
        {
            "preprocessing",  # worker CAS: picks up page (Step 1)
            "failed",  # recovery: infrastructure failure before start
        }
    ),
    "preprocessing": frozenset(
        {
            "rectification",  # artifact invalid → IEP1D fallback (Step 6)
            "ptiff_qa_pending",  # preprocessing artifact resolved (Step 8.5)
            "pending_human_correction",  # geometry / selection / normalization failures
            "split",  # spread parent: children created and enqueued (Step 8)
            "failed",  # infrastructure failure; retries exhausted
        }
    ),
    "rectification": frozenset(
        {
            "ptiff_qa_pending",  # IEP1D + second-pass succeeded (Step 8.5)
            "pending_human_correction",  # IEP1D / second-pass / final validation failure
            "split",  # spread parent: both child artifacts validated (Step 8)
            "failed",  # infrastructure failure; retries exhausted
        }
    ),
    "ptiff_qa_pending": frozenset(
        {
            "accepted",  # auto_continue + preprocess, or gate-release + preprocess
            "layout_detection",  # auto_continue + layout, or gate-release + layout (Step 9)
            "pending_human_correction",  # reviewer "edit" action in PTIFF QA screen
        }
    ),
    "layout_detection": frozenset(
        {
            "accepted",  # layout consensus passes (Step 14)
            "review",  # layout disagreement or IEP2A failure (Step 13)
            "failed",  # infrastructure failure; retries exhausted
        }
    ),
    "pending_human_correction": frozenset(
        {
            "ptiff_qa_pending",  # correction submitted (single-page or split child)
            "review",  # correction rejected
            "split",  # human split: parent → split after children reach terminal
        }
    ),
    # ── Leaf-final states: no outgoing transitions ────────────────────────────
    "accepted": frozenset(),
    "review": frozenset(),
    "failed": frozenset(),
    # ── Routing-terminal state: no outgoing transitions ───────────────────────
    "split": frozenset(),
}

# Invariant: ALLOWED_TRANSITIONS must cover every valid PageState.
_ALL_PAGE_STATES: frozenset[str] = frozenset(
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
assert (
    set(ALLOWED_TRANSITIONS.keys()) == _ALL_PAGE_STATES
), "ALLOWED_TRANSITIONS must cover every PageState"

# Leaf-final states: once reached, no further transitions are possible.
# These are permanent outcomes (spec Section 9.1).
_LEAF_FINAL_STATES: frozenset[str] = frozenset({"accepted", "review", "failed"})


# ── Exception ──────────────────────────────────────────────────────────────────


class InvalidTransitionError(Exception):
    """
    Raised when a requested page-state transition is not permitted.

    Callers should catch this exception and treat it as a hard error —
    the DB state must not be updated when this is raised.
    """

    def __init__(self, current: str, next_state: str) -> None:
        super().__init__(
            f"Transition '{current}' → '{next_state}' is not allowed. "
            f"Allowed from '{current}': {sorted(ALLOWED_TRANSITIONS.get(current, frozenset()))}"
        )
        self.current = current
        self.next_state = next_state


# ── Public API ─────────────────────────────────────────────────────────────────


def validate_transition(current: str, next_state: str) -> None:
    """
    Assert that transitioning from *current* to *next_state* is permitted.

    Raises:
        ValueError             — if either state is not a known PageState
        InvalidTransitionError — if the transition is not in ALLOWED_TRANSITIONS

    This function must be called by the worker, API, watchdog, and recovery
    service before any DB state update.  Callers must not bypass it.
    """
    if current not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown page state: '{current}'")
    if next_state not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown page state: '{next_state}'")
    if next_state not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(current, next_state)


def is_worker_terminal(state: str) -> bool:
    """
    Return True when automated worker processing must stop for this state.

    Delegates to TERMINAL_PAGE_STATES (imported from shared.schemas.eep) so
    that the definition is never duplicated.  All five terminal states stop
    automated processing; `ptiff_qa_pending` does NOT.
    """
    return state in TERMINAL_PAGE_STATES


def is_leaf_final(state: str) -> bool:
    """
    Return True for permanent terminal outcomes where no further transition
    is possible under any condition (accepted, review, failed).

    Unlike is_worker_terminal, this excludes:
    - pending_human_correction (worker-terminal but human can resume it)
    - split (routing-terminal but not a page outcome)
    """
    return state in _LEAF_FINAL_STATES


def allowed_next(state: str) -> frozenset[str]:
    """
    Return the frozenset of states reachable from *state*.

    Raises ValueError if *state* is not a known PageState.
    Returns an empty frozenset for leaf-final and routing-terminal states.
    """
    if state not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown page state: '{state}'")
    return ALLOWED_TRANSITIONS[state]
