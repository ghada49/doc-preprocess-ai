"""
services/eep_worker/app/main.py
--------------------------------
EEP Worker — page-processing background worker.
Phase 0 skeleton: health/ready/metrics are live.

Packet 4.1–4.6: worker pipeline modules implemented.
Packet 4.7: in-process watchdog started via FastAPI lifespan.
P2.2: Google Document AI config loaded and validated during lifespan startup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.eep_worker.app.google_config import get_google_worker_state, initialize_google
from services.eep_worker.app.watchdog import StaleTaskReport, TaskWatchdog
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="eep_worker")

logger = logging.getLogger(__name__)

# Module-level watchdog instance — imported by the task runner (intake.py et al.)
# to call watchdog.register() and watchdog.deregister() around each task.
watchdog: TaskWatchdog = TaskWatchdog()

# Google Document AI state is owned by google_config, not by this module.
# To read Google availability elsewhere use:
#   from services.eep_worker.app.google_config import get_google_worker_state
# For adjudication functions, prefer receiving the client as an explicit
# parameter rather than calling get_google_worker_state() inside them.


def _on_stale(report: StaleTaskReport) -> None:
    """
    Default stale-task callback: log a warning for each stale task.

    The recovery service (eep_recovery) handles the actual re-queue action
    by scanning the processing list against DB state.  This callback is
    informational only — it does not mutate Redis or DB.
    """
    logger.warning(
        "watchdog: %d stale task(s) detected; recovery service will reconcile: %s",
        len(report.stale_task_ids),
        report.stale_task_ids,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start the in-process watchdog loop; initialize Google Document AI config."""
    # ── P2.2: Google Document AI startup validation ────────────────────────────
    # Loads config from env vars, checks credentials file, initialises client.
    # State is stored in google_config._state and read via get_google_worker_state().
    initialize_google()
    logger.info(
        "eep_worker: Google Document AI availability: %s",
        "ENABLED" if get_google_worker_state().enabled else "DISABLED",
    )

    # ── Watchdog background task ───────────────────────────────────────────────
    bg = asyncio.create_task(watchdog.run_watch_loop(on_stale=_on_stale))
    logger.info("eep_worker: watchdog background task started")
    try:
        yield
    finally:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        logger.info("eep_worker: watchdog background task stopped")


app = FastAPI(
    title="EEP Worker",
    version="0.1.0",
    description=(
        "Background worker process that dequeues page tasks from Redis, "
        "orchestrates IEP1A/IEP1B geometry, IEP1C normalization, artifact "
        "validation, IEP1D rectification rescue, and IEP2A/IEP2B layout "
        "detection. Owns all page state transitions from queued to terminal states."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="eep_worker")
