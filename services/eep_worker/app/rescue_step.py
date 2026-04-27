"""
services/eep_worker/app/rescue_step.py
---------------------------------------
Packet 4.5 — rescue flow: IEP1D rectification, second geometry pass,
second normalization, final artifact validation.

Implements Steps 6, 6.5, and 7 of the EEP worker pipeline (spec Section 6.1)
for a single artifact routed to ``rescue_required`` by Packet 4.4:

  Step 6 — IEP1D rectification:
    1. Call IEP1D POST /v1/rectify on the first-pass normalized artifact.
    2. If IEP1D fails (BackendError, malformed response, circuit breaker open):
       → pending_human_correction (review_reason="rectification_failed").
       No retries (spec Section 8.4: iep1d retry=0).
    3. On success: proceed to Step 6.5.

  Step 6.5 — Second geometry pass:
    4. Load rectified artifact from storage; derive proxy image.
    5. Write proxy to rectified_proxy_uri (caller-supplied storage location).
    6. Call IEP1A and IEP1B in parallel via invoke_geometry_services().
    7. On GeometryServiceError (both fail):
       → pending_human_correction ("geometry_services_failed_post_rectification").
    8. Unexpected split guard (is_split_child=True only):
       If either model reports split_required=True or page_count > 1:
       → pending_human_correction ("geometry_unexpected_split_on_child").
    9. Route check on second-pass selection result:
       structural_agreement=False → "structural_disagreement_post_rectification"
       route_decision="pending_human_correction" → gate's own review_reason
       route_decision="rectification" (low trust) → "low_geometry_trust_post_rectification"
       route_decision="accepted" → proceed to normalization.
    10. Normalize rectified artifact using second-pass geometry (IEP1C, via
        run_normalization_and_first_validation()).

  Step 7 — Final artifact validation:
    11. run_artifact_validation() is invoked inside run_normalization_and_first_validation().
        accept_now  → RescueOutcome(route="accept_now")
        rescue_required → RescueOutcome(route="pending_human_correction",
                          review_reason="artifact_validation_failed")

Caller responsibilities:
    - Obtain artifact_uri from NormalizationOutcome.branch_response.processed_image_uri.
    - Supply rectified_proxy_uri and rescue_output_uri (storage layout is caller's concern).
    - Commit or roll back the DB session (not performed here).
    - Write quality_gate_log rows using second_selection_result and validation_result
      from the returned RescueOutcome (gate logging is the caller's responsibility).

Exported:
    RescueOutcome   — result dataclass
    run_rescue_flow — main entry point (async)
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import ValidationError
from sqlalchemy.orm import Session

from services.eep.app.db.models import ServiceInvocation
from services.eep.app.gates.artifact_validation import (
    ArtifactImageDimensions,
    ArtifactValidationResult,
)
from services.eep.app.db.quality_gate import log_gate
from services.eep.app.gates.geometry_selection import (
    GeometrySelectionResult,
    PreprocessingGateConfig,
    build_geometry_gate_log_record,
)
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.geometry_invocation import (
    GeometryServiceError,
    invoke_geometry_services,
)
from services.eep_worker.app.intake import ProxyConfig, decode_otiff, derive_proxy
from services.eep_worker.app.normalization_step import (
    NormalizationOutcome,
    run_normalization_and_first_validation,
)
from services.eep.app.google.document_ai import run_google_cleanup
from services.eep_worker.app.google_config import get_google_worker_state
from shared.gpu.backend import BackendError, BackendErrorKind, GPUBackend
from shared.io.storage import StorageBackend
from shared.metrics import (
    GOOGLE_CLEANUP_DECISIONS,
    IEP1D_QUALITY_GATE_DECISIONS,
    IEP1D_REJECTION_REASONS,
)
from shared.schemas.geometry import GeometryResponse
from shared.schemas.iep1d import RectifyResponse
from shared.schemas.preprocessing import PreprocessBranchResponse

__all__ = [
    "RescueOutcome",
    "run_rescue_flow",
]

logger = logging.getLogger(__name__)
_IEP1D_CONF_THRESHOLD = 0.6


# ── Result type ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class RescueOutcome:
    """
    Result of the rescue flow (Steps 6, 6.5, and 7).

    Attributes:
        route:                  "accept_now" or "pending_human_correction".
        review_reason:          Canonical reason string when route is
                                "pending_human_correction"; None otherwise.
        branch_response:        PreprocessBranchResponse from the rescue normalization
                                (Step 6.5 IEP1C output).  None when the rescue flow
                                exited before normalization (IEP1D failure, second
                                geometry pass failure).
        validation_result:      ArtifactValidationResult from final validation (Step 7).
                                None when the flow exited before Step 7.
        rectify_response:       RectifyResponse from IEP1D.  None on IEP1D failure.
        second_selection_result: GeometrySelectionResult from the second geometry pass.
                                 None when the flow exited before Step 6.5.
        duration_ms:            Wall-clock time from entering run_rescue_flow to
                                returning, in milliseconds.
    """

    route: Literal["accept_now", "pending_human_correction"]
    review_reason: str | None
    branch_response: PreprocessBranchResponse | None
    validation_result: ArtifactValidationResult | None
    rectify_response: RectifyResponse | None
    second_selection_result: GeometrySelectionResult | None
    duration_ms: float
    google_cleanup_used: bool = dataclasses.field(default=False)
    third_selection_result: GeometrySelectionResult | None = dataclasses.field(default=None)


# ── ServiceInvocation logging (same pattern as geometry_invocation._log_invocation) ──


def _log_invocation(
    session: Session,
    lineage_id: str,
    service_name: str,
    invoked_at: datetime,
    completed_at: datetime,
    duration_ms: float | None,
    status: str,
    error_message: str | None,
    metrics: dict[str, Any] | None,
) -> None:
    """Write one ServiceInvocation row to *session* (caller owns commit/rollback)."""
    record = ServiceInvocation(
        lineage_id=lineage_id,
        service_name=service_name,
        invoked_at=invoked_at,
        completed_at=completed_at,
        processing_time_ms=duration_ms,
        status=status,
        error_message=error_message,
        metrics=metrics,
    )
    session.add(record)


# ── IEP1D invocation ───────────────────────────────────────────────────────────


async def _call_iep1d(
    *,
    artifact_uri: str,
    job_id: str,
    page_number: int,
    material_type: str,
    endpoint: str,
    backend: GPUBackend,
    cb: CircuitBreaker,
    lineage_id: str,
    session: Session,
    execution_timeout_seconds: float | None = None,
) -> tuple[RectifyResponse | None, dict[str, str] | None]:
    """
    Invoke IEP1D POST /v1/rectify.  Never raises — all errors become a (None, error_dict) return.

    Circuit breaker is checked BEFORE the call.  On success, record_success() is called.
    On any failure, record_failure() is called.  One ServiceInvocation row is written
    regardless of outcome.

    Returns:
        (RectifyResponse, None) on success.
        (None, {"kind": ..., "message": ...}) on any failure or circuit breaker open.
    """
    # ── Circuit breaker open → skip ───────────────────────────────────────────
    if not cb.allow_call():
        now = datetime.now(timezone.utc)
        _log_invocation(
            session,
            lineage_id,
            "iep1d",
            now,
            now,
            None,
            "skipped",
            "Circuit breaker open for 'iep1d'",
            None,
        )
        return None, {"kind": "circuit_open", "message": "Circuit breaker open for 'iep1d'"}

    invoked_at = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "job_id": job_id,
        "page_number": page_number,
        "image_uri": artifact_uri,
        "material_type": material_type,
    }

    try:
        raw: dict[str, Any] = await backend.call(
            endpoint,
            payload,
            execution_timeout_seconds=execution_timeout_seconds,
        )
        response = RectifyResponse.model_validate(raw)
        cb.record_success()
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        _log_invocation(
            session,
            lineage_id,
            "iep1d",
            invoked_at,
            completed_at,
            duration_ms,
            "success",
            None,
            {
                "rectified_image_uri": response.rectified_image_uri,
                "rectification_confidence": response.rectification_confidence,
                "skew_residual_before": response.skew_residual_before,
                "skew_residual_after": response.skew_residual_after,
                "border_score_before": response.border_score_before,
                "border_score_after": response.border_score_after,
                "processing_time_ms": response.processing_time_ms,
            },
        )
        return response, None

    except BackendError as exc:
        cb.record_failure(exc.kind)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        is_timeout = exc.kind in (
            BackendErrorKind.COLD_START_TIMEOUT,
            BackendErrorKind.WARM_INFERENCE_TIMEOUT,
        )
        status = "timeout" if is_timeout else "error"
        _log_invocation(
            session,
            lineage_id,
            "iep1d",
            invoked_at,
            completed_at,
            duration_ms,
            status,
            str(exc),
            None,
        )
        return None, {"kind": exc.kind.value, "message": str(exc)}

    except ValidationError as exc:
        cb.record_failure(None)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        msg = f"Malformed rectify response from IEP1D: {exc}"
        _log_invocation(
            session,
            lineage_id,
            "iep1d",
            invoked_at,
            completed_at,
            duration_ms,
            "error",
            msg,
            None,
        )
        return None, {"kind": "malformed_response", "message": msg}

    except Exception as exc:
        cb.record_failure(None)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        msg = f"Unexpected error from IEP1D: {exc}"
        _log_invocation(
            session,
            lineage_id,
            "iep1d",
            invoked_at,
            completed_at,
            duration_ms,
            "error",
            msg,
            None,
        )
        return None, {"kind": "unexpected_error", "message": msg}


# ── Proxy derivation ───────────────────────────────────────────────────────────


def _derive_and_store_proxy(
    rectified_image: np.ndarray,
    material_type: str,
    proxy_uri: str,
    storage: StorageBackend,
    proxy_config: ProxyConfig | None,
) -> tuple[int, int]:
    """
    Derive a proxy of *rectified_image*, encode as PNG, write to storage.

    Args:
        rectified_image: Full-resolution rectified artifact as a numpy BGR array.
        material_type:   Material type string (governs max_long_edge_px).
        proxy_uri:       Storage URI where the proxy PNG is written.
        storage:         StorageBackend instance.
        proxy_config:    ProxyConfig; defaults to ProxyConfig() when None.

    Returns:
        (proxy_height, proxy_width) of the written proxy image.
    """
    proxy_arr = derive_proxy(rectified_image, material_type, proxy_config)
    success, buf = cv2.imencode(".png", proxy_arr)
    if not success:
        raise ValueError("cv2.imencode failed: could not encode proxy to PNG")
    raw: bytes = buf.tobytes()
    storage.put_bytes(proxy_uri, raw)
    proxy_h, proxy_w = proxy_arr.shape[:2]
    return proxy_h, proxy_w


# ── Split guard ─────────────────────────────────────────────────────────────────


def _any_model_reports_split(
    iep1a_result: GeometryResponse | None,
    iep1b_result: GeometryResponse | None,
) -> bool:
    """
    Return True if either result reports split_required=True or page_count > 1.

    Only called when is_split_child=True (spec Section 6.5).
    """
    for result in (iep1a_result, iep1b_result):
        if result is not None and (result.split_required or result.page_count > 1):
            return True
    return False


# ── Route helpers ──────────────────────────────────────────────────────────────


def _pending(
    reason: str,
    *,
    rectify_response: RectifyResponse | None,
    second_selection_result: GeometrySelectionResult | None,
    branch_response: PreprocessBranchResponse | None,
    validation_result: ArtifactValidationResult | None,
    t0: float,
    google_cleanup_used: bool = False,
    third_selection_result: GeometrySelectionResult | None = None,
) -> RescueOutcome:
    """Construct a pending_human_correction RescueOutcome."""
    return RescueOutcome(
        route="pending_human_correction",
        review_reason=reason,
        branch_response=branch_response,
        validation_result=validation_result,
        rectify_response=rectify_response,
        second_selection_result=second_selection_result,
        duration_ms=(time.monotonic() - t0) * 1000.0,
        google_cleanup_used=google_cleanup_used,
        third_selection_result=third_selection_result,
    )


# ── Google cleanup helpers ─────────────────────────────────────────────────────


async def _call_google_cleanup(
    *,
    image_bytes: bytes,
    job_id: str,
    config: Any,
) -> tuple[bytes | None, str]:
    """
    Invoke Google Document AI cleanup.  Never raises.

    Returns:
        (cleaned_bytes, status_label) where status_label is one of:
          "cleanup_accepted" — cleaned_bytes is non-empty
          "cleanup_failed"   — Google returned None or empty bytes
    """
    try:
        cleaned, meta = await run_google_cleanup(image_bytes, job_id=job_id, config=config)
    except Exception as exc:
        logger.warning({"event": "google_cleanup_error", "error": str(exc)})
        GOOGLE_CLEANUP_DECISIONS.labels(decision="cleanup_failed").inc()
        return None, "cleanup_failed"

    if not cleaned:
        logger.info(
            {
                "event": "google_cleanup_result",
                "decision": "cleanup_failed",
                "meta": meta,
            }
        )
        GOOGLE_CLEANUP_DECISIONS.labels(decision="cleanup_failed").inc()
        return None, "cleanup_failed"

    logger.info(
        {
            "event": "google_cleanup_result",
            "decision": "cleanup_accepted",
            "bytes": len(cleaned),
            "meta": meta,
        }
    )
    GOOGLE_CLEANUP_DECISIONS.labels(decision="cleanup_accepted").inc()
    return cleaned, "cleanup_accepted"


async def _run_google_third_pass(
    *,
    raw_bytes: bytes,
    job_id: str,
    page_number: int,
    lineage_id: str,
    material_type: str,
    google_cleanup_output_uri: str,
    google_cleanup_proxy_uri: str,
    rescue_output_uri: str,
    iep1a_endpoint: str,
    iep1b_endpoint: str,
    iep1a_circuit_breaker: CircuitBreaker,
    iep1b_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    session: Session,
    storage: StorageBackend,
    image_loader: Callable[[str], ArtifactImageDimensions],
    is_split_child: bool,
    page_index: int,
    proxy_config: ProxyConfig | None,
    gate_config: PreprocessingGateConfig | None,
    rectify_response: RectifyResponse | None,
    second_selection_result: GeometrySelectionResult | None,
    t0: float,
    config: Any,
) -> RescueOutcome | None:
    """
    Run Google cleanup + third geometry pass.

    Returns a RescueOutcome when the third pass completes (success or failure).
    Returns None when Google cleanup itself produced no bytes — caller should
    fall through to the existing pending_human_correction path.
    """
    cleaned_bytes, _status = await _call_google_cleanup(
        image_bytes=raw_bytes,
        job_id=job_id,
        config=config,
    )
    if cleaned_bytes is None:
        return None

    # ── Store cleaned artifact ────────────────────────────────────────────────
    storage.put_bytes(google_cleanup_output_uri, cleaned_bytes)
    cleaned_image = decode_otiff(cleaned_bytes, uri=google_cleanup_output_uri)

    # ── Derive proxy for third geometry pass ─────────────────────────────────
    proxy_h3, proxy_w3 = _derive_and_store_proxy(
        cleaned_image, material_type, google_cleanup_proxy_uri, storage, proxy_config
    )

    # ── Third geometry pass (IEP1A + IEP1B) ──────────────────────────────────
    try:
        third_invocation = await invoke_geometry_services(
            job_id=job_id,
            page_number=page_number,
            lineage_id=lineage_id,
            proxy_image_uri=google_cleanup_proxy_uri,
            material_type=material_type,
            proxy_width=proxy_w3,
            proxy_height=proxy_h3,
            iep1a_endpoint=iep1a_endpoint,
            iep1b_endpoint=iep1b_endpoint,
            iep1a_circuit_breaker=iep1a_circuit_breaker,
            iep1b_circuit_breaker=iep1b_circuit_breaker,
            backend=backend,
            session=session,
            gate_config=gate_config,
        )
    except GeometryServiceError:
        return _pending(
            "geometry_services_failed_post_google_cleanup",
            rectify_response=rectify_response,
            second_selection_result=second_selection_result,
            branch_response=None,
            validation_result=None,
            t0=t0,
            google_cleanup_used=True,
        )

    third_selection = third_invocation.selection_result

    # ── Unexpected split guard (split children only) ──────────────────────────
    if is_split_child and _any_model_reports_split(
        third_invocation.iep1a_result, third_invocation.iep1b_result
    ):
        return _pending(
            "geometry_unexpected_split_on_child",
            rectify_response=rectify_response,
            second_selection_result=second_selection_result,
            branch_response=None,
            validation_result=None,
            t0=t0,
            google_cleanup_used=True,
            third_selection_result=third_selection,
        )

    # ── Route check on third-pass selection ──────────────────────────────────
    if third_selection.route_decision != "accepted":
        if third_selection.structural_agreement is False:
            third_reason = "structural_disagreement_post_google_cleanup"
        elif third_selection.route_decision == "pending_human_correction":
            third_reason = third_selection.review_reason or "pending_human_correction"
        else:
            third_reason = "low_geometry_trust_post_google_cleanup"
        return _pending(
            third_reason,
            rectify_response=rectify_response,
            second_selection_result=second_selection_result,
            branch_response=None,
            validation_result=None,
            t0=t0,
            google_cleanup_used=True,
            third_selection_result=third_selection,
        )

    # ── Third-pass IEP1C normalization ────────────────────────────────────────
    assert (
        third_selection.selected is not None
    ), "route_decision=='accepted' guarantees a selected candidate"
    third_norm: NormalizationOutcome = run_normalization_and_first_validation(
        full_res_image=cleaned_image,
        selected_geometry=third_selection.selected.response,
        selected_model=third_selection.selected.model,
        geometry_route_decision=third_selection.route_decision,
        proxy_width=proxy_w3,
        proxy_height=proxy_h3,
        output_uri=rescue_output_uri,
        storage=storage,
        image_loader=image_loader,
        page_index=page_index,
        gate_config=gate_config,
    )

    if third_norm.route == "accept_now":
        return RescueOutcome(
            route="accept_now",
            review_reason=None,
            branch_response=third_norm.branch_response,
            validation_result=third_norm.validation_result,
            rectify_response=rectify_response,
            second_selection_result=second_selection_result,
            duration_ms=(time.monotonic() - t0) * 1000.0,
            google_cleanup_used=True,
            third_selection_result=third_selection,
        )

    return RescueOutcome(
        route="pending_human_correction",
        review_reason="artifact_validation_failed",
        branch_response=third_norm.branch_response,
        validation_result=third_norm.validation_result,
        rectify_response=rectify_response,
        second_selection_result=second_selection_result,
        duration_ms=(time.monotonic() - t0) * 1000.0,
        google_cleanup_used=True,
        third_selection_result=third_selection,
    )


# ── Main entry point ───────────────────────────────────────────────────────────


async def run_rescue_flow(
    *,
    artifact_uri: str,
    job_id: str,
    page_number: int,
    lineage_id: str,
    material_type: str,
    rectified_proxy_uri: str,
    rescue_output_uri: str,
    iep1d_endpoint: str,
    iep1a_endpoint: str,
    iep1b_endpoint: str,
    iep1d_circuit_breaker: CircuitBreaker,
    iep1a_circuit_breaker: CircuitBreaker,
    iep1b_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    session: Session,
    storage: StorageBackend,
    image_loader: Callable[[str], ArtifactImageDimensions],
    iep1d_execution_timeout_seconds: float | None = None,
    is_split_child: bool = False,
    page_index: int = 0,
    proxy_config: ProxyConfig | None = None,
    gate_config: PreprocessingGateConfig | None = None,
    google_cleanup_output_uri: str | None = None,
    google_cleanup_proxy_uri: str | None = None,
) -> RescueOutcome:
    """
    Execute Steps 6, 6.5, and 7 of the EEP pipeline: rectify → second geometry pass →
    second normalization → final validation.

    Step 6 — IEP1D rectification:
        Calls IEP1D with the first-pass normalized artifact URI.  Any failure (BackendError,
        malformed response, circuit breaker open) routes to pending_human_correction
        immediately.  No retries (spec Section 8.4: iep1d retry=0).

    Step 6.5 — Second geometry pass:
        Loads the rectified artifact, derives a proxy, writes the proxy to
        rectified_proxy_uri, and calls IEP1A + IEP1B in parallel via
        invoke_geometry_services().  GeometryServiceError (both models fail),
        an unexpected split on a split child, or a non-accepted route decision
        all route to pending_human_correction with appropriate review_reasons.

    Step 7 — Final artifact validation:
        Embedded in run_normalization_and_first_validation().  A rescue_required
        outcome from the normalization step maps to pending_human_correction with
        review_reason="artifact_validation_failed".

    Args:
        artifact_uri:           URI of the first-pass normalized artifact (IEP1D input).
        job_id:                 Parent job identifier.
        page_number:            1-indexed page number.
        lineage_id:             FK for ServiceInvocation audit rows.
        material_type:          Job material type string.
        rectified_proxy_uri:    Storage URI where the proxy of the rectified image is written.
        rescue_output_uri:      Storage URI where the rescue-normalized artifact is written.
        iep1d_endpoint:         Full HTTP URL for IEP1D POST /v1/rectify.
        iep1a_endpoint:         Full HTTP URL for IEP1A.
        iep1b_endpoint:         Full HTTP URL for IEP1B.
        iep1d_circuit_breaker:  Per-worker CircuitBreaker for IEP1D.
        iep1a_circuit_breaker:  Per-worker CircuitBreaker for IEP1A.
        iep1b_circuit_breaker:  Per-worker CircuitBreaker for IEP1B.
        backend:                Shared GPUBackend instance.
        iep1d_execution_timeout_seconds:
                                Optional warm-inference timeout override for the
                                IEP1D rectification call only.
        session:                SQLAlchemy session (caller owns commit/rollback).
        storage:                StorageBackend instance.
        image_loader:           Callable(uri) → ArtifactImageDimensions; used by the
                                final artifact validation gate (Step 7).
        is_split_child:         True when this artifact is a split child from Step 2.6.
                                Enables the unexpected-split guard in Step 6.5.
        page_index:             Index into selected_geometry.pages used by
                                run_normalization_and_first_validation.
        proxy_config:           ProxyConfig for proxy derivation; defaults to ProxyConfig().
        gate_config:            Policy thresholds; defaults to PreprocessingGateConfig().

    Returns:
        RescueOutcome with route, review_reason, branch_response, validation_result,
        rectify_response, second_selection_result, and duration_ms.

    Raises:
        Any exception from storage.get_bytes(), decode_otiff(), or
        run_normalization_and_first_validation() propagates unchanged (infrastructure
        failures — the caller decides retry vs. fail).
    """
    t0 = time.monotonic()

    logger.info(
        {
            "event": "rescue_flow_start",
            "job_id": job_id,
            "page_number": page_number,
            "iep1d_input_artifact_uri": artifact_uri,
        }
    )

    # ── Step 6 — IEP1D rectification ─────────────────────────────────────────
    rectify_response, _iep1d_error = await _call_iep1d(
        artifact_uri=artifact_uri,
        job_id=job_id,
        page_number=page_number,
        material_type=material_type,
        endpoint=iep1d_endpoint,
        backend=backend,
        execution_timeout_seconds=iep1d_execution_timeout_seconds,
        cb=iep1d_circuit_breaker,
        lineage_id=lineage_id,
        session=session,
    )

    if rectify_response is None:
        return _pending(
            "rectification_failed",
            rectify_response=None,
            second_selection_result=None,
            branch_response=None,
            validation_result=None,
            t0=t0,
        )

    # ── Step 6 — IEP1D quality gate (advisory mode) ───────────────────────────
    _r_low_conf = rectify_response.rectification_confidence < _IEP1D_CONF_THRESHOLD
    _r_skew = rectify_response.skew_residual_after >= rectify_response.skew_residual_before
    _r_border = rectify_response.border_score_after < rectify_response.border_score_before
    _r_warning = (
        "skew_residual_not_improved" in rectify_response.warnings
        or "border_score_not_improved" in rectify_response.warnings
    )
    _use_rectified = not (_r_low_conf or _r_skew or _r_border or _r_warning)
    _iep1d_decision = "rectified_accepted" if _use_rectified else "rectification_rejected"

    _rejection_reasons = [
        reason
        for reason, active in (
            ("low_confidence", _r_low_conf),
            ("skew_not_improved", _r_skew),
            ("border_regressed", _r_border),
            ("warning_veto", _r_warning),
        )
        if active
    ]
    logger.info(
        {
            "event": "iep1d_decision",
            "decision": _iep1d_decision,
            "confidence": rectify_response.rectification_confidence,
            "skew_before": rectify_response.skew_residual_before,
            "skew_after": rectify_response.skew_residual_after,
            "border_before": rectify_response.border_score_before,
            "border_after": rectify_response.border_score_after,
            "warnings": rectify_response.warnings,
            "rejection_reasons": _rejection_reasons,
        }
    )
    IEP1D_QUALITY_GATE_DECISIONS.labels(decision=_iep1d_decision).inc()
    for _reason in _rejection_reasons:
        IEP1D_REJECTION_REASONS.labels(reason=_reason).inc()

    _artifact_uri = rectify_response.rectified_image_uri if _use_rectified else artifact_uri

    logger.info(
        {
            "event": "rescue_iep1d_artifact_choice",
            "job_id": job_id,
            "page_number": page_number,
            "use_rectified": _use_rectified,
            "chosen_artifact_uri": _artifact_uri,
            "iep1d_rectified_uri": rectify_response.rectified_image_uri,
            "first_pass_uri": artifact_uri,
        }
    )

    # ── Step 6.5 — Load chosen artifact and derive proxy ─────────────────────
    raw_bytes = storage.get_bytes(_artifact_uri)
    rectified_image = decode_otiff(raw_bytes, uri=_artifact_uri)

    _rect_h, _rect_w = rectified_image.shape[:2]
    logger.info(
        {
            "event": "rescue_rectified_image_loaded",
            "job_id": job_id,
            "page_number": page_number,
            "chosen_artifact_uri": _artifact_uri,
            "rectified_image_wh": [_rect_w, _rect_h],
        }
    )

    proxy_h, proxy_w = _derive_and_store_proxy(
        rectified_image, material_type, rectified_proxy_uri, storage, proxy_config
    )

    logger.info(
        {
            "event": "rescue_proxy_derived",
            "job_id": job_id,
            "page_number": page_number,
            "rectified_proxy_uri": rectified_proxy_uri,
            "proxy_wh": [proxy_w, proxy_h],
        }
    )

    # ── Step 6.5 — Second geometry pass (IEP1A + IEP1B in parallel) ──────────
    try:
        invocation_result = await invoke_geometry_services(
            job_id=job_id,
            page_number=page_number,
            lineage_id=lineage_id,
            proxy_image_uri=rectified_proxy_uri,
            material_type=material_type,
            proxy_width=proxy_w,
            proxy_height=proxy_h,
            iep1a_endpoint=iep1a_endpoint,
            iep1b_endpoint=iep1b_endpoint,
            iep1a_circuit_breaker=iep1a_circuit_breaker,
            iep1b_circuit_breaker=iep1b_circuit_breaker,
            backend=backend,
            session=session,
            gate_config=gate_config,
        )
    except GeometryServiceError:
        return _pending(
            "geometry_services_failed_post_rectification",
            rectify_response=rectify_response,
            second_selection_result=None,
            branch_response=None,
            validation_result=None,
            t0=t0,
        )

    selection_result = invocation_result.selection_result

    # ── Log post-rectification geometry gate ──────────────────────────────────
    post_gate_record = build_geometry_gate_log_record(
        result=selection_result,
        job_id=job_id,
        page_number=page_number,
        gate_type="geometry_selection_post_rectification",
        iep1a_response=invocation_result.iep1a_result,
        iep1b_response=invocation_result.iep1b_result,
        processing_time_ms=(time.monotonic() - t0) * 1000.0,
    )
    log_gate(session, **post_gate_record)

    # ── Step 6.5 — Unexpected split guard (split children only) ──────────────
    if is_split_child and _any_model_reports_split(
        invocation_result.iep1a_result, invocation_result.iep1b_result
    ):
        return _pending(
            "geometry_unexpected_split_on_child",
            rectify_response=rectify_response,
            second_selection_result=selection_result,
            branch_response=None,
            validation_result=None,
            t0=t0,
        )

    # ── Step 6.5 — Route check on second-pass selection ───────────────────────
    if selection_result.route_decision != "accepted":
        if selection_result.structural_agreement is False:
            reason = "structural_disagreement_post_rectification"
        elif selection_result.route_decision == "pending_human_correction":
            reason = selection_result.review_reason or "pending_human_correction"
        else:
            # route_decision == "rectification" (low geometry trust after rescue)
            reason = "low_geometry_trust_post_rectification"

        # ── Google cleanup fallback (Step 5 → third geometry pass) ───────────
        if google_cleanup_output_uri and google_cleanup_proxy_uri:
            _gs = get_google_worker_state()
            if _gs.enabled and _gs.config:
                _google_outcome = await _run_google_third_pass(
                    raw_bytes=raw_bytes,
                    job_id=job_id,
                    page_number=page_number,
                    lineage_id=lineage_id,
                    material_type=material_type,
                    google_cleanup_output_uri=google_cleanup_output_uri,
                    google_cleanup_proxy_uri=google_cleanup_proxy_uri,
                    rescue_output_uri=rescue_output_uri,
                    iep1a_endpoint=iep1a_endpoint,
                    iep1b_endpoint=iep1b_endpoint,
                    iep1a_circuit_breaker=iep1a_circuit_breaker,
                    iep1b_circuit_breaker=iep1b_circuit_breaker,
                    backend=backend,
                    session=session,
                    storage=storage,
                    image_loader=image_loader,
                    is_split_child=is_split_child,
                    page_index=page_index,
                    proxy_config=proxy_config,
                    gate_config=gate_config,
                    rectify_response=rectify_response,
                    second_selection_result=selection_result,
                    t0=t0,
                    config=_gs.config,
                )
                if _google_outcome is not None:
                    return _google_outcome

        return _pending(
            reason,
            rectify_response=rectify_response,
            second_selection_result=selection_result,
            branch_response=None,
            validation_result=None,
            t0=t0,
        )

    # ── Step 6.5 — Second IEP1C normalization ─────────────────────────────────
    assert (
        selection_result.selected is not None
    ), "route_decision=='accepted' guarantees a selected candidate"
    norm_outcome: NormalizationOutcome = run_normalization_and_first_validation(
        full_res_image=rectified_image,
        selected_geometry=selection_result.selected.response,
        selected_model=selection_result.selected.model,
        geometry_route_decision=selection_result.route_decision,
        proxy_width=proxy_w,
        proxy_height=proxy_h,
        output_uri=rescue_output_uri,
        storage=storage,
        image_loader=image_loader,
        page_index=page_index,
        gate_config=gate_config,
    )

    logger.info(
        {
            "event": "rescue_normalization_complete",
            "job_id": job_id,
            "page_number": page_number,
            "rescue_output_uri": rescue_output_uri,
            "norm_route": norm_outcome.route,
            "artifact_validation_passed": norm_outcome.validation_result.passed,
            "processing_time_ms": norm_outcome.duration_ms,
        }
    )

    # ── Step 7 — Final validation routing ─────────────────────────────────────
    if norm_outcome.route == "accept_now":
        return RescueOutcome(
            route="accept_now",
            review_reason=None,
            branch_response=norm_outcome.branch_response,
            validation_result=norm_outcome.validation_result,
            rectify_response=rectify_response,
            second_selection_result=selection_result,
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )

    # norm_outcome.route == "rescue_required" → final validation failed
    # ── Google cleanup fallback (Step 5 → third geometry pass) ─────────────────
    if google_cleanup_output_uri and google_cleanup_proxy_uri:
        _gs = get_google_worker_state()
        if _gs.enabled and _gs.config:
            _google_outcome = await _run_google_third_pass(
                raw_bytes=raw_bytes,
                job_id=job_id,
                page_number=page_number,
                lineage_id=lineage_id,
                material_type=material_type,
                google_cleanup_output_uri=google_cleanup_output_uri,
                google_cleanup_proxy_uri=google_cleanup_proxy_uri,
                rescue_output_uri=rescue_output_uri,
                iep1a_endpoint=iep1a_endpoint,
                iep1b_endpoint=iep1b_endpoint,
                iep1a_circuit_breaker=iep1a_circuit_breaker,
                iep1b_circuit_breaker=iep1b_circuit_breaker,
                backend=backend,
                session=session,
                storage=storage,
                image_loader=image_loader,
                is_split_child=is_split_child,
                page_index=page_index,
                proxy_config=proxy_config,
                gate_config=gate_config,
                rectify_response=rectify_response,
                second_selection_result=selection_result,
                t0=t0,
                config=_gs.config,
            )
            if _google_outcome is not None:
                return _google_outcome

    return RescueOutcome(
        route="pending_human_correction",
        review_reason="artifact_validation_failed",
        branch_response=norm_outcome.branch_response,
        validation_result=norm_outcome.validation_result,
        rectify_response=rectify_response,
        second_selection_result=selection_result,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
