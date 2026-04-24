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
# Section 9.11 (pending_human_correction), Section 5 (human correction workflow).
#
# Every valid (from_state → to_state) pair is listed here.
# States not present as source keys have no outgoing transitions (leaf-final /
# routing-terminal).  The presence of both "accepted" and "split" as keys with
# empty frozensets is intentional: it allows `allowed_next` callers to query any
# state without a KeyError while the empty set encodes finality.
#
# Automation-first model: pages flow directly from preprocessing/rectification
# to layout_detection (layout mode) or accepted (preprocess-only mode) without
# any manual gate. Human review is an explicit opt-in transition; after correction
# pages resume automatically via layout_detection.

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
            "rectification",        # artifact invalid → IEP1D fallback (Step 6)
            "ptiff_qa_pending",     # preprocessing succeeded, ptiff_qa_mode=manual (Step 8.5)
            "layout_detection",     # preprocessing succeeded, ptiff_qa_mode=auto_continue, layout mode
            "accepted",             # preprocessing succeeded, ptiff_qa_mode=auto_continue, preprocess mode
            "pending_human_correction",  # geometry / selection / normalization failures
            "split",                # spread parent: children created and enqueued (Step 8)
            "failed",               # infrastructure failure; retries exhausted
        }
    ),
    "rectification": frozenset(
        {
            "ptiff_qa_pending",     # IEP1D + second-pass succeeded, ptiff_qa_mode=manual (Step 8.5)
            "layout_detection",     # IEP1D + second-pass succeeded, ptiff_qa_mode=auto_continue, layout mode
            "accepted",             # IEP1D + second-pass succeeded, ptiff_qa_mode=auto_continue, preprocess mode
            "pending_human_correction",  # IEP1D / second-pass / final validation failure
            "split",                # spread parent: both child artifacts validated (Step 8)
            "failed",               # infrastructure failure; retries exhausted
        }
    ),
    # PTIFF QA checkpoint (spec Section 3.1 / 8.5):
    #   Manual mode  — page waits here until reviewer approves or sends to correction.
    #   Auto mode    — gate releases immediately when all pages reach this state.
    #   Worker must stop here; no automated processing resumes until gate releases.
    "ptiff_qa_pending": frozenset(
        {
            "accepted",             # gate release, pipeline_mode=preprocess
            "layout_detection",     # gate release, pipeline_mode=layout
            "pending_human_correction",  # reviewer sends page to correction via /edit
        }
    ),
    "layout_detection": frozenset(
        {
            "accepted",  # layout consensus passes (Step 14)
            "review",  # layout adjudication flags for review
            "failed",  # infrastructure failure; retries exhausted
            "pending_human_correction",  # explicit user send-to-review action
        }
    ),
    "pending_human_correction": frozenset(
        {
            "semantic_norm",  # correction submitted → iep1e (all pipeline modes)
            "review",  # correction rejected
            "split",  # human split: parent → split after children reach terminal
        }
    ),
    # Post-human-correction semantic normalization: worker runs iep1e (orientation +
    # reading order) then routes to layout_detection (layout mode) or accepted
    # (preprocess-only mode).
    "semantic_norm": frozenset(
        {
            "layout_detection",  # iep1e done, pipeline_mode=layout → proceed to IEP2
            "accepted",          # iep1e done, pipeline_mode=preprocess → accept
            "failed",            # infrastructure failure; retries exhausted
        }
    ),
    # ── Accepted: leaf-final for automation, but reviewer/system may reopen it ──
    # Transition to pending_human_correction is user-initiated only (PTIFF QA viewer
    # flag action). This re-opens the page for human correction; after correction the
    # page resumes the normal pipeline (layout_detection or accepted depending on mode).
    # Transition to semantic_norm is system-initiated only: when a split sibling is
    # human-corrected, an already-accepted child must be reconsidered with the pair
    # for reading direction and rotation.
    "accepted": frozenset(
        {
            "pending_human_correction",  # reviewer flags via PTIFF QA viewer
            "semantic_norm",  # sibling correction re-runs pair-level IEP1E
        }
    ),
    # ── Truly leaf-final states: no outgoing transitions ─────────────────────────
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
        "semantic_norm",
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

# Leaf-final states: once reached, no further AUTOMATED transitions are possible.
# "accepted" is excluded: a reviewer may flag an accepted page for re-correction via
# the PTIFF QA viewer, which transitions it to pending_human_correction. This is a
# deliberate user-initiated action, not an automated pipeline step.
_LEAF_FINAL_STATES: frozenset[str] = frozenset({"review", "failed"})
_WORKER_STOP_STATES: frozenset[str] = frozenset(
    {
        "ptiff_qa_pending",          # worker stops; gate or reviewer resumes
        "accepted",
        "pending_human_correction",
        "review",
        "failed",
        "split",
    }
)


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

    Worker-stop states are intentionally broader than job-completion states:
    pending_human_correction still stops automated processing even though the
    job remains active until review is resolved.
    """
    return state in _WORKER_STOP_STATES


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
