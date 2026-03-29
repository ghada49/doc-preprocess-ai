"""
services/iep2b/app/main.py
--------------------------
IEP2B - DocLayout-YOLO layout detection service.

Endpoints:
  POST /v1/layout-detect -> LayoutDetectResponse on success (Packet 6.3)
                         -> HTTP 500 when IEP2B_MOCK_FAIL="true"
  GET  /health           -> {"status": "ok"}    (always 200)
  GET  /ready            -> {"status": "ready"} | {"status": "not_ready"}
                           (503 when IEP2B_MOCK_NOT_READY="true";
                            real mode requires a successful local model load)
  GET  /metrics          -> Prometheus text
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.iep2b.app.detect import is_model_ready
from services.iep2b.app.detect import router as detect_router
from services.iep2b.app.model import initialize_model_if_configured
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2b")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        initialize_model_if_configured()
    except Exception:
        logger.exception("Unexpected IEP2B startup error during DocLayout-YOLO warmup")
    yield


app = FastAPI(
    title="IEP2B - DocLayout-YOLO Layout Detection",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Layout detection service using DocLayout-YOLO "
        "(DocStructBench-aligned class vocabulary). Fast second-opinion detector. "
        "Maps native output classes to the canonical LibraryAI 5-class schema "
        "before returning LayoutDetectResponse (Packet 6.4)."
    ),
)

configure_observability(
    app,
    service_name="iep2b",
    health_checks=[is_model_ready],
)

app.include_router(detect_router)
