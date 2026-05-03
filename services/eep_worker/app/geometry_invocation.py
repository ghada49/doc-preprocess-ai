"""
services/eep_worker/app/geometry_invocation.py
-----------------------------------------------
Packet 4.3b — parallel IEP1A + IEP1B geometry invocation and selection wiring.

Implements Step 3 of the EEP worker pipeline (spec Section 6.1, Step 3):

  1. Invoke IEP1A and IEP1B concurrently (asyncio.gather).
  2. Wrap each call in its own CircuitBreaker — checked before, recorded after.
  3. Classify failures: timeout (COLD_START_TIMEOUT / WARM_INFERENCE_TIMEOUT)
     or error (SERVICE_ERROR, malformed Pydantic response, unexpected exception).
  4. Write one ServiceInvocation DB row per service (success / timeout / error / skipped).
  5. Pass the (possibly partial) results to run_geometry_selection() exactly once.
  6. Return GeometryInvocationResult.

Failure semantics (spec Section 6.8 + 18.x):
  One service fails  → continue with the other (LOW TRUST → gate returns "rectification")
  Both services fail → raise GeometryServiceError (caller decides retry vs. fail)
  Malformed response → treated as service failure; circuit breaker records it

Safety invariants (NON-NEGOTIABLE):
  - No single-model auto-acceptance (gate enforces this — run_geometry_selection
    always sets geometry_trust="low" when only one model is present).
  - No routing to "accepted" or "failed" from this layer.
  - run_geometry_selection() is called exactly once, never bypassed.

Exported:
    GeometryServiceError      — both services produced no usable result
    GeometryInvocationResult  — structured result from invoke_geometry_services()
    invoke_geometry_services  — main entry point (async)
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import ValidationError
from sqlalchemy.orm import Session

from monitoring.drift_observer import observe_and_check
from services.eep.app.db.models import ServiceInvocation
from shared.metrics import (
    EEP_GEOMETRY_SELECTION_ROUTE,
    IEP1A_GEOMETRY_CONFIDENCE,
    IEP1A_PAGE_COUNT,
    IEP1A_SPLIT_DETECTION_RATE,
    IEP1A_TTA_PREDICTION_VARIANCE,
    IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE,
    IEP1B_GEOMETRY_CONFIDENCE,
    IEP1B_PAGE_COUNT,
    IEP1B_SPLIT_DETECTION_RATE,
    IEP1B_TTA_PREDICTION_VARIANCE,
    IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE,
)
from services.eep.app.gates.geometry_selection import (
    GeometrySelectionResult,
    PreprocessingGateConfig,
    _area_fraction_bounds_for_material,
    run_geometry_selection,
)
from services.eep_worker.app.circuit_breaker import CircuitBreaker
from services.eep_worker.app.presigned_inputs import maybe_presign_input_uri
from shared.gpu.backend import BackendError, BackendErrorKind, GPUBackend
from shared.schemas.geometry import GeometryResponse

__all__ = [
    "GeometryServiceError",
    "GeometryInvocationResult",
    "invoke_geometry_services",
]


# ── Exceptions ─────────────────────────────────────────────────────────────────


class GeometryServiceError(RuntimeError):
    """
    Raised when both IEP1A and IEP1B produced no usable result.

    This is an infrastructure failure condition.  The caller must decide
    whether to retry the page (transient errors) or route to ``failed``
    after the retry budget is exhausted.

    Attributes:
        job_id:       Job identifier for the failing page.
        page_number:  1-indexed page number.
        iep1a_error:  Per-service error dict from IEP1A (may be None if skipped).
        iep1b_error:  Per-service error dict from IEP1B (may be None if skipped).
    """

    def __init__(
        self,
        *,
        job_id: str,
        page_number: int,
        iep1a_error: dict[str, str] | None,
        iep1b_error: dict[str, str] | None,
    ) -> None:
        super().__init__(
            f"Both geometry services failed for job={job_id!r} page={page_number}: "
            f"iep1a={iep1a_error}, iep1b={iep1b_error}"
        )
        self.job_id = job_id
        self.page_number = page_number
        self.iep1a_error = iep1a_error
        self.iep1b_error = iep1b_error


# ── Result types ───────────────────────────────────────────────────────────────


@dataclasses.dataclass
class GeometryInvocationResult:
    """
    Structured result from invoke_geometry_services().

    Contains per-service outcomes, error metadata, timing, and the
    geometry selection gate result.

    Attributes:
        iep1a_result:      GeometryResponse from IEP1A; None on failure / skipped.
        iep1b_result:      GeometryResponse from IEP1B; None on failure / skipped.
        iep1a_error:       Error dict {"kind": ..., "message": ...} if IEP1A failed.
        iep1b_error:       Error dict if IEP1B failed.
        iep1a_skipped:     True if IEP1A was skipped (circuit breaker open).
        iep1b_skipped:     True if IEP1B was skipped.
        iep1a_duration_ms: Wall-clock ms for the IEP1A call; None if skipped.
        iep1b_duration_ms: Wall-clock ms for the IEP1B call; None if skipped.
        selection_result:  Output of run_geometry_selection(); always present when
                           at least one service succeeded (i.e. not raised).
    """

    iep1a_result: GeometryResponse | None
    iep1b_result: GeometryResponse | None
    iep1a_error: dict[str, str] | None
    iep1b_error: dict[str, str] | None
    iep1a_skipped: bool
    iep1b_skipped: bool
    iep1a_duration_ms: float | None
    iep1b_duration_ms: float | None
    selection_result: GeometrySelectionResult


# ── Internal per-service outcome ───────────────────────────────────────────────


@dataclasses.dataclass
class _ServiceCallOutcome:
    """Internal carrier for the result of a single _invoke_one() call."""

    response: GeometryResponse | None
    duration_ms: float | None
    error: dict[str, str] | None
    skipped: bool
    status: str  # "success" | "timeout" | "error" | "skipped"


# ── Invocation logging ─────────────────────────────────────────────────────────


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
    service_version: str | None = None,
    model_version: str | None = None,
    model_source: str | None = None,
    config_snapshot: dict[str, Any] | None = None,
) -> None:
    """
    Write a ServiceInvocation row to *session*.

    The row is immutable once written (spec Section 13).  The caller owns
    commit/rollback — this function only adds the object to the session.

    Args:
        lineage_id:    FK linking to page_lineage; provides traceability.
        service_name:  "iep1a" or "iep1b".
        invoked_at:    UTC timestamp immediately before the backend call.
        completed_at:  UTC timestamp immediately after the call (= invoked_at
                       for skipped calls that make no network request).
        duration_ms:   Wall-clock time of the backend call in milliseconds;
                       None for skipped invocations.
        status:        "success" | "timeout" | "error" | "skipped".
        error_message: Exception message when status != "success"; None otherwise.
        metrics:       Optional JSONB metrics dict.
    """
    record = ServiceInvocation(
        lineage_id=lineage_id,
        service_name=service_name,
        service_version=service_version,
        model_version=model_version,
        model_source=model_source,
        invoked_at=invoked_at,
        completed_at=completed_at,
        processing_time_ms=duration_ms,
        status=status,
        error_message=error_message,
        metrics=metrics,
        config_snapshot=config_snapshot,
    )
    session.add(record)


# ── Single-service async call ──────────────────────────────────────────────────


async def _invoke_one(
    service_name: str,
    endpoint: str,
    payload: dict[str, Any],
    backend: GPUBackend,
    cb: CircuitBreaker,
    lineage_id: str,
    session: Session,
) -> _ServiceCallOutcome:
    """
    Invoke one geometry service, guarded by its circuit breaker.

    Always returns a _ServiceCallOutcome — never raises.  All exceptions from
    the backend call and Pydantic parsing are caught and translated into an
    appropriate outcome status.

    Circuit breaker is checked BEFORE the call.  On success, record_success()
    is called.  On any failure (BackendError, ValidationError, or unexpected
    exception), record_failure() is called with the appropriate kind.

    Args:
        service_name: "iep1a" or "iep1b" — stored in ServiceInvocation.
        endpoint:     Full HTTP URL for the geometry service.
        payload:      JSON-serialisable dict sent to the backend.
        backend:      Shared GPUBackend instance (LocalHTTPBackend in production).
        cb:           CircuitBreaker instance for this service.
        lineage_id:   FK for ServiceInvocation logging.
        session:      SQLAlchemy session for log writes.
    """
    # ── Circuit breaker open → skip without calling backend ──────────────────
    if not cb.allow_call():
        now = datetime.now(timezone.utc)
        _log_invocation(
            session,
            lineage_id,
            service_name,
            now,
            now,
            None,
            "skipped",
            f"Circuit breaker open for {service_name!r}",
            None,
        )
        return _ServiceCallOutcome(
            response=None,
            duration_ms=None,
            error={"kind": "circuit_open", "message": f"Circuit breaker open for {service_name!r}"},
            skipped=True,
            status="skipped",
        )

    invoked_at = datetime.now(timezone.utc)

    # ── Backend call ──────────────────────────────────────────────────────────
    try:
        raw: dict[str, Any] = await backend.call(endpoint, payload)
        response = GeometryResponse.model_validate(raw)
        cb.record_success()
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        _log_invocation(
            session,
            lineage_id,
            service_name,
            invoked_at,
            completed_at,
            duration_ms,
            "success",
            None,
            {
                "page_count": response.page_count,
                "split_required": response.split_required,
                "geometry_confidence": response.geometry_confidence,
                "tta_structural_agreement_rate": response.tta_structural_agreement_rate,
                "tta_prediction_variance": response.tta_prediction_variance,
            },
            response.service_version,
            response.model_version,
            response.model_source,
            {
                "endpoint": endpoint,
                "material_type": payload.get("material_type"),
            },
        )
        return _ServiceCallOutcome(
            response=response,
            duration_ms=duration_ms,
            error=None,
            skipped=False,
            status="success",
        )

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
            service_name,
            invoked_at,
            completed_at,
            duration_ms,
            status,
            str(exc),
            None,
        )
        return _ServiceCallOutcome(
            response=None,
            duration_ms=duration_ms,
            error={"kind": exc.kind.value, "message": str(exc)},
            skipped=False,
            status=status,
        )

    except ValidationError as exc:
        # Malformed response — schema does not match GeometryResponse.
        # Treated as a service failure; circuit breaker records it.
        cb.record_failure(None)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        msg = f"Malformed geometry response from {service_name!r}: {exc}"
        _log_invocation(
            session,
            lineage_id,
            service_name,
            invoked_at,
            completed_at,
            duration_ms,
            "error",
            msg,
            None,
        )
        return _ServiceCallOutcome(
            response=None,
            duration_ms=duration_ms,
            error={"kind": "malformed_response", "message": msg},
            skipped=False,
            status="error",
        )

    except Exception as exc:  # unexpected — still must not propagate
        cb.record_failure(None)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - invoked_at).total_seconds() * 1000
        msg = f"Unexpected error from {service_name!r}: {exc}"
        _log_invocation(
            session,
            lineage_id,
            service_name,
            invoked_at,
            completed_at,
            duration_ms,
            "error",
            msg,
            None,
        )
        return _ServiceCallOutcome(
            response=None,
            duration_ms=duration_ms,
            error={"kind": "unexpected_error", "message": msg},
            skipped=False,
            status="error",
        )


# ── Drift observation ─────────────────────────────────────────────────────────


def _observe_geometry_metrics(
    iep1a: GeometryResponse | None,
    iep1b: GeometryResponse | None,
    session: Session,
    selection_result: GeometrySelectionResult | None = None,
    route_decision: str | None = None,
) -> None:
    """
    Feed geometry and selection-gate metrics into the drift detector.

    Called once per successful ``invoke_geometry_services`` invocation.
    Each ``observe_and_check`` call is already wrapped in a try/except inside
    drift_observer — any individual failure is logged and suppressed, so this
    helper can never break the caller.

    Metrics observed:
      iep1a.geometry_confidence, iep1a.tta_structural_agreement_rate,
      iep1a.tta_prediction_variance, iep1a.split_detection_rate  (if IEP1A succeeded)
      iep1b.*  (same four fields, if IEP1B succeeded)
      eep.structural_agreement_rate  (binary: 1.0 when both models agree)
      all eep.geometry_selection_route.* fractions (binary one-vs-rest)
    """
    if iep1a is not None:
        IEP1A_GEOMETRY_CONFIDENCE.observe(iep1a.geometry_confidence)
        IEP1A_PAGE_COUNT.observe(float(iep1a.page_count))
        IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE.observe(iep1a.tta_structural_agreement_rate)
        IEP1A_TTA_PREDICTION_VARIANCE.observe(iep1a.tta_prediction_variance)
        if iep1a.split_required:
            IEP1A_SPLIT_DETECTION_RATE.inc()
        observe_and_check("iep1a.geometry_confidence", iep1a.geometry_confidence, session)
        observe_and_check(
            "iep1a.tta_structural_agreement_rate", iep1a.tta_structural_agreement_rate, session
        )
        observe_and_check("iep1a.tta_prediction_variance", iep1a.tta_prediction_variance, session)
        observe_and_check("iep1a.split_detection_rate", float(iep1a.split_required), session)
    if iep1b is not None:
        IEP1B_GEOMETRY_CONFIDENCE.observe(iep1b.geometry_confidence)
        IEP1B_PAGE_COUNT.observe(float(iep1b.page_count))
        IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE.observe(iep1b.tta_structural_agreement_rate)
        IEP1B_TTA_PREDICTION_VARIANCE.observe(iep1b.tta_prediction_variance)
        if iep1b.split_required:
            IEP1B_SPLIT_DETECTION_RATE.inc()
        observe_and_check("iep1b.geometry_confidence", iep1b.geometry_confidence, session)
        observe_and_check(
            "iep1b.tta_structural_agreement_rate", iep1b.tta_structural_agreement_rate, session
        )
        observe_and_check("iep1b.tta_prediction_variance", iep1b.tta_prediction_variance, session)
        observe_and_check("iep1b.split_detection_rate", float(iep1b.split_required), session)
    if iep1a is not None and iep1b is not None:
        structurally_agreed = (
            iep1a.page_count == iep1b.page_count
            and iep1a.split_required == iep1b.split_required
        )
        observe_and_check("eep.structural_agreement_rate", float(structurally_agreed), session)

    route_decision = route_decision or (
        selection_result.route_decision if selection_result is not None else "unknown"
    )
    route_flags = {
        "accepted_fraction": route_decision == "accepted",
        "review_fraction": route_decision in {"pending_human_correction", "review"},
        "structural_disagreement_fraction": (
            selection_result.structural_agreement is False
            if selection_result is not None
            else route_decision == "structural_disagreement"
        ),
        "sanity_failed_fraction": (
            selection_result.review_reason == "geometry_sanity_failed"
            if selection_result is not None
            else route_decision == "geometry_sanity_failed"
        ),
        "split_confidence_low_fraction": (
            selection_result.review_reason == "split_confidence_low"
            if selection_result is not None
            else route_decision == "split_confidence_low"
        ),
        "tta_variance_high_fraction": (
            selection_result.review_reason == "tta_variance_high"
            if selection_result is not None
            else route_decision == "tta_variance_high"
        ),
    }
    for suffix, active in route_flags.items():
        observe_and_check(
            f"eep.geometry_selection_route.{suffix}",
            1.0 if active else 0.0,
            session,
        )
    EEP_GEOMETRY_SELECTION_ROUTE.labels(route=route_decision).inc()


def _page_area_fractions(response: GeometryResponse | None) -> list[float]:
    if response is None:
        return []
    return [region.page_area_fraction for region in response.pages]


def _has_area_fraction_failure(selection_result: GeometrySelectionResult) -> bool:
    for sanity in selection_result.sanity_results.values():
        failed = sanity.get("failed_checks")
        if isinstance(failed, list) and "area_fraction_plausible" in failed:
            return True
    return False


# ── Main entry point ───────────────────────────────────────────────────────────


async def invoke_geometry_services(
    *,
    job_id: str,
    page_number: int,
    lineage_id: str,
    proxy_image_uri: str,
    material_type: str,
    proxy_width: int,
    proxy_height: int,
    iep1a_endpoint: str,
    iep1b_endpoint: str,
    iep1a_circuit_breaker: CircuitBreaker,
    iep1b_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    session: Session,
    gate_config: PreprocessingGateConfig | None = None,
) -> GeometryInvocationResult:
    """
    Invoke IEP1A and IEP1B concurrently and pass results to the geometry selection gate.

    Both services are invoked with asyncio.gather() — they run concurrently on
    the event loop.  Each is independently guarded by its own CircuitBreaker.

    A ServiceInvocation DB row is written for every service call (success,
    timeout, error, or skipped).  The quality_gate_log write is the caller's
    responsibility (use build_geometry_gate_log_record() + log_gate()).

    Args:
        job_id:                  Parent job identifier.
        page_number:             1-indexed page number.
        lineage_id:              FK for ServiceInvocation and traceability.
        proxy_image_uri:         URI of the downscaled proxy image.
        material_type:           Job material type string.
        proxy_width:             Pixel width of the proxy image.
        proxy_height:            Pixel height of the proxy image.
        iep1a_endpoint:          Full HTTP URL for IEP1A.
        iep1b_endpoint:          Full HTTP URL for IEP1B.
        iep1a_circuit_breaker:   Per-worker CircuitBreaker for IEP1A.
        iep1b_circuit_breaker:   Per-worker CircuitBreaker for IEP1B.
        backend:                 Shared GPUBackend instance.
        session:                 SQLAlchemy session (caller owns commit/rollback).
        gate_config:             Policy thresholds for the geometry selection gate;
                                 defaults to PreprocessingGateConfig().

    Returns:
        GeometryInvocationResult containing both per-service outcomes and the
        geometry selection gate result.

    Raises:
        GeometryServiceError: If both IEP1A and IEP1B produced no usable result
                              (both failed or both skipped).  The gate is NOT
                              called in this case.
    """
    request_image_uri = maybe_presign_input_uri(proxy_image_uri, iep1a_endpoint)
    request_image_uri = maybe_presign_input_uri(request_image_uri, iep1b_endpoint)

    payload: dict[str, Any] = {
        "job_id": job_id,
        "page_number": page_number,
        "image_uri": request_image_uri,
        "material_type": material_type,
    }

    # ── Parallel invocation ───────────────────────────────────────────────────
    outcome_a: _ServiceCallOutcome
    outcome_b: _ServiceCallOutcome
    outcome_a, outcome_b = await asyncio.gather(
        _invoke_one(
            "iep1a",
            iep1a_endpoint,
            payload,
            backend,
            iep1a_circuit_breaker,
            lineage_id,
            session,
        ),
        _invoke_one(
            "iep1b",
            iep1b_endpoint,
            payload,
            backend,
            iep1b_circuit_breaker,
            lineage_id,
            session,
        ),
    )

    # ── Both failed → infrastructure failure (caller decides retry/fail) ──────
    if outcome_a.response is None and outcome_b.response is None:
        raise GeometryServiceError(
            job_id=job_id,
            page_number=page_number,
            iep1a_error=outcome_a.error,
            iep1b_error=outcome_b.error,
        )

    # ── Geometry selection gate (called exactly once) ─────────────────────────
    selection_result = run_geometry_selection(
        iep1a_response=outcome_a.response,
        iep1b_response=outcome_b.response,
        material_type=material_type,  # type: ignore[arg-type]
        proxy_width=proxy_width,
        proxy_height=proxy_height,
        config=gate_config,
    )

    # ── Debug: log geometry gate outcome ──────────────────────────────────────
    logger.info(
        "geometry_gate: job=%s page=%d route=%s review=%s sanity=%s "
        "tta_var=%s structural_agreement=%s area_fractions=%s",
        job_id, page_number,
        selection_result.route_decision,
        selection_result.review_reason,
        selection_result.sanity_results,
        selection_result.tta_variance_per_model,
        selection_result.structural_agreement,
        {
            "iep1a": _page_area_fractions(outcome_a.response),
            "iep1b": _page_area_fractions(outcome_b.response),
        },
    )
    if material_type == "newspaper" and _has_area_fraction_failure(selection_result):
        logger.warning(
            "newspaper_area_fraction_gate: job=%s page=%d route=%s review=%s "
            "structural_agreement=%s area_bounds=%s mild_iep1b_min=%s "
            "iep1a_area_fractions=%s iep1b_area_fractions=%s sanity=%s",
            job_id,
            page_number,
            selection_result.route_decision,
            selection_result.review_reason,
            selection_result.structural_agreement,
            _area_fraction_bounds_for_material(
                gate_config or PreprocessingGateConfig(),
                "newspaper",
            ),
            (gate_config or PreprocessingGateConfig()).newspaper_iep1b_mild_area_min_fraction,
            _page_area_fractions(outcome_a.response),
            _page_area_fractions(outcome_b.response),
            selection_result.sanity_results,
        )

    # ── Drift observation (best-effort; never blocks return) ─────────────────
    _observe_geometry_metrics(
        iep1a=outcome_a.response,
        iep1b=outcome_b.response,
        selection_result=selection_result,
        session=session,
    )

    return GeometryInvocationResult(
        iep1a_result=outcome_a.response,
        iep1b_result=outcome_b.response,
        iep1a_error=outcome_a.error,
        iep1b_error=outcome_b.error,
        iep1a_skipped=outcome_a.skipped,
        iep1b_skipped=outcome_b.skipped,
        iep1a_duration_ms=outcome_a.duration_ms,
        iep1b_duration_ms=outcome_b.duration_ms,
        selection_result=selection_result,
    )
