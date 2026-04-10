"""
services/eep_worker/app/worker_loop.py
--------------------------------------
Real queue-consuming runtime path for the EEP worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import cv2
import numpy as np
import redis as redis_lib
from pydantic import ValidationError
from sqlalchemy.orm import Session

from services.eep.app.db.lineage import (
    confirm_layout_artifact,
    confirm_preprocessed_artifact,
    create_lineage,
    update_geometry_result,
    update_lineage_completion,
)
from services.eep.app.db.models import Job, JobPage, PageLineage
from services.eep.app.db.page_state import advance_page_state
from services.eep.app.db.session import SessionLocal
from services.eep.app.gates.artifact_validation import make_cv2_image_loader
from services.eep.app.jobs.status import _derive_job_status
from services.eep.app.queue import (
    MAX_TASK_RETRIES,
    ClaimedTask,
    ack_task,
    claim_task,
    enqueue_page_task,
    fail_task,
)
from services.eep_worker.app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from services.eep_worker.app.concurrency import WorkerSlotContext, initialize_semaphore
from services.eep_worker.app.downsample_step import run_downsample_step
from services.eep_worker.app.geometry_invocation import (
    GeometryInvocationResult,
    GeometryServiceError,
    invoke_geometry_services,
)
from services.eep_worker.app.intake import (
    OtiffDecodeError,
    OtiffHashMismatchError,
    OtiffLoadError,
    check_hash_consistency,
    compute_hash,
    decode_otiff,
    derive_proxy,
    load_otiff,
)
from services.eep_worker.app.layout_step import (
    LayoutTransitionError,
    complete_layout_detection,
    complete_layout_detection_from_adjudication,
)
from services.eep_worker.app.normalization_step import (
    NormalizationOutcome,
    run_normalization_and_first_validation,
)
from services.eep_worker.app.rescue_step import RescueOutcome, run_rescue_flow
from services.eep_worker.app.task import build_gate_config
from services.eep_worker.app.watchdog import TaskWatchdog
from shared.gpu.backend import BackendError, GPUBackend, LocalHTTPBackend, RunpodBackend
from shared.io.storage import get_backend
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutArtifactRole,
    LayoutDetectResponse,
    LayoutInputMetadata,
    Region,
)
from shared.schemas.preprocessing import PreprocessBranchResponse
from shared.schemas.ucf import BoundingBox
from shared.schemas.queue import PageTask

logger = logging.getLogger(__name__)

TaskResolution = Literal["ack", "retry"]

_ACK_ONLY_STATES: frozenset[str] = frozenset(
    {
        "accepted",
        "review",
        "failed",
        "pending_human_correction",
        "split",
    }
)


class RetryableTaskError(RuntimeError):
    """Signals that the current task should be retried via the queue."""


@dataclass(slots=True)
class WorkerConfig:
    worker_id: str
    poll_timeout_seconds: float
    max_concurrent_pages: int
    max_task_retries: int
    iep1a_endpoint: str
    iep1b_endpoint: str
    iep1d_endpoint: str
    iep2a_endpoint: str
    iep2b_endpoint: str
    backend: GPUBackend
    iep1d_execution_timeout_seconds: float
    iep1a_circuit_breaker: CircuitBreaker
    iep1b_circuit_breaker: CircuitBreaker
    iep1d_circuit_breaker: CircuitBreaker
    iep2a_circuit_breaker: CircuitBreaker
    iep2b_circuit_breaker: CircuitBreaker


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("worker_loop: invalid %s=%r; using %.1f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("worker_loop: invalid %s=%r; using %d", name, raw, default)
        return default


def _service_endpoint(base_env: str, default_base: str, path: str) -> str:
    base = os.environ.get(base_env, default_base).rstrip("/")
    return f"{base}{path}"


def build_worker_config(worker_id: str | None = None) -> WorkerConfig:
    """Build runtime worker configuration from environment variables."""
    resolved_worker_id = worker_id or f"eep-worker-{socket.gethostname()}-{os.getpid()}"
    backend_kind = os.environ.get("GPU_BACKEND", "local").strip().lower()
    cold_start_timeout = _env_float("COLD_START_TIMEOUT_SECONDS", 120.0)
    execution_timeout = _env_float("EXECUTION_TIMEOUT_SECONDS", 30.0)
    iep1d_execution_timeout = _env_float(
        "IEP1D_EXECUTION_TIMEOUT_SECONDS",
        max(execution_timeout, 180.0),
    )

    if backend_kind == "runpod":
        backend: GPUBackend = RunpodBackend(
            api_key=os.environ.get("RUNPOD_API_KEY", ""),
            cold_start_timeout_seconds=cold_start_timeout,
            execution_timeout_seconds=execution_timeout,
        )
    else:
        backend = LocalHTTPBackend(
            cold_start_timeout_seconds=cold_start_timeout,
            execution_timeout_seconds=execution_timeout,
        )

    cb_cfg = CircuitBreakerConfig(
        failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
        reset_timeout_seconds=_env_float("CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS", 60.0),
    )

    return WorkerConfig(
        worker_id=resolved_worker_id,
        poll_timeout_seconds=_env_float("WORKER_POLL_TIMEOUT_SECONDS", 5.0),
        max_concurrent_pages=_env_int("MAX_CONCURRENT_PAGES", 20),
        max_task_retries=_env_int("MAX_TASK_RETRIES", MAX_TASK_RETRIES),
        iep1a_endpoint=_service_endpoint("IEP1A_URL", "http://iep1a:8001", "/v1/geometry"),
        iep1b_endpoint=_service_endpoint("IEP1B_URL", "http://iep1b:8002", "/v1/geometry"),
        iep1d_endpoint=_service_endpoint("IEP1D_URL", "http://iep1d:8003", "/v1/rectify"),
        iep2a_endpoint=_service_endpoint("IEP2A_URL", "http://iep2a:8004", "/v1/layout-detect"),
        iep2b_endpoint=_service_endpoint("IEP2B_URL", "http://iep2b:8005", "/v1/layout-detect"),
        backend=backend,
        iep1d_execution_timeout_seconds=iep1d_execution_timeout,
        iep1a_circuit_breaker=CircuitBreaker("iep1a", cb_cfg),
        iep1b_circuit_breaker=CircuitBreaker("iep1b", cb_cfg),
        iep1d_circuit_breaker=CircuitBreaker("iep1d", cb_cfg),
        iep2a_circuit_breaker=CircuitBreaker("iep2a", cb_cfg),
        iep2b_circuit_breaker=CircuitBreaker("iep2b", cb_cfg),
    )


def _page_artifact_stem(page_number: int, sub_page_index: int | None) -> str:
    if sub_page_index is None:
        return str(page_number)
    return f"{page_number}_{sub_page_index}"


def _job_artifact_root(input_uri: str, job_id: str) -> str:
    parsed = urlparse(input_uri)
    if parsed.scheme == "s3":
        return f"s3://{parsed.netloc}/jobs/{job_id}"
    if parsed.scheme == "file":
        return f"file://jobs/{job_id}"
    raise ValueError(f"Unsupported artifact scheme for input URI {input_uri!r}")


def _artifact_uri(
    input_uri: str,
    job_id: str,
    section: str,
    page_number: int,
    sub_page_index: int | None,
    filename_suffix: str,
) -> str:
    stem = _page_artifact_stem(page_number, sub_page_index)
    root = _job_artifact_root(input_uri, job_id)
    return f"{root}/{section}/{stem}{filename_suffix}"


def _encode_png(image: Any) -> bytes:
    success, buf = cv2.imencode(".png", image)
    if not success:
        raise ValueError("cv2.imencode failed for proxy PNG")
    encoded = bytes(buf.tobytes())
    return encoded


def _quality_summary_dict(branch: PreprocessBranchResponse) -> dict[str, float | None]:
    return {
        "blur_score": branch.quality.blur_score,
        "border_score": branch.quality.border_score,
        "skew_residual": branch.quality.skew_residual,
        "foreground_coverage": branch.quality.foreground_coverage,
    }


def _find_lineage(
    session: Session,
    job_id: str,
    page_number: int,
    sub_page_index: int | None,
) -> PageLineage | None:
    return (
        session.query(PageLineage)
        .filter_by(
            job_id=job_id,
            page_number=page_number,
            sub_page_index=sub_page_index,
        )
        .first()
    )


def _sync_job_summary(session: Session, job: Job) -> None:
    pages = session.query(JobPage).filter_by(job_id=job.job_id).all()
    leaf_pages = [page for page in pages if page.status != "split"]
    now = datetime.now(timezone.utc)

    job.accepted_count = sum(1 for page in leaf_pages if page.status == "accepted")
    job.review_count = sum(1 for page in leaf_pages if page.status == "review")
    job.failed_count = sum(1 for page in leaf_pages if page.status == "failed")
    job.pending_human_correction_count = sum(
        1 for page in leaf_pages if page.status == "pending_human_correction"
    )
    job.status = _derive_job_status(leaf_pages)
    if job.status in {"done", "failed"}:
        job.completed_at = job.completed_at or now
    else:
        job.completed_at = None


def _resolve_material_type_placeholder(session: Session, job: Job, page: JobPage) -> str:
    """
    Placeholder IEP0 hook.

    Material type now resolves through this function so upload-time IEP0 can
    later replace the source without rewiring the worker runtime path.
    """
    predicted = getattr(page, "predicted_material_type", None) or getattr(
        job, "predicted_material_type", None
    )
    if predicted:
        return str(predicted)
    return job.material_type


def _page_task_for(page: JobPage) -> PageTask:
    return PageTask(
        task_id=str(uuid.uuid4()),
        job_id=page.job_id,
        page_id=page.page_id,
        page_number=page.page_number,
        sub_page_index=page.sub_page_index,
        retry_count=0,
    )


def _mime_type_for_uri(uri: str) -> str:
    if uri.lower().endswith(".png"):
        return "image/png"
    return "image/tiff"


def _commit(session: Session) -> None:
    session.flush()
    session.commit()


def _transition_to_preprocessing(session: Session, page: JobPage, job: Job) -> bool:
    advanced = advance_page_state(
        session,
        page.page_id,
        from_state="queued",
        to_state="preprocessing",
    )
    if not advanced:
        return False
    page.status = "preprocessing"
    page.review_reasons = None
    _sync_job_summary(session, job)
    _commit(session)
    return True


def _ensure_lineage(session: Session, page: JobPage, job: Job) -> PageLineage:
    lineage = _find_lineage(session, job.job_id, page.page_number, page.sub_page_index)
    if lineage is not None:
        return lineage

    lineage = create_lineage(
        session,
        lineage_id=str(uuid.uuid4()),
        job_id=job.job_id,
        page_number=page.page_number,
        sub_page_index=page.sub_page_index,
        correlation_id=str(uuid.uuid4()),
        input_image_uri=page.input_image_uri,
        otiff_uri=page.input_image_uri,
        input_image_hash=None,
        material_type=job.material_type,
        policy_version=job.policy_version,
    )
    _commit(session)
    return lineage


def _best_output_uri(
    page: JobPage,
    branch_response: PreprocessBranchResponse | None,
) -> str | None:
    if branch_response is not None:
        return branch_response.processed_image_uri
    return page.output_image_uri


def _complete_pending_human_correction(
    session: Session,
    *,
    page: JobPage,
    job: Job,
    lineage: PageLineage | None,
    from_state: str,
    review_reason: str,
    total_processing_ms: float,
    output_image_uri: str | None = None,
    quality_summary: dict[str, float | None] | None = None,
) -> None:
    advanced = advance_page_state(
        session,
        page.page_id,
        from_state=from_state,
        to_state="pending_human_correction",
        output_image_uri=output_image_uri,
        quality_summary=quality_summary,
        processing_time_ms=total_processing_ms,
    )
    if not advanced:
        logger.warning(
            "worker_loop: pending_human_correction CAS miss job=%s page_id=%s from=%s",
            job.job_id,
            page.page_id,
            from_state,
        )
        return

    page.status = "pending_human_correction"
    page.review_reasons = [review_reason]
    page.output_image_uri = output_image_uri
    page.quality_summary = quality_summary
    page.processing_time_ms = total_processing_ms

    if lineage is not None:
        update_lineage_completion(
            session,
            lineage.lineage_id,
            acceptance_decision="pending_human_correction",
            acceptance_reason=review_reason,
            routing_path=page.routing_path,
            total_processing_ms=total_processing_ms,
            output_image_uri=output_image_uri,
        )

    _sync_job_summary(session, job)
    _commit(session)


def _complete_failed(
    session: Session,
    *,
    page: JobPage,
    job: Job,
    lineage: PageLineage | None,
    from_state: str,
    failure_reason: str,
    total_processing_ms: float | None,
) -> None:
    advanced = advance_page_state(
        session,
        page.page_id,
        from_state=from_state,
        to_state="failed",
        acceptance_decision="failed",
        processing_time_ms=total_processing_ms,
    )
    if not advanced:
        logger.warning(
            "worker_loop: failed CAS miss job=%s page_id=%s from=%s",
            job.job_id,
            page.page_id,
            from_state,
        )
        return

    page.status = "failed"
    page.acceptance_decision = "failed"
    page.review_reasons = [failure_reason]
    page.processing_time_ms = total_processing_ms

    if lineage is not None:
        update_lineage_completion(
            session,
            lineage.lineage_id,
            acceptance_decision="failed",
            acceptance_reason=failure_reason,
            routing_path=page.routing_path,
            total_processing_ms=total_processing_ms,
            output_image_uri=page.output_image_uri,
        )

    _sync_job_summary(session, job)
    _commit(session)


def _mark_exhausted_task_failed(
    session_factory: Any,
    *,
    job_id: str,
    page_id: str,
    reason: str,
) -> None:
    session = session_factory()
    try:
        page = session.get(JobPage, page_id)
        job = session.get(Job, job_id)
        if page is None or job is None:
            return
        if page.status in {"accepted", "review", "failed", "pending_human_correction", "split"}:
            return
        lineage = _find_lineage(session, page.job_id, page.page_number, page.sub_page_index)
        _complete_failed(
            session,
            page=page,
            job=job,
            lineage=lineage,
            from_state=page.status,
            failure_reason=reason,
            total_processing_ms=page.processing_time_ms,
        )
    except Exception:
        session.rollback()
        logger.exception(
            "worker_loop: could not mark exhausted task failed for page_id=%s", page_id
        )
    finally:
        session.close()


async def _call_layout_service(
    *,
    service_name: str,
    endpoint: str,
    job_id: str,
    page_number: int,
    image_uri: str,
    material_type: str,
    backend: GPUBackend,
    circuit_breaker: CircuitBreaker,
) -> LayoutDetectResponse | None:
    if not circuit_breaker.allow_call():
        logger.warning(
            "worker_loop: %s skipped by open circuit breaker job=%s page=%d",
            service_name,
            job_id,
            page_number,
        )
        return None

    payload = {
        "job_id": job_id,
        "page_number": page_number,
        "image_uri": image_uri,
        "material_type": material_type,
    }

    try:
        raw = await backend.call(endpoint, payload)
        response = LayoutDetectResponse.model_validate(raw)
        circuit_breaker.record_success()
        return response
    except BackendError as exc:
        circuit_breaker.record_failure(exc.kind)
        logger.warning(
            "worker_loop: %s failed job=%s page=%d kind=%s error=%s",
            service_name,
            job_id,
            page_number,
            exc.kind.value,
            exc,
        )
    except ValidationError as exc:
        circuit_breaker.record_failure(None)
        logger.warning(
            "worker_loop: %s returned malformed response job=%s page=%d error=%s",
            service_name,
            job_id,
            page_number,
            exc,
        )
    except Exception:
        circuit_breaker.record_failure(None)
        logger.exception(
            "worker_loop: unexpected %s failure job=%s page=%d",
            service_name,
            job_id,
            page_number,
        )
    return None


def _write_layout_artifact(uri: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    get_backend(uri).put_bytes(uri, data)


# ── Inline layout helpers ─────────────────────────────────────────────────────


def _best_layout_artifact_uri(
    page: JobPage,
    branch_response: PreprocessBranchResponse | None = None,
) -> str | None:
    """Select the best available artifact URI for inline layout detection.

    Priority (highest first):
      1. branch_response.processed_image_uri  — freshly normalized artifact
      2. page.output_image_uri                — normalized artifact from an earlier pass
      3. page.input_image_uri                 — original uploaded OTIFF (last resort)
    """
    if branch_response is not None:
        return branch_response.processed_image_uri
    return page.output_image_uri or page.input_image_uri


async def _run_preprocessing(
    *,
    session: Session,
    page: JobPage,
    job: Job,
    config: WorkerConfig,
    redis_client: redis_lib.Redis,
    task_started_at: float,
) -> TaskResolution:
    if page.status == "queued":
        if not _transition_to_preprocessing(session, page, job):
            return "ack"
        page = session.get(JobPage, page.page_id) or page
        job = session.get(Job, job.job_id) or job

    current_state = page.status
    lineage = _ensure_lineage(session, page, job)
    storage = get_backend(page.input_image_uri)
    material_type = _resolve_material_type_placeholder(session, job, page)

    try:
        raw_otiff = load_otiff(page.input_image_uri, storage)
    except OtiffLoadError as exc:
        raise RetryableTaskError(str(exc)) from exc

    try:
        current_hash = compute_hash(raw_otiff)
        check_hash_consistency(page.input_image_uri, current_hash, lineage.input_image_hash)
        full_res_image = decode_otiff(raw_otiff, page.input_image_uri)
    except (OtiffDecodeError, OtiffHashMismatchError) as exc:
        _complete_failed(
            session,
            page=page,
            job=job,
            lineage=lineage,
            from_state=current_state,
            failure_reason=str(exc),
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
        )
        return "ack"

    if lineage.input_image_hash is None:
        lineage.input_image_hash = current_hash
        _commit(session)

    downsample_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "downsampled",
        page.page_number,
        page.sub_page_index,
        ".tiff",
    )
    downsample_backend = get_backend(downsample_uri)
    try:
        downsample_result = run_downsample_step(
            full_res_image=full_res_image,
            source_artifact_uri=page.input_image_uri,
            output_uri=downsample_uri,
            storage=downsample_backend,
        )
        existing_gate = lineage.gate_results or {}
        lineage.gate_results = {**existing_gate, "downsample": asdict(downsample_result)}
        _commit(session)
    except Exception as exc:
        raise RetryableTaskError(f"downsampling failed: {exc}") from exc

    proxy_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "proxy",
        page.page_number,
        page.sub_page_index,
        ".png",
    )
    proxy_backend = get_backend(proxy_uri)
    try:
        proxy_image = derive_proxy(full_res_image, material_type)
        proxy_backend.put_bytes(proxy_uri, _encode_png(proxy_image))
    except Exception as exc:
        raise RetryableTaskError(f"proxy generation failed: {exc}") from exc

    gate_config = build_gate_config(session)
    proxy_height, proxy_width = proxy_image.shape[:2]

    try:
        geometry_result: GeometryInvocationResult = await invoke_geometry_services(
            job_id=job.job_id,
            page_number=page.page_number,
            lineage_id=lineage.lineage_id,
            proxy_image_uri=proxy_uri,
            material_type=material_type,
            proxy_width=proxy_width,
            proxy_height=proxy_height,
            iep1a_endpoint=config.iep1a_endpoint,
            iep1b_endpoint=config.iep1b_endpoint,
            iep1a_circuit_breaker=config.iep1a_circuit_breaker,
            iep1b_circuit_breaker=config.iep1b_circuit_breaker,
            backend=config.backend,
            session=session,
            gate_config=gate_config,
        )
    except GeometryServiceError as exc:
        raise RetryableTaskError(str(exc)) from exc

    selection = geometry_result.selection_result
    update_geometry_result(
        session,
        lineage.lineage_id,
        iep1a_used=geometry_result.iep1a_result is not None or geometry_result.iep1a_skipped,
        iep1b_used=geometry_result.iep1b_result is not None or geometry_result.iep1b_skipped,
        selected_geometry_model=(
            selection.selected.model if selection.selected is not None else None
        ),
        structural_agreement=selection.structural_agreement,
        iep1d_used=(page.status == "rectification"),
    )

    if selection.route_decision == "pending_human_correction" or selection.selected is None:
        review_reason = selection.review_reason or "geometry_selection_failed"
        _complete_pending_human_correction(
            session,
            page=page,
            job=job,
            lineage=lineage,
            from_state=current_state,
            review_reason=review_reason,
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
            output_image_uri=page.output_image_uri,
        )
        return "ack"

    # split_required check is deferred until after normalization so the reviewer
    # has a preprocessed artifact (output_image_uri) to work with.

    output_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "output",
        page.page_number,
        page.sub_page_index,
        ".tiff",
    )
    output_backend = get_backend(output_uri)
    output_loader = make_cv2_image_loader(output_backend)

    try:
        norm_outcome: NormalizationOutcome = run_normalization_and_first_validation(
            full_res_image=full_res_image,
            selected_geometry=selection.selected.response,
            selected_model=selection.selected.model,
            geometry_route_decision=selection.route_decision,
            proxy_width=proxy_width,
            proxy_height=proxy_height,
            output_uri=output_uri,
            storage=output_backend,
            image_loader=output_loader,
            gate_config=gate_config,
        )
    except Exception as exc:
        raise RetryableTaskError(f"normalization failed: {exc}") from exc

    branch_response = norm_outcome.branch_response
    quality_summary = _quality_summary_dict(branch_response)

    if selection.selected.response.split_required:
        # Normalization has now run so the reviewer has a preprocessed artifact
        # (branch_response.processed_image_uri) to open and split.
        confirm_preprocessed_artifact(session, lineage.lineage_id)
        _complete_pending_human_correction(
            session,
            page=page,
            job=job,
            lineage=lineage,
            from_state=current_state,
            review_reason="split_required",
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
            output_image_uri=branch_response.processed_image_uri,
            quality_summary=quality_summary,
        )
        return "ack"

    if page.status == "preprocessing" and norm_outcome.route == "rescue_required":
        advanced = advance_page_state(
            session,
            page.page_id,
            from_state="preprocessing",
            to_state="rectification",
            output_image_uri=branch_response.processed_image_uri,
            quality_summary=quality_summary,
            processing_time_ms=(time.monotonic() - task_started_at) * 1000.0,
        )
        if not advanced:
            logger.warning(
                "worker_loop: rectification CAS miss job=%s page_id=%s",
                job.job_id,
                page.page_id,
            )
            return "ack"
        page.status = "rectification"
        page.output_image_uri = branch_response.processed_image_uri
        page.quality_summary = quality_summary
        page.processing_time_ms = (time.monotonic() - task_started_at) * 1000.0
        _sync_job_summary(session, job)
        _commit(session)
        page = session.get(JobPage, page.page_id) or page
        job = session.get(Job, job.job_id) or job

    final_branch = branch_response

    if page.status == "rectification":
        rectified_proxy_uri = _artifact_uri(
            page.input_image_uri,
            job.job_id,
            "proxy_rectified",
            page.page_number,
            page.sub_page_index,
            ".png",
        )
        rescue_output_uri = _artifact_uri(
            page.input_image_uri,
            job.job_id,
            "output_rectified",
            page.page_number,
            page.sub_page_index,
            ".tiff",
        )
        rescue_storage = get_backend(rescue_output_uri)
        rescue_loader = make_cv2_image_loader(rescue_storage)

        google_cleanup_output_uri = _artifact_uri(
            page.input_image_uri,
            job.job_id,
            "google_cleanup",
            page.page_number,
            page.sub_page_index,
            ".tiff",
        )
        google_cleanup_proxy_uri = _artifact_uri(
            page.input_image_uri,
            job.job_id,
            "google_cleanup_proxy",
            page.page_number,
            page.sub_page_index,
            ".png",
        )

        try:
            rescue_outcome: RescueOutcome = await run_rescue_flow(
                artifact_uri=branch_response.processed_image_uri,
                job_id=job.job_id,
                page_number=page.page_number,
                lineage_id=lineage.lineage_id,
                material_type=material_type,
                rectified_proxy_uri=rectified_proxy_uri,
                rescue_output_uri=rescue_output_uri,
                iep1d_endpoint=config.iep1d_endpoint,
                iep1a_endpoint=config.iep1a_endpoint,
                iep1b_endpoint=config.iep1b_endpoint,
                iep1d_circuit_breaker=config.iep1d_circuit_breaker,
                iep1a_circuit_breaker=config.iep1a_circuit_breaker,
                iep1b_circuit_breaker=config.iep1b_circuit_breaker,
                backend=config.backend,
                iep1d_execution_timeout_seconds=config.iep1d_execution_timeout_seconds,
                session=session,
                storage=rescue_storage,
                image_loader=rescue_loader,
                gate_config=gate_config,
                google_cleanup_output_uri=google_cleanup_output_uri,
                google_cleanup_proxy_uri=google_cleanup_proxy_uri,
            )
        except Exception as exc:
            raise RetryableTaskError(f"rescue flow failed: {exc}") from exc

        update_geometry_result(
            session,
            lineage.lineage_id,
            iep1a_used=True,
            iep1b_used=True,
            selected_geometry_model=(
                rescue_outcome.second_selection_result.selected.model
                if rescue_outcome.second_selection_result is not None
                and rescue_outcome.second_selection_result.selected is not None
                else selection.selected.model
            ),
            structural_agreement=(
                rescue_outcome.second_selection_result.structural_agreement
                if rescue_outcome.second_selection_result is not None
                else selection.structural_agreement
            ),
            iep1d_used=True,
        )

        if rescue_outcome.route == "pending_human_correction":
            rescue_branch = rescue_outcome.branch_response
            rescue_quality = (
                _quality_summary_dict(rescue_branch) if rescue_branch is not None else None
            )
            if rescue_branch is not None:
                confirm_preprocessed_artifact(session, lineage.lineage_id)
            _complete_pending_human_correction(
                session,
                page=page,
                job=job,
                lineage=lineage,
                from_state="rectification",
                review_reason=rescue_outcome.review_reason or "pending_human_correction",
                total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
                output_image_uri=_best_output_uri(page, rescue_branch),
                quality_summary=rescue_quality,
            )
            return "ack"

        assert rescue_outcome.branch_response is not None
        final_branch = rescue_outcome.branch_response

    confirm_preprocessed_artifact(session, lineage.lineage_id)

    total_processing_ms = (time.monotonic() - task_started_at) * 1000.0
    final_quality_summary = _quality_summary_dict(final_branch)
    from_state = page.status

    if job.pipeline_mode == "layout":
        # Automation-first: route directly to layout_detection and enqueue IEP2.
        to_state = "layout_detection"
    else:
        # Preprocess-only: accept immediately.
        to_state = "accepted"

    advanced = advance_page_state(
        session,
        page.page_id,
        from_state=from_state,
        to_state=to_state,
        output_image_uri=final_branch.processed_image_uri,
        quality_summary=final_quality_summary,
        processing_time_ms=total_processing_ms,
    )
    if not advanced:
        logger.warning(
            "worker_loop: %s CAS miss job=%s page_id=%s from=%s",
            to_state,
            job.job_id,
            page.page_id,
            from_state,
        )
        return "ack"

    page.status = to_state
    page.output_image_uri = final_branch.processed_image_uri
    page.quality_summary = final_quality_summary
    page.processing_time_ms = total_processing_ms
    page.review_reasons = None

    if job.pipeline_mode == "layout":
        enqueue_page_task(redis_client, _page_task_for(page))
    else:
        page.acceptance_decision = "accepted"
        page.routing_path = "preprocessing_only"
        update_lineage_completion(
            session,
            lineage.lineage_id,
            acceptance_decision="accepted",
            acceptance_reason="preprocessing accepted",
            routing_path="preprocessing_only",
            total_processing_ms=total_processing_ms,
            output_image_uri=final_branch.processed_image_uri,
        )

    _sync_job_summary(session, job)
    _commit(session)
    return "ack"


def _extract_downsample_gate(lineage: PageLineage) -> dict[str, Any] | None:
    """Return the validated downsample gate dict from lineage, or None if absent/incomplete."""
    gate = (lineage.gate_results or {}).get("downsample")
    if not gate:
        return None
    for key in (
        "downsampled_artifact_uri",
        "original_width",
        "original_height",
        "downsampled_width",
        "downsampled_height",
    ):
        if not gate.get(key):
            return None
    if gate["downsampled_width"] <= 0 or gate["downsampled_height"] <= 0:
        return None
    if gate["original_width"] <= 0 or gate["original_height"] <= 0:
        return None
    return gate


def _decode_image_shape(image_bytes: bytes, *, uri: str) -> tuple[int, int]:
    """Return (width, height) for a raster artifact."""
    buf = _decode_image_array(image_bytes, uri=uri)
    height, width = buf.shape[:2]
    return int(width), int(height)


def _decode_image_array(image_bytes: bytes, *, uri: str) -> Any:
    """Decode bytes into a raster ndarray suitable for OpenCV processing."""
    try:
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
    except cv2.error as exc:
        raise RetryableTaskError(f"could not decode image dimensions for {uri}: {exc}") from exc
    if image is None or image.ndim < 2:
        raise RetryableTaskError(f"could not decode image dimensions for {uri}")
    return image


def _resolve_layout_artifact_role(
    *,
    page: JobPage,
    lineage: PageLineage,
    source_page_artifact_uri: str,
) -> LayoutArtifactRole:
    if source_page_artifact_uri == page.input_image_uri:
        return "original_upload"
    if lineage.human_corrected:
        return "human_corrected"
    if page.sub_page_index is not None or lineage.split_source:
        return "split_child"
    return "normalized_output"


def _build_layout_input_metadata(
    *,
    page: JobPage,
    lineage: PageLineage,
    source_page_artifact_uri: str,
    analyzed_artifact_uri: str,
    input_source: Literal["page_output", "downsampled"],
    layout_input_width: int,
    layout_input_height: int,
    canonical_output_width: int,
    canonical_output_height: int,
    coordinate_rescaled: bool,
) -> LayoutInputMetadata:
    return LayoutInputMetadata(
        source_page_artifact_uri=source_page_artifact_uri,
        analyzed_artifact_uri=analyzed_artifact_uri,
        artifact_role=_resolve_layout_artifact_role(
            page=page,
            lineage=lineage,
            source_page_artifact_uri=source_page_artifact_uri,
        ),
        input_source=input_source,
        layout_input_width=layout_input_width,
        layout_input_height=layout_input_height,
        canonical_output_width=canonical_output_width,
        canonical_output_height=canonical_output_height,
        coordinate_rescaled=coordinate_rescaled,
    )


def _prepare_layout_input_artifact(
    *,
    session: Session,
    page: JobPage,
    job: Job,
    lineage: PageLineage,
    source_page_artifact_uri: str,
) -> tuple[str, LayoutInputMetadata]:
    """Return the analyzed artifact URI plus persisted metadata for this layout run."""
    source_bytes = get_backend(source_page_artifact_uri).get_bytes(source_page_artifact_uri)
    source_image = _decode_image_array(source_bytes, uri=source_page_artifact_uri)
    source_height, source_width = source_image.shape[:2]

    downsample_gate = _extract_downsample_gate(lineage)
    if (
        downsample_gate is not None
        and downsample_gate.get("source_artifact_uri") == source_page_artifact_uri
    ):
        return (
            downsample_gate["downsampled_artifact_uri"],
            _build_layout_input_metadata(
                page=page,
                lineage=lineage,
                source_page_artifact_uri=source_page_artifact_uri,
                analyzed_artifact_uri=downsample_gate["downsampled_artifact_uri"],
                input_source="downsampled",
                layout_input_width=downsample_gate["downsampled_width"],
                layout_input_height=downsample_gate["downsampled_height"],
                canonical_output_width=downsample_gate["original_width"],
                canonical_output_height=downsample_gate["original_height"],
                coordinate_rescaled=True,
            ),
        )

    downsample_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "downsampled",
        page.page_number,
        page.sub_page_index,
        ".tiff",
    )
    downsample_result = run_downsample_step(
        full_res_image=source_image,
        source_artifact_uri=source_page_artifact_uri,
        output_uri=downsample_uri,
        storage=get_backend(downsample_uri),
    )
    existing_gate = lineage.gate_results or {}
    lineage.gate_results = {**existing_gate, "downsample": asdict(downsample_result)}
    _commit(session)
    return (
        downsample_result.downsampled_artifact_uri,
        _build_layout_input_metadata(
            page=page,
            lineage=lineage,
            source_page_artifact_uri=source_page_artifact_uri,
            analyzed_artifact_uri=downsample_result.downsampled_artifact_uri,
            input_source="downsampled",
            layout_input_width=downsample_result.downsampled_width,
            layout_input_height=downsample_result.downsampled_height,
            canonical_output_width=downsample_result.original_width,
            canonical_output_height=downsample_result.original_height,
            coordinate_rescaled=True,
        ),
    )


def _build_page_output_layout_input(
    *,
    page: JobPage,
    lineage: PageLineage,
    source_page_artifact_uri: str,
) -> LayoutInputMetadata:
    source_bytes = get_backend(source_page_artifact_uri).get_bytes(source_page_artifact_uri)
    width, height = _decode_image_shape(source_bytes, uri=source_page_artifact_uri)
    return _build_layout_input_metadata(
        page=page,
        lineage=lineage,
        source_page_artifact_uri=source_page_artifact_uri,
        analyzed_artifact_uri=source_page_artifact_uri,
        input_source="page_output",
        layout_input_width=width,
        layout_input_height=height,
        canonical_output_width=width,
        canonical_output_height=height,
        coordinate_rescaled=False,
    )


def _existing_layout_matches_current_output(
    page: JobPage,
    current_output_uri: str,
) -> LayoutAdjudicationResult | None:
    raw = page.layout_consensus_result
    if not raw or page.output_layout_uri is None:
        return None
    try:
        adjudication = LayoutAdjudicationResult.model_validate(raw)
    except ValidationError:
        logger.warning(
            "worker_loop: ignoring malformed stored layout adjudication job=%s page=%d",
            page.job_id,
            page.page_number,
        )
        return None
    if adjudication.layout_input is None:
        return None
    if adjudication.layout_input.source_page_artifact_uri != current_output_uri:
        return None
    return adjudication


def _rescale_layout_response(
    response: LayoutDetectResponse | None,
    scale_x: float,
    scale_y: float,
) -> LayoutDetectResponse | None:
    """
    Return a new LayoutDetectResponse with region bboxes rescaled to canonical coordinates.

    Rescaling formula (downsample-space → canonical/original-space):
        canonical_x = downsample_x * scale_x  (scale_x = original_width / downsampled_width)
        canonical_y = downsample_y * scale_y  (scale_y = original_height / downsampled_height)

    Returns the original response unchanged when response is None or both scale factors
    equal 1.0 (no downsample was applied).
    """
    if response is None or (scale_x == 1.0 and scale_y == 1.0):
        return response
    rescaled = [
        Region(
            id=r.id,
            type=r.type,
            confidence=r.confidence,
            bbox=BoundingBox(
                x_min=r.bbox.x_min * scale_x,
                y_min=r.bbox.y_min * scale_y,
                x_max=r.bbox.x_max * scale_x,
                y_max=r.bbox.y_max * scale_y,
            ),
        )
        for r in response.regions
    ]
    return response.model_copy(update={"regions": rescaled})


async def _run_layout(
    *,
    session: Session,
    page: JobPage,
    job: Job,
    config: WorkerConfig,
    task_started_at: float,
) -> TaskResolution:
    lineage = _find_lineage(session, page.job_id, page.page_number, page.sub_page_index)
    if lineage is None:
        _complete_failed(
            session,
            page=page,
            job=job,
            lineage=None,
            from_state=page.status,
            failure_reason="missing_lineage_for_layout",
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
        )
        return "ack"

    image_uri = page.output_image_uri
    if not image_uri:
        _complete_failed(
            session,
            page=page,
            job=job,
            lineage=lineage,
            from_state=page.status,
            failure_reason="missing_preprocessed_artifact_for_layout",
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
        )
        return "ack"

    material_type = _resolve_material_type_placeholder(session, job, page)

    logger.info(
        "worker_loop: starting layout detection job=%s page=%d uri=%s",
        job.job_id,
        page.page_number,
        image_uri,
    )

    existing_adjudication = _existing_layout_matches_current_output(page, image_uri)
    if existing_adjudication is not None:
        try:
            layout_result = complete_layout_detection_from_adjudication(
                session=session,
                page=page,
                lineage_id=lineage.lineage_id,
                adjudication=existing_adjudication,
                total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
                output_layout_uri=page.output_layout_uri,
            )
        except LayoutTransitionError:
            logger.warning(
                "worker_loop: layout transition CAS miss job=%s page_id=%s",
                job.job_id,
                page.page_id,
            )
            return "ack"
        page.output_layout_uri = page.output_layout_uri
        _sync_job_summary(session, job)
        _commit(session)
        logger.info(
            "worker_loop: reused current layout adjudication job=%s page=%d uri=%s",
            job.job_id,
            page.page_number,
            image_uri,
        )
        return "ack"

    layout_image_uri, layout_input = _prepare_layout_input_artifact(
        session=session,
        page=page,
        job=job,
        lineage=lineage,
        source_page_artifact_uri=image_uri,
    )
    downsample_gate = _extract_downsample_gate(lineage)
    if layout_input.input_source == "downsampled":
        logger.info(
            "worker_loop: using downsampled artifact for layout job=%s page=%d uri=%s source=%s",
            job.job_id,
            page.page_number,
            layout_image_uri,
            image_uri,
        )

    image_bytes: bytes | None = None
    try:
        image_bytes = get_backend(layout_image_uri).get_bytes(layout_image_uri)
    except Exception:
        logger.warning(
            "worker_loop: could not load image bytes for Google fallback job=%s page=%d uri=%s",
            job.job_id,
            page.page_number,
            layout_image_uri,
        )

    iep2a_result = await _call_layout_service(
        service_name="iep2a",
        endpoint=config.iep2a_endpoint,
        job_id=job.job_id,
        page_number=page.page_number,
        image_uri=layout_image_uri,
        material_type=material_type,
        backend=config.backend,
        circuit_breaker=config.iep2a_circuit_breaker,
    )
    iep2b_result = await _call_layout_service(
        service_name="iep2b",
        endpoint=config.iep2b_endpoint,
        job_id=job.job_id,
        page_number=page.page_number,
        image_uri=layout_image_uri,
        material_type=material_type,
        backend=config.backend,
        circuit_breaker=config.iep2b_circuit_breaker,
    )

    # Rescale IEP2A and IEP2B outputs back to canonical (original-resolution) coordinates.
    if layout_input.coordinate_rescaled and downsample_gate is not None:
        _scale_x = layout_input.canonical_output_width / layout_input.layout_input_width
        _scale_y = layout_input.canonical_output_height / layout_input.layout_input_height
        iep2a_result = _rescale_layout_response(iep2a_result, _scale_x, _scale_y)
        iep2b_result = _rescale_layout_response(iep2b_result, _scale_x, _scale_y)

    try:
        layout_result = await complete_layout_detection(
            session=session,
            page=page,
            lineage_id=lineage.lineage_id,
            material_type=material_type,
            image_uri=layout_image_uri,
            iep2a_result=iep2a_result,
            iep2b_result=iep2b_result,
            image_bytes=image_bytes,
            mime_type=_mime_type_for_uri(layout_image_uri),
            total_processing_ms=(time.monotonic() - task_started_at) * 1000.0,
            output_layout_uri=None,
            layout_input=layout_input,
        )
    except LayoutTransitionError:
        logger.warning(
            "worker_loop: layout transition CAS miss job=%s page_id=%s",
            job.job_id,
            page.page_id,
        )
        return "ack"
    except Exception as exc:
        raise RetryableTaskError(f"layout adjudication failed: {exc}") from exc

    if layout_result.adjudication.layout_decision_source == "google_document_ai":
        logger.info(
            "worker_loop: Google adjudication triggered job=%s page=%d regions=%d fallback_used=%s",
            job.job_id,
            page.page_number,
            len(layout_result.adjudication.final_layout_result),
            layout_result.adjudication.fallback_used,
        )

    output_layout_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "layout",
        page.page_number,
        page.sub_page_index,
        ".layout.json",
    )

    try:
        _write_layout_artifact(
            output_layout_uri, layout_result.adjudication.model_dump(mode="json")
        )
    except Exception as exc:
        raise RetryableTaskError(f"layout artifact write failed: {exc}") from exc

    page.output_layout_uri = output_layout_uri
    confirm_layout_artifact(session, lineage.lineage_id)
    _sync_job_summary(session, job)
    _commit(session)

    logger.info(
        "worker_loop: layout result persisted job=%s page=%d source=%s regions=%d uri=%s",
        job.job_id,
        page.page_number,
        layout_result.adjudication.layout_decision_source,
        len(layout_result.adjudication.final_layout_result),
        output_layout_uri,
    )
    return "ack"


async def process_page_task(
    claimed: ClaimedTask,
    config: WorkerConfig,
    redis_client: redis_lib.Redis,
    session_factory: Any = SessionLocal,
) -> None:
    """Canonical runtime task runner for one claimed page task."""
    session = session_factory()
    resolution: TaskResolution = "ack"

    try:
        page = session.get(JobPage, claimed.task.page_id)
        job = session.get(Job, claimed.task.job_id)

        if page is None or job is None:
            logger.warning(
                "worker_loop: dropping task %s page_exists=%s job_exists=%s",
                claimed.task.task_id,
                page is not None,
                job is not None,
            )
            resolution = "ack"
        elif page.status in _ACK_ONLY_STATES:
            resolution = "ack"
        elif page.status in {"queued", "preprocessing", "rectification"}:
            resolution = await _run_preprocessing(
                session=session,
                page=page,
                job=job,
                config=config,
                redis_client=redis_client,
                task_started_at=time.monotonic(),
            )
        elif page.status == "layout_detection":
            resolution = await _run_layout(
                session=session,
                page=page,
                job=job,
                config=config,
                task_started_at=time.monotonic(),
            )
        else:
            logger.error(
                "worker_loop: unknown page state %r for page_id=%s; acking task",
                page.status,
                page.page_id,
            )
            resolution = "ack"
    except RetryableTaskError as exc:
        session.rollback()
        logger.warning(
            "worker_loop: retryable failure task=%s page_id=%s error=%s",
            claimed.task.task_id,
            claimed.task.page_id,
            exc,
        )
        if claimed.task.retry_count >= config.max_task_retries:
            _mark_exhausted_task_failed(
                session_factory,
                job_id=claimed.task.job_id,
                page_id=claimed.task.page_id,
                reason=str(exc),
            )
        resolution = "retry"
    except Exception:
        session.rollback()
        logger.exception(
            "worker_loop: unexpected failure task=%s page_id=%s",
            claimed.task.task_id,
            claimed.task.page_id,
        )
        if claimed.task.retry_count >= config.max_task_retries:
            _mark_exhausted_task_failed(
                session_factory,
                job_id=claimed.task.job_id,
                page_id=claimed.task.page_id,
                reason="unexpected_worker_error",
            )
        resolution = "retry"
    finally:
        session.close()

    try:
        if resolution == "ack":
            ack_task(redis_client, claimed)
        else:
            fail_task(redis_client, claimed, max_retries=config.max_task_retries)
    except redis_lib.RedisError:
        logger.exception(
            "worker_loop: queue resolution failed task=%s resolution=%s",
            claimed.task.task_id,
            resolution,
        )


async def run_worker_loop(
    redis_client: redis_lib.Redis,
    config: WorkerConfig,
    *,
    session_factory: Any = SessionLocal,
    watchdog: TaskWatchdog | None = None,
) -> None:
    """Infinite queue consumer loop for claimed page tasks."""
    initialize_semaphore(redis_client, config.max_concurrent_pages)
    logger.info(
        "worker_loop: started worker_id=%s poll_timeout=%.1fs",
        config.worker_id,
        config.poll_timeout_seconds,
    )

    while True:
        try:
            claimed = await asyncio.to_thread(
                claim_task,
                redis_client,
                config.worker_id,
                config.poll_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker_loop: claim_task failed; retrying")
            await asyncio.sleep(1.0)
            continue

        if claimed is None:
            continue

        if watchdog is not None:
            watchdog.register(claimed.task.task_id)

        try:
            async with WorkerSlotContext(redis_client):
                await process_page_task(
                    claimed,
                    config,
                    redis_client,
                    session_factory=session_factory,
                )
        finally:
            if watchdog is not None:
                watchdog.deregister(claimed.task.task_id)
