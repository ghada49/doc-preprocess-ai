"""
services/iep1e/app/main.py
---------------------------
IEP1E — Semantic normalization service.

Resolves page orientation (0 / 90 / 180 / 270 °) and spread reading order
using PaddleOCR as a decision signal, not a text reader.

Endpoints:
  POST /v1/semantic-norm  → SemanticNormResponse
  GET  /health            → {"status": "ok"}
  GET  /ready             → {"status": "ready"} | {"status": "not_ready"} (503)
  GET  /metrics           → Prometheus text
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.iep1e.app.model import initialize_model, is_model_ready, readiness_failure_extras
from services.iep1e.app.semantic_norm_router import router as semantic_norm_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1e")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # PaddleOCR init blocks the event loop and delays uvicorn from opening the
    # port — ECS health checks see connection refused and kill the container.
    # Running init in a daemon thread lets uvicorn reach yield immediately so
    # /health responds while the model loads in the background.
    # eep-worker polls /ready (which stays 503 until _loaded=True) before
    # sending inference, so it waits for actual readiness independently.
    thread = threading.Thread(target=initialize_model, daemon=True, name="iep1e-model-init")
    thread.start()
    logger.info("iep1e: model initialisation started in background thread")
    yield


app = FastAPI(
    title="IEP1E — Semantic Normalization",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Resolves page orientation and spread reading order using PaddleOCR "
        "as a decision signal. Runs after IEP1C and before IEP2A/2B."
    ),
)

configure_observability(
    app,
    service_name="iep1e",
    health_checks=[is_model_ready],
    readiness_failure_extras=readiness_failure_extras,
)

app.include_router(semantic_norm_router)
