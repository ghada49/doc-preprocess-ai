"""
services/iep1b/app/main.py
--------------------------
IEP1B — YOLOv8-pose page geometry service.

Endpoints:
  POST /v1/geometry   → GeometryResponse on success
                      → PreprocessError (HTTP 422 or 503) on failure
  GET  /health        → {"status": "ok"}   (always 200)
  GET  /ready         → {"status": "ready"} | {"status": "not_ready"}
                        (503 when IEP1B_MOCK_NOT_READY="true";
                         Phase 12 wires real CUDA + model-loaded check)
  GET  /metrics       → Prometheus text
"""

from fastapi import FastAPI

from services.iep1b.app.geometry import router as geometry_router
from services.iep1b.app.inference import is_model_ready
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1b")

app = FastAPI(
    title="IEP1B — YOLOv8-pose Geometry",
    version="0.1.0",
    description=(
        "Page geometry service using YOLOv8-pose keypoint regression. "
        "Predicts page corners directly as coordinate keypoints; provides "
        "geometry from a fundamentally different representation than IEP1A. "
        "Mock inference — real model loaded in Phase 12."
    ),
)

configure_observability(
    app,
    service_name="iep1b",
    health_checks=[is_model_ready],
)

app.include_router(geometry_router)
