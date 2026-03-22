"""
tests/test_p4_worker_integration.py
-------------------------------------
Packet 4.8 — Worker integration tests.

Test IDs map to the roadmap simulation / contract registry:

  IEP1D Contract (CT-WKR-01-a):
    1.  RectifyRequest: valid payload validates
    2.  RectifyRequest: page_number=0 rejected
    3.  RectifyRequest: invalid material_type rejected
    4.  RectifyResponse: valid payload validates
    5.  RectifyResponse: rectification_confidence > 1.0 rejected
    6.  RectifyResponse: negative processing_time_ms rejected
    7.  RectifyResponse: negative skew_residual_before rejected
    8.  _call_iep1d: success → (RectifyResponse, None), invocation logged "success"
    9.  _call_iep1d: BackendError SERVICE_ERROR → (None, error_dict), status "error"
    10. _call_iep1d: COLD_START_TIMEOUT → (None, error_dict), status "timeout"
    11. _call_iep1d: WARM_INFERENCE_TIMEOUT → (None, error_dict), status "timeout"
    12. _call_iep1d: circuit breaker open → (None, circuit_open), status "skipped"
    13. _call_iep1d: malformed response (ValidationError) → (None, malformed_response)
    14. _call_iep1d: unexpected exception → (None, unexpected_error)

  Queue Worker Contract (CT-WKR-01-b):
    15. claim_task: task atomically in processing list, absent from main queue
    16. crash recovery: task stays in processing list when worker never acks
    17. worker_id recorded in CLAIMS_KEY
    18. fail_task: requeues with retry_count+1 when retries remain
    19. fail_task: dead-letters when retry_count >= max_retries
    20. queue fencing: reconciler acks tasks whose page state is terminal

  SIM-01 — First-pass structural disagreement:
    21. Structural disagreement → geometry_trust="low" → route_decision not "failed"
    22. route_decision="rectification" ≠ "failed" (safety invariant)
    23. NormalizationOutcome.route=="rescue_required" when geometry_route_decision="rectification"

  SIM-02 — Second-pass structural disagreement:
    24. run_rescue_flow structural_disagreement_post_rectification → pending_human_correction
    25. branch_response is None when rescue exits before normalization (IEP1D fails)

  SIM-03 — Timeout and cold-start timeout handling:
    26. COLD_START_TIMEOUT → status="timeout" (distinct from "error")
    27. WARM_INFERENCE_TIMEOUT → status="timeout"
    28. SERVICE_ERROR BackendError → status="error" (not timeout)
    29. One service timeout → other service succeeds → gate called (partial-failure path)

  SIM-04 — Malformed model response:
    30. Malformed geometry response → status="error", circuit breaker penalised
    31. Both services malformed → GeometryServiceError raised (no silent success)
    32. IEP1D malformed → (None, malformed_response), circuit breaker penalised

  SIM-05 — Redis reconnect recovery:
    33. Processing list persists across fake disconnect (data not lost)
    34. Reconciler: queued page re-enqueued after simulated reconnect + reconcile pass

  SIM-06 — Worker crash mid-task:
    35. Task claimed but never acked → still in processing list (at-least-once guarantee)
    36. Reconciler detects stale preprocessing task and requeues it
    37. Reconciler does NOT mutate page state (DB authoritative)

  SIM-07 — Split retry / idempotency:
    38. left child has sub_page_index==0, right has sub_page_index==1
    39. Right child processed regardless of left child failure
    40. SplitOutcome.duration_ms is non-negative

  Integration — Happy path:
    41. invoke_geometry_services: both succeed → selection_result populated
    42. _decide_route: geometry_route_decision="accepted" + validation passed → "accept_now"
    43. _decide_route: geometry_route_decision="rectification" → "rescue_required"
    44. _decide_route: validation failed → "rescue_required" regardless of geometry trust
    45. decide_ptiff_qa_route: manual → ptiff_qa_pending
    46. decide_ptiff_qa_route: auto_continue + preprocess → accepted
    47. decide_ptiff_qa_route: auto_continue + layout → layout_detection

  Integration — Rescue path:
    48. run_rescue_flow: IEP1D success + second pass accepted → route="accept_now"
    49. run_rescue_flow: IEP1D failure → route="pending_human_correction"

  Integration — Failure classification:
    50. OtiffHashMismatchError raised when hash differs from prior lineage
    51. GeometryServiceError carries iep1a_error and iep1b_error
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import numpy as np
import pytest
from pydantic import ValidationError

from services.eep.app.db.models import JobPage
from services.eep.app.queue import (
    CLAIMS_KEY,
    MAX_TASK_RETRIES,
    claim_task,
    enqueue_page_task,
    fail_task,
)
from services.eep_recovery.app.reconciler import ReconcilerConfig, reconcile_once
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
from services.eep_worker.app.intake import OtiffHashMismatchError, check_hash_consistency
from services.eep_worker.app.normalization_step import _decide_route
from services.eep_worker.app.rescue_step import RescueOutcome, _call_iep1d, run_rescue_flow
from services.eep_worker.app.split_step import decide_ptiff_qa_route, run_split_normalization
from shared.gpu.backend import BackendError, BackendErrorKind
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.iep1d import RectifyRequest, RectifyResponse
from shared.schemas.preprocessing import PreprocessBranchResponse
from shared.schemas.queue import (
    QUEUE_DEAD_LETTER,
    QUEUE_PAGE_TASKS,
    QUEUE_PAGE_TASKS_PROCESSING,
    PageTask,
)

# ── Shared test constants ───────────────────────────────────────────────────────

_JOB_ID = "job-p48-test"
_PAGE_ID = "page-p48-001"
_LINEAGE_ID = "lineage-p48-001"
_PAGE_NUMBER = 3

_IEP1A_EP = "http://iep1a:8001/v1/geometry"
_IEP1B_EP = "http://iep1b:8002/v1/geometry"
_IEP1D_EP = "http://iep1d:8003/v1/rectify"

_ARTIFACT_URI = "local://normalized/page3.tiff"
_RECTIFIED_URI = "local://rectified/page3.tiff"
_PROXY_URI = "local://proxy/page3.png"
_OUTPUT_URI = "local://rescue/page3.tiff"


# ── Shared helpers ──────────────────────────────────────────────────────────────


def _make_geometry_response_dict(
    split_required: bool = False,
    page_count: int = 1,
    confidence: float = 0.93,
    agreement: float = 0.96,
) -> dict[str, Any]:
    """Return a valid GeometryResponse dict."""
    pages = [
        {
            "region_id": f"p{i}",
            "geometry_type": "bbox",
            "corners": None,
            "bbox": [10, 10, 90, 90],
            "confidence": confidence,
            "page_area_fraction": 0.80,
        }
        for i in range(max(1, page_count))
    ]
    return {
        "page_count": page_count,
        "pages": pages,
        "split_required": split_required,
        "split_x": 500 if split_required else None,
        "geometry_confidence": confidence,
        "tta_structural_agreement_rate": agreement,
        "tta_prediction_variance": 0.02,
        "tta_passes": 3,
        "uncertainty_flags": [],
        "warnings": [],
        "processing_time_ms": 90.0,
    }


def _make_geometry_obj(
    split_required: bool = False,
    page_count: int = 1,
    confidence: float = 0.93,
) -> GeometryResponse:
    pages = [
        PageRegion(
            region_id=f"p{i}",
            geometry_type="bbox",
            bbox=(10, 10, 90, 90),
            corners=None,
            confidence=confidence,
            page_area_fraction=0.80,
        )
        for i in range(max(1, page_count))
    ]
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=500 if split_required else None,
        geometry_confidence=confidence,
        tta_structural_agreement_rate=0.96,
        tta_prediction_variance=0.02,
        tta_passes=3,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=90.0,
    )


def _valid_rectify_response_dict(uri: str = _RECTIFIED_URI) -> dict[str, Any]:
    return {
        "rectified_image_uri": uri,
        "rectification_confidence": 0.90,
        "skew_residual_before": 3.5,
        "skew_residual_after": 0.3,
        "border_score_before": 0.70,
        "border_score_after": 0.88,
        "processing_time_ms": 1100.0,
        "warnings": [],
    }


def _make_test_image(h: int = 200, w: int = 300) -> np.ndarray:
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _make_page_task(
    page_id: str = _PAGE_ID,
    job_id: str = _JOB_ID,
    retry_count: int = 0,
) -> PageTask:
    return PageTask(
        task_id="task-p48-aaa",
        job_id=job_id,
        page_id=page_id,
        page_number=_PAGE_NUMBER,
        retry_count=retry_count,
    )


def _make_fakeredis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def _make_session_with_status(status: str) -> MagicMock:
    """Return a mocked SQLAlchemy Session returning a JobPage with given status."""
    page = MagicMock(spec=JobPage)
    page.status = status
    page.status_updated_at = datetime.now(tz=UTC)
    page.created_at = datetime.now(tz=UTC)
    session = MagicMock()
    session.get = MagicMock(return_value=page)
    session.add = MagicMock()
    return session


def _make_cbs() -> tuple[CircuitBreaker, CircuitBreaker, CircuitBreaker]:
    """Return fresh (iep1a_cb, iep1b_cb, iep1d_cb)."""
    a = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=5))
    b = CircuitBreaker("iep1b", CircuitBreakerConfig(failure_threshold=5))
    d = CircuitBreaker("iep1d", CircuitBreakerConfig(failure_threshold=5))
    return a, b, d


# ── Module-level async helpers ──────────────────────────────────────────────────


async def _call_iep1d_helper(
    backend_side_effect: Any = None,
    backend_return: dict[str, Any] | None = None,
    cb: CircuitBreaker | None = None,
) -> tuple[RectifyResponse | None, dict[str, Any] | None, MagicMock, CircuitBreaker]:
    """Run _call_iep1d with a configurable backend. Returns (response, error, session, cb)."""
    backend = AsyncMock()
    if backend_side_effect is not None:
        backend.call = AsyncMock(side_effect=backend_side_effect)
    else:
        backend.call = AsyncMock(return_value=backend_return or _valid_rectify_response_dict())

    session = MagicMock()
    session.add = MagicMock()

    if cb is None:
        cb = CircuitBreaker("iep1d", CircuitBreakerConfig(failure_threshold=5))

    response, error = await _call_iep1d(
        artifact_uri=_ARTIFACT_URI,
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        material_type="book",
        endpoint=_IEP1D_EP,
        backend=backend,
        cb=cb,
        lineage_id=_LINEAGE_ID,
        session=session,
    )
    return response, error, session, cb


async def _invoke_geometry(
    side_effect_a: Any = None,
    side_effect_b: Any = None,
    return_a: dict[str, Any] | None = None,
    return_b: dict[str, Any] | None = None,
) -> tuple[Any, MagicMock]:
    """Run invoke_geometry_services with one or two mocked services. Returns (result, session)."""

    async def _fake_call(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if "iep1a" in endpoint:
            if side_effect_a is not None:
                raise side_effect_a
            return return_a or _make_geometry_response_dict()
        else:
            if side_effect_b is not None:
                raise side_effect_b
            return return_b or _make_geometry_response_dict()

    backend = AsyncMock()
    backend.call = _fake_call

    session = MagicMock()
    session.add = MagicMock()
    cb_a, cb_b, _ = _make_cbs()

    result = await invoke_geometry_services(
        job_id=_JOB_ID,
        page_number=_PAGE_NUMBER,
        lineage_id=_LINEAGE_ID,
        proxy_image_uri=_PROXY_URI,
        material_type="book",
        proxy_width=300,
        proxy_height=200,
        iep1a_endpoint=_IEP1A_EP,
        iep1b_endpoint=_IEP1B_EP,
        iep1a_circuit_breaker=cb_a,
        iep1b_circuit_breaker=cb_b,
        backend=backend,
        session=session,
    )
    return result, session


# ══════════════════════════════════════════════════════════════════════════════
# CT-WKR-01-a  IEP1D Contract — schema validation
# ══════════════════════════════════════════════════════════════════════════════


class TestIep1dSchemas:
    """Tests 1–7: RectifyRequest and RectifyResponse schema contract."""

    def test_rectify_request_valid(self) -> None:
        """Test 1: Valid RectifyRequest validates without error."""
        req = RectifyRequest(
            job_id=_JOB_ID,
            page_number=1,
            image_uri=_ARTIFACT_URI,
            material_type="book",
        )
        assert req.job_id == _JOB_ID
        assert req.page_number == 1
        assert req.material_type == "book"

    def test_rectify_request_page_number_zero_rejected(self) -> None:
        """Test 2: page_number=0 violates ge=1 constraint."""
        with pytest.raises(ValidationError):
            RectifyRequest(
                job_id=_JOB_ID,
                page_number=0,
                image_uri=_ARTIFACT_URI,
                material_type="book",
            )

    def test_rectify_request_invalid_material_type_rejected(self) -> None:
        """Test 3: material_type not in the allowed literal set is rejected."""
        with pytest.raises(ValidationError):
            RectifyRequest(
                job_id=_JOB_ID,
                page_number=1,
                image_uri=_ARTIFACT_URI,
                material_type="microfilm",  # type: ignore[arg-type]
            )

    def test_rectify_response_valid(self) -> None:
        """Test 4: Valid RectifyResponse validates all fields."""
        resp = RectifyResponse.model_validate(_valid_rectify_response_dict())
        assert resp.rectified_image_uri == _RECTIFIED_URI
        assert 0.0 <= resp.rectification_confidence <= 1.0
        assert resp.processing_time_ms >= 0.0
        assert isinstance(resp.warnings, list)

    def test_rectify_response_confidence_above_one_rejected(self) -> None:
        """Test 5: rectification_confidence > 1.0 rejected."""
        d = _valid_rectify_response_dict()
        d["rectification_confidence"] = 1.5
        with pytest.raises(ValidationError):
            RectifyResponse.model_validate(d)

    def test_rectify_response_negative_processing_time_rejected(self) -> None:
        """Test 6: Negative processing_time_ms rejected."""
        d = _valid_rectify_response_dict()
        d["processing_time_ms"] = -1.0
        with pytest.raises(ValidationError):
            RectifyResponse.model_validate(d)

    def test_rectify_response_negative_skew_residual_rejected(self) -> None:
        """Test 7: Negative skew_residual_before rejected."""
        d = _valid_rectify_response_dict()
        d["skew_residual_before"] = -0.5
        with pytest.raises(ValidationError):
            RectifyResponse.model_validate(d)


# ══════════════════════════════════════════════════════════════════════════════
# CT-WKR-01-a  IEP1D Contract — _call_iep1d behavior
# ══════════════════════════════════════════════════════════════════════════════


class TestCallIep1d:
    """Tests 8–14: _call_iep1d error handling and logging contract."""

    @pytest.mark.asyncio
    async def test_iep1d_success(self) -> None:
        """Test 8: Success → (RectifyResponse, None), ServiceInvocation logged "success"."""
        resp, err, session, cb = await _call_iep1d_helper()
        assert resp is not None
        assert isinstance(resp, RectifyResponse)
        assert err is None
        session.add.assert_called_once()
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "success"
        assert logged.service_name == "iep1d"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_iep1d_service_error(self) -> None:
        """Test 9: BackendError(SERVICE_ERROR) → (None, error_dict), status "error"."""
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "svc error")
        resp, err, session, cb = await _call_iep1d_helper(backend_side_effect=exc)
        assert resp is None
        assert err is not None
        assert err["kind"] == BackendErrorKind.SERVICE_ERROR.value
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "error"

    @pytest.mark.asyncio
    async def test_iep1d_cold_start_timeout(self) -> None:
        """Test 10: COLD_START_TIMEOUT → status "timeout" in invocation log."""
        exc = BackendError(BackendErrorKind.COLD_START_TIMEOUT, "cold timeout")
        resp, err, session, cb = await _call_iep1d_helper(backend_side_effect=exc)
        assert resp is None
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "timeout"

    @pytest.mark.asyncio
    async def test_iep1d_warm_inference_timeout(self) -> None:
        """Test 11: WARM_INFERENCE_TIMEOUT → status "timeout" in invocation log."""
        exc = BackendError(BackendErrorKind.WARM_INFERENCE_TIMEOUT, "warm timeout")
        resp, err, session, cb = await _call_iep1d_helper(backend_side_effect=exc)
        assert resp is None
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "timeout"

    @pytest.mark.asyncio
    async def test_iep1d_circuit_breaker_open(self) -> None:
        """Test 12: Circuit breaker open → (None, circuit_open dict), status "skipped"."""
        backend = AsyncMock()
        backend.call = AsyncMock(return_value=_valid_rectify_response_dict())
        session = MagicMock()
        session.add = MagicMock()

        cb = CircuitBreaker("iep1d", CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure(BackendErrorKind.SERVICE_ERROR)
        assert cb.state == CircuitState.OPEN

        resp, err = await _call_iep1d(
            artifact_uri=_ARTIFACT_URI,
            job_id=_JOB_ID,
            page_number=_PAGE_NUMBER,
            material_type="book",
            endpoint=_IEP1D_EP,
            backend=backend,
            cb=cb,
            lineage_id=_LINEAGE_ID,
            session=session,
        )
        assert resp is None
        assert err is not None
        assert "circuit" in err["kind"]
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "skipped"
        backend.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_iep1d_malformed_response(self) -> None:
        """Test 13: Malformed response from IEP1D → (None, malformed_response)."""
        resp, err, session, cb = await _call_iep1d_helper(backend_return={"bad_field": 1})
        assert resp is None
        assert err is not None
        assert err["kind"] == "malformed_response"
        logged: Any = session.add.call_args[0][0]
        assert logged.status == "error"

    @pytest.mark.asyncio
    async def test_iep1d_unexpected_exception(self) -> None:
        """Test 14: Unexpected exception → (None, unexpected_error)."""
        exc = RuntimeError("unexpected!")
        resp, err, session, cb = await _call_iep1d_helper(backend_side_effect=exc)
        assert resp is None
        assert err is not None
        assert err["kind"] == "unexpected_error"


# ══════════════════════════════════════════════════════════════════════════════
# CT-WKR-01-b  Queue Worker Contract
# ══════════════════════════════════════════════════════════════════════════════


class TestQueueWorkerContract:
    """Tests 15–20: Queue ownership, fencing, and recovery contract."""

    def test_claim_atomically_moves_task(self) -> None:
        """Test 15: Claimed task appears in processing list, absent from main queue."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="worker-1", timeout=1)
        assert claimed is not None
        assert r.llen(QUEUE_PAGE_TASKS) == 0
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_crash_recovery_task_stays_in_processing(self) -> None:
        """Test 16: Task remains in processing list when worker crashes (never acks)."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="worker-crash", timeout=1)
        assert claimed is not None
        # Simulate crash: no ack_task() called
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_worker_id_recorded_in_claims_hash(self) -> None:
        """Test 17: Claiming worker's ID stored in CLAIMS_KEY hash."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="worker-abc", timeout=1)
        assert claimed is not None
        claim_entry = r.hget(CLAIMS_KEY, task.task_id)
        assert isinstance(claim_entry, str)
        assert "worker-abc" in claim_entry

    def test_fail_task_requeues_with_incremented_retry(self) -> None:
        """Test 18: fail_task re-enqueues with retry_count+1 when retries remain."""
        r = _make_fakeredis()
        task = _make_page_task(retry_count=0)
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="w1", timeout=1)
        assert claimed is not None
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)

        assert r.llen(QUEUE_PAGE_TASKS) == 1
        raw = cast(str, r.rpop(QUEUE_PAGE_TASKS))
        retried = PageTask.model_validate_json(raw)
        assert retried.retry_count == 1

    def test_fail_task_dead_letters_at_max_retries(self) -> None:
        """Test 19: fail_task moves to dead-letter when retry_count >= max_retries."""
        r = _make_fakeredis()
        task = _make_page_task(retry_count=MAX_TASK_RETRIES)
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="w1", timeout=1)
        assert claimed is not None
        fail_task(r, claimed, max_retries=MAX_TASK_RETRIES)

        assert r.llen(QUEUE_PAGE_TASKS) == 0
        assert r.llen(QUEUE_DEAD_LETTER) == 1

    def test_reconciler_acks_terminal_task(self) -> None:
        """Test 20: reconcile_once removes task from processing list when page is terminal."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, worker_id="w1", timeout=1)
        assert claimed is not None
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

        session = _make_session_with_status("accepted")
        cfg = ReconcilerConfig(task_timeout_seconds=900.0)
        result = reconcile_once(r, session, cfg)

        assert result.acked_terminal == 1
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0


# ══════════════════════════════════════════════════════════════════════════════
# SIM-01  First-pass structural disagreement
# ══════════════════════════════════════════════════════════════════════════════


class TestSim01FirstPassDisagreement:
    """Tests 21–23: Structural disagreement → low trust → rescue_required."""

    def test_disagreement_route_not_failed(self) -> None:
        """Test 21–22: Structural disagreement NEVER routes to "failed" (safety invariant)."""
        from services.eep.app.gates.geometry_selection import run_geometry_selection

        # IEP1A: single page; IEP1B: two-page spread → structural disagreement
        geo_a = _make_geometry_obj(page_count=1, confidence=0.92)
        geo_b = _make_geometry_obj(page_count=2, split_required=True, confidence=0.90)

        result = run_geometry_selection(
            iep1a_response=geo_a,
            iep1b_response=geo_b,
            material_type="book",
            proxy_width=300,
            proxy_height=200,
        )
        # With structural disagreement, trust must be low (not high)
        assert result.geometry_trust != "high"
        # Route must never be "failed" — safety invariant
        assert result.route_decision != "failed"  # type: ignore[comparison-overlap]
        assert result.route_decision in ("rectification", "pending_human_correction")

    def test_low_trust_route_decision_produces_rescue_required(self) -> None:
        """Test 23: _decide_route with "rectification" → rescue_required."""
        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        validation = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.85,
            signal_scores=None,
            soft_passed=True,
            passed=True,
        )
        # Even when validation passes, low geometry trust forces rescue
        route = _decide_route("rectification", validation)
        assert route == "rescue_required"


# ══════════════════════════════════════════════════════════════════════════════
# SIM-02  Second-pass structural disagreement
# ══════════════════════════════════════════════════════════════════════════════


class TestSim02SecondPassDisagreement:
    """Tests 24–25: Second-pass routing and branch_response state."""

    @pytest.mark.asyncio
    async def test_second_pass_disagreement_routes_to_pending(self) -> None:
        """Test 24: Rescue flow: structural disagreement post-rectification → PHC."""
        from services.eep.app.gates.geometry_selection import GeometrySelectionResult

        # Second-pass selection has structural disagreement
        sel = GeometrySelectionResult(
            selected=None,
            geometry_trust="low",
            selection_reason="structural_disagreement",
            route_decision="rectification",
            review_reason=None,
            structural_agreement=False,
            sanity_results={},
            split_confidence_per_model=None,
            tta_variance_per_model={},
            page_area_preference_triggered=False,
        )
        inv_result = GeometryInvocationResult(
            iep1a_result=_make_geometry_obj(),
            iep1b_result=_make_geometry_obj(),
            iep1a_error=None,
            iep1b_error=None,
            iep1a_skipped=False,
            iep1b_skipped=False,
            iep1a_duration_ms=50.0,
            iep1b_duration_ms=50.0,
            selection_result=sel,
        )

        import cv2

        tiff_bytes = cv2.imencode(".tiff", _make_test_image())[1].tobytes()
        storage = MagicMock()
        storage.get_bytes = MagicMock(return_value=tiff_bytes)
        storage.put_bytes = MagicMock()

        backend = AsyncMock()
        backend.call = AsyncMock(return_value=_valid_rectify_response_dict())

        with patch(
            "services.eep_worker.app.rescue_step.invoke_geometry_services",
            new_callable=AsyncMock,
            return_value=inv_result,
        ):
            cb_a, cb_b, cb_d = _make_cbs()
            outcome = await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                material_type="book",
                rectified_proxy_uri=_PROXY_URI,
                rescue_output_uri=_OUTPUT_URI,
                iep1d_endpoint=_IEP1D_EP,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=MagicMock(),
                storage=storage,
                image_loader=MagicMock(),
            )

        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason is not None and "disagreement" in outcome.review_reason

    @pytest.mark.asyncio
    async def test_iep1d_failure_branch_response_is_none(self) -> None:
        """Test 25: IEP1D failure → branch_response is None (exit before normalization)."""
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "fail")
        cb_a, cb_b, cb_d = _make_cbs()
        backend = AsyncMock()
        backend.call = AsyncMock(side_effect=exc)

        outcome = await run_rescue_flow(
            artifact_uri=_ARTIFACT_URI,
            job_id=_JOB_ID,
            page_number=_PAGE_NUMBER,
            lineage_id=_LINEAGE_ID,
            material_type="book",
            rectified_proxy_uri=_PROXY_URI,
            rescue_output_uri=_OUTPUT_URI,
            iep1d_endpoint=_IEP1D_EP,
            iep1a_endpoint=_IEP1A_EP,
            iep1b_endpoint=_IEP1B_EP,
            iep1d_circuit_breaker=cb_d,
            iep1a_circuit_breaker=cb_a,
            iep1b_circuit_breaker=cb_b,
            backend=backend,
            session=MagicMock(),
            storage=MagicMock(),
            image_loader=MagicMock(),
        )
        assert outcome.branch_response is None
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"


# ══════════════════════════════════════════════════════════════════════════════
# SIM-03  Timeout and cold-start timeout handling
# ══════════════════════════════════════════════════════════════════════════════


class TestSim03TimeoutHandling:
    """Tests 26–29: Timeout vs error classification and partial failure behavior."""

    @pytest.mark.asyncio
    async def test_cold_start_timeout_logged_as_timeout(self) -> None:
        """Test 26: COLD_START_TIMEOUT → ServiceInvocation status "timeout"."""
        exc = BackendError(BackendErrorKind.COLD_START_TIMEOUT, "cold")
        result, session = await _invoke_geometry(side_effect_a=exc)
        logged_statuses = [call[0][0].status for call in session.add.call_args_list]
        assert "timeout" in logged_statuses

    @pytest.mark.asyncio
    async def test_warm_inference_timeout_logged_as_timeout(self) -> None:
        """Test 27: WARM_INFERENCE_TIMEOUT → ServiceInvocation status "timeout"."""
        exc = BackendError(BackendErrorKind.WARM_INFERENCE_TIMEOUT, "warm")
        result, session = await _invoke_geometry(side_effect_a=exc)
        logged_statuses = [call[0][0].status for call in session.add.call_args_list]
        assert "timeout" in logged_statuses

    @pytest.mark.asyncio
    async def test_service_error_logged_as_error_not_timeout(self) -> None:
        """Test 28: SERVICE_ERROR BackendError → status "error", not "timeout"."""
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "svc")
        result, session = await _invoke_geometry(side_effect_a=exc)
        logged_statuses = [call[0][0].status for call in session.add.call_args_list]
        assert "error" in logged_statuses
        assert "timeout" not in logged_statuses

    @pytest.mark.asyncio
    async def test_one_service_timeout_other_continues(self) -> None:
        """Test 29: One service times out, other succeeds → selection_result populated."""
        exc = BackendError(BackendErrorKind.COLD_START_TIMEOUT, "timeout_a")
        result, session = await _invoke_geometry(side_effect_a=exc)
        assert result.selection_result is not None
        assert result.iep1a_result is None
        assert result.iep1b_result is not None


# ══════════════════════════════════════════════════════════════════════════════
# SIM-04  Malformed model response
# ══════════════════════════════════════════════════════════════════════════════


class TestSim04MalformedModelResponse:
    """Tests 30–32: Malformed geometry and IEP1D response handling."""

    @pytest.mark.asyncio
    async def test_malformed_geometry_status_error_and_cb_penalised(self) -> None:
        """Test 30: Malformed geometry response → status "error", circuit breaker penalised."""

        async def _fake_call(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "iep1a" in endpoint:
                return {"bad": "data"}  # does not match GeometryResponse
            return _make_geometry_response_dict()

        backend = AsyncMock()
        backend.call = _fake_call
        session = MagicMock()
        session.add = MagicMock()
        cb_a = CircuitBreaker("iep1a", CircuitBreakerConfig(failure_threshold=1))
        cb_b = CircuitBreaker("iep1b", CircuitBreakerConfig(failure_threshold=5))

        await invoke_geometry_services(
            job_id=_JOB_ID,
            page_number=_PAGE_NUMBER,
            lineage_id=_LINEAGE_ID,
            proxy_image_uri=_PROXY_URI,
            material_type="book",
            proxy_width=300,
            proxy_height=200,
            iep1a_endpoint=_IEP1A_EP,
            iep1b_endpoint=_IEP1B_EP,
            iep1a_circuit_breaker=cb_a,
            iep1b_circuit_breaker=cb_b,
            backend=backend,
            session=session,
        )
        # failure_threshold=1 trips circuit breaker on first failure
        assert cb_a.state == CircuitState.OPEN
        iep1a_calls = [
            call[0][0] for call in session.add.call_args_list if call[0][0].service_name == "iep1a"
        ]
        assert any(c.status == "error" for c in iep1a_calls)

    @pytest.mark.asyncio
    async def test_both_malformed_raises_geometry_service_error(self) -> None:
        """Test 31: Both services return malformed responses → GeometryServiceError raised."""
        backend = AsyncMock()
        backend.call = AsyncMock(return_value={"not_valid": True})
        session = MagicMock()
        session.add = MagicMock()
        cb_a, cb_b, _ = _make_cbs()

        with pytest.raises(GeometryServiceError):
            await invoke_geometry_services(
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                proxy_image_uri=_PROXY_URI,
                material_type="book",
                proxy_width=300,
                proxy_height=200,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=session,
            )

    @pytest.mark.asyncio
    async def test_iep1d_malformed_circuit_breaker_penalised(self) -> None:
        """Test 32: Malformed IEP1D response penalises circuit breaker."""
        cb = CircuitBreaker("iep1d", CircuitBreakerConfig(failure_threshold=1))
        resp, err, session, cb = await _call_iep1d_helper(backend_return={"garbage": 99}, cb=cb)
        assert resp is None
        assert err is not None and err["kind"] == "malformed_response"
        assert cb.state == CircuitState.OPEN


# ══════════════════════════════════════════════════════════════════════════════
# SIM-05  Redis reconnect recovery
# ══════════════════════════════════════════════════════════════════════════════


class TestSim05RedisReconnect:
    """Tests 33–34: Processing list survives reconnect; reconciler can recover."""

    def test_processing_list_persists_across_reconnect(self) -> None:
        """Test 33: Data in processing list is not lost on client reconnect."""
        server = fakeredis.FakeServer()
        r1 = fakeredis.FakeRedis(server=server, decode_responses=True)

        task = _make_page_task()
        enqueue_page_task(r1, task)
        claimed = claim_task(r1, worker_id="worker-1", timeout=1)
        assert claimed is not None
        assert r1.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

        # Simulate disconnect + reconnect: open new client to same server
        r2 = fakeredis.FakeRedis(server=server, decode_responses=True)
        assert r2.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1

    def test_reconciler_requeues_queued_page_after_reconnect(self) -> None:
        """Test 34: After reconnect, reconciler requeues a page stuck in "queued" state."""
        server = fakeredis.FakeServer()
        r1 = fakeredis.FakeRedis(server=server, decode_responses=True)

        task = _make_page_task()
        enqueue_page_task(r1, task)
        claimed = claim_task(r1, worker_id="worker-crash", timeout=1)
        assert claimed is not None
        # Worker crashed — task still in processing, DB says "queued"

        r2 = fakeredis.FakeRedis(server=server, decode_responses=True)
        session = _make_session_with_status("queued")
        cfg = ReconcilerConfig(
            task_timeout_seconds=900.0,
            max_task_retries=MAX_TASK_RETRIES,
        )
        result = reconcile_once(r2, session, cfg)

        assert result.requeued_stale == 1
        assert r2.llen(QUEUE_PAGE_TASKS) == 1
        assert r2.llen(QUEUE_PAGE_TASKS_PROCESSING) == 0


# ══════════════════════════════════════════════════════════════════════════════
# SIM-06  Worker crash mid-task
# ══════════════════════════════════════════════════════════════════════════════


class TestSim06WorkerCrash:
    """Tests 35–37: Task persists in processing list; reconciler recovers without DB mutation."""

    def test_task_stays_in_processing_list_on_crash(self) -> None:
        """Test 35: Claimed but never-acked task remains in processing list."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)

        claimed = claim_task(r, worker_id="w-crash", timeout=1)
        assert claimed is not None
        # Crash: no ack
        assert r.llen(QUEUE_PAGE_TASKS_PROCESSING) == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 0

    def test_reconciler_requeues_stale_preprocessing_task(self) -> None:
        """Test 36: Reconciler detects stale preprocessing task and requeues it."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, worker_id="w-stale", timeout=1)
        assert claimed is not None

        page = MagicMock(spec=JobPage)
        page.status = "preprocessing"
        page.status_updated_at = datetime.now(tz=UTC) - timedelta(seconds=2000)
        page.created_at = datetime.now(tz=UTC) - timedelta(seconds=2000)
        session = MagicMock()
        session.get = MagicMock(return_value=page)

        cfg = ReconcilerConfig(task_timeout_seconds=900.0)
        result = reconcile_once(r, session, cfg)

        assert result.requeued_stale == 1
        assert r.llen(QUEUE_PAGE_TASKS) == 1

    def test_reconciler_does_not_mutate_page_state(self) -> None:
        """Test 37: reconcile_once never calls session.add() — DB is authoritative."""
        r = _make_fakeredis()
        task = _make_page_task()
        enqueue_page_task(r, task)
        claimed = claim_task(r, worker_id="w-check", timeout=1)
        assert claimed is not None

        page = MagicMock(spec=JobPage)
        page.status = "preprocessing"
        page.status_updated_at = datetime.now(tz=UTC) - timedelta(seconds=2000)
        page.created_at = datetime.now(tz=UTC) - timedelta(seconds=2000)
        session = MagicMock()
        session.get = MagicMock(return_value=page)
        session.add = MagicMock()

        reconcile_once(r, session, ReconcilerConfig(task_timeout_seconds=900.0))

        # Reconciler must not mutate page state — only moves Redis queue entries
        session.add.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# SIM-07  Split retry / idempotency
# ══════════════════════════════════════════════════════════════════════════════


class TestSim07SplitIdempotency:
    """Tests 38–40: Split children have correct indices and are processed independently."""

    def _make_norm_outcome(self, route: str) -> Any:
        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )
        from services.eep_worker.app.normalization_step import NormalizationOutcome

        hard = ArtifactHardCheckResult(
            passed=route == "accept_now",
            failed_checks=[] if route == "accept_now" else ["dimensions_consistent"],
        )
        val = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.85 if route == "accept_now" else None,
            signal_scores=None,
            soft_passed=route == "accept_now" or None,
            passed=route == "accept_now",
        )
        branch = MagicMock(spec=PreprocessBranchResponse)
        branch.processed_image_uri = "local://out.tiff"
        return NormalizationOutcome(
            branch_response=branch,
            validation_result=val,
            route=cast(Literal["accept_now", "rescue_required"], route),
            duration_ms=10.0,
        )

    @pytest.mark.asyncio
    async def test_left_and_right_sub_page_indices(self) -> None:
        """Test 38: left.sub_page_index==0, right.sub_page_index==1."""
        norms = [self._make_norm_outcome("accept_now"), self._make_norm_outcome("accept_now")]

        with patch(
            "services.eep_worker.app.split_step.run_normalization_and_first_validation",
            side_effect=norms,
        ):
            cb_a, cb_b, cb_d = _make_cbs()
            outcome = await run_split_normalization(
                full_res_image=_make_test_image(),
                selected_geometry=_make_geometry_obj(split_required=True, page_count=2),
                selected_model="iep1a",
                proxy_width=300,
                proxy_height=200,
                left_output_uri="local://left.tiff",
                right_output_uri="local://right.tiff",
                left_rescue_output_uri="local://left_rescue.tiff",
                right_rescue_output_uri="local://right_rescue.tiff",
                left_rectified_proxy_uri="local://left_proxy.png",
                right_rectified_proxy_uri="local://right_proxy.png",
                storage=MagicMock(),
                image_loader=MagicMock(),
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                material_type="book",
                iep1d_endpoint=_IEP1D_EP,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=AsyncMock(),
                session=MagicMock(),
            )

        assert outcome.left.sub_page_index == 0
        assert outcome.right.sub_page_index == 1

    @pytest.mark.asyncio
    async def test_right_child_processed_regardless_of_left_failure(self) -> None:
        """Test 39: Left rescue fails → right child still reaches accept_now."""
        left_norm = self._make_norm_outcome("rescue_required")
        right_norm = self._make_norm_outcome("accept_now")

        rescue_outcome = RescueOutcome(
            route="pending_human_correction",
            review_reason="rectification_failed",
            branch_response=None,
            validation_result=None,
            rectify_response=None,
            second_selection_result=None,
            duration_ms=10.0,
        )

        with (
            patch(
                "services.eep_worker.app.split_step.run_normalization_and_first_validation",
                side_effect=[left_norm, right_norm],
            ),
            patch(
                "services.eep_worker.app.split_step.run_rescue_flow",
                new_callable=AsyncMock,
                return_value=rescue_outcome,
            ),
        ):
            cb_a, cb_b, cb_d = _make_cbs()
            outcome = await run_split_normalization(
                full_res_image=_make_test_image(),
                selected_geometry=_make_geometry_obj(split_required=True, page_count=2),
                selected_model="iep1a",
                proxy_width=300,
                proxy_height=200,
                left_output_uri="local://left.tiff",
                right_output_uri="local://right.tiff",
                left_rescue_output_uri="local://left_rescue.tiff",
                right_rescue_output_uri="local://right_rescue.tiff",
                left_rectified_proxy_uri="local://left_proxy.png",
                right_rectified_proxy_uri="local://right_proxy.png",
                storage=MagicMock(),
                image_loader=MagicMock(),
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                material_type="book",
                iep1d_endpoint=_IEP1D_EP,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=AsyncMock(),
                session=MagicMock(),
            )

        assert outcome.left.route == "pending_human_correction"
        assert outcome.right.route == "accept_now"

    @pytest.mark.asyncio
    async def test_split_outcome_duration_ms_non_negative(self) -> None:
        """Test 40: SplitOutcome.duration_ms >= 0."""
        norms = [self._make_norm_outcome("accept_now"), self._make_norm_outcome("accept_now")]

        with patch(
            "services.eep_worker.app.split_step.run_normalization_and_first_validation",
            side_effect=norms,
        ):
            cb_a, cb_b, cb_d = _make_cbs()
            outcome = await run_split_normalization(
                full_res_image=_make_test_image(),
                selected_geometry=_make_geometry_obj(split_required=True, page_count=2),
                selected_model="iep1a",
                proxy_width=300,
                proxy_height=200,
                left_output_uri="local://left.tiff",
                right_output_uri="local://right.tiff",
                left_rescue_output_uri="local://left_rescue.tiff",
                right_rescue_output_uri="local://right_rescue.tiff",
                left_rectified_proxy_uri="local://left_proxy.png",
                right_rectified_proxy_uri="local://right_proxy.png",
                storage=MagicMock(),
                image_loader=MagicMock(),
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                material_type="book",
                iep1d_endpoint=_IEP1D_EP,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=AsyncMock(),
                session=MagicMock(),
            )

        assert outcome.duration_ms >= 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Integration — Happy path
# ══════════════════════════════════════════════════════════════════════════════


class TestIntegrationHappyPath:
    """Tests 41–47: End-to-end happy path and PTIFF QA routing."""

    @pytest.mark.asyncio
    async def test_both_geometry_succeed_produces_selection_result(self) -> None:
        """Test 41: Both services succeed → selection_result is populated."""
        result, session = await _invoke_geometry()
        assert result.selection_result is not None
        assert result.iep1a_result is not None
        assert result.iep1b_result is not None

    def test_decide_route_accepted_and_passed_is_accept_now(self) -> None:
        """Test 42: geometry_route_decision="accepted" + validation passed → accept_now."""
        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        val = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.88,
            signal_scores=None,
            soft_passed=True,
            passed=True,
        )
        assert _decide_route("accepted", val) == "accept_now"

    def test_decide_route_rectification_is_rescue_required(self) -> None:
        """Test 43: geometry_route_decision="rectification" → rescue_required."""
        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        val = ArtifactValidationResult(
            hard_result=hard, soft_score=0.88, signal_scores=None, soft_passed=True, passed=True
        )
        assert _decide_route("rectification", val) == "rescue_required"

    def test_decide_route_validation_failed_is_rescue_required(self) -> None:
        """Test 44: Validation failed → rescue_required regardless of geometry trust."""
        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )

        hard = ArtifactHardCheckResult(passed=False, failed_checks=["blur_score"])
        val = ArtifactValidationResult(
            hard_result=hard, soft_score=None, signal_scores=None, soft_passed=None, passed=False
        )
        assert _decide_route("accepted", val) == "rescue_required"

    def test_ptiff_qa_manual_mode(self) -> None:
        """Test 45: manual ptiff_qa_mode → ptiff_qa_pending."""
        route = decide_ptiff_qa_route("preprocess", "manual")
        assert route.next_status == "ptiff_qa_pending"
        assert route.routing_path is None

    def test_ptiff_qa_auto_continue_preprocess(self) -> None:
        """Test 46: auto_continue + preprocess → accepted, routing_path='preprocessing_only'."""
        route = decide_ptiff_qa_route("preprocess", "auto_continue")
        assert route.next_status == "accepted"
        assert route.routing_path == "preprocessing_only"

    def test_ptiff_qa_auto_continue_layout(self) -> None:
        """Test 47: auto_continue + layout → layout_detection."""
        route = decide_ptiff_qa_route("layout", "auto_continue")
        assert route.next_status == "layout_detection"
        assert route.routing_path is None


# ══════════════════════════════════════════════════════════════════════════════
# Integration — Rescue path
# ══════════════════════════════════════════════════════════════════════════════


class TestIntegrationRescuePath:
    """Tests 48–49: Rescue flow accepted and rejected paths."""

    @pytest.mark.asyncio
    async def test_rescue_accepted_when_iep1d_and_second_pass_succeed(self) -> None:
        """Test 48: IEP1D success + second pass accepted → RescueOutcome.route="accept_now"."""
        import cv2

        image = _make_test_image()
        tiff_bytes = cv2.imencode(".tiff", image)[1].tobytes()

        storage = MagicMock()
        storage.get_bytes = MagicMock(return_value=tiff_bytes)
        storage.put_bytes = MagicMock()

        from services.eep.app.gates.artifact_validation import (
            ArtifactHardCheckResult,
            ArtifactValidationResult,
        )
        from services.eep.app.gates.geometry_selection import (
            GeometryCandidate,
            GeometrySelectionResult,
        )
        from services.eep_worker.app.normalization_step import NormalizationOutcome

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        val = ArtifactValidationResult(
            hard_result=hard, soft_score=0.90, signal_scores=None, soft_passed=True, passed=True
        )
        branch = MagicMock(spec=PreprocessBranchResponse)
        branch.processed_image_uri = _OUTPUT_URI
        second_norm = NormalizationOutcome(
            branch_response=branch, validation_result=val, route="accept_now", duration_ms=20.0
        )

        sel = GeometrySelectionResult(
            selected=GeometryCandidate(model="iep1a", response=_make_geometry_obj()),
            geometry_trust="high",
            selection_reason="higher_confidence",
            route_decision="accepted",
            review_reason=None,
            structural_agreement=True,
            sanity_results={},
            split_confidence_per_model=None,
            tta_variance_per_model={"iep1a": 0.01},
            page_area_preference_triggered=False,
        )
        inv_result = GeometryInvocationResult(
            iep1a_result=_make_geometry_obj(),
            iep1b_result=_make_geometry_obj(),
            iep1a_error=None,
            iep1b_error=None,
            iep1a_skipped=False,
            iep1b_skipped=False,
            iep1a_duration_ms=50.0,
            iep1b_duration_ms=50.0,
            selection_result=sel,
        )

        backend = AsyncMock()
        backend.call = AsyncMock(return_value=_valid_rectify_response_dict())

        with (
            patch(
                "services.eep_worker.app.rescue_step.invoke_geometry_services",
                new_callable=AsyncMock,
                return_value=inv_result,
            ),
            patch(
                "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
                return_value=second_norm,
            ),
        ):
            cb_a, cb_b, cb_d = _make_cbs()
            outcome = await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                material_type="book",
                rectified_proxy_uri=_PROXY_URI,
                rescue_output_uri=_OUTPUT_URI,
                iep1d_endpoint=_IEP1D_EP,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1d_circuit_breaker=cb_d,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=MagicMock(),
                storage=storage,
                image_loader=MagicMock(),
            )

        assert outcome.route == "accept_now"
        assert outcome.review_reason is None
        assert outcome.rectify_response is not None

    @pytest.mark.asyncio
    async def test_rescue_pending_when_iep1d_fails(self) -> None:
        """Test 49: IEP1D failure → RescueOutcome.route="pending_human_correction"."""
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "iep1d fail")
        cb_a, cb_b, cb_d = _make_cbs()
        backend = AsyncMock()
        backend.call = AsyncMock(side_effect=exc)

        outcome = await run_rescue_flow(
            artifact_uri=_ARTIFACT_URI,
            job_id=_JOB_ID,
            page_number=_PAGE_NUMBER,
            lineage_id=_LINEAGE_ID,
            material_type="book",
            rectified_proxy_uri=_PROXY_URI,
            rescue_output_uri=_OUTPUT_URI,
            iep1d_endpoint=_IEP1D_EP,
            iep1a_endpoint=_IEP1A_EP,
            iep1b_endpoint=_IEP1B_EP,
            iep1d_circuit_breaker=cb_d,
            iep1a_circuit_breaker=cb_a,
            iep1b_circuit_breaker=cb_b,
            backend=backend,
            session=MagicMock(),
            storage=MagicMock(),
            image_loader=MagicMock(),
        )
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"


# ══════════════════════════════════════════════════════════════════════════════
# Integration — Failure classification
# ══════════════════════════════════════════════════════════════════════════════


class TestIntegrationFailureClassification:
    """Tests 50–51: Hash mismatch and both-services-fail error types."""

    def test_hash_mismatch_raises_otiff_hash_mismatch_error(self) -> None:
        """Test 50: OtiffHashMismatchError raised when computed hash differs from prior."""
        with pytest.raises(OtiffHashMismatchError):
            check_hash_consistency(
                uri="local://page1.otiff",
                current_hash="def456",
                prior_hash="abc123",
            )

    @pytest.mark.asyncio
    async def test_geometry_service_error_carries_both_errors(self) -> None:
        """Test 51: GeometryServiceError exposes iep1a_error and iep1b_error."""
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "fail")
        backend = AsyncMock()
        backend.call = AsyncMock(side_effect=exc)
        session = MagicMock()
        session.add = MagicMock()
        cb_a, cb_b, _ = _make_cbs()

        with pytest.raises(GeometryServiceError) as exc_info:
            await invoke_geometry_services(
                job_id=_JOB_ID,
                page_number=_PAGE_NUMBER,
                lineage_id=_LINEAGE_ID,
                proxy_image_uri=_PROXY_URI,
                material_type="book",
                proxy_width=300,
                proxy_height=200,
                iep1a_endpoint=_IEP1A_EP,
                iep1b_endpoint=_IEP1B_EP,
                iep1a_circuit_breaker=cb_a,
                iep1b_circuit_breaker=cb_b,
                backend=backend,
                session=session,
            )

        err = exc_info.value
        assert err.job_id == _JOB_ID
        assert err.page_number == _PAGE_NUMBER
        assert err.iep1a_error is not None
        assert err.iep1b_error is not None
