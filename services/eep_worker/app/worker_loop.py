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
import httpx
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
from services.eep.app.db.quality_gate import log_gate
from services.eep.app.gates.geometry_selection import build_geometry_gate_log_record
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
from services.eep_worker.app.split_step import SplitOutcome, run_split_normalization
from services.eep_worker.app.rescue_step import RescueOutcome, run_rescue_flow
from services.eep_worker.app.task import build_gate_config
from services.eep_worker.app.watchdog import TaskWatchdog
from shared.gpu.backend import BackendError, GPUBackend, LocalHTTPBackend, RunpodBackend
from shared.io.storage import get_backend
from shared.schemas.iep0 import BatchClassifyResponse, ClassifyResponse
from shared.schemas.layout import (
    LayoutAdjudicationResult,
    LayoutArtifactRole,
    LayoutDetectResponse,
    LayoutInputMetadata,
    Region,
)
from shared.schemas.preprocessing import PreprocessBranchResponse
from shared.schemas.semantic_norm import SemanticNormResponse
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
    layout_artifact_io_timeout_seconds: float
    layout_artifact_io_attempts: int
    layout_artifact_io_backoff_seconds: float
    iep0_endpoint: str
    iep0_batch_endpoint: str
    iep1a_endpoint: str
    iep1b_endpoint: str
    iep1d_endpoint: str
    iep2a_endpoint: str
    iep2b_endpoint: str
    backend: GPUBackend
    iep1d_execution_timeout_seconds: float
    iep2_call_timeout_seconds: float
    iep0_circuit_breaker: CircuitBreaker
    iep1a_circuit_breaker: CircuitBreaker
    iep1b_circuit_breaker: CircuitBreaker
    iep1d_circuit_breaker: CircuitBreaker
    iep2a_circuit_breaker: CircuitBreaker
    iep2b_circuit_breaker: CircuitBreaker
    iep1e_endpoint: str
    iep1e_circuit_breaker: CircuitBreaker


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
        layout_artifact_io_timeout_seconds=_env_float(
            "LAYOUT_ARTIFACT_IO_TIMEOUT_SECONDS",
            45.0,
        ),
        layout_artifact_io_attempts=max(1, _env_int("LAYOUT_ARTIFACT_IO_ATTEMPTS", 3)),
        layout_artifact_io_backoff_seconds=_env_float(
            "LAYOUT_ARTIFACT_IO_BACKOFF_SECONDS",
            1.0,
        ),
        iep0_endpoint=_service_endpoint("IEP0_URL", "http://iep0:8006", "/v1/classify"),
        iep0_batch_endpoint=_service_endpoint("IEP0_URL", "http://iep0:8006", "/v1/classify-batch"),
        iep1a_endpoint=_service_endpoint("IEP1A_URL", "http://iep1a:8001", "/v1/geometry"),
        iep1b_endpoint=_service_endpoint("IEP1B_URL", "http://iep1b:8002", "/v1/geometry"),
        iep1d_endpoint=_service_endpoint("IEP1D_URL", "http://iep1d:8003", "/v1/rectify"),
        iep2a_endpoint=_service_endpoint("IEP2A_URL", "http://iep2a:8004", "/v1/layout-detect"),
        iep2b_endpoint=_service_endpoint("IEP2B_URL", "http://iep2b:8005", "/v1/layout-detect"),
        backend=backend,
        iep1d_execution_timeout_seconds=iep1d_execution_timeout,
        iep2_call_timeout_seconds=_env_float(
            "IEP2_CALL_TIMEOUT_SECONDS",
            cold_start_timeout + execution_timeout,
        ),
        iep0_circuit_breaker=CircuitBreaker("iep0", cb_cfg),
        iep1a_circuit_breaker=CircuitBreaker("iep1a", cb_cfg),
        iep1b_circuit_breaker=CircuitBreaker("iep1b", cb_cfg),
        iep1d_circuit_breaker=CircuitBreaker("iep1d", cb_cfg),
        iep2a_circuit_breaker=CircuitBreaker("iep2a", cb_cfg),
        iep2b_circuit_breaker=CircuitBreaker("iep2b", cb_cfg),
        iep1e_endpoint=_service_endpoint("IEP1E_URL", "http://iep1e:8007", "/v1/semantic-norm"),
        iep1e_circuit_breaker=CircuitBreaker("iep1e", cb_cfg),
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


