"""
services/retraining_worker/app/main.py
----------------------------------------
Retraining Worker — model retraining trigger background worker.

Health/ready/metrics endpoints: live (Phase 0 skeleton preserved).

Real implementation (Packet 8.5):
  Poll loop — queries retraining_triggers for pending rows every
  RETRAINING_POLL_INTERVAL seconds, claims each by transitioning
  status → 'processing', then calls execute_retraining_task.

  On task exception: rolls back the session, re-fetches the trigger,
  and marks it failed so it can be picked up by the recovery reconciler.
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
from services.retraining_worker.app.task import execute_retraining_task
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="retraining_worker")
logger = logging.getLogger(__name__)

_POLL_INTERVAL: float = float(os.environ.get("RETRAINING_POLL_INTERVAL", "30"))


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
    bg = asyncio.create_task(_poll_loop())
    logger.info("retraining_worker: background poll task started")
    try:
        yield
    finally:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        logger.info("retraining_worker: background poll task stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Retraining Worker",
    version="0.1.0",
    description=(
        "Background worker that polls retraining_triggers for pending events, "
        "executes stub training and offline evaluation, and writes gate_results "
        "to model_versions so promotion gates can be re-checked."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="retraining_worker")
