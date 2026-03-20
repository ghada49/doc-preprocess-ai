"""
services/eep_worker/app/circuit_breaker.py
-------------------------------------------
Per-worker in-process circuit breaker for external IEP service calls.

Implements the model from spec Sections 8.1, 8.3, and 8.4:

  States:
    CLOSED    — normal operation; all calls pass through
    OPEN      — circuit has tripped; calls are rejected immediately
    HALF_OPEN — one probe call is allowed after the reset timeout elapses

  Transitions:
    CLOSED   → OPEN      after consecutive_failures >= failure_threshold
    OPEN     → HALF_OPEN after reset_timeout_seconds have elapsed
    HALF_OPEN → CLOSED   on probe success (record_success)
    HALF_OPEN → OPEN     on probe failure (record_failure)

  Defaults (spec Section 8.4 libraryai-policy ConfigMap):
    failure_threshold:       5
    reset_timeout_seconds:  60

  Scope (spec Section 8.4):
    One instance per IEP service per worker process.
    State is NEVER shared across workers — intentional design.

Failure counting:
  All three BackendErrorKind values are hard failures (spec Section 8.3):
    - COLD_START_TIMEOUT  (cold-start budget exceeded)
    - WARM_INFERENCE_TIMEOUT
    - SERVICE_ERROR
  A cold-start delay that completes within budget is transparent to the
  circuit breaker — the backend handles it internally and raises no exception.

Async usage (Phase 4 task.py pattern)::

    cb = CircuitBreaker("iep1a")

    if not cb.allow_call():
        raise CircuitBreakerOpenError("iep1a")
    try:
        result = await backend.call(endpoint, payload)
        cb.record_success()
    except BackendError as exc:
        cb.record_failure(exc.kind)
        raise

Sync usage (for testing or non-async call sites)::

    result = cb.call(lambda: sync_fn())

Exported:
    CircuitState           — CLOSED / OPEN / HALF_OPEN enum
    CircuitBreakerConfig   — config dataclass (mirrors libraryai-policy)
    CircuitBreakerOpenError — raised when a call is blocked
    CircuitBreaker         — the circuit breaker class
"""

from __future__ import annotations

import dataclasses
import enum
import time
from collections.abc import Callable
from typing import TypeVar

from shared.gpu.backend import BackendError, BackendErrorKind

_T = TypeVar("_T")


# ── State enum ─────────────────────────────────────────────────────────────────


class CircuitState(enum.Enum):
    """Operating state of a CircuitBreaker instance."""

    CLOSED = "closed"
    """Normal operation.  All calls pass through."""

    OPEN = "open"
    """Circuit has tripped.  Calls are rejected until the reset timeout elapses."""

    HALF_OPEN = "half_open"
    """
    One probe call is allowed.  If it succeeds the circuit closes; if it
    fails the circuit re-opens and the reset timer restarts.
    """


# ── Configuration ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class CircuitBreakerConfig:
    """
    Configuration for a CircuitBreaker instance.

    Defaults match the libraryai-policy ConfigMap (spec Section 8.4):
        failure_threshold:      5
        reset_timeout_seconds: 60
    """

    failure_threshold: int = 5
    reset_timeout_seconds: float = 60.0


# ── Exception ──────────────────────────────────────────────────────────────────


class CircuitBreakerOpenError(Exception):
    """
    Raised when a call is attempted while the circuit breaker is OPEN
    (i.e. allow_call() returned False).

    Callers must handle this by routing the page to an appropriate fallback
    (e.g. pending_human_correction for IEP1D unavailability per spec Section 18.5).
    """

    def __init__(self, service_name: str) -> None:
        super().__init__(
            f"Circuit breaker OPEN for service '{service_name}'; "
            "call rejected until reset timeout elapses."
        )
        self.service_name = service_name


# ── Circuit breaker ────────────────────────────────────────────────────────────