async def _read_artifact_bytes_with_retry(
    *,
    uri: str,
    timeout_seconds: float,
    attempts: int,
    backoff_seconds: float,
    job_id: str,
    page_number: int,
    context: str,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            logger.info(
                "worker_loop: reading artifact bytes job=%s page=%d context=%s attempt=%d/%d uri=%s timeout=%.1fs",
                job_id,
                page_number,
                context,
                attempt,
                attempts,
                uri,
                timeout_seconds,
            )
            data = await asyncio.wait_for(
                asyncio.to_thread(get_backend(uri).get_bytes, uri),
                timeout=timeout_seconds,
            )
            logger.info(
                "worker_loop: loaded artifact bytes job=%s page=%d context=%s bytes=%d uri=%s",
                job_id,
                page_number,
                context,
                len(data),
                uri,
            )
            return data
        except asyncio.TimeoutError:
            last_error = TimeoutError(
                f"artifact read timed out after {timeout_seconds:.1f}s for {uri}"
            )
            logger.warning(
                "worker_loop: artifact read timeout job=%s page=%d context=%s attempt=%d/%d uri=%s timeout=%.1fs",
                job_id,
                page_number,
                context,
                attempt,
                attempts,
                uri,
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "worker_loop: artifact read failed job=%s page=%d context=%s attempt=%d/%d uri=%s error=%s",
                job_id,
                page_number,
                context,
                attempt,
                attempts,
                uri,
                exc,
            )

        if attempt < attempts and backoff_seconds > 0:
            await asyncio.sleep(backoff_seconds * attempt)

    assert last_error is not None
    raise RuntimeError(
        f"could not read artifact bytes for {context} after {attempts} attempt(s): {uri} ({last_error})"
    ) from last_error


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


def _update_job_reading_direction(job: Job, new_direction: str) -> None:
    """
    Persist reading_direction on job with merge policy:
      - Write "ltr" or "rtl" unconditionally (resolved always wins).
      - Write "unresolved" only if no resolved value has been stored yet.

    This ensures a resolved direction from an earlier page is never demoted.
    """
    if new_direction in ("ltr", "rtl"):
        job.reading_direction = new_direction
    elif new_direction == "unresolved" and job.reading_direction is None:
        job.reading_direction = "unresolved"


def _reading_order_for_sub(direction: str, sub_page_index: int) -> int:
    """
    Compute the semantic reading_order (1-based) for a split child page.

    After the post-rotation swap, sub_page_index=0 is always the physical
    left page and sub_page_index=1 is the physical right page.

    RTL:  right page (sub=1) is first in reading order → reading_order=1
    LTR / unresolved: left page (sub=0) is first → reading_order=1
    """
    if direction == "rtl":
        return 2 if sub_page_index == 0 else 1
    # ltr or unresolved: left-first
    return 1 if sub_page_index == 0 else 2


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


async def _invoke_iep0_classification(
    *,
    job_id: str,
    page_number: int,
    image_uri: str,
    iep0_endpoint: str,
    iep0_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    fallback_material_type: str,
) -> str:
    """
    Call IEP0 to classify the material type of a single image.

    If IEP0 is unavailable (circuit breaker open, timeout, error),
    falls back to the job-level material_type.

    Returns the resolved material_type string.
    """
    if not iep0_circuit_breaker.allow_call():
        logger.warning(
            "worker_loop: iep0 skipped by open circuit breaker job=%s page=%d; "
            "using fallback material_type=%s",
            job_id,
            page_number,
            fallback_material_type,
        )
        return fallback_material_type

    payload = {
        "job_id": job_id,
        "page_number": page_number,
        "image_uri": image_uri,
    }

    try:
        raw = await backend.call(iep0_endpoint, payload)
        response = ClassifyResponse.model_validate(raw)
        iep0_circuit_breaker.record_success()
        logger.info(
            "worker_loop: iep0 classified job=%s page=%d as %s (conf=%.3f)",
            job_id,
            page_number,
            response.material_type,
            response.confidence,
        )
        return response.material_type
    except BackendError as exc:
        iep0_circuit_breaker.record_failure(exc.kind)
        logger.warning(
            "worker_loop: iep0 failed job=%s page=%d kind=%s error=%s; "
            "using fallback material_type=%s",
            job_id,
            page_number,
            exc.kind.value,
            exc,
            fallback_material_type,
        )
    except ValidationError as exc:
        iep0_circuit_breaker.record_failure(None)
        logger.warning(
            "worker_loop: iep0 returned malformed response job=%s page=%d error=%s; "
            "using fallback material_type=%s",
            job_id,
            page_number,
            exc,
            fallback_material_type,
        )
    except Exception:
        iep0_circuit_breaker.record_failure(None)
        logger.exception(
            "worker_loop: unexpected iep0 failure job=%s page=%d; "
            "using fallback material_type=%s",
            job_id,
            page_number,
            fallback_material_type,
        )
    return fallback_material_type


# ── IEP0: batch classification with majority voting ─────────────────────────

_IEP0_SMALL_THRESHOLD = 11  # classify all if fewer than this many pages
_IEP0_SAMPLE_RATIO = 0.2    # sample 20% of pages when >= threshold
_IEP0_MAX_SAMPLE = 50       # hard cap on sample size
_IEP0_VOTE_CONFIDENCE_THRESHOLD = 0.70  # 70% vote share required


def _compute_sample_size(total_pages: int) -> int:
    """
    Compute number of images to sample for IEP0 majority voting.

    - < 11 pages  → classify all
    - >= 11 pages → 20% of total, capped at 50
    """
    if total_pages < _IEP0_SMALL_THRESHOLD:
        return total_pages
    import math
    return min(math.ceil(total_pages * _IEP0_SAMPLE_RATIO), _IEP0_MAX_SAMPLE)


async def _invoke_iep0_batch_classification(
    *,
    session: Session,
    job: Job,
    current_proxy_uri: str,
    iep0_batch_endpoint: str,
    iep0_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
    fallback_material_type: str,
) -> str:
    """
    Classify job pages via IEP0 with majority voting and a confidence retry.

    Round 1: sample images and classify.  If the winner gets >= 70% of votes,
    accept immediately.  Otherwise take a second, *different* sample and
    classify again, then combine both rounds' votes.  The final winner is
    used regardless of its vote share (with a warning if still < 70%).

    Falls back to fallback_material_type if IEP0 is unavailable.
    """
    if not iep0_circuit_breaker.allow_call():
        logger.warning(
            "worker_loop: iep0 batch skipped by open circuit breaker job=%s; "
            "using fallback material_type=%s",
            job.job_id,
            fallback_material_type,
        )
        return fallback_material_type

    # Gather all page image URIs from the job.
    pages = session.query(JobPage).filter_by(job_id=job.job_id).all()
    all_uris: list[str] = [current_proxy_uri]
    for p in pages:
        uri = p.input_image_uri
        if uri and uri != current_proxy_uri:
            all_uris.append(uri)

    sample_size = _compute_sample_size(len(all_uris))

    # ── Round 1 ─────────────────────────────────────────────────────────────
    round1_uris = all_uris[:sample_size]
    round1_response = await _call_iep0_batch(
        job_id=job.job_id,
        image_uris=round1_uris,
        iep0_batch_endpoint=iep0_batch_endpoint,
        iep0_circuit_breaker=iep0_circuit_breaker,
        backend=backend,
    )
    if round1_response is None:
        return fallback_material_type

    # Check if winner has >= 70% of votes.
    total_votes = round1_response.sample_size
    winner_votes = round1_response.vote_counts.get(round1_response.material_type, 0)
    vote_ratio = winner_votes / total_votes if total_votes > 0 else 0.0

    if vote_ratio >= _IEP0_VOTE_CONFIDENCE_THRESHOLD:
        logger.info(
            "worker_loop: iep0 round 1 confident job=%s type=%s "
            "votes=%d/%d (%.0f%%)",
            job.job_id,
            round1_response.material_type,
            winner_votes,
            total_votes,
            vote_ratio * 100,
        )
        return round1_response.material_type

    # ── Round 2: low confidence, retry with different images ────────────────
    remaining_uris = all_uris[sample_size:]
    if not remaining_uris:
        # No more images to sample — use round 1 result with a warning.
        logger.warning(
            "worker_loop: iep0 low confidence job=%s type=%s "
            "votes=%d/%d (%.0f%%) but no more images to sample",
            job.job_id,
            round1_response.material_type,
            winner_votes,
            total_votes,
            vote_ratio * 100,
        )
        return round1_response.material_type

    round2_size = min(sample_size, len(remaining_uris))
    round2_uris = remaining_uris[:round2_size]
    round2_response = await _call_iep0_batch(
        job_id=job.job_id,
        image_uris=round2_uris,
        iep0_batch_endpoint=iep0_batch_endpoint,
        iep0_circuit_breaker=iep0_circuit_breaker,
        backend=backend,
    )

    if round2_response is None:
        # Round 2 failed — use round 1 result.
        logger.warning(
            "worker_loop: iep0 round 2 failed job=%s; using round 1 result %s",
            job.job_id,
            round1_response.material_type,
        )
        return round1_response.material_type

    # ── Combine both rounds' votes ──────────────────────────────────────────
    from collections import Counter

    combined_votes: Counter[str] = Counter()
    for type_name, count in round1_response.vote_counts.items():
        combined_votes[type_name] += count
    for type_name, count in round2_response.vote_counts.items():
        combined_votes[type_name] += count

    final_winner = combined_votes.most_common(1)[0][0]
    final_total = sum(combined_votes.values())
    final_winner_votes = combined_votes[final_winner]
    final_ratio = final_winner_votes / final_total if final_total > 0 else 0.0

    if final_ratio < _IEP0_VOTE_CONFIDENCE_THRESHOLD:
        logger.warning(
            "worker_loop: iep0 still low confidence after 2 rounds job=%s "
            "type=%s votes=%d/%d (%.0f%%)",
            job.job_id,
            final_winner,
            final_winner_votes,
            final_total,
            final_ratio * 100,
        )
    else:
        logger.info(
            "worker_loop: iep0 round 2 resolved job=%s type=%s "
            "votes=%d/%d (%.0f%%)",
            job.job_id,
            final_winner,
            final_winner_votes,
            final_total,
            final_ratio * 100,
        )

    return final_winner


async def _call_iep0_batch(
    *,
    job_id: str,
    image_uris: list[str],
    iep0_batch_endpoint: str,
    iep0_circuit_breaker: CircuitBreaker,
    backend: GPUBackend,
) -> BatchClassifyResponse | None:
    """
    Call the IEP0 batch endpoint.  Returns the response on success,
    or None on any failure (circuit breaker, backend error, validation).
    """
    payload = {
        "job_id": job_id,
        "image_uris": image_uris,
    }

    try:
        raw = await backend.call(iep0_batch_endpoint, payload)
        response = BatchClassifyResponse.model_validate(raw)
        iep0_circuit_breaker.record_success()
        return response
    except BackendError as exc:
        iep0_circuit_breaker.record_failure(exc.kind)
        logger.warning(
            "worker_loop: iep0 batch failed job=%s kind=%s error=%s",
            job_id,
            exc.kind.value,
            exc,
        )
    except ValidationError as exc:
        iep0_circuit_breaker.record_failure(None)
        logger.warning(
            "worker_loop: iep0 batch malformed response job=%s error=%s",
            job_id,
            exc,
        )
    except Exception:
        iep0_circuit_breaker.record_failure(None)
        logger.exception(
            "worker_loop: unexpected iep0 batch failure job=%s",
            job_id,
        )
    return None


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
    sub_page_index: int | None,
    image_uri: str,
    material_type: str,
    backend: GPUBackend,
    circuit_breaker: CircuitBreaker,
) -> LayoutDetectResponse | None:
    if not circuit_breaker.allow_call():
        logger.warning(
            "worker_loop: %s skipped by open circuit breaker job=%s page=%d sub=%s",
            service_name,
            job_id,
            page_number,
            sub_page_index,
        )
        return None

    payload: dict[str, object] = {
        "job_id": job_id,
        "page_number": page_number,
        "image_uri": image_uri,
        "material_type": material_type,
    }
    if sub_page_index is not None:
        payload["sub_page_index"] = sub_page_index

    try:
        raw = await backend.call(endpoint, payload)
        response = LayoutDetectResponse.model_validate(raw)
        circuit_breaker.record_success()
        return response
    except BackendError as exc:
        circuit_breaker.record_failure(exc.kind)
        logger.warning(
            "worker_loop: %s failed job=%s page=%d sub=%s kind=%s error=%s",
            service_name,
            job_id,
            page_number,
            sub_page_index,
            exc.kind.value,
            exc,
        )
    except ValidationError as exc:
        circuit_breaker.record_failure(None)
        logger.warning(
            "worker_loop: %s returned malformed response job=%s page=%d sub=%s error=%s",
            service_name,
            job_id,
            page_number,
            sub_page_index,
            exc,
        )
    except Exception:
        circuit_breaker.record_failure(None)
        logger.exception(
            "worker_loop: unexpected %s failure job=%s page=%d sub=%s",
            service_name,
            job_id,
            page_number,
            sub_page_index,
        )
    return None


