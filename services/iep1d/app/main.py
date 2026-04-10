"""
services/iep1d/app/main.py
--------------------------
IEP1D â€” UVDoc rectification fallback service.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.iep1d.app.model import initialize_model_if_configured, is_model_ready
from services.iep1d.app.rectify import router as rectify_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1d")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        initialize_model_if_configured()
    except Exception:
        logger.exception("Unexpected IEP1D startup error during UVDoc warmup")
    yield


app = FastAPI(
    title="IEP1D â€” UVDoc Rectification",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Rectification fallback service using UVDoc. Rescues pages where "
        "deterministic normalization produces an artifact that fails validation "
        "(warped pages, strong curl, perspective-heavy captures). "
        "Does not decide split; does not replace IEP1A/IEP1B as geometry source."
    ),
)

configure_observability(app, service_name="iep1d", health_checks=[is_model_ready])

app.include_router(rectify_router)
