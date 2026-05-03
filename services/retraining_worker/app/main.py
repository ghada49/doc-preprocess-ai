"""
services/retraining_worker/app/main.py
----------------------------------------
Retraining Worker — model retraining trigger worker + reconciler.

Runs two concurrent background loops in one process:

  Poll loop (every RETRAINING_POLL_INTERVAL seconds, default 30):
    Queries retraining_triggers for pending rows, claims each by
    transitioning status → 'processing', then calls execute_retraining_task.
    On task exception: rolls back, marks trigger failed.

  Reconcile loop (every RETRAINING_RECONCILE_INTERVAL seconds, default 60):
    Detects retraining_jobs stuck in 'running' beyond the timeout window and
    retraining_triggers stuck in 'processing' whose linked job failed, and
    marks them failed so they are visible and actionable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import Session

from services.eep.app.db.models import RetrainingTrigger
from services.eep.app.db.session import SessionLocal
from services.retraining_worker.app.dataset_registry import select_retraining_dataset
from services.retraining_worker.app.golden_gate_merge import build_iep1ab_live_gates
from services.retraining_worker.app.live_train import run_live_preprocessing_training
from services.retraining_worker.app.reconcile import ReconcileConfig, run_reconciliation_loop
from services.retraining_worker.app.task import _resolve_env_pretrained_weights, execute_retraining_task
from shared.logging_config import setup_logging
from shared.metrics import (
    RETRAINING_JOB_DURATION_SECONDS,
    RETRAINING_JOBS_COMPLETED,
    RETRAINING_JOBS_FAILED,
    RETRAINING_JOBS_STARTED,
)
from shared.middleware import configure_observability

setup_logging(service_name="retraining_worker")
logger = logging.getLogger(__name__)

_POLL_INTERVAL: float = float(os.environ.get("RETRAINING_POLL_INTERVAL", "30"))
_RECONCILE_INTERVAL: float = float(os.environ.get("RETRAINING_RECONCILE_INTERVAL", "60"))
_RUNPOD_TERMINATE_ON_IDLE = os.environ.get("RUNPOD_TERMINATE_ON_IDLE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_WORKER_MODE = os.environ.get("RETRAINING_WORKER_MODE", "db_poll").strip().lower()


# ── Poll loop ─────────────────────────────────────────────────────────────────


async def _poll_loop() -> None:
    """
    Async loop: poll DB for pending retraining triggers and execute them.

    Each trigger is processed in its own DB session so a failure in one task
    cannot affect others in the same iteration.
    """
    logger.info("retraining_worker: poll loop started (interval=%.0fs)", _POLL_INTERVAL)
    while True:
        await asyncio.sleep(_POLL_INTERVAL)

        # Collect pending trigger IDs in a short-lived read session
        id_db: Session = SessionLocal()
        try:
            pending_ids: list[str] = [
                row[0]
                for row in id_db.query(RetrainingTrigger.trigger_id)
                .filter(RetrainingTrigger.status == "pending")
                .all()
            ]
        except Exception:
            logger.exception("retraining_worker: error querying pending triggers")
            pending_ids = []
        finally:
            id_db.close()

        for trigger_id in pending_ids:
            task_db: Session = SessionLocal()
            started_at: float | None = None
            try:
                trigger = task_db.get(RetrainingTrigger, trigger_id)
                if trigger is None or trigger.status != "pending":
                    # Already claimed or processed since we read the ID list
                    continue

                # Claim: transition to processing before executing
                trigger.status = "processing"
                task_db.commit()

                started_at = time.monotonic()
                RETRAINING_JOBS_STARTED.inc()
                execute_retraining_task(trigger, task_db)
                RETRAINING_JOBS_COMPLETED.inc()
                RETRAINING_JOB_DURATION_SECONDS.observe(time.monotonic() - started_at)

            except Exception:
                logger.exception("retraining_worker: task failed for trigger_id=%s", trigger_id)
                if started_at is not None:
                    RETRAINING_JOBS_FAILED.inc()
                    RETRAINING_JOB_DURATION_SECONDS.observe(time.monotonic() - started_at)
                try:
                    task_db.rollback()
                    failed_trigger = task_db.get(RetrainingTrigger, trigger_id)
                    if failed_trigger is not None:
                        failed_trigger.status = "failed"
                        task_db.commit()
                except Exception:
                    logger.exception(
                        "retraining_worker: could not mark trigger failed trigger_id=%s",
                        trigger_id,
                    )
            finally:
                task_db.close()

        if not pending_ids and _should_terminate_runpod_on_idle():
            if _no_retraining_work_left():
                logger.info("retraining_worker: no retraining work left; terminating RunPod pod")
                _terminate_runpod_pod()
                os._exit(0)


def _should_terminate_runpod_on_idle() -> bool:
    return bool(
        _RUNPOD_TERMINATE_ON_IDLE
        and os.environ.get("RUNPOD_POD_ID", "").strip()
        and os.environ.get("RUNPOD_API_KEY", "").strip()
    )


def _no_retraining_work_left() -> bool:
    db: Session = SessionLocal()
    try:
        count = (
            db.query(RetrainingTrigger)
            .filter(RetrainingTrigger.status.in_(["pending", "processing"]))
            .count()
        )
        return count == 0
    except Exception:
        logger.exception("retraining_worker: could not check idle state")
        return False
    finally:
        db.close()


def _terminate_runpod_pod() -> None:
    pod_id = os.environ.get("RUNPOD_POD_ID", "").strip()
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not pod_id or not api_key:
        return
    try:
        response = httpx.delete(
            f"https://rest.runpod.io/v1/pods/{pod_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(os.environ.get("RUNPOD_API_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        logger.info("retraining_worker: RunPod pod delete requested pod_id=%s", pod_id)
    except Exception:
        logger.exception("retraining_worker: failed to terminate RunPod pod_id=%s", pod_id)


def _stub_gate_results() -> dict:
    return {
        "geometry_iou": {"pass": True, "value": 0.84},
        "split_precision": {"pass": True, "value": 0.77},
        "structural_agreement_rate": {"pass": True, "value": 0.71},
        "golden_dataset": {"pass": True, "regressions": 0},
        "latency_p95": {"pass": True, "value": 2.3},
    }


async def _post_callback(payload: dict) -> None:
    callback_url = os.environ.get("RETRAINING_CALLBACK_URL", "").strip()
    callback_secret = os.environ.get("RETRAINING_CALLBACK_SECRET", "").strip()
    if not callback_url or not callback_secret:
        raise RuntimeError("RETRAINING_CALLBACK_URL and RETRAINING_CALLBACK_SECRET are required")

    async with httpx.AsyncClient(
        timeout=float(os.environ.get("RETRAINING_CALLBACK_TIMEOUT_SECONDS", "60"))
    ) as client:
        response = await client.post(
            callback_url,
            headers={"X-Retraining-Callback-Secret": callback_secret},
            json=payload,
        )
        response.raise_for_status()


def _gate_map(gate_results: dict[str, Any]) -> float | None:
    geometry = gate_results.get("geometry_iou")
    if isinstance(geometry, dict):
        value = geometry.get("value")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _live_model_notes(
    *,
    s3_uris: list[str],
    manifest_path: Path,
    dataset_checksum: str,
) -> str:
    parts: list[str] = []
    if s3_uris:
        parts.append("s3_weights:" + ",".join(s3_uris))
    parts.append(f"dataset_manifest={manifest_path}")
    if dataset_checksum:
        parts.append(f"dataset_checksum={dataset_checksum}")
    parts.append("RunPod callback_once live retraining")
    return " ".join(parts)


def _download_s3_uri(uri: str, dest: Path) -> None:
    if not uri.startswith("s3://"):
        raise RuntimeError(f"Expected s3:// dataset archive URI, got {uri!r}")
    try:
        import boto3  # type: ignore[import]
        from botocore.config import Config  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("boto3 is required to download retraining dataset archives") from exc

    without_scheme = uri[len("s3://") :]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise RuntimeError(f"Invalid dataset archive URI: {uri}")

    kwargs: dict[str, Any] = {}
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    region = os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "")).strip()
    if region:
        kwargs["region_name"] = region
    kwargs["config"] = Config(
        retries={"max_attempts": int(os.environ.get("S3_MAX_RETRIES", "3")), "mode": "adaptive"}
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    boto3.client("s3", **kwargs).download_file(bucket, key, str(dest))


def _extract_tar_safe(archive_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = (dest_dir / member.name).resolve()
            if not str(member_path).startswith(str(root)):
                raise RuntimeError(f"Unsafe dataset archive member path: {member.name}")
        archive.extractall(dest_dir)


def _provided_dataset_manifest(job_id: str) -> Path | None:
    archive_uri = os.environ.get("RETRAINING_DATASET_ARCHIVE_URI", "").strip()
    if not archive_uri:
        manifest = os.environ.get("RETRAINING_TRAIN_MANIFEST", "").strip()
        return Path(manifest) if manifest else None

    base_dir = Path("/workspace/retraining_dataset") / job_id
    manifest_rel = os.environ.get(
        "RETRAINING_DATASET_MANIFEST_REL",
        "retraining_train_manifest.json",
    ).strip()
    manifest_path = base_dir / manifest_rel
    if manifest_path.is_file():
        return manifest_path

    archive_path = base_dir.with_suffix(".tar.gz")
    _download_s3_uri(archive_uri, archive_path)
    _extract_tar_safe(archive_path, base_dir)
    if not manifest_path.is_file():
        raise RuntimeError(
            f"Dataset archive extracted but manifest was not found: {manifest_path}"
        )
    return manifest_path


def _build_live_callback_payload(trigger_id: str, job_id: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    provided_manifest = _provided_dataset_manifest(job_id)
    if provided_manifest is not None:
        manifest_path = provided_manifest
        dataset_version = os.environ.get(
            "RETRAINING_DATASET_VERSION",
            f"rt-{job_id.replace('-', '')[:12]}",
        ).strip()
        dataset_checksum = os.environ.get("RETRAINING_DATASET_CHECKSUM", "").strip()
    else:
        selection = select_retraining_dataset(repo_root, prefer_mode="corrected_only")
        manifest_path = selection.manifest_path
        dataset_version = selection.dataset_version or f"rt-{job_id.replace('-', '')[:12]}"
        dataset_checksum = selection.dataset_checksum

    trained_weights = run_live_preprocessing_training(
        repo_root,
        job_id,
        dataset_version,
        manifest_path=manifest_path,
        include_iep0=False,
        pretrained_iep1a=_resolve_env_pretrained_weights("iep1a") or None,
        pretrained_iep1b=_resolve_env_pretrained_weights("iep1b") or None,
    )

    mlflow_run_id = (
        ",".join(trained_weights.mlflow_run_ids)
        if trained_weights.mlflow_run_ids
        else f"live-no-runid-{uuid.uuid4().hex[:12]}"
    )

    eval_mode = os.environ.get("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub").strip().lower()
    if eval_mode == "live":
        gate_results = build_iep1ab_live_gates(
            repo_root,
            weights_iep1a_by_material=trained_weights.iep1a_weights,
            weights_iep1b_by_material=trained_weights.iep1b_weights,
        )
    else:
        gate_results = _stub_gate_results()

    notes = _live_model_notes(
        s3_uris=trained_weights.s3_uris,
        manifest_path=manifest_path,
        dataset_checksum=dataset_checksum,
    )
    short_job = job_id.replace("-", "")[:12]
    model_versions: list[dict[str, Any]] = []
    if trained_weights.iep1a_weights:
        model_versions.append(
            {
                "service_name": "iep1a",
                "version_tag": f"rt-{short_job}-iep1a",
                "mlflow_run_id": mlflow_run_id,
                "dataset_version": dataset_version,
                "gate_results": gate_results,
                "notes": notes,
            }
        )
    if trained_weights.iep1b_weights:
        model_versions.append(
            {
                "service_name": "iep1b",
                "version_tag": f"rt-{short_job}-iep1b",
                "mlflow_run_id": mlflow_run_id,
                "dataset_version": dataset_version,
                "gate_results": gate_results,
                "notes": notes,
            }
        )

    return {
        "trigger_id": trigger_id,
        "job_id": job_id,
        "status": "completed",
        "mlflow_run_id": mlflow_run_id,
        "dataset_version": dataset_version,
        "result_model_version": ",".join(v["version_tag"] for v in model_versions),
        "result_mAP": _gate_map(gate_results),
        "promotion_decision": "pending_gate_review",
        "model_versions": model_versions,
    }


async def _callback_once() -> None:
    trigger_id = os.environ.get("RETRAINING_TRIGGER_ID", "").strip()
    job_id = os.environ.get("RETRAINING_JOB_ID", "").strip()
    if not trigger_id or not job_id:
        logger.error("retraining_worker: callback_once missing trigger_id/job_id")
        os._exit(2)

    logger.info(
        "retraining_worker: callback_once started trigger_id=%s job_id=%s",
        trigger_id,
        job_id,
    )

    try:
        await _post_callback(
            {
                "trigger_id": trigger_id,
                "job_id": job_id,
                "status": "running",
            }
        )

        train_mode = os.environ.get("LIBRARYAI_RETRAINING_TRAIN", "stub").strip().lower()
        eval_mode = os.environ.get("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub").strip().lower()
        if train_mode == "live":
            await _post_callback(_build_live_callback_payload(trigger_id, job_id))
            logger.info(
                "retraining_worker: callback_once live training completed trigger_id=%s job_id=%s",
                trigger_id,
                job_id,
            )
            return

        if train_mode != "stub" or eval_mode != "stub":
            raise RuntimeError(
                "RunPod callback_once supports LIBRARYAI_RETRAINING_TRAIN=live or stub. "
                f"Got train={train_mode!r}, eval={eval_mode!r}."
            )

        mlflow_run_id = f"stub-run-{uuid.uuid4().hex[:12]}"
        dataset_version = os.environ.get(
            "RETRAINING_DATASET_VERSION",
            "ds-stub-preprocessing-001",
        ).strip()
        short_job = job_id.replace("-", "")[:12]
        model_versions = [
            {
                "service_name": "iep1a",
                "version_tag": f"stub-iep1a-{short_job}",
                "mlflow_run_id": mlflow_run_id,
                "dataset_version": dataset_version,
                "gate_results": _stub_gate_results(),
                "notes": "RunPod callback_once stub retraining",
            },
            {
                "service_name": "iep1b",
                "version_tag": f"stub-iep1b-{short_job}",
                "mlflow_run_id": mlflow_run_id,
                "dataset_version": dataset_version,
                "gate_results": _stub_gate_results(),
                "notes": "RunPod callback_once stub retraining",
            },
        ]

        await _post_callback(
            {
                "trigger_id": trigger_id,
                "job_id": job_id,
                "status": "completed",
                "mlflow_run_id": mlflow_run_id,
                "dataset_version": dataset_version,
                "result_model_version": ",".join(v["version_tag"] for v in model_versions),
                "result_mAP": 0.84,
                "promotion_decision": "pending_gate_review",
                "model_versions": model_versions,
            }
        )
        logger.info(
            "retraining_worker: callback_once completed trigger_id=%s job_id=%s",
            trigger_id,
            job_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "retraining_worker: callback_once failed trigger_id=%s job_id=%s",
            trigger_id,
            job_id,
        )
        try:
            await _post_callback(
                {
                    "trigger_id": trigger_id,
                    "job_id": job_id,
                    "status": "failed",
                    "error_message": str(exc),
                }
            )
        except Exception:
            logger.exception("retraining_worker: failed to post failure callback")
    finally:
        if _should_terminate_runpod_on_idle():
            _terminate_runpod_pod()
        os._exit(0)


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if _WORKER_MODE == "callback_once":
        callback_task = asyncio.create_task(_callback_once())
        logger.info("retraining_worker: callback_once task started")
        try:
            yield
        finally:
            callback_task.cancel()
            try:
                await callback_task
            except asyncio.CancelledError:
                pass
            logger.info("retraining_worker: callback_once task stopped")
        return

    poll_task = asyncio.create_task(_poll_loop())
    reconcile_task = asyncio.create_task(
        run_reconciliation_loop(
            session_factory=SessionLocal,
            config=ReconcileConfig(),
            interval_seconds=_RECONCILE_INTERVAL,
        )
    )
    logger.info("retraining_worker: poll + reconcile loops started")
    try:
        yield
    finally:
        poll_task.cancel()
        reconcile_task.cancel()
        for task in (poll_task, reconcile_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("retraining_worker: poll + reconcile loops stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Retraining Worker",
    version="0.1.0",
    description=(
        "Background worker that polls retraining_triggers for pending events, "
        "runs training (stub by default; LIBRARYAI_RETRAINING_TRAIN=live for real runs) "
        "and offline evaluation, and writes gate_results to model_versions. "
        "Also runs an inline reconciliation loop that detects and recovers stuck "
        "retraining jobs and triggers (formerly a separate retraining-recovery service)."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="retraining_worker")
