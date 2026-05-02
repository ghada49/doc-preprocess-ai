"""
tests/test_p4_geometry_invocation.py
-------------------------------------
Packet 4.3b — parallel geometry invocation and selection wiring tests.

Covers:
  1. Happy path: both services succeed → gate called, result populated
  2. Partial failure A: IEP1A fails, IEP1B succeeds → LOW trust path
  3. Partial failure B: IEP1B fails, IEP1A succeeds → LOW trust path
  4. Full failure: both fail → GeometryServiceError raised; gate NOT called
  5. Timeout classification: COLD_START_TIMEOUT → status="timeout"
  6. Timeout classification: WARM_INFERENCE_TIMEOUT → status="timeout"
  7. Circuit breaker open → invocation skipped; backend not called
  8. Circuit breaker trips on repeated failures → breaker opens
  9. Circuit breaker recovers after cooldown → HALF_OPEN → CLOSED
 10. Malformed response → ValidationError treated as "error", circuit breaker penalised
 11. Idempotency: two consecutive calls write exactly 4 ServiceInvocation rows total
 12. ServiceInvocation logging: correct lineage_id, service_name, status, error_message

Session is mocked — no live database required.
Backend is mocked (AsyncMock) — no live IEP services required.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.eep.app.db.models import ServiceInvocation
from services.eep_worker.app.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from services.eep_worker.app.geometry_invocation import (
    GeometryInvocationResult,
    GeometryServiceError,
    invoke_geometry_services,
)
from shared.gpu.backend import BackendError, BackendErrorKind

# ── Helpers ────────────────────────────────────────────────────────────────────


def _valid_response_dict(page_count: int = 1) -> dict[str, Any]:
    """Build a minimal valid GeometryResponse dict."""
    pages = [
        {
            "region_id": f"page_{i}",
            "geometry_type": "bbox",
            "corners": None,
            "bbox": [10, 10, 800, 750],
            "confidence": 0.95,
            "page_area_fraction": 0.80,
        }
        for i in range(page_count)
    ]
    return {
        "page_count": page_count,
        "pages": pages,
        "split_required": page_count > 1,
        "split_x": 500 if page_count > 1 else None,
        "geometry_confidence": 0.95,
        "tta_structural_agreement_rate": 0.95,
        "tta_prediction_variance": 0.05,
        "tta_passes": 3,
        "uncertainty_flags": [],
        "warnings": [],
        "processing_time_ms": 120.0,
        "service_version": "test-service-v1",
        "model_version": "test-model-v1",
        "model_source": "test-model-source",
    }


def _make_cbs(
    failure_threshold_a: int = 5,
    failure_threshold_b: int = 5,
) -> tuple[CircuitBreaker, CircuitBreaker]:
    """Create fresh circuit breakers for IEP1A and IEP1B."""
    a = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=failure_threshold_a))
    b = CircuitBreaker("iep1b", CircuitBreakerConfig(failure_threshold=failure_threshold_b))
    return a, b


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


@pytest.fixture
def backend() -> AsyncMock:
    b = AsyncMock()
    b.call = AsyncMock(return_value=_valid_response_dict())
    return b


# ── Invoke helper ──────────────────────────────────────────────────────────────


async def _invoke(
    backend: AsyncMock,
    session: MagicMock,
    cbs: tuple[CircuitBreaker, CircuitBreaker] | None = None,
    **kwargs: Any,
) -> GeometryInvocationResult:
    """Call invoke_geometry_services() with sensible defaults."""
    a_cb, b_cb = cbs if cbs is not None else _make_cbs()
    return await invoke_geometry_services(
        job_id="job-1",
        page_number=1,
        lineage_id="lin-1",
        proxy_image_uri="s3://bucket/proxy.png",
        material_type="book",
        proxy_width=1024,
        proxy_height=800,
        iep1a_endpoint="http://iep1a:8001/v1/geometry",
        iep1b_endpoint="http://iep1b:8002/v1/geometry",
        iep1a_circuit_breaker=a_cb,
        iep1b_circuit_breaker=b_cb,
        backend=backend,
        session=session,
        **kwargs,
    )


def _added_records(session: MagicMock) -> list[ServiceInvocation]:
    """Extract ServiceInvocation objects passed to session.add()."""
    return [c.args[0] for c in session.add.call_args_list]


# ── 1. Happy path ──────────────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_geometry_invocation_result(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend, session)
        assert isinstance(result, GeometryInvocationResult)

    @pytest.mark.asyncio
    async def test_both_results_populated(self, backend: AsyncMock, session: MagicMock) -> None:
        result = await _invoke(backend, session)
        assert result.iep1a_result is not None
        assert result.iep1b_result is not None

    @pytest.mark.asyncio
    async def test_no_errors(self, backend: AsyncMock, session: MagicMock) -> None:
        result = await _invoke(backend, session)
        assert result.iep1a_error is None
        assert result.iep1b_error is None

    @pytest.mark.asyncio
    async def test_not_skipped(self, backend: AsyncMock, session: MagicMock) -> None:
        result = await _invoke(backend, session)
        assert result.iep1a_skipped is False
        assert result.iep1b_skipped is False

    @pytest.mark.asyncio
    async def test_selection_result_populated(self, backend: AsyncMock, session: MagicMock) -> None:
        result = await _invoke(backend, session)
        assert result.selection_result is not None

    @pytest.mark.asyncio
    async def test_backend_called_twice(self, backend: AsyncMock, session: MagicMock) -> None:
        await _invoke(backend, session)
        assert backend.call.call_count == 2

    @pytest.mark.asyncio
    async def test_two_service_invocation_rows_written(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        assert session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_both_invocation_rows_are_success(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        records = _added_records(session)
        statuses = {r.status for r in records}
        assert statuses == {"success"}

    @pytest.mark.asyncio
    async def test_high_trust_when_both_agree(self, backend: AsyncMock, session: MagicMock) -> None:
        # Both responses identical → structural agreement → high trust → accepted
        result = await _invoke(backend, session)
        assert result.selection_result.route_decision == "accepted"

    @pytest.mark.asyncio
    async def test_timing_populated(self, backend: AsyncMock, session: MagicMock) -> None:
        result = await _invoke(backend, session)
        assert result.iep1a_duration_ms is not None
        assert result.iep1b_duration_ms is not None
        assert result.iep1a_duration_ms >= 0.0
        assert result.iep1b_duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_service_invocation_model_metadata_stored(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        records = _added_records(session)
        for record in records:
            assert record.service_version == "test-service-v1"
            assert record.model_version == "test-model-v1"
            assert record.model_source == "test-model-source"


# ── 2. Partial failure A — IEP1A fails, IEP1B succeeds ────────────────────────


class TestPartialFailureAFails:
    @pytest.fixture
    def backend_a_fails(self) -> AsyncMock:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "IEP1A service error")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        return b

    @pytest.mark.asyncio
    async def test_result_has_iep1b_only(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_a_fails, session)
        assert result.iep1a_result is None
        assert result.iep1b_result is not None

    @pytest.mark.asyncio
    async def test_iep1a_error_populated(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_a_fails, session)
        assert result.iep1a_error is not None
        assert result.iep1a_error["kind"] == BackendErrorKind.SERVICE_ERROR.value

    @pytest.mark.asyncio
    async def test_selection_result_low_trust(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_a_fails, session)
        assert result.selection_result.geometry_trust == "low"

    @pytest.mark.asyncio
    async def test_selection_routes_to_rectification(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_a_fails, session)
        assert result.selection_result.route_decision == "rectification"

    @pytest.mark.asyncio
    async def test_iep1a_row_status_is_error(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend_a_fails, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.status == "error"

    @pytest.mark.asyncio
    async def test_iep1b_row_status_is_success(
        self, backend_a_fails: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend_a_fails, session)
        records = _added_records(session)
        b_record = next(r for r in records if r.service_name == "iep1b")
        assert b_record.status == "success"


# ── 3. Partial failure B — IEP1B fails, IEP1A succeeds ────────────────────────


class TestPartialFailureBFails:
    @pytest.fixture
    def backend_b_fails(self) -> AsyncMock:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1b" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "IEP1B service error")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        return b

    @pytest.mark.asyncio
    async def test_result_has_iep1a_only(
        self, backend_b_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_b_fails, session)
        assert result.iep1a_result is not None
        assert result.iep1b_result is None

    @pytest.mark.asyncio
    async def test_iep1b_error_populated(
        self, backend_b_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_b_fails, session)
        assert result.iep1b_error is not None

    @pytest.mark.asyncio
    async def test_selection_result_low_trust(
        self, backend_b_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_b_fails, session)
        assert result.selection_result.geometry_trust == "low"

    @pytest.mark.asyncio
    async def test_iep1a_is_not_skipped(
        self, backend_b_fails: AsyncMock, session: MagicMock
    ) -> None:
        result = await _invoke(backend_b_fails, session)
        assert result.iep1a_skipped is False


# ── 4. Full failure — both services fail ───────────────────────────────────────


class TestFullFailure:
    @pytest.fixture
    def backend_both_fail(self) -> AsyncMock:
        b = AsyncMock()
        b.call = AsyncMock(side_effect=BackendError(BackendErrorKind.SERVICE_ERROR, "all broken"))
        return b

    @pytest.mark.asyncio
    async def test_raises_geometry_service_error(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        with pytest.raises(GeometryServiceError):
            await _invoke(backend_both_fail, session)

    @pytest.mark.asyncio
    async def test_error_carries_job_id(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        with pytest.raises(GeometryServiceError) as exc_info:
            await _invoke(backend_both_fail, session)
        assert exc_info.value.job_id == "job-1"

    @pytest.mark.asyncio
    async def test_error_carries_page_number(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        with pytest.raises(GeometryServiceError) as exc_info:
            await _invoke(backend_both_fail, session)
        assert exc_info.value.page_number == 1

    @pytest.mark.asyncio
    async def test_error_carries_both_error_dicts(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        with pytest.raises(GeometryServiceError) as exc_info:
            await _invoke(backend_both_fail, session)
        assert exc_info.value.iep1a_error is not None
        assert exc_info.value.iep1b_error is not None

    @pytest.mark.asyncio
    async def test_is_subclass_of_runtime_error(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        with pytest.raises(RuntimeError):
            await _invoke(backend_both_fail, session)

    @pytest.mark.asyncio
    async def test_two_service_invocation_rows_still_written(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        """Logging must happen even when both fail."""
        with pytest.raises(GeometryServiceError):
            await _invoke(backend_both_fail, session)
        assert session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_gate_not_called_when_both_fail(
        self, backend_both_fail: AsyncMock, session: MagicMock
    ) -> None:
        """run_geometry_selection() must NOT be called on total failure."""
        with patch(
            "services.eep_worker.app.geometry_invocation.run_geometry_selection"
        ) as mock_gate:
            with pytest.raises(GeometryServiceError):
                await _invoke(backend_both_fail, session)
            mock_gate.assert_not_called()


# ── 5 & 6. Timeout classification ─────────────────────────────────────────────


class TestTimeoutClassification:
    @pytest.mark.asyncio
    async def test_cold_start_timeout_classified_as_timeout(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.COLD_START_TIMEOUT, "cold start exceeded")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.status == "timeout"

    @pytest.mark.asyncio
    async def test_warm_inference_timeout_classified_as_timeout(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1b" in endpoint:
                raise BackendError(BackendErrorKind.WARM_INFERENCE_TIMEOUT, "inference timeout")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        b_record = next(r for r in records if r.service_name == "iep1b")
        assert b_record.status == "timeout"

    @pytest.mark.asyncio
    async def test_service_error_classified_as_error(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "service down")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.status == "error"

    @pytest.mark.asyncio
    async def test_timeout_error_message_stored(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.COLD_START_TIMEOUT, "deadline exceeded")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.error_message is not None
        assert len(a_record.error_message) > 0


# ── 7. Circuit breaker open → skip ────────────────────────────────────────────


class TestCircuitBreakerOpen:
    @pytest.mark.asyncio
    async def test_skipped_service_not_calling_backend(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        # Pre-open IEP1A circuit breaker (not yet past reset timeout)
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        await _invoke(backend, session, cbs=(a_cb, b_cb))
        # backend.call should be called only once (for IEP1B)
        assert backend.call.call_count == 1

    @pytest.mark.asyncio
    async def test_skipped_service_result_is_none(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        result = await _invoke(backend, session, cbs=(a_cb, b_cb))
        assert result.iep1a_result is None
        assert result.iep1a_skipped is True

    @pytest.mark.asyncio
    async def test_skipped_service_row_has_status_skipped(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        await _invoke(backend, session, cbs=(a_cb, b_cb))
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.status == "skipped"

    @pytest.mark.asyncio
    async def test_other_service_still_runs(self, backend: AsyncMock, session: MagicMock) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        result = await _invoke(backend, session, cbs=(a_cb, b_cb))
        assert result.iep1b_result is not None
        assert result.iep1b_skipped is False

    @pytest.mark.asyncio
    async def test_both_skipped_raises_geometry_service_error(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        for cb in (a_cb, b_cb):
            cb._state = CircuitState.OPEN
            cb._opened_at = time.monotonic()

        with pytest.raises(GeometryServiceError):
            await _invoke(backend, session, cbs=(a_cb, b_cb))

    @pytest.mark.asyncio
    async def test_skipped_invocation_has_circuit_open_error(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        result = await _invoke(backend, session, cbs=(a_cb, b_cb))
        assert result.iep1a_error is not None
        assert result.iep1a_error["kind"] == "circuit_open"


# ── 8. Circuit breaker trips on repeated failures ──────────────────────────────


class TestCircuitBreakerTrips:
    @pytest.mark.asyncio
    async def test_breaker_opens_after_threshold_failures(self, session: MagicMock) -> None:
        a_cb = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=1))
        b_cb = CircuitBreaker("iep1b")

        b = AsyncMock()
        b.call = AsyncMock(side_effect=BackendError(BackendErrorKind.SERVICE_ERROR, "fail"))

        with pytest.raises(GeometryServiceError):
            await invoke_geometry_services(
                job_id="job-1",
                page_number=1,
                lineage_id="lin-1",
                proxy_image_uri="s3://p.png",
                material_type="book",
                proxy_width=1024,
                proxy_height=800,
                iep1a_endpoint="http://iep1a:8001/v1/geometry",
                iep1b_endpoint="http://iep1b:8002/v1/geometry",
                iep1a_circuit_breaker=a_cb,
                iep1b_circuit_breaker=b_cb,
                backend=b,
                session=session,
            )

        # After 1 failure with threshold=1, IEP1A breaker should be OPEN.
        assert a_cb._state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_subsequent_call_to_open_breaker_is_skipped(self, session: MagicMock) -> None:
        a_cb = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=1))
        b_cb = CircuitBreaker("iep1b")
        backend_all_fail = AsyncMock()
        backend_all_fail.call = AsyncMock(
            side_effect=BackendError(BackendErrorKind.SERVICE_ERROR, "fail")
        )
        backend_partial = AsyncMock()

        async def partial_side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "iep1a fail")
            return _valid_response_dict()

        backend_partial.call = AsyncMock(side_effect=partial_side_effect)

        # First call trips IEP1A (threshold=1).
        with pytest.raises(GeometryServiceError):
            await invoke_geometry_services(
                job_id="j1",
                page_number=1,
                lineage_id="l1",
                proxy_image_uri="s3://x",
                material_type="book",
                proxy_width=512,
                proxy_height=400,
                iep1a_endpoint="http://iep1a:8001/v1/geometry",
                iep1b_endpoint="http://iep1b:8002/v1/geometry",
                iep1a_circuit_breaker=a_cb,
                iep1b_circuit_breaker=b_cb,
                backend=backend_all_fail,
                session=session,
            )

        session.reset_mock()

        # Second call with IEP1B only working: IEP1A is OPEN → skipped.
        result = await invoke_geometry_services(
            job_id="j1",
            page_number=1,
            lineage_id="l1",
            proxy_image_uri="s3://x",
            material_type="book",
            proxy_width=512,
            proxy_height=400,
            iep1a_endpoint="http://iep1a:8001/v1/geometry",
            iep1b_endpoint="http://iep1b:8002/v1/geometry",
            iep1a_circuit_breaker=a_cb,
            iep1b_circuit_breaker=b_cb,
            backend=backend_partial,
            session=session,
        )
        assert result.iep1a_skipped is True
        assert result.iep1b_result is not None


# ── 9. Circuit breaker recovery after cooldown ─────────────────────────────────


class TestCircuitBreakerRecovery:
    @pytest.mark.asyncio
    async def test_recovery_after_cooldown(self, backend: AsyncMock, session: MagicMock) -> None:
        a_cb, b_cb = _make_cbs()
        # Trip IEP1A and artificially age the opened_at well past reset timeout (60s).
        a_cb._state = CircuitState.OPEN
        a_cb._consecutive_failures = 5
        a_cb._opened_at = time.monotonic() - 1000  # 1000s ago — well past 60s reset

        # Backend returns success for both.
        result = await _invoke(backend, session, cbs=(a_cb, b_cb))

        # IEP1A should have transitioned OPEN → HALF_OPEN → CLOSED via allow_call + record_success.
        assert a_cb._state is CircuitState.CLOSED
        assert result.iep1a_result is not None

    @pytest.mark.asyncio
    async def test_recovery_resets_failure_count(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._consecutive_failures = 5
        a_cb._opened_at = time.monotonic() - 1000

        await _invoke(backend, session, cbs=(a_cb, b_cb))
        assert a_cb.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_probe_failure_reopens(self, session: MagicMock) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic() - 1000  # ready to HALF_OPEN

        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "probe failed")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)

        # IEP1A probe fails → circuit should re-open.
        result = await _invoke(b, session, cbs=(a_cb, b_cb))
        assert a_cb._state is CircuitState.OPEN
        assert result.iep1a_result is None


# ── 10. Malformed response ─────────────────────────────────────────────────────


class TestMalformedResponse:
    @pytest.mark.asyncio
    async def test_malformed_iep1a_treated_as_error(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                # Missing required fields → ValidationError
                return {"page_count": 1}
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        result = await _invoke(b, session)
        assert result.iep1a_result is None
        assert result.iep1a_error is not None
        assert result.iep1a_error["kind"] == "malformed_response"

    @pytest.mark.asyncio
    async def test_malformed_response_row_status_is_error(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                return {"junk": True}
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.status == "error"

    @pytest.mark.asyncio
    async def test_malformed_response_penalises_circuit_breaker(self, session: MagicMock) -> None:
        a_cb = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=1))
        b_cb = CircuitBreaker("iep1b")

        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                return {"invalid": "schema"}
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session, cbs=(a_cb, b_cb))
        # threshold=1 → should be OPEN after one malformed response
        assert a_cb._state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_both_malformed_raises_geometry_service_error(self, session: MagicMock) -> None:
        b = AsyncMock()
        b.call = AsyncMock(return_value={"invalid": True})
        with pytest.raises(GeometryServiceError):
            await _invoke(b, session)


# ── 11. Idempotency ────────────────────────────────────────────────────────────


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_two_calls_write_four_rows_total(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        cbs = _make_cbs()
        await _invoke(backend, session, cbs=cbs)
        await _invoke(backend, session, cbs=cbs)
        assert session.add.call_count == 4

    @pytest.mark.asyncio
    async def test_each_call_writes_exactly_two_rows(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        cbs = _make_cbs()
        session_first = MagicMock()
        session_second = MagicMock()
        await _invoke(backend, session_first, cbs=cbs)
        await _invoke(backend, session_second, cbs=cbs)
        assert session_first.add.call_count == 2
        assert session_second.add.call_count == 2

    @pytest.mark.asyncio
    async def test_repeated_calls_produce_independent_selection_results(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        cbs = _make_cbs()
        r1 = await _invoke(backend, MagicMock(), cbs=cbs)
        r2 = await _invoke(backend, MagicMock(), cbs=cbs)
        # Both should be valid and independent.
        assert r1.selection_result is not None
        assert r2.selection_result is not None
        assert r1.selection_result is not r2.selection_result


# ── 12. ServiceInvocation logging correctness ──────────────────────────────────


class TestServiceInvocationLogging:
    @pytest.mark.asyncio
    async def test_lineage_id_set_on_all_rows(self, backend: AsyncMock, session: MagicMock) -> None:
        await _invoke(backend, session)
        for record in _added_records(session):
            assert record.lineage_id == "lin-1"

    @pytest.mark.asyncio
    async def test_service_names_are_iep1a_and_iep1b(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        names = {r.service_name for r in _added_records(session)}
        assert names == {"iep1a", "iep1b"}

    @pytest.mark.asyncio
    async def test_invoked_at_is_set(self, backend: AsyncMock, session: MagicMock) -> None:
        await _invoke(backend, session)
        for record in _added_records(session):
            assert record.invoked_at is not None

    @pytest.mark.asyncio
    async def test_completed_at_is_set(self, backend: AsyncMock, session: MagicMock) -> None:
        await _invoke(backend, session)
        for record in _added_records(session):
            assert record.completed_at is not None

    @pytest.mark.asyncio
    async def test_processing_time_ms_is_set_for_success(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        for record in _added_records(session):
            assert record.processing_time_ms is not None
            assert record.processing_time_ms >= 0.0

    @pytest.mark.asyncio
    async def test_processing_time_ms_is_none_for_skipped(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        a_cb, b_cb = _make_cbs()
        a_cb._state = CircuitState.OPEN
        a_cb._opened_at = time.monotonic()

        await _invoke(backend, session, cbs=(a_cb, b_cb))
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.processing_time_ms is None

    @pytest.mark.asyncio
    async def test_error_message_none_on_success(
        self, backend: AsyncMock, session: MagicMock
    ) -> None:
        await _invoke(backend, session)
        for record in _added_records(session):
            assert record.error_message is None

    @pytest.mark.asyncio
    async def test_error_message_set_on_failure(self, session: MagicMock) -> None:
        b = AsyncMock()

        async def side_effect(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                raise BackendError(BackendErrorKind.SERVICE_ERROR, "connection refused")
            return _valid_response_dict()

        b.call = AsyncMock(side_effect=side_effect)
        await _invoke(b, session)
        records = _added_records(session)
        a_record = next(r for r in records if r.service_name == "iep1a")
        assert a_record.error_message is not None
        assert "connection refused" in a_record.error_message
