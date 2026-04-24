"""
services/iep1a/app/main.py
--------------------------
IEP1A — YOLOv8-seg page geometry service.

Endpoints:
  POST /v1/geometry   → GeometryResponse on success
                      → PreprocessError (HTTP 422 or 503) on failure
  GET  /health        → {"status": "ok"}   (always 200)
  GET  /ready         → {"status": "ready"} | {"status": "not_ready"}
                        (503 when IEP1A_MOCK_NOT_READY="true";
                         Phase 12 wires real CUDA + model-loaded check)
  GET  /metrics       → Prometheus text
"""

from fastapi import FastAPI

from services.iep1a.app.geometry import router as geometry_router
from services.iep1a.app.inference import is_model_ready
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1a")

app = FastAPI(
    title="IEP1A — YOLOv8-seg Geometry",
    version="0.1.0",
    description=(
        "Page geometry service using YOLOv8-seg instance segmentation. "
        "Predicts page regions as segmentation masks; geometry is derived from "
        "mask contours. Mock inference — real model loaded in Phase 12."
    ),
)

configure_observability(
    app,
    service_name="iep1a",
    health_checks=[is_model_ready],
)


@app.on_event("startup")
async def _preload_models() -> None:
    """Eagerly load YOLO-seg models so the first request doesn't timeout."""
    import logging as _log
    from services.iep1a.app.inference import _load_model, _MODEL_FILES, _is_mock_mode
    if _is_mock_mode():
        return
    _log.getLogger(__name__).info("iep1a: pre-loading models at startup...")
    for material_type in _MODEL_FILES:
        try:
            _load_model(material_type)
        except Exception as exc:
            _log.getLogger(__name__).warning("iep1a: failed to pre-load %s: %s", material_type, exc)
    _log.getLogger(__name__).info("iep1a: model pre-load complete")


app.include_router(geometry_router)
