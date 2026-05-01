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
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI

from monitoring.drift_observer import export_baselines_for_process
from services.eep.app.redis_client import get_redis
from services.eep_worker.app.google_config import get_google_worker_state, initialize_google
from services.eep_worker.app.watchdog import TaskWatchdog, WatchdogConfig
from services.eep_worker.app.worker_loop import WorkerConfig, build_worker_config, run_worker_loop
from shared.logging_config import setup_logging
from shared.metrics import SERVICE_TARGET_UP
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


def _health_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _configured_targets(worker_config: WorkerConfig) -> dict[str, str]:
    return {
        "iep0": _health_url(worker_config.iep0_endpoint),
        "iep1a": _health_url(worker_config.iep1a_endpoint),
        "iep1b": _health_url(worker_config.iep1b_endpoint),
        "iep1d": _health_url(worker_config.iep1d_endpoint),
        "iep1e": _health_url(worker_config.iep1e_endpoint),
        "iep2a": _health_url(worker_config.iep2a_endpoint),
        "iep2b": _health_url(worker_config.iep2b_endpoint),
    }


async def _target_health_loop(worker_config: WorkerConfig) -> None:
    targets = _configured_targets(worker_config)
    interval_seconds = max(5.0, _env_float("TARGET_HEALTH_CHECK_INTERVAL_SECONDS", 15.0))
    timeout_seconds = max(1.0, _env_float("TARGET_HEALTH_CHECK_TIMEOUT_SECONDS", 5.0))
    logger.info("eep_worker: target health loop started targets=%s", sorted(targets))

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        while True:
            for service, url in targets.items():
                try:
                    response = await client.get(url)
                    SERVICE_TARGET_UP.labels(service=service).set(1 if response.is_success else 0)
                except httpx.RequestError:
                    SERVICE_TARGET_UP.labels(service=service).set(0)
            await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start the worker runtime loop and Google config."""
    initialize_google()
    export_baselines_for_process()
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
    target_health_bg = asyncio.create_task(_target_health_loop(worker_config))
    logger.info("eep_worker: worker runtime loop started as %s", worker_config.worker_id)
    try:
        yield
    finally:
        for task in (worker_bg, target_health_bg):
            task.cancel()
            try:
                await task
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
