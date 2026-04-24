"""
services/iep0/app/main.py
--------------------------
IEP0 — Material-type classification service.

Endpoints:
  POST /v1/classify → ClassifyResponse on success
                    → PreprocessError (HTTP 422 or 503) on failure
  GET  /health      → {"status": "ok"}   (always 200)
  GET  /ready       → {"status": "ready"} | {"status": "not_ready"}
  GET  /metrics     → Prometheus text
"""

from fastapi import FastAPI

from services.iep0.app.classify import router as classify_router
from services.iep0.app.inference import is_model_ready
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep0")

app = FastAPI(
    title="IEP0 — Material-Type Classification",
    version="0.1.0",
    description=(
        "Material-type classification service. Classifies input images as "
        "book, newspaper, microfilm, or archival_document before geometry "
        "inference. Drives model selection for downstream IEP1A/IEP1B."
    ),
)

configure_observability(
    app,
    service_name="iep0",
    health_checks=[is_model_ready],
)


@app.on_event("startup")
async def _preload_model() -> None:
    """Eagerly load the YOLO-cls model so the first request doesn't timeout."""
    import logging as _log
    _log.getLogger(__name__).info("iep0: pre-loading model at startup...")
    is_model_ready()  # triggers _try_load_model()
    _log.getLogger(__name__).info("iep0: model pre-load complete")


app.include_router(classify_router)