class CircuitBreaker:
    """
    Per-worker in-process circuit breaker for a single IEP service.

    Create one instance per service per worker process at worker startup:

        _cb_iep1a = CircuitBreaker("iep1a")
        _cb_iep1b = CircuitBreaker("iep1b")
        _cb_iep1d = CircuitBreaker("iep1d")
        _cb_iep2a = CircuitBreaker("iep2a")
        _cb_iep2b = CircuitBreaker("iep2b")

    Thread/task safety: this class is not thread-safe.  In the async EEP
    worker, all page tasks run on a single event loop thread, so concurrent
    mutation is not possible without explicit concurrency.  If the worker ever
    moves to multi-threaded concurrency, a lock must be added.
    """

    def __init__(
        self,
        service_name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self.service_name = service_name
        self.config = config or CircuitBreakerConfig()

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: float | None = None
        self._last_failure_kind: BackendErrorKind | None = None

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """
        Current state, reflecting any timeout-triggered OPEN → HALF_OPEN
        transition.  Reading this property may change ``_state``.
        """
        self._maybe_transition_to_half_open()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failures since the last success."""
        return self._consecutive_failures

    @property
    def last_failure_kind(self) -> BackendErrorKind | None:
        """BackendErrorKind of the most recent recorded failure, or None."""
        return self._last_failure_kind

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _maybe_transition_to_half_open(self) -> None:
        """
        If the circuit is OPEN and the reset timeout has elapsed, transition to
        HALF_OPEN so that one probe call is permitted.
        """
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.config.reset_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN

    # ── Core API ───────────────────────────────────────────────────────────────

    def allow_call(self) -> bool:
        """
        Return True when the circuit breaker permits a call attempt.

        - CLOSED:    always True
        - OPEN:      False unless the reset timeout has elapsed, in which case
                     the circuit transitions to HALF_OPEN and returns True
        - HALF_OPEN: True (one probe allowed)

        Side-effect: may transition OPEN → HALF_OPEN.
        """
        if self._state is CircuitState.CLOSED:
            return True
        self._maybe_transition_to_half_open()
        return self._state is CircuitState.HALF_OPEN

    def record_success(self) -> None:
        """
        Record a successful IEP call.

        Resets the consecutive failure counter and closes the circuit from any
        state (CLOSED, HALF_OPEN).
        """
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self, kind: BackendErrorKind | None = None) -> None:
        """
        Record a failed IEP call.

        All BackendErrorKind values are hard failures (spec Section 8.3):
          - COLD_START_TIMEOUT  (budget exceeded — never counts cold starts that succeed)
          - WARM_INFERENCE_TIMEOUT
          - SERVICE_ERROR

        Opens the circuit when:
          - consecutive_failures reaches failure_threshold, or
          - the circuit is already in HALF_OPEN (probe failed → re-open immediately)

        The reset timer restarts each time the circuit opens.

        Args:
            kind: BackendErrorKind of the failure, stored for diagnostics.
        """
        self._consecutive_failures += 1
        self._last_failure_kind = kind

        if (
            self._state is CircuitState.HALF_OPEN
            or self._consecutive_failures >= self.config.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def call(self, fn: Callable[[], _T]) -> _T:
        """
        Execute ``fn()`` guarded by the circuit breaker.

        Intended for synchronous call sites and tests.  For async callers
        (Phase 4 task.py), use ``allow_call`` / ``record_success`` /
        ``record_failure`` directly around the awaited backend call.

        Raises:
            CircuitBreakerOpenError: if ``allow_call()`` returns False.
            BackendError:            if ``fn()`` raises BackendError; the
                                     failure is recorded and the exception
                                     is re-raised unchanged.
        """
        if not self.allow_call():
            raise CircuitBreakerOpenError(self.service_name)
        try:
            result = fn()
        except BackendError as exc:
            self.record_failure(exc.kind)
            raise
        self.record_success()
        return result
