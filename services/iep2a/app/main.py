"""
services/iep2a/app/main.py
--------------------------
IEP2A - Layout detection service (Detectron2 or PaddleOCR backend).

Endpoints:
  POST /v1/layout-detect -> LayoutDetectResponse on success (Packet 6.1)
                         -> HTTP 500 when IEP2A_MOCK_FAIL="true"
  GET  /health           -> {"status": "ok"}    (always 200)
  GET  /ready            -> {"status": "ready"} | {"status": "not_ready"}
                           (503 when IEP2A_MOCK_NOT_READY="true";
                            reflects real backend load status in real mode)
  GET  /metrics          -> Prometheus text

Backend selection (real mode only; IEP2A_USE_REAL_MODEL=true):
  IEP2A_LAYOUT_BACKEND = "detectron2" (default) | "paddleocr"
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.iep2a.app.backends.factory import initialize_backend
from services.iep2a.app.detect import is_model_ready
from services.iep2a.app.detect import router as detect_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2a")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        initialize_backend()
    except Exception:
        logger.exception("Unexpected IEP2A startup error during backend initialization")
    yield


app = FastAPI(
    title="IEP2A - Layout Detection",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Layout detection service. Backend selected via IEP2A_LAYOUT_BACKEND: "
        "detectron2 (default, Faster R-CNN ResNet-50-FPN PubLayNet) or "
        "paddleocr (PP-DocLayoutV2 layout analysis). "
        "Canonical 5-class schema: text_block, title, table, image, caption. "
        "Column structure inferred via DBSCAN on text_block x-centroids (Packet 6.2)."
    ),
)

configure_observability(
    app,
    service_name="iep2a",
    health_checks=[is_model_ready],
)

app.include_router(detect_router)