_IEP1E_READY_POLL_INTERVAL_SECONDS: float = 5.0
_IEP1E_READY_TIMEOUT_SECONDS: float = 600.0  # covers model download + init on cold start


async def _wait_for_iep1e_ready(endpoint: str) -> bool:
    """
    Poll iep1e /ready until it returns HTTP 200 or the timeout expires.

    Background: iep1e's /health returns 200 as soon as FastAPI starts (~3 s),
    but PaddlePaddle model loading takes another 60-120 s.  The shared
    LocalHTTPBackend._wait_for_warm polls /health and passes too early,
    causing the first inference request to hang until execution_timeout fires.

    By polling /ready here (which returns 503 until is_model_ready() == True),
    we absorb the model-loading wait as a cold-start grace period instead of
    burning the inference timeout.

    Returns:
        True  — iep1e is ready to serve inference
        False — timed out; caller should fall back
    """
    parsed = urlparse(endpoint)
    ready_url = f"{parsed.scheme}://{parsed.netloc}/ready"
    deadline = asyncio.get_event_loop().time() + _IEP1E_READY_TIMEOUT_SECONDS

    async with httpx.AsyncClient() as client:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "worker_loop: iep1e not ready after %.0fs — skipping",
                    _IEP1E_READY_TIMEOUT_SECONDS,
                )
                return False
            try:
                resp = await client.get(
                    ready_url, timeout=min(5.0, remaining)
                )
                if resp.status_code == 200:
                    return True
                # 503 = model still loading; any other non-200 is unexpected but
                # we keep polling rather than failing fast.
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass  # container still starting

            await asyncio.sleep(_IEP1E_READY_POLL_INTERVAL_SECONDS)


