"""
services/eep_worker/app/main.py
--------------------------------
EEP Worker - page-processing background worker.

Packet 4.1-4.6: worker pipeline modules implemented.
Packet 4.7: in-process watchdog is owned by run_worker_loop().
P2.2: Google Document AI config loaded and validated during lifespan startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.eep.app.redis_client import get_redis
from services.eep_worker.app.google_config import get_google_worker_state, initialize_google
from services.eep_worker.app.watchdog import TaskWatchdog, WatchdogConfig
from services.eep_worker.app.worker_loop import build_worker_config, run_worker_loop
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="eep_worker")

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("eep_worker: invalid %s=%r; using %.1f", name, raw, default)
        return default


def _build_watchdog() -> TaskWatchdog:
    return TaskWatchdog(
        WatchdogConfig(
            task_timeout_seconds=_env_float("WORKER_TASK_TIMEOUT_SECONDS", 900.0),
            check_interval_seconds=_env_float("WORKER_TASK_CHECK_INTERVAL_SECONDS", 30.0),
        )
    )


# Module-level watchdog instance imported by the task runner.
watchdog: TaskWatchdog = _build_watchdog()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start the worker runtime loop and Google config."""
    initialize_google()
    logger.info(
        "eep_worker: Google Document AI availability: %s",
        "ENABLED" if get_google_worker_state().enabled else "DISABLED",
    )

    redis_client = get_redis()
    worker_config = build_worker_config(redis_client=redis_client)

    worker_bg = asyncio.create_task(
        run_worker_loop(
            redis_client,
            worker_config,
            watchdog=watchdog,
        )
    )
    logger.info("eep_worker: worker runtime loop started as %s", worker_config.worker_id)
    try:
        yield
    finally:
        worker_bg.cancel()
        try:
            await worker_bg
        except asyncio.CancelledError:
            pass
        await worker_config.backend.close()
        logger.info("eep_worker: worker runtime loop stopped")


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
