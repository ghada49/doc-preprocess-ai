"""
tests/test_p4_circuit_breaker.py
----------------------------------
Packet 4.1 — circuit breaker state machine tests.

Covers:
  - Initial state (CLOSED)
  - Consecutive failure counting
  - CLOSED → OPEN after failure_threshold consecutive failures
  - OPEN blocks calls (CircuitBreakerOpenError)
  - OPEN → HALF_OPEN after reset_timeout_seconds
  - HALF_OPEN → CLOSED on probe success
  - HALF_OPEN → OPEN on probe failure (timer restarts)
  - Success resets consecutive failure counter
  - All BackendErrorKind values count as hard failures
  - Cold-start delay that succeeds is transparent (never reaches circuit breaker)
  - call() helper: wraps fn(), records on success/failure, re-raises BackendError
  - CircuitBreakerOpenError carries service_name
  - last_failure_kind is updated per failure

time.monotonic is patched where needed to control the reset timeout without
actual sleeping.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest

from services.eep_worker.app.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
)
from shared.gpu.backend import BackendError, BackendErrorKind

# ── Helpers ────────────────────────────────────────────────────────────────────


def _cb(threshold: int = 5, timeout: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker("iep1a", CircuitBreakerConfig(threshold, timeout))


def _fail(cb: CircuitBreaker, kind: BackendErrorKind = BackendErrorKind.SERVICE_ERROR) -> None:
    """Record a failure of the given kind on the circuit breaker."""
    cb.record_failure(kind)


def _trip(cb: CircuitBreaker, threshold: int = 5) -> None:
    """Trip the circuit breaker by recording threshold consecutive failures."""
    for _ in range(threshold):
        _fail(cb)


# ── Initial state ──────────────────────────────────────────────────────────────


class TestInitialState:
    def test_state_is_closed(self) -> None:
        assert _cb().state is CircuitState.CLOSED

    def test_consecutive_failures_zero(self) -> None:
        assert _cb().consecutive_failures == 0

    def test_last_failure_kind_none(self) -> None:
        assert _cb().last_failure_kind is None

    def test_allow_call_true_when_closed(self) -> None:
        assert _cb().allow_call() is True

    def test_service_name_stored(self) -> None:
        cb = CircuitBreaker("iep1b")
        assert cb.service_name == "iep1b"

    def test_default_config_threshold(self) -> None:
        assert CircuitBreakerConfig().failure_threshold == 5

    def test_default_config_timeout(self) -> None:
        assert CircuitBreakerConfig().reset_timeout_seconds == 60.0


# ── Failure counting ───────────────────────────────────────────────────────────


class TestFailureCounting:
    def test_single_failure_increments_counter(self) -> None:
        cb = _cb()
        _fail(cb)
        assert cb.consecutive_failures == 1

    def test_multiple_failures_increment_counter(self) -> None:
        cb = _cb(threshold=10)
        for i in range(4):
            _fail(cb)
        assert cb.consecutive_failures == 4

    def test_success_resets_counter(self) -> None:
        cb = _cb(threshold=10)
        _fail(cb)
        _fail(cb)
        cb.record_success()
        assert cb.consecutive_failures == 0

    def test_success_closes_circuit(self) -> None:
        cb = _cb(threshold=10)
        _fail(cb)
        cb.record_success()
        assert cb.state is CircuitState.CLOSED

    def test_state_remains_closed_below_threshold(self) -> None:
        cb = _cb(threshold=5)
        for _ in range(4):
            _fail(cb)
        assert cb.state is CircuitState.CLOSED

    def test_last_failure_kind_updated(self) -> None:
        cb = _cb()
        _fail(cb, BackendErrorKind.WARM_INFERENCE_TIMEOUT)
        assert cb.last_failure_kind is BackendErrorKind.WARM_INFERENCE_TIMEOUT

    def test_last_failure_kind_overwritten_on_new_failure(self) -> None:
        cb = _cb(threshold=10)
        _fail(cb, BackendErrorKind.COLD_START_TIMEOUT)
        _fail(cb, BackendErrorKind.SERVICE_ERROR)
        assert cb.last_failure_kind is BackendErrorKind.SERVICE_ERROR


# ── CLOSED → OPEN ─────────────────────────────────────────────────────────────


class TestClosedToOpen:
    def test_opens_at_threshold(self) -> None:
        cb = _cb(threshold=5)
        _trip(cb, 5)
        assert cb.state is CircuitState.OPEN

    def test_opens_exactly_at_threshold_not_before(self) -> None:
        cb = _cb(threshold=3)
        _fail(cb)
        _fail(cb)
        assert cb.state == CircuitState.CLOSED
        _fail(cb)
        assert cast(CircuitState, cb.state) is CircuitState.OPEN

    def test_allow_call_false_when_open_within_timeout(self) -> None:
        cb = _cb(threshold=5, timeout=60.0)
        _trip(cb)
        # Time has not advanced — still within the reset window
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=0.0):
            cb._opened_at = 0.0
            assert cb.allow_call() is False

    def test_open_raises_circuit_breaker_open_error_via_call(self) -> None:
        cb = _cb()
        _trip(cb)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=0.0):
            cb._opened_at = 0.0
            with pytest.raises(CircuitBreakerOpenError):
                cb.call(lambda: None)

    def test_circuit_breaker_open_error_carries_service_name(self) -> None:
        cb = CircuitBreaker("iep1d", CircuitBreakerConfig(failure_threshold=1))
        _trip(cb, 1)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=0.0):
            cb._opened_at = 0.0
            with pytest.raises(CircuitBreakerOpenError) as exc_info:
                cb.call(lambda: None)
        assert exc_info.value.service_name == "iep1d"


# ── OPEN → HALF_OPEN ─────────────────────────────────────────────────────────


class TestOpenToHalfOpen:
    def _open_cb(self, timeout: float = 60.0) -> CircuitBreaker:
        cb = _cb(timeout=timeout)
        _trip(cb)
        return cb

    def test_allow_call_true_after_timeout(self) -> None:
        cb = self._open_cb(timeout=60.0)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 61.0  # past the timeout
            assert cb.allow_call() is True

    def test_state_becomes_half_open_after_timeout(self) -> None:
        cb = self._open_cb(timeout=60.0)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 60.0  # exactly at the boundary
            _ = cb.state  # trigger transition
        assert cb._state is CircuitState.HALF_OPEN

    def test_not_half_open_just_before_timeout(self) -> None:
        cb = self._open_cb(timeout=60.0)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 59.9
            _ = cb.state
        assert cb._state is CircuitState.OPEN

    def test_allow_call_false_just_before_timeout(self) -> None:
        cb = self._open_cb(timeout=60.0)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 59.9
            assert cb.allow_call() is False


# ── HALF_OPEN → CLOSED (probe success) ────────────────────────────────────────


class TestHalfOpenToClosedOnSuccess:
    def _half_open_cb(self) -> CircuitBreaker:
        cb = _cb(timeout=60.0)
        _trip(cb)
        # Force into HALF_OPEN by simulating timeout elapsed
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 61.0
            _ = cb.state  # trigger transition
        return cb

    def test_probe_success_closes_circuit(self) -> None:
        cb = self._half_open_cb()
        assert cb._state is CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state is CircuitState.CLOSED

    def test_probe_success_resets_consecutive_failures(self) -> None:
        cb = self._half_open_cb()
        cb.record_success()
        assert cb.consecutive_failures == 0

    def test_call_closes_circuit_on_success(self) -> None:
        cb = self._half_open_cb()
        cb.call(lambda: 42)
        assert cb.state is CircuitState.CLOSED

    def test_call_returns_result_on_probe_success(self) -> None:
        cb = self._half_open_cb()
        result = cb.call(lambda: "ok")
        assert result == "ok"


# ── HALF_OPEN → OPEN (probe failure) ─────────────────────────────────────────


class TestHalfOpenToOpenOnFailure:
    def _half_open_cb(self) -> CircuitBreaker:
        cb = _cb(timeout=60.0)
        _trip(cb)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 61.0
            _ = cb.state
        return cb

    def test_probe_failure_reopens_circuit(self) -> None:
        cb = self._half_open_cb()
        assert cb._state == CircuitState.HALF_OPEN
        _fail(cb)
        assert cast(CircuitState, cb._state) is CircuitState.OPEN

    def test_probe_failure_restarts_timer(self) -> None:
        cb = self._half_open_cb()
        t_before = 1000.0
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=t_before):
            _fail(cb)
        assert cb._opened_at == t_before

    def test_allow_call_false_after_probe_failure(self) -> None:
        cb = self._half_open_cb()
        t_open = 1000.0
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=t_open):
            _fail(cb)
        # Just past re-open time but before new reset timeout → still blocked
        with patch(
            "services.eep_worker.app.circuit_breaker.time.monotonic",
            return_value=t_open + 30.0,
        ):
            assert cb.allow_call() is False


# ── All BackendErrorKind values count ─────────────────────────────────────────


class TestAllBackendErrorKindsCount:
    def test_service_error_counts(self) -> None:
        cb = _cb(threshold=1)
        _fail(cb, BackendErrorKind.SERVICE_ERROR)
        assert cb.state is CircuitState.OPEN

    def test_cold_start_timeout_counts(self) -> None:
        """Cold-start budget exceeded is a hard failure (spec Section 8.3)."""
        cb = _cb(threshold=1)
        _fail(cb, BackendErrorKind.COLD_START_TIMEOUT)
        assert cb.state is CircuitState.OPEN

    def test_warm_inference_timeout_counts(self) -> None:
        cb = _cb(threshold=1)
        _fail(cb, BackendErrorKind.WARM_INFERENCE_TIMEOUT)
        assert cb.state is CircuitState.OPEN

    def test_cold_start_success_is_transparent(self) -> None:
        """A cold start that completes within budget never reaches the circuit breaker."""
        cb = _cb(threshold=5)
        # Simulate 4 consecutive operations that happen to be slow cold starts
        # but all succeed — circuit breaker sees only successes
        for _ in range(4):
            cb.record_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.consecutive_failures == 0


# ── call() helper ──────────────────────────────────────────────────────────────


class TestCallHelper:
    def test_returns_fn_result_on_success(self) -> None:
        cb = _cb()
        assert cb.call(lambda: 99) == 99

    def test_records_success(self) -> None:
        cb = _cb(threshold=10)
        _fail(cb)
        cb.call(lambda: None)
        assert cb.consecutive_failures == 0

    def test_records_failure_and_reraises_backend_error(self) -> None:
        cb = _cb()
        err = BackendError(BackendErrorKind.SERVICE_ERROR, "connection refused")

        def bad_fn() -> None:
            raise err

        with pytest.raises(BackendError) as exc_info:
            cb.call(bad_fn)

        assert exc_info.value is err
        assert cb.consecutive_failures == 1

    def test_does_not_catch_non_backend_errors(self) -> None:
        cb = _cb()

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("bug")))

    def test_non_backend_error_does_not_record_failure(self) -> None:
        cb = _cb()

        def raise_value_error() -> None:
            raise ValueError("not a backend issue")

        with pytest.raises(ValueError):
            cb.call(raise_value_error)

        assert cb.consecutive_failures == 0

    def test_raises_open_error_when_circuit_open(self) -> None:
        cb = _cb()
        _trip(cb)
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic", return_value=0.0):
            cb._opened_at = 0.0
            with pytest.raises(CircuitBreakerOpenError):
                cb.call(lambda: None)

    def test_call_returns_value_after_trip_and_recovery(self) -> None:
        cb = _cb(timeout=60.0)
        _trip(cb)
        # Simulate timeout elapsed → HALF_OPEN
        with patch("services.eep_worker.app.circuit_breaker.time.monotonic") as mock_t:
            cb._opened_at = 0.0
            mock_t.return_value = 61.0
            result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state is CircuitState.CLOSED
