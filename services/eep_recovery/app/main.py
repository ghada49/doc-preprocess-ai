"""
services/eep_recovery/app/main.py
----------------------------------
EEP Recovery — stuck-task reconciliation service.

Packet 4.7: reconciliation loop wired to live Redis and DB via FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.eep.app.db.session import SessionLocal
from services.eep.app.redis_client import get_redis
from services.eep_recovery.app.reconciler import (  # noqa: F401 — re-exported for callers  # noqa: F401 — re-exported for callers
    ReconcilerConfig,
    ReconciliationResult,
    reconcile_once,
    run_reconciliation_loop,
)
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="eep_recovery")

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("eep_recovery: invalid %s=%r; using %.1f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("eep_recovery: invalid %s=%r; using %d", name, raw, default)
        return default


def _build_reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        task_timeout_seconds=_env_float("RECOVERY_TASK_TIMEOUT_SECONDS", 900.0),
        layout_task_timeout_seconds=_env_float("RECOVERY_LAYOUT_TASK_TIMEOUT_SECONDS", 180.0),
        check_interval_seconds=_env_float("RECOVERY_CHECK_INTERVAL_SECONDS", 30.0),
        max_task_retries=_env_int("MAX_TASK_RETRIES", ReconcilerConfig().max_task_retries),
        dead_letter_warning_threshold=_env_int(
            "RECOVERY_DEAD_LETTER_WARNING_THRESHOLD",
            ReconcilerConfig().dead_letter_warning_threshold,
        ),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Start the DB-authoritative queue reconciliation loop and shut it down
    cleanly on service exit.

    Redis client and DB session factory are created once at startup.
    The reconciliation loop runs every ReconcilerConfig.check_interval_seconds
    (default 30 s) and uses a fresh DB session per cycle.
    """
    r = get_redis()
    config = _build_reconciler_config()
    bg = asyncio.create_task(run_reconciliation_loop(r, SessionLocal, config))
    logger.info(
        "eep_recovery: reconciliation loop started "
        "(interval=%.0fs, task_timeout=%.0fs, layout_timeout=%.0fs)",
        config.check_interval_seconds,
        config.task_timeout_seconds,
        config.layout_task_timeout_seconds,
    )
    try:
        yield
    finally:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        logger.info("eep_recovery: reconciliation loop stopped")


app = FastAPI(
    title="EEP Recovery",
    version="0.1.0",
    description=(
        "Periodic reconciliation service that compares DB page states against "
        "Redis queue contents to detect and re-enqueue abandoned, stuck, or "
        "orphaned tasks. Complements the EEP worker watchdog."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="eep_recovery")
