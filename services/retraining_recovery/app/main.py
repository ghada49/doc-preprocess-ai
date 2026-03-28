"""
services/retraining_recovery/app/main.py
------------------------------------------
Retraining Recovery — retraining task reconciliation service.

Health/ready/metrics endpoints: live (Phase 0 skeleton preserved).

Real implementation (Packet 8.5):
  Reconciliation loop — calls reconcile_once() every
  RETRAINING_RECONCILE_INTERVAL seconds to detect and recover:
    - retraining_jobs stuck in 'running' beyond the timeout window
    - retraining_triggers stuck in 'processing' whose linked job failed
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from services.eep.app.db.session import SessionLocal
from services.retraining_recovery.app.reconcile import (
    ReconcileConfig,
    run_reconciliation_loop,
)
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="retraining_recovery")
logger = logging.getLogger(__name__)

_RECONCILE_INTERVAL: float = float(
    os.environ.get("RETRAINING_RECONCILE_INTERVAL", "60")
)


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bg = asyncio.create_task(
        run_reconciliation_loop(
            session_factory=SessionLocal,
            config=ReconcileConfig(),
            interval_seconds=_RECONCILE_INTERVAL,
        )
    )
    logger.info("retraining_recovery: reconciliation loop started")
    try:
        yield
    finally:
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        logger.info("retraining_recovery: reconciliation loop stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Retraining Recovery",
    version="0.1.0",
    description=(
        "Periodic reconciliation service for retraining trigger tasks. "
        "Detects stuck or abandoned retraining jobs and triggers, and marks "
        "them failed so they are visible and actionable."
    ),
    lifespan=_lifespan,
)

configure_observability(app, service_name="retraining_recovery")
