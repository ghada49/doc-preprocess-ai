"""
tests/test_p4_rescue_step.py
------------------------------
Packet 4.5 — rescue flow tests.

Covers:
  1. IEP1D invocation: success, timeout, error, circuit breaker open,
     malformed response, ServiceInvocation logging.
  2. Second geometry pass routing: GeometryServiceError, structural disagreement,
     gate returns pending_human_correction, low trust, accepted.
  3. Split child guard: unexpected split on child routes to
     pending_human_correction; non-split child is unaffected.
  4. Final validation routing: accept_now, rescue_required → pending_human_correction.
  5. RescueOutcome contents: rectify_response, second_selection_result,
     branch_response, validation_result populated correctly.
  6. Integration: real normalization + real validation with controlled mocks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from shutil import rmtree
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import cv2
import numpy as np
import pytest

from services.eep.app.gates.artifact_validation import (
    ArtifactHardCheckResult,
    ArtifactImageDimensions,
    ArtifactValidationResult,
)
from services.eep.app.gates.geometry_selection import GeometryCandidate, GeometrySelectionResult
from services.eep_worker.app.circuit_breaker import CircuitBreaker, CircuitState
from services.eep_worker.app.geometry_invocation import (
    GeometryInvocationResult,
    GeometryServiceError,
)
from services.eep_worker.app.normalization_step import NormalizationOutcome
from services.eep_worker.app.rescue_step import RescueOutcome, run_rescue_flow
from shared.gpu.backend import BackendError, BackendErrorKind
from shared.io.storage import get_backend
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessBranchResponse

# ── Test constants ──────────────────────────────────────────────────────────────

_ARTIFACT_URI = "s3://bucket/normalized.tiff"
_RECTIFIED_URI = "s3://bucket/rectified.tiff"
_PROXY_URI = "s3://bucket/rectified_proxy.png"
_OUTPUT_URI = "s3://bucket/rescue_output.tiff"
_IEP1D_ENDPOINT = "http://iep1d:8003/v1/rectify"
_IEP1A_ENDPOINT = "http://iep1a:8001/v1/geometry"
_IEP1B_ENDPOINT = "http://iep1b:8002/v1/geometry"


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _make_test_image(h: int = 200, w: int = 300) -> np.ndarray:
    return np.full((h, w, 3), 128, dtype=np.uint8)


@pytest.fixture
def workspace_tmp_path() -> Iterator[Callable[[], Path]]:
    created_paths: list[Path] = []

    def factory() -> Path:
        path = Path.cwd() / "test_tmp" / "rescue" / uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        created_paths.append(path)
        return path

    yield factory

    for path in created_paths:
        rmtree(path, ignore_errors=True)


def _encode_png(image: np.ndarray) -> bytes:
    success, buf = cv2.imencode(".png", image)
    assert success, "cv2.imencode failed in test helper"
    raw: bytes = buf.tobytes()
    return raw


def _encode_tiff(image: np.ndarray) -> bytes:
    success, buf = cv2.imencode(".tiff", image)
    assert success, "cv2.imencode failed in test helper"
    raw: bytes = buf.tobytes()
    return raw


def _make_rectify_response_dict(uri: str = _RECTIFIED_URI) -> dict[str, Any]:
    return {
        "rectified_image_uri": uri,
        "rectification_confidence": 0.92,
        "skew_residual_before": 3.5,
        "skew_residual_after": 0.2,
        "border_score_before": 0.70,
        "border_score_after": 0.88,
        "processing_time_ms": 1200.0,
        "warnings": [],
    }


def _make_bad_rectify_response_dict(uri: str = _RECTIFIED_URI) -> dict[str, Any]:
    """Rectify response that fails the quality gate: skew worsened, border regressed."""
    return {
        "rectified_image_uri": uri,
        "rectification_confidence": 0.3,
        "skew_residual_before": 1.0,
        "skew_residual_after": 2.5,  # worse
        "border_score_before": 0.80,
        "border_score_after": 0.55,  # worse
        "processing_time_ms": 1200.0,
        "warnings": ["skew_residual_not_improved", "border_score_not_improved"],
    }


def _make_geometry_response(
    split_required: bool = False,
    page_count: int = 1,
) -> GeometryResponse:
    pages = [
        PageRegion(
            region_id=f"page_{i}",
            geometry_type="bbox",
            bbox=(10, 10, 90, 90),
            corners=None,
            confidence=0.92,
            page_area_fraction=0.80,
        )
        for i in range(max(1, page_count))
    ]
    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=None,
        geometry_confidence=0.92,
        tta_structural_agreement_rate=0.95,
        tta_prediction_variance=0.01,
        tta_passes=3,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=80.0,
    )


def _make_selection_result(
    route_decision: str = "accepted",
    structural_agreement: bool | None = True,
    review_reason: str | None = None,
) -> GeometrySelectionResult:
    geo = _make_geometry_response()
    candidate = GeometryCandidate(model="iep1a", response=geo)
    return GeometrySelectionResult(
        selected=candidate if route_decision != "pending_human_correction" else None,
        geometry_trust="high" if route_decision == "accepted" else "low",
        selection_reason="higher_confidence",
        route_decision=route_decision,  # type: ignore[arg-type]
        review_reason=review_reason,
        structural_agreement=structural_agreement,
        sanity_results={},
        split_confidence_per_model=None,
        tta_variance_per_model={"iep1a": 0.01},
        page_area_preference_triggered=False,
    )


def _make_invocation_result(
    route_decision: str = "accepted",
    structural_agreement: bool | None = True,
    review_reason: str | None = None,
    iep1a_split: bool = False,
    iep1b_split: bool = False,
) -> GeometryInvocationResult:
    geo_a = _make_geometry_response(split_required=iep1a_split, page_count=2 if iep1a_split else 1)
    geo_b = _make_geometry_response(split_required=iep1b_split, page_count=2 if iep1b_split else 1)
    selection = _make_selection_result(route_decision, structural_agreement, review_reason)
    return GeometryInvocationResult(
        iep1a_result=geo_a,
        iep1b_result=geo_b,
        iep1a_error=None,
        iep1b_error=None,
        iep1a_skipped=False,
        iep1b_skipped=False,
        iep1a_duration_ms=50.0,
        iep1b_duration_ms=50.0,
        selection_result=selection,
    )


def _make_norm_outcome(route: str = "accept_now") -> NormalizationOutcome:
    branch = MagicMock(spec=PreprocessBranchResponse)
    branch.processed_image_uri = _OUTPUT_URI
    hard = ArtifactHardCheckResult(
        passed=route == "accept_now",
        failed_checks=[] if route == "accept_now" else ["dimensions_consistent"],
    )
    validation = ArtifactValidationResult(
        hard_result=hard,
        soft_score=0.85 if route == "accept_now" else None,
        signal_scores=None,
        soft_passed=route == "accept_now" or None,
        passed=route == "accept_now",
    )
    return NormalizationOutcome(
        branch_response=branch,
        validation_result=validation,
        route=route,  # type: ignore[arg-type]
        duration_ms=120.0,
    )


def _make_cbs() -> tuple[CircuitBreaker, CircuitBreaker, CircuitBreaker]:
    """Return fresh (iep1a_cb, iep1b_cb, iep1d_cb) circuit breakers."""
    return (
        CircuitBreaker("iep1a"),
        CircuitBreaker("iep1b"),
        CircuitBreaker("iep1d"),
    )


def _make_storage_mock(image: np.ndarray | None = None) -> MagicMock:
    """Return a storage mock whose get_bytes() returns TIFF-encoded test image bytes."""
    img = image if image is not None else _make_test_image()
    storage = MagicMock()
    storage.get_bytes.return_value = _encode_tiff(img)
    storage.put_bytes.return_value = None
    return storage


def _make_image_loader(h: int = 10000, w: int = 10000) -> Callable[[str], ArtifactImageDimensions]:
    """Fixed-dimension loader (useful when exact match is not required by the test)."""

    def loader(uri: str) -> ArtifactImageDimensions:
        return ArtifactImageDimensions(width=w, height=h)

    return loader


def _make_capturing_storage(
    rectified_image: np.ndarray,
) -> tuple[MagicMock, Callable[[str], ArtifactImageDimensions]]:
    """
    Return a (storage_mock, image_loader) pair where the loader reads back
    whatever put_bytes() captured.  Used in integration tests so that the
    dimensions_consistent hard check always passes.
    """
    stored: dict[str, bytes] = {}
    stored["__rectified__"] = _encode_tiff(rectified_image)

    sto = MagicMock()

    def get_bytes(uri: str) -> bytes:
        if uri in stored:
            return stored[uri]
        return stored["__rectified__"]

    def put_bytes(uri: str, data: bytes) -> None:
        stored[uri] = data

    sto.get_bytes.side_effect = get_bytes
    sto.put_bytes.side_effect = put_bytes

    def loader(uri: str) -> ArtifactImageDimensions:
        if uri not in stored:
            raise FileNotFoundError(f"URI not found: {uri!r}")
        raw_bytes = stored[uri]
        buf = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"Cannot decode image at {uri!r}")
        h, w = img.shape[:2]
        return ArtifactImageDimensions(width=w, height=h)

    return sto, loader


def _failing_loader(uri: str) -> ArtifactImageDimensions:
    raise FileNotFoundError(f"Loader intentionally fails for {uri!r}")


async def _run(
    *,
    backend_call_side_effect: Any = None,
    iep1d_response_dict: dict[str, Any] | None = None,
    inv_result: GeometryInvocationResult | None = None,
    inv_side_effect: Any = None,
    norm_outcome: NormalizationOutcome | None = None,
    image_loader: Callable[[str], ArtifactImageDimensions] | None = None,
    storage: MagicMock | None = None,
    is_split_child: bool = False,
    iep1d_cb_open: bool = False,
) -> RescueOutcome:
    """
    Run run_rescue_flow with patched invoke_geometry_services and
    run_normalization_and_first_validation.  Backend is configured via
    backend_call_side_effect or iep1d_response_dict.
    """
    a_cb, b_cb, d_cb = _make_cbs()
    if iep1d_cb_open:
        d_cb._state = CircuitState.OPEN
        d_cb._opened_at = __import__("time").monotonic()

    if backend_call_side_effect is not None:
        backend = AsyncMock()
        backend.call.side_effect = backend_call_side_effect
    elif iep1d_response_dict is not None:
        backend = AsyncMock()
        backend.call.return_value = iep1d_response_dict
    else:
        backend = AsyncMock()
        backend.call.return_value = _make_rectify_response_dict()

    sto = storage if storage is not None else _make_storage_mock()
    ldr = image_loader if image_loader is not None else _make_image_loader()
    session = MagicMock()

    _inv = inv_result if inv_result is not None else _make_invocation_result()
    _norm = norm_outcome if norm_outcome is not None else _make_norm_outcome("accept_now")
    _inv_mock = (
        AsyncMock(side_effect=inv_side_effect)
        if inv_side_effect is not None
        else AsyncMock(return_value=_inv)
    )

    with (
        patch(
            "services.eep_worker.app.rescue_step.invoke_geometry_services",
            new=_inv_mock,
        ),
        patch(
            "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
            return_value=_norm,
        ),
    ):
        return await run_rescue_flow(
            artifact_uri=_ARTIFACT_URI,
            job_id="job-1",
            page_number=1,
            lineage_id="lin-1",
            material_type="book",
            rectified_proxy_uri=_PROXY_URI,
            rescue_output_uri=_OUTPUT_URI,
            iep1d_endpoint=_IEP1D_ENDPOINT,
            iep1a_endpoint=_IEP1A_ENDPOINT,
            iep1b_endpoint=_IEP1B_ENDPOINT,
            iep1d_circuit_breaker=d_cb,
            iep1a_circuit_breaker=a_cb,
            iep1b_circuit_breaker=b_cb,
            backend=backend,
            session=session,
            storage=sto,
            image_loader=ldr,
            is_split_child=is_split_child,
        )


# ── 1. IEP1D invocation ─────────────────────────────────────────────────────────


class TestIep1dCall:
    @pytest.mark.asyncio
    async def test_success_proceeds_to_next_step(self) -> None:
        outcome = await _run(iep1d_response_dict=_make_rectify_response_dict())
        # IEP1D succeeded; downstream is patched to accept_now
        assert outcome.route == "accept_now"
        assert outcome.rectify_response is not None
        assert outcome.rectify_response.rectified_image_uri == _RECTIFIED_URI

    @pytest.mark.asyncio
    async def test_cold_start_timeout_routes_pending(self) -> None:
        exc = BackendError(BackendErrorKind.COLD_START_TIMEOUT, "cold start")
        outcome = await _run(backend_call_side_effect=exc)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"
        assert outcome.rectify_response is None

    @pytest.mark.asyncio
    async def test_service_error_routes_pending(self) -> None:
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "http 500")
        outcome = await _run(backend_call_side_effect=exc)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_routes_pending(self) -> None:
        outcome = await _run(iep1d_cb_open=True)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"

    @pytest.mark.asyncio
    async def test_malformed_response_routes_pending(self) -> None:
        # Return a dict missing required fields → ValidationError
        outcome = await _run(iep1d_response_dict={"not_a_valid_field": True})
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"

    @pytest.mark.asyncio
    async def test_service_invocation_logged_for_iep1d_success(self) -> None:
        session = MagicMock()
        sto = _make_storage_mock()
        a_cb, b_cb, d_cb = _make_cbs()
        backend = AsyncMock()
        backend.call.return_value = _make_rectify_response_dict()

        with (
            patch(
                "services.eep_worker.app.rescue_step.invoke_geometry_services",
                new=AsyncMock(return_value=_make_invocation_result()),
            ),
            patch(
                "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
                return_value=_make_norm_outcome("accept_now"),
            ),
        ):
            await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id="job-1",
                page_number=1,
                lineage_id="lin-1",
                material_type="book",
                rectified_proxy_uri=_PROXY_URI,
                rescue_output_uri=_OUTPUT_URI,
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=d_cb,
                iep1a_circuit_breaker=a_cb,
                iep1b_circuit_breaker=b_cb,
                backend=backend,
                session=session,
                storage=sto,
                image_loader=_make_image_loader(),
            )

        # session.add() should have been called with a ServiceInvocation somewhere
        # (may be followed by other add() calls, e.g. QualityGateLog)
        from services.eep.app.db.models import ServiceInvocation

        assert session.add.called
        invocations = [
            call[0][0]
            for call in session.add.call_args_list
            if isinstance(call[0][0], ServiceInvocation)
        ]
        assert len(invocations) == 1, (
            f"Expected exactly one ServiceInvocation added; "
            f"got {[type(c[0][0]).__name__ for c in session.add.call_args_list]}"
        )
        added = invocations[0]
        assert added.service_name == "iep1d"
        assert added.status == "success"
        assert added.metrics is not None
        assert added.metrics["rectified_image_uri"] == _RECTIFIED_URI

    @pytest.mark.asyncio
    async def test_service_invocation_logged_for_iep1d_failure(self) -> None:
        session = MagicMock()
        a_cb, b_cb, d_cb = _make_cbs()
        backend = AsyncMock()
        backend.call.side_effect = BackendError(BackendErrorKind.SERVICE_ERROR, "err")

        await run_rescue_flow(
            artifact_uri=_ARTIFACT_URI,
            job_id="job-1",
            page_number=1,
            lineage_id="lin-1",
            material_type="book",
            rectified_proxy_uri=_PROXY_URI,
            rescue_output_uri=_OUTPUT_URI,
            iep1d_endpoint=_IEP1D_ENDPOINT,
            iep1a_endpoint=_IEP1A_ENDPOINT,
            iep1b_endpoint=_IEP1B_ENDPOINT,
            iep1d_circuit_breaker=d_cb,
            iep1a_circuit_breaker=a_cb,
            iep1b_circuit_breaker=b_cb,
            backend=backend,
            session=session,
            storage=_make_storage_mock(),
            image_loader=_make_image_loader(),
        )

        added = session.add.call_args[0][0]
        from services.eep.app.db.models import ServiceInvocation

        assert isinstance(added, ServiceInvocation)
        assert added.service_name == "iep1d"
        assert added.status == "error"


# ── 2. Second geometry pass routing ────────────────────────────────────────────


class TestSecondGeometryPassRouting:
    @pytest.mark.asyncio
    async def test_geometry_service_error_routes_pending(self) -> None:
        err = GeometryServiceError(
            job_id="job-1",
            page_number=1,
            iep1a_error={"kind": "timeout", "message": "timeout"},
            iep1b_error={"kind": "timeout", "message": "timeout"},
        )
        outcome = await _run(inv_side_effect=err)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "geometry_services_failed_post_rectification"
        assert outcome.second_selection_result is None

    @pytest.mark.asyncio
    async def test_structural_disagreement_routes_pending(self) -> None:
        inv = _make_invocation_result(
            route_decision="rectification",
            structural_agreement=False,
        )
        outcome = await _run(inv_result=inv)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "structural_disagreement_post_rectification"

    @pytest.mark.asyncio
    async def test_gate_pending_human_correction_propagates_reason(self) -> None:
        inv = _make_invocation_result(
            route_decision="pending_human_correction",
            review_reason="sanity_check_failed",
        )
        outcome = await _run(inv_result=inv)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "sanity_check_failed"

    @pytest.mark.asyncio
    async def test_low_trust_rectification_routes_pending(self) -> None:
        inv = _make_invocation_result(
            route_decision="rectification",
            structural_agreement=True,  # trust low for other reason
        )
        outcome = await _run(inv_result=inv)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "low_geometry_trust_post_rectification"

    @pytest.mark.asyncio
    async def test_accepted_proceeds_to_normalization(self) -> None:
        inv = _make_invocation_result(route_decision="accepted")
        norm = _make_norm_outcome("accept_now")
        outcome = await _run(inv_result=inv, norm_outcome=norm)
        assert outcome.route == "accept_now"
        assert outcome.second_selection_result is not None
        assert outcome.second_selection_result.route_decision == "accepted"


# ── 3. Split child guard ─────────────────────────────────────────────────────────


class TestSplitChildGuard:
    @pytest.mark.asyncio
    async def test_iep1a_split_on_child_routes_pending(self) -> None:
        inv = _make_invocation_result(iep1a_split=True)
        outcome = await _run(inv_result=inv, is_split_child=True)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "geometry_unexpected_split_on_child"

    @pytest.mark.asyncio
    async def test_iep1b_split_on_child_routes_pending(self) -> None:
        inv = _make_invocation_result(iep1b_split=True)
        outcome = await _run(inv_result=inv, is_split_child=True)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "geometry_unexpected_split_on_child"

    @pytest.mark.asyncio
    async def test_split_on_non_child_does_not_route_pending(self) -> None:
        # is_split_child=False: split in second pass is allowed (handled by caller)
        inv = _make_invocation_result(iep1a_split=True)
        norm = _make_norm_outcome("accept_now")
        outcome = await _run(inv_result=inv, norm_outcome=norm, is_split_child=False)
        # Should NOT be "geometry_unexpected_split_on_child"
        assert outcome.review_reason != "geometry_unexpected_split_on_child"


# ── 4. Final validation routing ─────────────────────────────────────────────────


class TestFinalValidationRouting:
    @pytest.mark.asyncio
    async def test_accept_now_produces_accept_now_route(self) -> None:
        outcome = await _run(norm_outcome=_make_norm_outcome("accept_now"))
        assert outcome.route == "accept_now"
        assert outcome.review_reason is None

    @pytest.mark.asyncio
    async def test_rescue_required_produces_pending_artifact_validation_failed(self) -> None:
        outcome = await _run(norm_outcome=_make_norm_outcome("rescue_required"))
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "artifact_validation_failed"

    @pytest.mark.asyncio
    async def test_branch_response_populated_even_on_validation_failure(self) -> None:
        outcome = await _run(norm_outcome=_make_norm_outcome("rescue_required"))
        # branch_response is still populated — caller may need it for logging
        assert outcome.branch_response is not None
        assert outcome.validation_result is not None


# ── 5. RescueOutcome contents ───────────────────────────────────────────────────


class TestRescueOutcomeContents:
    @pytest.mark.asyncio
    async def test_rectify_response_populated_on_success(self) -> None:
        outcome = await _run()
        assert outcome.rectify_response is not None
        assert outcome.rectify_response.rectified_image_uri == _RECTIFIED_URI

    @pytest.mark.asyncio
    async def test_second_selection_result_populated_on_success(self) -> None:
        outcome = await _run()
        assert outcome.second_selection_result is not None

    @pytest.mark.asyncio
    async def test_branch_response_populated_on_success(self) -> None:
        outcome = await _run()
        assert outcome.branch_response is not None

    @pytest.mark.asyncio
    async def test_validation_result_populated_on_success(self) -> None:
        outcome = await _run()
        assert outcome.validation_result is not None

    @pytest.mark.asyncio
    async def test_duration_ms_is_positive(self) -> None:
        outcome = await _run()
        assert outcome.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_early_exit_fields_are_none(self) -> None:
        # IEP1D fails → branch_response, validation_result, etc. are None
        exc = BackendError(BackendErrorKind.SERVICE_ERROR, "err")
        outcome = await _run(backend_call_side_effect=exc)
        assert outcome.branch_response is None
        assert outcome.validation_result is None
        assert outcome.second_selection_result is None
        assert outcome.rectify_response is None


# ── 6. Integration ──────────────────────────────────────────────────────────────


class TestIntegration:
    """
    Full-stack tests: real normalization + real validation gate.
    Backend calls are mocked at the HTTP level; storage is a MagicMock.
    """

    def _setup(
        self,
        image_loader: Callable[[str], ArtifactImageDimensions] | None = None,
        use_capturing_storage: bool = True,
    ) -> dict[str, Any]:
        img = _make_test_image(400, 600)
        if use_capturing_storage and image_loader is None:
            sto, ldr = _make_capturing_storage(img)
        else:
            sto = _make_storage_mock(img)
            ldr = image_loader if image_loader is not None else _make_image_loader()
        a_cb, b_cb, d_cb = _make_cbs()
        backend = AsyncMock()
        backend.call.return_value = _make_rectify_response_dict()
        session = MagicMock()
        return {
            "img": img,
            "storage": sto,
            "a_cb": a_cb,
            "b_cb": b_cb,
            "d_cb": d_cb,
            "backend": backend,
            "session": session,
            "image_loader": ldr,
        }

    async def _call(self, ctx: dict[str, Any], **overrides: Any) -> RescueOutcome:
        inv = overrides.pop("inv_result", _make_invocation_result())
        with patch(
            "services.eep_worker.app.rescue_step.invoke_geometry_services",
            new=AsyncMock(return_value=inv),
        ):
            return await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id="job-1",
                page_number=1,
                lineage_id="lin-1",
                material_type="book",
                rectified_proxy_uri=_PROXY_URI,
                rescue_output_uri=_OUTPUT_URI,
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=ctx["d_cb"],
                iep1a_circuit_breaker=ctx["a_cb"],
                iep1b_circuit_breaker=ctx["b_cb"],
                backend=ctx["backend"],
                session=ctx["session"],
                storage=ctx["storage"],
                image_loader=ctx["image_loader"],
                **overrides,
            )

    @pytest.mark.asyncio
    async def test_full_happy_path_accept_now(self) -> None:
        """
        Happy path: IEP1D succeeds, second geometry pass accepted, normalization runs,
        validation is patched to pass → route=accept_now.
        Verifies the full orchestration flow without depending on specific image quality.
        """
        ctx = self._setup()
        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        passing_validation = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.9,
            signal_scores=None,
            soft_passed=True,
            passed=True,
        )
        with patch(
            "services.eep_worker.app.normalization_step.run_artifact_validation",
            return_value=passing_validation,
        ):
            outcome = await self._call(ctx)
        assert outcome.route == "accept_now"
        assert outcome.review_reason is None
        assert outcome.rectify_response is not None
        assert outcome.branch_response is not None
        assert outcome.validation_result is not None
        assert outcome.validation_result.passed is True

    @pytest.mark.asyncio
    async def test_iep1d_failure_short_circuits(self) -> None:
        ctx = self._setup()
        ctx["backend"].call.side_effect = BackendError(BackendErrorKind.SERVICE_ERROR, "down")
        outcome = await self._call(ctx)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "rectification_failed"
        # storage was NOT read (IEP1D failed before loading rectified image)
        ctx["storage"].get_bytes.assert_not_called()

    @pytest.mark.asyncio
    async def test_geometry_service_error_routes_pending(self) -> None:
        ctx = self._setup()
        err = GeometryServiceError(
            job_id="job-1",
            page_number=1,
            iep1a_error={"kind": "error", "message": "err"},
            iep1b_error={"kind": "error", "message": "err"},
        )
        with patch(
            "services.eep_worker.app.rescue_step.invoke_geometry_services",
            new=AsyncMock(side_effect=err),
        ):
            outcome = await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id="job-1",
                page_number=1,
                lineage_id="lin-1",
                material_type="book",
                rectified_proxy_uri=_PROXY_URI,
                rescue_output_uri=_OUTPUT_URI,
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=ctx["d_cb"],
                iep1a_circuit_breaker=ctx["a_cb"],
                iep1b_circuit_breaker=ctx["b_cb"],
                backend=ctx["backend"],
                session=ctx["session"],
                storage=ctx["storage"],
                image_loader=ctx["image_loader"],
            )
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "geometry_services_failed_post_rectification"

    @pytest.mark.asyncio
    async def test_hard_check_fail_causes_artifact_validation_failed(self) -> None:
        # image_loader raises → hard check file_exists fails → validation fails
        # → norm_outcome.route = "rescue_required"
        # → RescueOutcome.route = "pending_human_correction", reason = "artifact_validation_failed"
        ctx = self._setup(image_loader=_failing_loader, use_capturing_storage=False)
        outcome = await self._call(ctx)
        assert outcome.route == "pending_human_correction"
        assert outcome.review_reason == "artifact_validation_failed"
        # branch_response is still populated (normalization succeeded, only validation failed)
        assert outcome.branch_response is not None
        assert outcome.validation_result is not None
        assert outcome.validation_result.passed is False

    @pytest.mark.asyncio
    async def test_split_child_right_index_does_not_crash_post_rescue(
        self,
    ) -> None:
        """
        Regression test for the "split normalization failed: list index out of
        range" bug observed in production.

        When a split child with sub_page_index=1 (right half) goes through the
        rescue flow, IEP1D rectifies its single-page input into a single-page
        output.  IEP1A/IEP1B then return a GeometryResponse with page_count=1
        and len(pages)==1.  The post-rescue normalization must index into
        pages[0] regardless of the original page_index — using page_index=1
        here would raise IndexError.

        This test exercises the real run_normalization_and_first_validation
        with is_split_child=True, page_index=1, and asserts the rescue completes
        without raising.
        """
        ctx = self._setup()
        # Post-rescue geometry response describes ONE page (the rectified
        # single-page image).  Caller passes page_index=1 because this is
        # the right-half rescue of a split parent.
        single_page_inv = _make_invocation_result(route_decision="accepted")
        assert len(single_page_inv.iep1a_result.pages) == 1, (
            "test fixture sanity: post-rescue response has one page"
        )

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        passing_validation = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.9,
            signal_scores=None,
            soft_passed=True,
            passed=True,
        )
        with patch(
            "services.eep_worker.app.normalization_step.run_artifact_validation",
            return_value=passing_validation,
        ):
            outcome = await self._call(
                ctx,
                inv_result=single_page_inv,
                is_split_child=True,
                page_index=1,
            )

        assert outcome.route == "accept_now", (
            f"expected accept_now post-rescue (no IndexError), got "
            f"{outcome.route!r} review_reason={outcome.review_reason!r}"
        )

    @pytest.mark.asyncio
    async def test_worker_consumes_real_rectified_artifact_from_storage(
        self,
        workspace_tmp_path: Callable[[], Path],
    ) -> None:
        tmp_path = workspace_tmp_path()
        rectified_image = _make_test_image(400, 600)
        rectified_uri = (
            f"file://{(tmp_path / 'jobs' / 'job-1' / 'rectified' / '1.tiff').as_posix()}"
        )
        rectified_proxy_uri = (
            f"file://{(tmp_path / 'jobs' / 'job-1' / 'proxy_rectified' / '1.png').as_posix()}"
        )
        rescue_output_uri = (
            f"file://{(tmp_path / 'jobs' / 'job-1' / 'output_rectified' / '1.tiff').as_posix()}"
        )

        rectified_storage = get_backend(rectified_uri)
        rectified_storage.put_bytes(rectified_uri, _encode_tiff(rectified_image))
        rescue_storage = get_backend(rescue_output_uri)

        hard = ArtifactHardCheckResult(passed=True, failed_checks=[])
        passing_validation = ArtifactValidationResult(
            hard_result=hard,
            soft_score=0.92,
            signal_scores=None,
            soft_passed=True,
            passed=True,
        )
        backend = AsyncMock()
        backend.call.return_value = _make_rectify_response_dict(uri=rectified_uri)
        a_cb, b_cb, d_cb = _make_cbs()

        with (
            patch(
                "services.eep_worker.app.rescue_step.invoke_geometry_services",
                new=AsyncMock(return_value=_make_invocation_result(route_decision="accepted")),
            ),
            patch(
                "services.eep_worker.app.normalization_step.run_artifact_validation",
                return_value=passing_validation,
            ),
        ):
            outcome = await run_rescue_flow(
                artifact_uri=_ARTIFACT_URI,
                job_id="job-1",
                page_number=1,
                lineage_id="lin-1",
                material_type="book",
                rectified_proxy_uri=rectified_proxy_uri,
                rescue_output_uri=rescue_output_uri,
                iep1d_endpoint=_IEP1D_ENDPOINT,
                iep1a_endpoint=_IEP1A_ENDPOINT,
                iep1b_endpoint=_IEP1B_ENDPOINT,
                iep1d_circuit_breaker=d_cb,
                iep1a_circuit_breaker=a_cb,
                iep1b_circuit_breaker=b_cb,
                backend=backend,
                session=MagicMock(),
                storage=rescue_storage,
                image_loader=_make_image_loader(),
            )

        assert outcome.route == "accept_now"
        assert outcome.rectify_response is not None
        assert outcome.rectify_response.rectified_image_uri == rectified_uri
        assert Path(rectified_proxy_uri[len("file://") :]).exists()
        assert Path(rescue_output_uri[len("file://") :]).exists()


# ── 7. IEP1D quality gate ───────────────────────────────────────────────────────


class TestIep1dQualityGate:
    """
    Verifies that IEP1D is treated as advisory (conditionally accepted) rather
    than authoritative.  Tests cover all gate criteria individually and the
    storage URI used downstream in each case.
    """

    @pytest.mark.asyncio
    async def test_accepted_when_all_criteria_met(self) -> None:
        # confidence >= 0.6, skew improved, border not regressed, no bad warnings
        storage = _make_storage_mock()
        outcome = await _run(
            iep1d_response_dict=_make_rectify_response_dict(),
            storage=storage,
        )
        assert outcome.route == "accept_now"
        storage.get_bytes.assert_called_once_with(_RECTIFIED_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_skew_and_border_both_fail(self) -> None:
        # Validation target: skew_after >= skew_before AND border_after < border_before
        storage = _make_storage_mock()
        outcome = await _run(
            iep1d_response_dict=_make_bad_rectify_response_dict(),
            storage=storage,
        )
        # Pipeline still proceeds (fallback to original) — outcome is accept_now from norm mock
        assert outcome.route == "accept_now"
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_confidence_below_threshold(self) -> None:
        resp = _make_rectify_response_dict()
        resp["rectification_confidence"] = 0.59  # just below 0.6
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_accepted_at_exact_confidence_threshold(self) -> None:
        resp = _make_rectify_response_dict()
        resp["rectification_confidence"] = 0.6  # exactly at threshold
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_RECTIFIED_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_skew_not_improved(self) -> None:
        resp = _make_rectify_response_dict()
        resp["skew_residual_after"] = resp["skew_residual_before"]  # equal → not strictly less
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_accepted_when_border_score_equal(self) -> None:
        # border_after == border_before → >= condition passes
        resp = _make_rectify_response_dict()
        resp["border_score_after"] = resp["border_score_before"]
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_RECTIFIED_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_border_score_regressed(self) -> None:
        resp = _make_rectify_response_dict()
        resp["border_score_after"] = resp["border_score_before"] - 0.01  # regressed
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_skew_residual_not_improved_warning(self) -> None:
        resp = _make_rectify_response_dict()
        resp["warnings"] = ["skew_residual_not_improved"]
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_rejected_when_border_score_not_improved_warning(self) -> None:
        resp = _make_rectify_response_dict()
        resp["warnings"] = ["border_score_not_improved"]
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_ARTIFACT_URI)

    @pytest.mark.asyncio
    async def test_other_warnings_do_not_cause_rejection(self) -> None:
        # blur_score_regressed is not a gate criterion
        resp = _make_rectify_response_dict()
        resp["warnings"] = ["blur_score_regressed"]
        storage = _make_storage_mock()
        await _run(iep1d_response_dict=resp, storage=storage)
        storage.get_bytes.assert_called_once_with(_RECTIFIED_URI)

    @pytest.mark.asyncio
    async def test_rejection_does_not_change_route_outcome(self) -> None:
        # Gate rejection falls back to original but does NOT itself return pending_human_correction
        outcome = await _run(iep1d_response_dict=_make_bad_rectify_response_dict())
        assert outcome.route == "accept_now"
        assert outcome.review_reason is None

    @pytest.mark.asyncio
    async def test_rectify_response_always_populated_on_gate_rejection(self) -> None:
        outcome = await _run(iep1d_response_dict=_make_bad_rectify_response_dict())
        assert outcome.rectify_response is not None
        assert outcome.rectify_response.rectification_confidence == 0.3


# ── 8. IEP1D gate metrics and structured log ───────────────────────────────────


class TestIep1dGateMetricsAndLog:
    """Verify Prometheus counter emission and structured log fields for the quality gate."""

    @pytest.mark.asyncio
    async def test_accepted_emits_decision_counter(self) -> None:
        with (
            patch(
                "services.eep_worker.app.rescue_step.IEP1D_QUALITY_GATE_DECISIONS"
            ) as mock_decisions,
            patch("services.eep_worker.app.rescue_step.IEP1D_REJECTION_REASONS") as mock_reasons,
        ):
            await _run(iep1d_response_dict=_make_rectify_response_dict())

        mock_decisions.labels.assert_called_once_with(decision="rectified_accepted")
        mock_decisions.labels.return_value.inc.assert_called_once()
        mock_reasons.labels.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejected_emits_decision_counter(self) -> None:
        with (
            patch(
                "services.eep_worker.app.rescue_step.IEP1D_QUALITY_GATE_DECISIONS"
            ) as mock_decisions,
            patch("services.eep_worker.app.rescue_step.IEP1D_REJECTION_REASONS"),
        ):
            await _run(iep1d_response_dict=_make_bad_rectify_response_dict())

        mock_decisions.labels.assert_called_once_with(decision="rectification_rejected")
        mock_decisions.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejection_emits_all_active_reason_counters(self) -> None:
        with (
            patch("services.eep_worker.app.rescue_step.IEP1D_QUALITY_GATE_DECISIONS"),
            patch("services.eep_worker.app.rescue_step.IEP1D_REJECTION_REASONS") as mock_reasons,
        ):
            await _run(iep1d_response_dict=_make_bad_rectify_response_dict())

        # bad response has low_confidence + skew_not_improved + border_regressed + warning_veto
        called_reasons = {call.kwargs["reason"] for call in mock_reasons.labels.call_args_list}
        assert called_reasons == {
            "low_confidence",
            "skew_not_improved",
            "border_regressed",
            "warning_veto",
        }

    @pytest.mark.asyncio
    async def test_single_reason_emits_only_that_reason(self) -> None:
        resp = _make_rectify_response_dict()
        resp["rectification_confidence"] = 0.59  # only low_confidence fails
        with (
            patch("services.eep_worker.app.rescue_step.IEP1D_QUALITY_GATE_DECISIONS"),
            patch("services.eep_worker.app.rescue_step.IEP1D_REJECTION_REASONS") as mock_reasons,
        ):
            await _run(iep1d_response_dict=resp)

        called_reasons = {call.kwargs["reason"] for call in mock_reasons.labels.call_args_list}
        assert called_reasons == {"low_confidence"}

    @pytest.mark.asyncio
    async def test_structured_log_includes_rejection_reasons_on_rejection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="services.eep_worker.app.rescue_step"):
            await _run(iep1d_response_dict=_make_bad_rectify_response_dict())

        log_records = [r for r in caplog.records if isinstance(r.getMessage(), str)]
        gate_log = next(
            (r for r in log_records if "iep1d_decision" in str(r.getMessage())),
            None,
        )
        assert gate_log is not None
        payload = gate_log.getMessage()
        assert "rectification_rejected" in payload
        assert "rejection_reasons" in payload

    @pytest.mark.asyncio
    async def test_structured_log_rejection_reasons_empty_on_acceptance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="services.eep_worker.app.rescue_step"):
            await _run(iep1d_response_dict=_make_rectify_response_dict())

        gate_log = next(
            (r for r in caplog.records if "iep1d_decision" in str(r.getMessage())),
            None,
        )
        assert gate_log is not None
        assert "rectified_accepted" in gate_log.getMessage()
        # rejection_reasons key present but empty list
        assert "'rejection_reasons': []" in gate_log.getMessage()
