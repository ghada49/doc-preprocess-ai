"""
services/shadow_worker/app/main.py
----------------------------------
Shadow Worker.

This service finalizes pending shadow evaluations reserved by the live worker:

1. Poll loop:
   - Claims ShadowTask payloads from Redis.
   - Loads the reserved shadow_evaluations row and matching page_lineage row.
   - Marks the evaluation completed or no_shadow_model.

2. Reconcile loop:
   - Requeues pending evaluations missing from both Redis lists.
   - Retries stale in-flight tasks whose claim timestamp expired.
   - Marks exhausted or unrecoverable pending evaluations failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import redis as redis_lib
from fastapi import FastAPI
from sqlalchemy.orm import Session

from typing import Any

from services.eep.app.db.models import Job, JobPage, ModelVersion, PageLineage, ShadowEvaluation
from services.eep.app.db.session import SessionLocal
from services.eep.app.redis_client import get_redis
from shared.logging_config import setup_logging
from shared.metrics import SHADOW_CONF_DELTA, SHADOW_TASKS_FAILED, SHADOW_TASKS_PROCESSED
from shared.middleware import configure_observability
from shared.schemas.queue import QUEUE_SHADOW_TASKS, QUEUE_SHADOW_TASKS_PROCESSING, ShadowTask

setup_logging(service_name="shadow_worker")
logger = logging.getLogger(__name__)

_SHADOW_CLAIMS_KEY = "libraryai:shadow_tasks:claims"
_REQUEUE_BACKOFF_MINUTES = 0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("shadow_worker: invalid %s=%r; using %.1f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("shadow_worker: invalid %s=%r; using %d", name, raw, default)
        return default


_POLL_TIMEOUT_SECONDS = _env_float("SHADOW_POLL_TIMEOUT_SECONDS", 5.0)
_RECONCILE_INTERVAL_SECONDS = _env_float("SHADOW_RECONCILE_INTERVAL_SECONDS", 120.0)
_TASK_TIMEOUT_MINUTES = _env_int("SHADOW_EVAL_TIMEOUT_MINUTES", 30)
_MAX_RETRIES = _env_int("SHADOW_MAX_RETRIES", 3)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _blmove_safe(redis_client: redis_lib.Redis, timeout: float) -> str | None:
    try:
        result = redis_client.blmove(
            QUEUE_SHADOW_TASKS,
            QUEUE_SHADOW_TASKS_PROCESSING,
            timeout,
            src="RIGHT",
            dest="LEFT",
        )
        return result
    except (redis_lib.ResponseError, AttributeError, TypeError):
        result = redis_client.brpoplpush(
            QUEUE_SHADOW_TASKS,
            QUEUE_SHADOW_TASKS_PROCESSING,
            timeout,
        )
        return result


def _claim_shadow_task(redis_client: redis_lib.Redis) -> tuple[ShadowTask, str] | None:
    raw_json = _blmove_safe(redis_client, _POLL_TIMEOUT_SECONDS)
    if raw_json is None:
        return None

    try:
        task = ShadowTask.model_validate_json(raw_json)
    except Exception:
        logger.exception("shadow_worker: invalid task payload in queue")
        pipe = redis_client.pipeline(transaction=True)
        pipe.lrem(QUEUE_SHADOW_TASKS_PROCESSING, 1, raw_json)
        pipe.execute()
        return None

    try:
        redis_client.hset(_SHADOW_CLAIMS_KEY, task.task_id, _utc_now().isoformat())
    except redis_lib.RedisError:
        logger.warning(
            "shadow_worker: failed to record claim timestamp task_id=%s",
            task.task_id,
        )

    return task, raw_json


def _ack_shadow_task(redis_client: redis_lib.Redis, task_id: str, raw_json: str) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.lrem(QUEUE_SHADOW_TASKS_PROCESSING, 1, raw_json)
    pipe.hdel(_SHADOW_CLAIMS_KEY, task_id)
    pipe.execute()


def _requeue_shadow_task(
    redis_client: redis_lib.Redis,
    task: ShadowTask,
    raw_json: str,
) -> None:
    retried = task.model_copy(update={"retry_count": task.retry_count + 1})
    pipe = redis_client.pipeline(transaction=True)
    pipe.lrem(QUEUE_SHADOW_TASKS_PROCESSING, 1, raw_json)
    pipe.hdel(_SHADOW_CLAIMS_KEY, task.task_id)
    pipe.lpush(QUEUE_SHADOW_TASKS, retried.model_dump_json())
    pipe.execute()


def _drop_processing_shadow_task(
    redis_client: redis_lib.Redis,
    task_id: str,
    raw_json: str,
) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.lrem(QUEUE_SHADOW_TASKS_PROCESSING, 1, raw_json)
    pipe.hdel(_SHADOW_CLAIMS_KEY, task_id)
    pipe.execute()


def _extract_gate_value(gate_results: Any, gate_name: str) -> float | None:
    """Extract a numeric 'value' from a gate_results JSONB dict entry."""
    if not isinstance(gate_results, dict):
        return None
    gate = gate_results.get(gate_name)
    if not isinstance(gate, dict):
        return None
    val = gate.get("value")
    return float(val) if isinstance(val, (int, float)) else None


def _find_lineage_for_page(db: Session, page: JobPage) -> PageLineage | None:
    return (
        db.query(PageLineage)
        .filter_by(
            job_id=page.job_id,
            page_number=page.page_number,
            sub_page_index=page.sub_page_index,
        )
        .first()
    )


def _ensure_shadow_evaluation(
    db: Session,
    task: ShadowTask,
    page_status: str,
) -> ShadowEvaluation:
    evaluation = db.get(ShadowEvaluation, task.task_id)
    if evaluation is None:
        evaluation = ShadowEvaluation(
            eval_id=task.task_id,
            job_id=task.job_id,
            page_id=task.page_id,
            page_status=page_status,
            status="pending",
        )
        db.add(evaluation)
    return evaluation


def _mark_shadow_evaluation_failed(
    db: Session,
    task: ShadowTask,
    *,
    page_status: str | None = None,
) -> None:
    evaluation = _ensure_shadow_evaluation(db, task, page_status or task.page_status)
    evaluation.page_status = page_status or evaluation.page_status
    evaluation.status = "failed"
    evaluation.completed_at = _utc_now()
    db.commit()


def _process_shadow_task(task: ShadowTask, db: Session) -> None:
    now = _utc_now()

    job = db.get(Job, task.job_id)
    page = db.get(JobPage, task.page_id)
    if job is None:
        raise RuntimeError(f"missing job {task.job_id}")
    if page is None:
        raise RuntimeError(f"missing page {task.page_id}")

    lineage = _find_lineage_for_page(db, page)
    if lineage is None:
        raise RuntimeError(
            f"missing lineage for job={page.job_id} page={page.page_number} sub={page.sub_page_index}"
        )

    if lineage.shadow_eval_id and lineage.shadow_eval_id != task.task_id:
        logger.info(
            "shadow_worker: duplicate task ignored job=%s page=%d task_id=%s existing_eval_id=%s",
            task.job_id,
            task.page_number,
            task.task_id,
            lineage.shadow_eval_id,
        )
        return

    evaluation = _ensure_shadow_evaluation(db, task, page.status)
    if lineage.shadow_eval_id is None:
        lineage.shadow_eval_id = task.task_id

    if evaluation.status in {"completed", "no_shadow_model"}:
        db.commit()
        logger.info(
            "shadow_worker: task already finalized eval_id=%s status=%s",
            evaluation.eval_id,
            evaluation.status,
        )
        return

    evaluation.page_status = page.status
    evaluation.confidence_delta = None

    if not job.shadow_mode:
        evaluation.status = "failed"
        evaluation.completed_at = now
        db.commit()
        SHADOW_TASKS_FAILED.inc()
        logger.warning(
            "shadow_worker: non-shadow job encountered job=%s page=%d",
            job.job_id,
            page.page_number,
        )
        return

    shadow_model = (
        db.query(ModelVersion)
        .filter(ModelVersion.stage == "shadow")
        .order_by(ModelVersion.created_at.desc())
        .first()
    )

    if shadow_model is not None:
        # Compute confidence_delta as the geometry_iou gap between shadow and
        # production models.  This is a model-level comparison using stored
        # gate_results from offline evaluation.  Per-page live inference is not
        # implemented — the same delta applies to every page in a shadow-mode job.
        production_model = (
            db.query(ModelVersion)
            .filter(ModelVersion.stage == "production")
            .order_by(ModelVersion.created_at.desc())
            .first()
        )
        shadow_iou = _extract_gate_value(shadow_model.gate_results, "geometry_iou")
        prod_iou = _extract_gate_value(
            production_model.gate_results if production_model is not None else None,
            "geometry_iou",
        )
        if shadow_iou is not None and prod_iou is not None:
            evaluation.confidence_delta = round(shadow_iou - prod_iou, 4)

    evaluation.status = "completed" if shadow_model is not None else "no_shadow_model"
    evaluation.completed_at = now
    db.commit()

    SHADOW_TASKS_PROCESSED.inc()
    if evaluation.confidence_delta is not None:
        SHADOW_CONF_DELTA.observe(evaluation.confidence_delta)

    logger.info(
        "shadow_worker: finalized eval_id=%s job=%s page=%d status=%s",
        evaluation.eval_id,
        job.job_id,
        page.page_number,
        evaluation.status,
    )


def _load_queue_items(redis_client: redis_lib.Redis, queue_key: str) -> dict[str, tuple[ShadowTask, str]]:
    items: dict[str, tuple[ShadowTask, str]] = {}
    try:
        raw_items = redis_client.lrange(queue_key, 0, -1)
    except redis_lib.RedisError:
        logger.exception("shadow_worker: could not read queue %s", queue_key)
        return items

    for raw_json in raw_items:
        try:
            task = ShadowTask.model_validate_json(raw_json)
        except Exception:
            logger.warning("shadow_worker: skipping invalid payload in %s", queue_key)
            continue
        items[task.task_id] = (task, raw_json)
    return items


def _parse_claimed_at(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        logger.warning("shadow_worker: invalid claim timestamp %r", raw_value)
        return None


def _requeue_missing_pending_evaluation(
    redis_client: redis_lib.Redis,
    db: Session,
    evaluation: ShadowEvaluation,
) -> None:
    page = db.get(JobPage, evaluation.page_id)
    if page is None:
        evaluation.status = "failed"
        evaluation.completed_at = _utc_now()
        logger.warning(
            "shadow_worker: failing pending eval with missing page eval_id=%s page_id=%s",
            evaluation.eval_id,
            evaluation.page_id,
        )
        return

    task = ShadowTask(
        task_id=evaluation.eval_id,
        job_id=evaluation.job_id,
        page_id=evaluation.page_id,
        page_number=page.page_number,
        page_status=evaluation.page_status,
        retry_count=0,
    )
    redis_client.lpush(QUEUE_SHADOW_TASKS, task.model_dump_json())
    logger.info(
        "shadow_worker: re-queued missing pending eval eval_id=%s job=%s page=%d",
        evaluation.eval_id,
        evaluation.job_id,
        page.page_number,
    )


async def _poll_loop(redis_client: redis_lib.Redis) -> None:
    logger.info(
        "shadow_worker: poll loop started timeout=%.1fs",
        _POLL_TIMEOUT_SECONDS,
    )
    while True:
        try:
            claimed = _claim_shadow_task(redis_client)
        except redis_lib.RedisError:
            logger.exception("shadow_worker: redis error in poll loop")
            await asyncio.sleep(5.0)
            continue

        if claimed is None:
            await asyncio.sleep(0)
            continue

        task, raw_json = claimed
        db: Session = SessionLocal()
        try:
            _process_shadow_task(task, db)
            _ack_shadow_task(redis_client, task.task_id, raw_json)
        except Exception:
            logger.exception(
                "shadow_worker: task failed job=%s page_id=%s retry=%d",
                task.job_id,
                task.page_id,
                task.retry_count,
            )
            db.rollback()
            SHADOW_TASKS_FAILED.inc()
            try:
                if task.retry_count >= _MAX_RETRIES:
                    _mark_shadow_evaluation_failed(db, task)
                    _drop_processing_shadow_task(redis_client, task.task_id, raw_json)
                else:
                    _requeue_shadow_task(redis_client, task, raw_json)
            except Exception:
                db.rollback()
                logger.exception(
                    "shadow_worker: failed resolving retry path task_id=%s",
                    task.task_id,
                )
        finally:
            db.close()

        await asyncio.sleep(0)


async def _reconcile_loop(redis_client: redis_lib.Redis) -> None:
    logger.info(
        "shadow_worker: reconcile loop started interval=%.1fs timeout=%dmin",
        _RECONCILE_INTERVAL_SECONDS,
        _TASK_TIMEOUT_MINUTES,
    )
    while True:
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)

        main_queue = _load_queue_items(redis_client, QUEUE_SHADOW_TASKS)
        processing_queue = _load_queue_items(redis_client, QUEUE_SHADOW_TASKS_PROCESSING)
        try:
            claims = redis_client.hgetall(_SHADOW_CLAIMS_KEY)
        except redis_lib.RedisError:
            logger.exception("shadow_worker: could not read shadow claims hash")
            claims = {}

        cutoff = _utc_now() - timedelta(minutes=_TASK_TIMEOUT_MINUTES)
        db: Session = SessionLocal()
        try:
            pending_rows = (
                db.query(ShadowEvaluation)
                .filter(ShadowEvaluation.status == "pending")
                .all()
            )

            for evaluation in pending_rows:
                if evaluation.eval_id in main_queue:
                    continue

                processing_entry = processing_queue.get(evaluation.eval_id)
                if processing_entry is not None:
                    task, raw_json = processing_entry
                    claimed_at = _parse_claimed_at(claims.get(evaluation.eval_id))
                    if claimed_at is None:
                        claimed_at = evaluation.created_at
                    if claimed_at is not None and claimed_at >= cutoff:
                        continue

                    if task.retry_count >= _MAX_RETRIES:
                        _mark_shadow_evaluation_failed(
                            db,
                            task,
                            page_status=evaluation.page_status,
                        )
                        _drop_processing_shadow_task(redis_client, task.task_id, raw_json)
                        logger.warning(
                            "shadow_worker: stale task exhausted retries eval_id=%s",
                            evaluation.eval_id,
                        )
                    else:
                        _requeue_shadow_task(redis_client, task, raw_json)
                        logger.warning(
                            "shadow_worker: re-queued stale in-flight task eval_id=%s retry=%d",
                            evaluation.eval_id,
                            task.retry_count + 1,
                        )
                    continue

                created_at = evaluation.created_at or _utc_now()
                if created_at + timedelta(minutes=_REQUEUE_BACKOFF_MINUTES) > _utc_now():
                    continue
                _requeue_missing_pending_evaluation(redis_client, db, evaluation)

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("shadow_worker: reconcile loop failed")
        finally:
            db.close()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    redis_client = get_redis()
    poll_task = asyncio.create_task(_poll_loop(redis_client), name="shadow-poll")
    reconcile_task = asyncio.create_task(
        _reconcile_loop(redis_client),
        name="shadow-reconcile",
    )
    logger.info("shadow_worker: poll and reconcile loops started")
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
        redis_client.close()
        logger.info("shadow_worker: poll and reconcile loops stopped")


app = FastAPI(
    title="Shadow Worker",
    version="0.1.0",
    description=(
        "Background worker that finalizes reserved shadow evaluations for "
        "shadow_mode jobs and reconciles missing or stale shadow tasks."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="shadow_worker")
