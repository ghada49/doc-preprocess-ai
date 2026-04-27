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
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from sqlalchemy.orm import Session

from services.eep.app.db.models import RetrainingTrigger
from services.eep.app.db.session import SessionLocal
from services.retraining_worker.app.reconcile import ReconcileConfig, run_reconciliation_loop
from services.retraining_worker.app.task import execute_retraining_task
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="retraining_worker")
logger = logging.getLogger(__name__)

_POLL_INTERVAL: float = float(os.environ.get("RETRAINING_POLL_INTERVAL", "30"))
_RECONCILE_INTERVAL: float = float(os.environ.get("RETRAINING_RECONCILE_INTERVAL", "60"))


# ── Poll loop ─────────────────────────────────────────────────────────────────


async def _poll_loop() -> None:
    """
    Async loop: poll DB for pending retraining triggers and execute them.

    Each trigger is processed in its own DB session so a failure in one task
    cannot affect others in the same iteration.
    """
    logger.info(
        "retraining_worker: poll loop started (interval=%.0fs)", _POLL_INTERVAL
    )
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
            try:
                trigger = task_db.get(RetrainingTrigger, trigger_id)
                if trigger is None or trigger.status != "pending":
                    # Already claimed or processed since we read the ID list
                    continue

                # Claim: transition to processing before executing
                trigger.status = "processing"
                task_db.commit()

                execute_retraining_task(trigger, task_db)

            except Exception:
                logger.exception(
                    "retraining_worker: task failed for trigger_id=%s", trigger_id
                )
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


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
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
