"""
services/iep1b/app/geometry.py
-------------------------------
IEP1B POST /v1/geometry router.

Separated from main.py so tests can import this router into a minimal
FastAPI app without pulling in the prometheus_client dependency that
configure_observability requires.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.iep1b.app.inference import InferenceError, run_mock_inference
from shared.schemas.geometry import GeometryRequest, GeometryResponse
from shared.schemas.preprocessing import PreprocessError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["geometry"])

# Map fallback_action → HTTP status code.
# ESCALATE_REVIEW: content/quality failure; EEP routes page to pending_human_correction
# RETRY:           transient infra failure; EEP may retry within configured budget
_ACTION_TO_STATUS: dict[str, int] = {
    "ESCALATE_REVIEW": 422,
    "RETRY": 503,
}


@router.post(
    "/v1/geometry",
    response_model=GeometryResponse,
    responses={
        422: {
            "model": PreprocessError,
            "description": "Content or quality failure (ESCALATE_REVIEW)",
        },
        503: {
            "model": PreprocessError,
            "description": "Transient service failure (RETRY)",
        },
    },
    summary="Run IEP1B geometry inference on a proxy image",
)
def geometry(body: GeometryRequest) -> GeometryResponse | JSONResponse:
    """
    Run IEP1B (YOLOv8-pose) geometry inference on the provided proxy image.

    Returns GeometryResponse (200) on success.
    Returns PreprocessError body with HTTP 422 (ESCALATE_REVIEW) or
    HTTP 503 (RETRY) on failure.
    """
    try:
        return run_mock_inference(body)
    except InferenceError as exc:
        err = exc.preprocess_error
        status = _ACTION_TO_STATUS.get(err.fallback_action, 500)
        logger.warning(
            "IEP1B inference error: %s (fallback_action=%s)",
            err.error_code,
            err.fallback_action,
        )
        return JSONResponse(status_code=status, content=err.model_dump())
