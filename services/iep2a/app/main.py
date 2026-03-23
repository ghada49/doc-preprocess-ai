"""
services/iep2a/app/main.py
--------------------------
IEP2A — Detectron2 layout detection service.

Endpoints:
  POST /v1/layout-detect → LayoutDetectResponse on success (Packet 6.1)
                         → HTTP 500 when IEP2A_MOCK_FAIL="true"
  GET  /health           → {"status": "ok"}    (always 200)
  GET  /ready            → {"status": "ready"} | {"status": "not_ready"}
                           (503 when IEP2A_MOCK_NOT_READY="true";
                            Phase 12 wires real CUDA + model-loaded check)
  GET  /metrics          → Prometheus text
"""

from fastapi import FastAPI

from services.iep2a.app.detect import is_model_ready
from services.iep2a.app.detect import router as detect_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2a")

app = FastAPI(
    title="IEP2A — Detectron2 Layout Detection",
    version="0.1.0",
    description=(
        "Layout detection service using Detectron2 Faster R-CNN "
        "(ResNet-50-FPN, PubLayNet weights). Primary layout detector. "
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
