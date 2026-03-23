"""
services/iep2b/app/main.py
--------------------------
IEP2B — DocLayout-YOLO layout detection service.

Endpoints:
  POST /v1/layout-detect → LayoutDetectResponse on success (Packet 6.3)
                         → HTTP 500 when IEP2B_MOCK_FAIL="true"
  GET  /health           → {"status": "ok"}    (always 200)
  GET  /ready            → {"status": "ready"} | {"status": "not_ready"}
                           (503 when IEP2B_MOCK_NOT_READY="true";
                            Phase 12 wires real CUDA + model-loaded check)
  GET  /metrics          → Prometheus text
"""

from fastapi import FastAPI

from services.iep2b.app.detect import is_model_ready
from services.iep2b.app.detect import router as detect_router
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep2b")

app = FastAPI(
    title="IEP2B — DocLayout-YOLO Layout Detection",
    version="0.1.0",
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