async def _call_iep1e(
    *,
    page_uris: list[str],
    x_centers: list[float],
    sub_page_indices: list[int],
    job_id: str,
    page_number: int,
    material_type: str,
    endpoint: str,
    backend: GPUBackend,
    cb: CircuitBreaker,
) -> SemanticNormResponse | None:
    """
    Call IEP1E POST /v1/semantic-norm.  Never raises — all failures return None.

    On None, callers must fall back to the original (geometry-only) URIs.
    Circuit breaker is checked before the call; recorded on success/failure.

    Args:
        page_uris:         1 or 2 IEP1C-normalized artifact URIs, physical left-first.
        x_centers:         Physical x-center for each URI (same order).
        sub_page_indices:  sub_page_index for each URI.
        job_id:            Parent job identifier.
        page_number:       1-indexed page number (for logging).
        material_type:     Job material type string.
        endpoint:          Full HTTP URL for IEP1E POST /v1/semantic-norm.
        backend:           Shared GPUBackend instance.
        cb:                Per-worker CircuitBreaker for IEP1E.

    Returns:
        SemanticNormResponse on success; None on any failure.
    """
    if not cb.allow_call():
        logger.warning(
            "worker_loop: iep1e skipped by open circuit breaker job=%s page=%d",
            job_id,
            page_number,
        )
        return None

    # Wait for iep1e to finish model initialisation before sending inference.
    # /health returns 200 immediately (FastAPI up), but /ready returns 503 until
    # PaddlePaddle models are loaded.  Waiting here prevents warm_inference_timeout
    # from firing on the first request after a container restart.
    if not await _wait_for_iep1e_ready(endpoint):
        return None

    payload: dict[str, object] = {
        "job_id": job_id,
        "page_number": page_number,
        "page_uris": page_uris,
        "x_centers": x_centers,
        "sub_page_indices": sub_page_indices,
        "material_type": material_type,
    }

    try:
        raw = await backend.call(endpoint, payload)
        response = SemanticNormResponse.model_validate(raw)
        cb.record_success()
        logger.info(
            "worker_loop: iep1e semantic-norm job=%s page=%d "
            "direction=%s fallback=%s pages=%d",
            job_id,
            page_number,
            response.reading_direction,
            response.fallback_used,
            len(response.pages),
        )
        return response
    except BackendError as exc:
        cb.record_failure(exc.kind)
        logger.warning(
            "worker_loop: iep1e failed job=%s page=%d kind=%s error=%s; "
            "using geometry-only fallback",
            job_id,
            page_number,
            exc.kind.value,
            exc,
        )
    except ValidationError as exc:
        cb.record_failure(None)
        logger.warning(
            "worker_loop: iep1e returned malformed response job=%s page=%d error=%s; "
            "using geometry-only fallback",
            job_id,
            page_number,
            exc,
        )
    except Exception:
        cb.record_failure(None)
        logger.exception(
            "worker_loop: unexpected iep1e failure job=%s page=%d; "
            "using geometry-only fallback",
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

    # ── IEP0: material-type classification via batch majority voting ───────
    # Sample up to 10 page images from the job and classify them.
    # The majority-voted result overrides the job-level material_type.
    material_type = await _invoke_iep0_batch_classification(
        session=session,
        job=job,
        current_proxy_uri=proxy_uri,
        iep0_batch_endpoint=config.iep0_batch_endpoint,
        iep0_circuit_breaker=config.iep0_circuit_breaker,
        backend=config.backend,
        fallback_material_type=material_type,
    )

    # Persist the classified material_type back to the job record so the
    # frontend and downstream queries reflect the real classification.
    if material_type != job.material_type:
        logger.info(
            "worker_loop: updating job material_type %s -> %s job=%s",
            job.material_type,
            material_type,
            job.job_id,
        )
        job.material_type = material_type
        session.commit()

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

    # ── Write quality_gate_log row ─────────────────────────────────────────
    gate_record = build_geometry_gate_log_record(
        result=selection,
        job_id=job.job_id,
        page_number=page.page_number,
        gate_type="geometry_selection",
        iep1a_response=geometry_result.iep1a_result,
        iep1b_response=geometry_result.iep1b_result,
        processing_time_ms=(time.monotonic() - task_started_at) * 1000.0,
    )
    log_gate(session, **gate_record)

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

    # ── Split handling ────────────────────────────────────────────────────────
    # When split_required=True the image is a book spread with two page regions.
    # Normalize both child pages, create child JobPage + PageLineage rows, and
    # set the parent page to "split".

    if selection.selected.response.split_required:
        left_output_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "output",
            page.page_number, 0, ".tiff",
        )
        right_output_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "output",
            page.page_number, 1, ".tiff",
        )
        left_rescue_output_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "output_rectified",
            page.page_number, 0, ".tiff",
        )
        right_rescue_output_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "output_rectified",
            page.page_number, 1, ".tiff",
        )
        left_rectified_proxy_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "proxy_rectified",
            page.page_number, 0, ".png",
        )
        right_rectified_proxy_uri = _artifact_uri(
            page.input_image_uri, job.job_id, "proxy_rectified",
            page.page_number, 1, ".png",
        )
        split_storage = get_backend(left_output_uri)
        split_loader = make_cv2_image_loader(split_storage)

        try:
            split_outcome: SplitOutcome = await run_split_normalization(
                full_res_image=full_res_image,
                selected_geometry=selection.selected.response,
                selected_model=selection.selected.model,
                proxy_width=proxy_width,
                proxy_height=proxy_height,
                left_output_uri=left_output_uri,
                right_output_uri=right_output_uri,
                left_rescue_output_uri=left_rescue_output_uri,
                right_rescue_output_uri=right_rescue_output_uri,
                left_rectified_proxy_uri=left_rectified_proxy_uri,
                right_rectified_proxy_uri=right_rectified_proxy_uri,
                storage=split_storage,
                image_loader=split_loader,
                job_id=job.job_id,
                page_number=page.page_number,
                lineage_id=lineage.lineage_id,
                material_type=material_type,
                iep1d_endpoint=config.iep1d_endpoint,
                iep1a_endpoint=config.iep1a_endpoint,
                iep1b_endpoint=config.iep1b_endpoint,
                iep1d_circuit_breaker=config.iep1d_circuit_breaker,
                iep1a_circuit_breaker=config.iep1a_circuit_breaker,
                iep1b_circuit_breaker=config.iep1b_circuit_breaker,
                backend=config.backend,
                session=session,
                iep1d_execution_timeout_seconds=config.iep1d_execution_timeout_seconds,
                gate_config=gate_config,
            )
        except Exception as exc:
            raise RetryableTaskError(f"split normalization failed: {exc}") from exc

        # ── IEP1E — semantic normalization for split children ────────────────
        # Resolve orientation + reading order for both crops (best-effort).
        # On failure, original physical-left-first order is preserved.
        _split_iep1e_uri_map: dict[int, str] = {
            0: split_outcome.left.branch_response.processed_image_uri
            if split_outcome.left.branch_response is not None else left_output_uri,
            1: split_outcome.right.branch_response.processed_image_uri
            if split_outcome.right.branch_response is not None else right_output_uri,
        }
        try:
            _left_br = split_outcome.left.branch_response
            _right_br = split_outcome.right.branch_response
            if _left_br is not None and _right_br is not None:
                _split_page_uris = [
                    _left_br.processed_image_uri,
                    _right_br.processed_image_uri,
                ]
                _split_x_centers = [
                    (_left_br.crop.crop_box.x_min + _left_br.crop.crop_box.x_max) / 2.0,
                    (_right_br.crop.crop_box.x_min + _right_br.crop.crop_box.x_max) / 2.0,
                ]
                _split_sem_result = await _call_iep1e(
                    page_uris=_split_page_uris,
                    x_centers=_split_x_centers,
                    sub_page_indices=[0, 1],
                    job_id=job.job_id,
                    page_number=page.page_number,
                    material_type=material_type,
                    endpoint=config.iep1e_endpoint,
                    backend=config.backend,
                    cb=config.iep1e_circuit_breaker,
                )
                if _split_sem_result is not None and _split_sem_result.pages:
                    for _sp in _split_sem_result.pages:
                        _split_iep1e_uri_map[_sp.sub_page_index] = _sp.oriented_uri

                    # ── Blank-page rotation inheritance ──────────────────────
                    # If exactly one page has no OCR text (blank page),
                    # it cannot self-determine its orientation.  Apply the
                    # same rotation as the confident sibling so both halves
                    # are consistently oriented.
                    _pages = _split_sem_result.pages
                    if len(_pages) == 2:
                        _p0, _p1 = _pages[0], _pages[1]
                        _confident_page = None
                        _blank_page = None
                        if _p0.orientation.orientation_confident and not _p1.orientation.orientation_confident:
                            _confident_page, _blank_page = _p0, _p1
                        elif _p1.orientation.orientation_confident and not _p0.orientation.orientation_confident:
                            _confident_page, _blank_page = _p1, _p0

                        if (
                            _confident_page is not None
                            and _blank_page is not None
                            and _confident_page.orientation.best_rotation_deg != 0
                        ):
                            _donor_deg = _confident_page.orientation.best_rotation_deg
                            _blank_src_uri = _blank_page.original_uri
                            _blank_oriented_uri = _artifact_uri(
                                page.input_image_uri,
                                job.job_id,
                                "oriented",
                                page.page_number,
                                _blank_page.sub_page_index,
                                ".tiff",
                            )
                            try:
                                _raw = await asyncio.to_thread(
                                    get_backend(_blank_src_uri).get_bytes, _blank_src_uri
                                )
                                _arr = np.frombuffer(_raw, dtype=np.uint8)
                                _blank_img = cv2.imdecode(_arr, cv2.IMREAD_COLOR)
                                if _blank_img is not None:
                                    _cv2_rot = {
                                        90: cv2.ROTATE_90_CLOCKWISE,
                                        180: cv2.ROTATE_180,
                                        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                                    }[_donor_deg]
                                    _rotated_blank = cv2.rotate(_blank_img, _cv2_rot)
                                    _success, _buf = cv2.imencode(".tiff", _rotated_blank)
                                    if _success:
                                        get_backend(_blank_oriented_uri).put_bytes(
                                            _blank_oriented_uri, bytes(_buf.tobytes())
                                        )
                                        _split_iep1e_uri_map[_blank_page.sub_page_index] = (
                                            _blank_oriented_uri
                                        )
                                        logger.info(
                                            "worker_loop: blank split child sub=%d rotated %d° "
                                            "(inherited from sibling sub=%d) job=%s page=%d → %s",
                                            _blank_page.sub_page_index,
                                            _donor_deg,
                                            _confident_page.sub_page_index,
                                            job.job_id,
                                            page.page_number,
                                            _blank_oriented_uri,
                                        )
                            except Exception as _rot_exc:
                                logger.warning(
                                    "worker_loop: blank split rotation inheritance failed "
                                    "sub=%d job=%s: %s — leaving original URI",
                                    _blank_page.sub_page_index,
                                    job.job_id,
                                    _rot_exc,
                                )

                    # ── Post-rotation left/right ordering ─────────────────────
                    # The geometry model runs on the pre-rotation scan, so the
                    # initial pages[0]/pages[1] order (and hence sub=0/sub=1)
                    # reflects scan-space positions, not corrected-view positions.
                    # Example: a landscape spread photographed 90° CCW appears
                    # as two vertically-stacked crops; the top crop (pages[0])
                    # becomes the RIGHT page after 270° CW correction, not left.
                    #
                    # Algorithm: transform each page's center (from the full-res
                    # crop_box) through the IEP1E rotation angle to get its
                    # post-correction x-position.  If sub=0 ends up to the RIGHT
                    # of sub=1 after correction, swap the URI assignments.
                    #
                    # Rotation transforms for a W×H image:
                    #   0°   → x_post = x_pre           (no change)
                    #   90°  → x_post = H - y_pre       (top → right)
                    #   180° → x_post = W - x_pre       (left ↔ right)
                    #   270° → x_post = y_pre            (top → left)
                    try:
                        _iep1e_by_sub = {
                            _sp.sub_page_index: _sp for _sp in _split_sem_result.pages
                        }
                        _rot_p0 = _iep1e_by_sub.get(0)
                        _rot_p1 = _iep1e_by_sub.get(1)
                        if (
                            _rot_p0 is not None
                            and _rot_p1 is not None
                            and _left_br is not None
                            and _right_br is not None
                        ):
                            # Use the confident page's rotation; fallback to sub=0
                            if (
                                _rot_p1.orientation.orientation_confident
                                and not _rot_p0.orientation.orientation_confident
                            ):
                                _ref_rot_deg = _rot_p1.orientation.best_rotation_deg
                            else:
                                _ref_rot_deg = _rot_p0.orientation.best_rotation_deg

                            _fr_h, _fr_w = full_res_image.shape[:2]

                            # Page centers in full-res scan space
                            _cx0 = (
                                _left_br.crop.crop_box.x_min
                                + _left_br.crop.crop_box.x_max
                            ) / 2.0
                            _cy0 = (
                                _left_br.crop.crop_box.y_min
                                + _left_br.crop.crop_box.y_max
                            ) / 2.0
                            _cx1 = (
                                _right_br.crop.crop_box.x_min
                                + _right_br.crop.crop_box.x_max
                            ) / 2.0
                            _cy1 = (
                                _right_br.crop.crop_box.y_min
                                + _right_br.crop.crop_box.y_max
                            ) / 2.0

                            def _post_rot_x(
                                cx: float, cy: float, deg: int, W: int, H: int
                            ) -> float:
                                if deg == 90:
                                    return H - cy   # top → right
                                if deg == 180:
                                    return W - cx   # left ↔ right
                                if deg == 270:
                                    return cy       # top → left
                                return cx           # 0°: unchanged

                            _px0 = _post_rot_x(_cx0, _cy0, _ref_rot_deg, _fr_w, _fr_h)
                            _px1 = _post_rot_x(_cx1, _cy1, _ref_rot_deg, _fr_w, _fr_h)

                            if _px0 > _px1:
                                # After correction sub=0 is to the RIGHT of sub=1:
                                # swap URI assignments so sub=0 → left, sub=1 → right.
                                _split_iep1e_uri_map[0], _split_iep1e_uri_map[1] = (
                                    _split_iep1e_uri_map[1],
                                    _split_iep1e_uri_map[0],
                                )
                                logger.info(
                                    "worker_loop: post-rotation left/right swap "
                                    "rot=%d° cx0=%.0f cy0=%.0f→px0=%.0f "
                                    "cx1=%.0f cy1=%.0f→px1=%.0f "
                                    "job=%s page=%d",
                                    _ref_rot_deg,
                                    _cx0, _cy0, _px0,
                                    _cx1, _cy1, _px1,
                                    job.job_id,
                                    page.page_number,
                                )
                            else:
                                logger.debug(
                                    "worker_loop: post-rotation order preserved "
                                    "rot=%d° px0=%.0f px1=%.0f job=%s page=%d",
                                    _ref_rot_deg, _px0, _px1,
                                    job.job_id, page.page_number,
                                )
                    except Exception as _swap_exc:
                        logger.warning(
                            "worker_loop: post-rotation swap failed job=%s page=%d: %s "
                            "— keeping current URI order",
                            job.job_id,
                            page.page_number,
                            _swap_exc,
                        )
        except Exception:
            logger.exception(
                "worker_loop: iep1e split call raised unexpectedly job=%s page=%d; "
                "using original URIs",
                job.job_id,
                page.page_number,
            )

        # Set parent page to "split" status
        advanced = advance_page_state(
            session,
            page.page_id,
            from_state=current_state,
            to_state="split",
            processing_time_ms=(time.monotonic() - task_started_at) * 1000.0,
        )
        if not advanced:
            logger.warning(
                "worker_loop: split CAS miss job=%s page_id=%s",
                job.job_id, page.page_id,
            )
            return "ack"
        page.status = "split"
        lineage.split_source = True
        _commit(session)

        # Derive reading_direction for this spread from IEP1E result (or "unresolved").
        _split_direction: str = (
            _split_sem_result.reading_direction
            if _split_sem_result is not None
            else "unresolved"
        )

        # Create child rows for each split half
        for child_outcome, child_uri in [
            (split_outcome.left, left_output_uri),
            (split_outcome.right, right_output_uri),
        ]:
            sub_idx = child_outcome.sub_page_index
            if child_outcome.route == "accept_now":
                # Mirror single-page routing: layout mode goes to layout_detection,
                # ptiff_qa_mode=manual stops at ptiff_qa_pending, else accepted.
                if job.ptiff_qa_mode == "manual":
                    child_status = "ptiff_qa_pending"
                elif job.pipeline_mode == "layout":
                    child_status = "layout_detection"
                else:
                    child_status = "accepted"
            else:
                child_status = "pending_human_correction"
            # Use IEP1E-oriented URI when available; fall back to IEP1C output.
            _child_oriented_uri = _split_iep1e_uri_map.get(sub_idx)
            _child_output_uri: str
            if _child_oriented_uri:
                _child_output_uri = _child_oriented_uri
            elif child_outcome.branch_response is not None:
                _child_output_uri = child_outcome.branch_response.processed_image_uri
            else:
                _child_output_uri = child_uri
            child_page = JobPage(
                page_id=str(uuid.uuid4()),
                job_id=job.job_id,
                page_number=page.page_number,
                sub_page_index=sub_idx,
                status=child_status,
                input_image_uri=page.input_image_uri,
                output_image_uri=_child_output_uri,
                review_reasons=(
                    [child_outcome.review_reason or "split_child_failed"]
                    if child_status == "pending_human_correction"
                    else None
                ),
                reading_order=_reading_order_for_sub(_split_direction, sub_idx),
            )
            session.add(child_page)
            session.flush()

            child_lineage = create_lineage(
                session,
                lineage_id=str(uuid.uuid4()),
                job_id=job.job_id,
                page_number=page.page_number,
                sub_page_index=sub_idx,
                correlation_id=lineage.correlation_id,
                input_image_uri=lineage.input_image_uri,
                otiff_uri=lineage.otiff_uri,
                input_image_hash=lineage.input_image_hash,
                material_type=material_type,
                policy_version=job.policy_version,
                parent_page_id=page.page_id,
                split_source=True,
            )
            child_lineage.output_image_uri = child_page.output_image_uri
            if child_status == "accepted":
                child_lineage.acceptance_decision = "accepted"
                child_lineage.routing_path = "split_child_accepted"
            confirm_preprocessed_artifact(session, child_lineage.lineage_id)

            logger.info(
                "worker_loop: split child created job=%s page=%d sub=%d route=%s status=%s uri=%s",
                job.job_id, page.page_number, sub_idx,
                child_outcome.route,
                child_status,
                child_page.output_image_uri,
            )

            # Enqueue split children that need further processing:
            # - layout_detection: IEP2 worker picks up the child page
            # - ptiff_qa_pending:  QA viewer holds the child until a reviewer releases it
            # Children in "accepted" or "pending_human_correction" need no enqueue.
            if child_status in ("layout_detection", "ptiff_qa_pending"):
                enqueue_page_task(redis_client, _page_task_for(child_page))

        _sync_job_summary(session, job)
        _update_job_reading_direction(job, _split_direction)
        _commit(session)
        return "ack"

    # ── Single-page normalization ──────────────────────────────────────────────

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

    # ── IEP1E — semantic normalization (orientation + reading order) ──────────
    # Best-effort: failure falls back silently to the IEP1C output URI.
    _iep1e_x_center = (
        final_branch.crop.crop_box.x_min + final_branch.crop.crop_box.x_max
    ) / 2.0
    _iep1e_sub_idx = page.sub_page_index if page.sub_page_index is not None else 0
    _sem_norm_uri = final_branch.processed_image_uri  # default: unchanged
    _sem_norm_result = None
    try:
        _sem_norm_result = await _call_iep1e(
            page_uris=[final_branch.processed_image_uri],
            x_centers=[_iep1e_x_center],
            sub_page_indices=[_iep1e_sub_idx],
            job_id=job.job_id,
            page_number=page.page_number,
            material_type=material_type,
            endpoint=config.iep1e_endpoint,
            backend=config.backend,
            cb=config.iep1e_circuit_breaker,
        )
        if (
            _sem_norm_result is not None
            and _sem_norm_result.ordered_page_uris
        ):
            _sem_norm_uri = _sem_norm_result.ordered_page_uris[0]
    except Exception:
        logger.exception(
            "worker_loop: iep1e call raised unexpectedly job=%s page=%d; "
            "using original URI",
            job.job_id,
            page.page_number,
        )

    total_processing_ms = (time.monotonic() - task_started_at) * 1000.0
    final_quality_summary = _quality_summary_dict(final_branch)
    from_state = page.status

    if job.ptiff_qa_mode == "manual":
        # PTIFF QA checkpoint (spec Section 3.1 / 8.5):
        # Route to ptiff_qa_pending regardless of pipeline_mode.
        # The gate (ptiff_qa.py) will release the page to layout_detection or
        # accepted once the reviewer approves via the QA viewer.
        to_state = "ptiff_qa_pending"
    elif job.pipeline_mode == "layout":
        # auto_continue + layout: proceed directly to layout detection and enqueue IEP2.
        to_state = "layout_detection"
    else:
        # auto_continue + preprocess-only: accept immediately.
        to_state = "accepted"

    advanced = advance_page_state(
        session,
        page.page_id,
        from_state=from_state,
        to_state=to_state,
        output_image_uri=_sem_norm_uri,
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
    page.output_image_uri = _sem_norm_uri
    page.quality_summary = final_quality_summary
    page.processing_time_ms = total_processing_ms
    page.review_reasons = None
    page.reading_order = 1  # single page is always the first (and only) in reading order

    if to_state == "ptiff_qa_pending":
        # Worker stops here; the PTIFF QA gate (ptiff_qa.py) owns the next
        # transition.  No lineage completion or layout enqueue at this point.
        logger.info(
            "worker_loop: ptiff_qa_pending job=%s page_id=%s page=%d sub=%s",
            job.job_id,
            page.page_id,
            page.page_number,
            page.sub_page_index,
        )
    elif job.pipeline_mode == "layout":
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
            output_image_uri=_sem_norm_uri,
        )

    _single_direction: str = (
        _sem_norm_result.reading_direction
        if _sem_norm_result is not None
        else "unresolved"
    )
    _sync_job_summary(session, job)
    _update_job_reading_direction(job, _single_direction)
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


