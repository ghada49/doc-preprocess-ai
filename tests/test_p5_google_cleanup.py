"""
tests/test_p5_google_cleanup.py
---------------------------------
Packet 5 — Google Document AI cleanup as final rescue fallback.

Covers:
  1. Second-pass failure → Google cleanup succeeds → third-pass succeeds → accept_now
  2. Google cleanup API returns None → fallback to pending_human_correction
  3. Google is disabled (GoogleWorkerState.enabled=False) → fallback to pending_human_correction
  4. Cleaned artifact bytes are actually written to storage via put_bytes
  5. Third-pass geometry invocation uses google_cleanup_proxy_uri
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import numpy as np
import cv2
import pytest

from services.eep.app.gates.artifact_validation import (
    ArtifactHardCheckResult,
    ArtifactImageDimensions,
    ArtifactValidationResult,
)
from services.eep.app.gates.geometry_selection import GeometryCandidate, GeometrySelectionResult
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.geometry_invocation import GeometryInvocationResult
from services.eep_worker.app.google_config import GoogleWorkerState
from services.eep_worker.app.normalization_step import NormalizationOutcome
from services.eep_worker.app.rescue_step import RescueOutcome, run_rescue_flow
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessBranchResponse

# ── Test constants ───────────────────────────────────────────────────────────────

_ARTIFACT_URI = "s3://bucket/normalized.tiff"
_RECTIFIED_URI = "s3://bucket/rectified.tiff"
_PROXY_URI = "s3://bucket/rectified_proxy.png"
_OUTPUT_URI = "s3://bucket/rescue_output.tiff"
_GOOGLE_CLEANUP_URI = "s3://bucket/google_cleaned.tiff"
_GOOGLE_PROXY_URI = "s3://bucket/google_cleaned_proxy.png"
_IEP1D_ENDPOINT = "http://iep1d:8003/v1/rectify"
_IEP1A_ENDPOINT = "http://iep1a:8001/v1/geometry"
_IEP1B_ENDPOINT = "http://iep1b:8002/v1/geometry"


# ── Helpers ──────────────────────────────────────────────────────────────────────


def _make_test_image(h: int = 200, w: int = 300) -> np.ndarray:
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _encode_tiff(image: np.ndarray) -> bytes:
    success, buf = cv2.imencode(".tiff", image)
    assert success, "cv2.imencode failed in test helper"
    return buf.tobytes()


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


def _make_geometry_response() -> GeometryResponse:
    pages = [
        PageRegion(
            region_id="page_0",
            geometry_type="bbox",
            bbox=(10, 10, 90, 90),
            corners=None,
            confidence=0.92,
            page_area_fraction=0.80,
        )
    ]
    return GeometryResponse(
        page_count=1,
        pages=pages,
        split_required=False,
        split_x=None,
        geometry_confidence=0.92,
        tta_structural_agreement_rate=0.95,
        tta_prediction_variance=0.01,
        tta_passes=3,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=80.0,
    )


def _make_selection_result(route_decision: str = "accepted") -> GeometrySelectionResult:
    geo = _make_geometry_response()
    candidate = GeometryCandidate(model="iep1a", response=geo)
    return GeometrySelectionResult(
        selected=candidate if route_decision == "accepted" else None,
        geometry_trust="high" if route_decision == "accepted" else "low",
        selection_reason="higher_confidence",
        route_decision=route_decision,  # type: ignore[arg-type]
        review_reason=None,
        structural_agreement=True if route_decision == "accepted" else False,
        sanity_results={},
        split_confidence_per_model=None,
        tta_variance_per_model={"iep1a": 0.01},
        page_area_preference_triggered=False,
    )


def _make_invocation_result(route_decision: str = "accepted") -> GeometryInvocationResult:
    geo_a = _make_geometry_response()
    geo_b = _make_geometry_response()
    selection = _make_selection_result(route_decision)
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
    return CircuitBreaker("iep1a"), CircuitBreaker("iep1b"), CircuitBreaker("iep1d")


def _make_google_state(enabled: bool = True) -> GoogleWorkerState:
    config = MagicMock()
    config.processor_id_cleanup = "cleanup-proc-id"
    client = MagicMock()
    return GoogleWorkerState(
        enabled=enabled,
        config=config if enabled else None,
        client=client if enabled else None,
    )


def _make_storage_mock(image: np.ndarray | None = None) -> MagicMock:
    img = image if image is not None else _make_test_image()
    storage = MagicMock()
    storage.get_bytes.return_value = _encode_tiff(img)
    storage.put_bytes.return_value = None
    return storage


def _make_image_loader(h: int = 10000, w: int = 10000) -> Callable[[str], ArtifactImageDimensions]:
    def loader(uri: str) -> ArtifactImageDimensions:
        return ArtifactImageDimensions(width=w, height=h)

    return loader


async def _run(
    *,
    # IEP1D
    iep1d_response_dict: dict[str, Any] | None = None,
    # Second-pass result (fails by default to trigger Google cleanup)
    second_inv_result: GeometryInvocationResult | None = None,
    # Third-pass result (succeeds by default)
    third_inv_result: GeometryInvocationResult | None = None,
    # Normalization result for third pass
    third_norm_outcome: NormalizationOutcome | None = None,
    # Google state
    google_state: GoogleWorkerState | None = None,
    # Google cleanup return value: (bytes | None, dict)
    google_cleanup_return: tuple[bytes | None, dict[str, Any]] | None = None,
    # Storage
    storage: MagicMock | None = None,
    image_loader: Callable[[str], ArtifactImageDimensions] | None = None,
    # URIs
    google_cleanup_output_uri: str | None = _GOOGLE_CLEANUP_URI,
    google_cleanup_proxy_uri: str | None = _GOOGLE_PROXY_URI,
) -> RescueOutcome:
    """
    Run run_rescue_flow with:
    - IEP1D patched via backend mock
    - Second geometry pass: by default produces a structural_disagreement (triggers cleanup)
    - Google state and cleanup patched
    - Third geometry pass and normalization patched
    """
    a_cb, b_cb, d_cb = _make_cbs()

    backend = AsyncMock()
    backend.call.return_value = (
        iep1d_response_dict if iep1d_response_dict is not None else _make_rectify_response_dict()
    )

    sto = storage if storage is not None else _make_storage_mock()
    ldr = image_loader if image_loader is not None else _make_image_loader()
    session = MagicMock()

    # Default: second pass fails (structural_disagreement), third pass accepts
    _second = (
        second_inv_result
        if second_inv_result is not None
        else _make_invocation_result("rectification")  # not "accepted" → triggers Google
    )
    _third = (
        third_inv_result
        if third_inv_result is not None
        else _make_invocation_result("accepted")
    )
    _third_norm = third_norm_outcome if third_norm_outcome is not None else _make_norm_outcome("accept_now")

    # invoke_geometry_services: first call returns second_pass, second call returns third_pass
    _inv_mock = AsyncMock(side_effect=[_second, _third])

    _google_st = google_state if google_state is not None else _make_google_state(enabled=True)

    _cleaned_bytes = _encode_tiff(_make_test_image())
    _cleanup_return = (
        google_cleanup_return
        if google_cleanup_return is not None
        else (_cleaned_bytes, {"success": True, "error": None, "google_response_time_ms": 50.0, "implemented": True})
    )

    with (
        patch(
            "services.eep_worker.app.rescue_step.invoke_geometry_services",
            new=_inv_mock,
        ),
        patch(
            "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
            return_value=_third_norm,
        ),
        patch(
            "services.eep_worker.app.rescue_step.get_google_worker_state",
            return_value=_google_st,
        ),
        patch(
            "services.eep_worker.app.rescue_step.run_google_cleanup",
            new=AsyncMock(return_value=_cleanup_return),
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
            google_cleanup_output_uri=google_cleanup_output_uri,
            google_cleanup_proxy_uri=google_cleanup_proxy_uri,
        )


# ── Tests ────────────────────────────────────────────────────────────────────────


class TestGoogleCleanupFallback:
    @pytest.mark.asyncio
    async def test_second_pass_fails_google_succeeds_third_pass_accepted(self) -> None:
        """
        When the second geometry pass returns a non-accepted route, Google cleanup
        succeeds, and the third geometry pass is accepted → route is accept_now
        with google_cleanup_used=True.
        """
        outcome = await _run()

        assert outcome.route == "accept_now"
        assert outcome.google_cleanup_used is True
        assert outcome.third_selection_result is not None
        assert outcome.third_selection_result.route_decision == "accepted"

    @pytest.mark.asyncio
    async def test_google_cleanup_fails_fallback_to_human(self) -> None:
        """
        When Google cleanup returns (None, {...}), the flow falls back to
        pending_human_correction with the second-pass failure reason.
        """
        outcome = await _run(
            google_cleanup_return=(None, {"success": False, "error": "Google cleanup returned no result (processor not configured or no image in response)", "implemented": False}),
        )

        assert outcome.route == "pending_human_correction"
        assert outcome.google_cleanup_used is False
        assert outcome.third_selection_result is None

    @pytest.mark.asyncio
    async def test_google_disabled_fallback_to_human(self) -> None:
        """
        When GoogleWorkerState.enabled=False, Google cleanup is skipped entirely
        and the flow falls back to pending_human_correction.
        run_google_cleanup must NOT be called.
        """
        disabled_state = _make_google_state(enabled=False)

        with (
            patch(
                "services.eep_worker.app.rescue_step.invoke_geometry_services",
                new=AsyncMock(return_value=_make_invocation_result("rectification")),
            ),
            patch(
                "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
                return_value=_make_norm_outcome("accept_now"),
            ),
            patch(
                "services.eep_worker.app.rescue_step.get_google_worker_state",
                return_value=disabled_state,
            ),
            patch(
                "services.eep_worker.app.rescue_step.run_google_cleanup",
                new=AsyncMock(),
            ) as mock_cleanup,
        ):
            a_cb, b_cb, d_cb = _make_cbs()
            backend = AsyncMock()
            backend.call.return_value = _make_rectify_response_dict()
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
                iep1d_circuit_breaker=d_cb,
                iep1a_circuit_breaker=a_cb,
                iep1b_circuit_breaker=b_cb,
                backend=backend,
                session=MagicMock(),
                storage=_make_storage_mock(),
                image_loader=_make_image_loader(),
                google_cleanup_output_uri=_GOOGLE_CLEANUP_URI,
                google_cleanup_proxy_uri=_GOOGLE_PROXY_URI,
            )

        assert outcome.route == "pending_human_correction"
        assert outcome.google_cleanup_used is False
        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleaned_artifact_is_written_to_storage(self) -> None:
        """
        After a successful Google cleanup call, the cleaned bytes must be written
        to google_cleanup_output_uri via storage.put_bytes().
        """
        storage_mock = _make_storage_mock()
        cleaned_bytes = _encode_tiff(_make_test_image(150, 200))
        cleanup_return = (
            cleaned_bytes,
            {"success": True, "error": None, "google_response_time_ms": 60.0, "implemented": True},
        )

        await _run(
            storage=storage_mock,
            google_cleanup_return=cleanup_return,
        )

        # Verify put_bytes was called with the google_cleanup_output_uri
        put_calls = [c for c in storage_mock.put_bytes.call_args_list if c.args[0] == _GOOGLE_CLEANUP_URI]
        assert len(put_calls) == 1, (
            f"Expected storage.put_bytes called once with {_GOOGLE_CLEANUP_URI!r}, "
            f"calls were: {storage_mock.put_bytes.call_args_list}"
        )
        written_bytes = put_calls[0].args[1]
        assert len(written_bytes) > 0, "Written bytes must be non-empty"

    @pytest.mark.asyncio
    async def test_third_pass_geometry_uses_google_cleanup_proxy_uri(self) -> None:
        """
        The third geometry pass (invoke_geometry_services) must use
        google_cleanup_proxy_uri as the proxy_image_uri, not the rectified proxy.
        """
        with (
            patch(
                "services.eep_worker.app.rescue_step.invoke_geometry_services",
            ) as mock_invoke,
            patch(
                "services.eep_worker.app.rescue_step.run_normalization_and_first_validation",
                return_value=_make_norm_outcome("accept_now"),
            ),
            patch(
                "services.eep_worker.app.rescue_step.get_google_worker_state",
                return_value=_make_google_state(enabled=True),
            ),
            patch(
                "services.eep_worker.app.rescue_step.run_google_cleanup",
                new=AsyncMock(
                    return_value=(
                        _encode_tiff(_make_test_image()),
                        {"success": True, "error": None, "google_response_time_ms": 50.0, "implemented": True},
                    )
                ),
            ),
        ):
            # First call → second pass (non-accepted); second call → third pass (accepted)
            mock_invoke.side_effect = [
                _make_invocation_result("rectification"),  # second pass — not accepted
                _make_invocation_result("accepted"),       # third pass — accepted
            ]

            a_cb, b_cb, d_cb = _make_cbs()
            backend = AsyncMock()
            backend.call.return_value = _make_rectify_response_dict()
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
                session=MagicMock(),
                storage=_make_storage_mock(),
                image_loader=_make_image_loader(),
                google_cleanup_output_uri=_GOOGLE_CLEANUP_URI,
                google_cleanup_proxy_uri=_GOOGLE_PROXY_URI,
            )

        # There should be exactly 2 invoke_geometry_services calls
        assert mock_invoke.call_count == 2, (
            f"Expected 2 geometry invocations (second pass + third pass), got {mock_invoke.call_count}"
        )
        # The SECOND call (third pass) must use the google cleanup proxy URI
        third_pass_call = mock_invoke.call_args_list[1]
        assert third_pass_call.kwargs["proxy_image_uri"] == _GOOGLE_PROXY_URI, (
            f"Third pass must use google_cleanup_proxy_uri={_GOOGLE_PROXY_URI!r}, "
            f"got {third_pass_call.kwargs.get('proxy_image_uri')!r}"
        )
