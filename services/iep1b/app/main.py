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

import logging
import os
import threading

from fastapi import FastAPI

from services.iep1b.app.geometry import router as geometry_router
from services.iep1b.app.inference import get_model_info, is_model_ready, reload_models
from shared.logging_config import setup_logging
from shared.middleware import configure_observability

setup_logging(service_name="iep1b")
logger = logging.getLogger(__name__)

_RELOAD_CHANNEL = "libraryai:model_reload:iep1b"


def _start_reload_subscriber() -> None:
    """Start a daemon thread that subscribes to Redis model_reload signals."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

    def _subscriber() -> None:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(redis_url)
            pubsub = client.pubsub()
            pubsub.subscribe(_RELOAD_CHANNEL)
            logger.info("iep1b: subscribed to %s", _RELOAD_CHANNEL)
            for message in pubsub.listen():
                if message.get("type") == "message":
                    version = (message.get("data") or b"").decode("utf-8", errors="replace")
                    logger.info("iep1b: model_reload signal received version=%r — clearing cache", version)
                    reload_models(version_tag=version or None)
        except Exception:
            logger.exception("iep1b: model_reload subscriber thread failed")

    t = threading.Thread(target=_subscriber, daemon=True, name="iep1b-reload-subscriber")
    t.start()

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


@app.on_event("startup")
async def _preload_models() -> None:
    """Eagerly load YOLO-pose models so the first request doesn't timeout."""
    import logging as _log
    from services.iep1b.app.inference import _load_model, _MODEL_FILES, _is_mock_mode
    if _is_mock_mode():
        return
    _log.getLogger(__name__).info("iep1b: pre-loading models at startup...")
    for material_type in _MODEL_FILES:
        try:
            _load_model(material_type)
        except Exception as exc:
            _log.getLogger(__name__).warning("iep1b: failed to pre-load %s: %s", material_type, exc)
    _log.getLogger(__name__).info("iep1b: model pre-load complete")


@app.on_event("startup")
async def _start_model_reload_subscriber() -> None:
    """Start the Redis model_reload subscriber in a background daemon thread."""
    _start_reload_subscriber()


@app.get(
    "/model-info",
    summary="Current model-loading state for this iep1b instance",
)
def model_info() -> dict:
    """
    Return the current model cache state: which weight files are loaded,
    how many hot-reloads have occurred since startup, and when the last
    reload happened.

    ``version_tag`` is always ``null`` — iep1b loads weights from local .pt
    files and has no runtime mapping to a ModelVersion DB record.
    TODO: persist the version_tag emitted by the Redis reload signal and
    return it here so operators can correlate this response with the
    promotion-audit log.
    """
    return get_model_info()


app.include_router(geometry_router)