async def _prepare_layout_input_artifact(
    *,
    session: Session,
    page: JobPage,
    job: Job,
    lineage: PageLineage,
    source_page_artifact_uri: str,
    config: WorkerConfig,
) -> tuple[str, LayoutInputMetadata]:
    """Return the analyzed artifact URI plus persisted metadata for this layout run."""
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

    source_bytes = await _read_artifact_bytes_with_retry(
        uri=source_page_artifact_uri,
        timeout_seconds=config.layout_artifact_io_timeout_seconds,
        attempts=config.layout_artifact_io_attempts,
        backoff_seconds=config.layout_artifact_io_backoff_seconds,
        job_id=job.job_id,
        page_number=page.page_number,
        context="layout_source_artifact",
    )
    try:
        source_image = await asyncio.wait_for(
            asyncio.to_thread(_decode_image_array, source_bytes, uri=source_page_artifact_uri),
            timeout=config.layout_artifact_io_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RetryableTaskError(
            "layout source decode timed out "
            f"after {config.layout_artifact_io_timeout_seconds:.1f}s for {source_page_artifact_uri}"
        ) from exc

    downsample_uri = _artifact_uri(
        page.input_image_uri,
        job.job_id,
        "downsampled",
        page.page_number,
        page.sub_page_index,
        ".tiff",
    )
    try:
        downsample_result = await asyncio.wait_for(
            asyncio.to_thread(
                run_downsample_step,
                full_res_image=source_image,
                source_artifact_uri=source_page_artifact_uri,
                output_uri=downsample_uri,
                storage=get_backend(downsample_uri),
            ),
            timeout=config.layout_artifact_io_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RetryableTaskError(
            "layout downsample timed out "
            f"after {config.layout_artifact_io_timeout_seconds:.1f}s for {source_page_artifact_uri}"
        ) from exc
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

    try:
        layout_image_uri, layout_input = await _prepare_layout_input_artifact(
            session=session,
            page=page,
            job=job,
            lineage=lineage,
            source_page_artifact_uri=image_uri,
            config=config,
        )
    except RetryableTaskError:
        raise
    except Exception as exc:
        raise RetryableTaskError(f"layout input preparation failed: {exc}") from exc

    downsample_gate = _extract_downsample_gate(lineage)
    if layout_input.input_source == "downsampled":
        logger.info(
            "worker_loop: using downsampled artifact for layout job=%s page=%d uri=%s source=%s",
            job.job_id,
            page.page_number,
            layout_image_uri,
            image_uri,
        )

    async def _load_google_fallback_bytes() -> bytes:
        return await _read_artifact_bytes_with_retry(
            uri=layout_image_uri,
            timeout_seconds=config.layout_artifact_io_timeout_seconds,
            attempts=config.layout_artifact_io_attempts,
            backoff_seconds=config.layout_artifact_io_backoff_seconds,
            job_id=job.job_id,
            page_number=page.page_number,
            context="layout_google_fallback",
        )

    try:
        iep2a_result = await asyncio.wait_for(
            _call_layout_service(
                service_name="iep2a",
                endpoint=config.iep2a_endpoint,
                job_id=job.job_id,
                page_number=page.page_number,
                sub_page_index=page.sub_page_index,
                image_uri=layout_image_uri,
                material_type=material_type,
                backend=config.backend,
                circuit_breaker=config.iep2a_circuit_breaker,
            ),
            timeout=config.iep2_call_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "worker_loop: iep2a timed out after %.1fs job=%s page=%d",
            config.iep2_call_timeout_seconds,
            job.job_id,
            page.page_number,
        )
        config.iep2a_circuit_breaker.record_failure(None)
        iep2a_result = None

    try:
        iep2b_result = await asyncio.wait_for(
            _call_layout_service(
                service_name="iep2b",
                endpoint=config.iep2b_endpoint,
                job_id=job.job_id,
                page_number=page.page_number,
                sub_page_index=page.sub_page_index,
                image_uri=layout_image_uri,
                material_type=material_type,
                backend=config.backend,
                circuit_breaker=config.iep2b_circuit_breaker,
            ),
            timeout=config.iep2_call_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "worker_loop: iep2b timed out after %.1fs job=%s page=%d",
            config.iep2_call_timeout_seconds,
            job.job_id,
            page.page_number,
        )
        config.iep2b_circuit_breaker.record_failure(None)
        iep2b_result = None

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
            image_bytes=None,
            image_bytes_loader=_load_google_fallback_bytes,
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

    # task_id -> ClaimedTask map so the on_stale callback can call fail_task.
    _claimed_tasks: dict[str, ClaimedTask] = {}

    def _on_stale(report: Any) -> None:
        for task_id in report.stale_task_ids:
            claimed_stale = _claimed_tasks.pop(task_id, None)
            if claimed_stale is None:
                continue
            logger.warning(
                "worker_loop: watchdog stale — removing from queue "
                "task=%s page_id=%s retry_count=%d",
                task_id,
                claimed_stale.task.page_id,
                claimed_stale.task.retry_count,
            )
            try:
                fail_task(redis_client, claimed_stale, max_retries=config.max_task_retries)
            except redis_lib.RedisError:
                logger.exception(
                    "worker_loop: could not remove stale task=%s from queue", task_id
                )
                continue
            if claimed_stale.task.retry_count >= config.max_task_retries:
                _mark_exhausted_task_failed(
                    session_factory,
                    job_id=claimed_stale.task.job_id,
                    page_id=claimed_stale.task.page_id,
                    reason="task_watchdog_timeout",
                )

    watchdog_task: asyncio.Task[None] | None = None
    if watchdog is not None:
        watchdog_task = asyncio.create_task(
            watchdog.run_watch_loop(on_stale=_on_stale),
            name="watchdog",
        )

    try:
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
                _claimed_tasks[claimed.task.task_id] = claimed

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
                    _claimed_tasks.pop(claimed.task.task_id, None)
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
