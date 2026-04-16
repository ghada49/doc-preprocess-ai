"""
services/iep0/app/classify.py
-------------------------------
IEP0 classification routers:
  POST /v1/classify       — single image classification
  POST /v1/classify-batch — batch classification with majority voting
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.iep0.app.inference import InferenceError, classify_single, classify_batch
from shared.schemas.iep0 import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassifyRequest,
    ClassifyResponse,
)
from shared.schemas.preprocessing import PreprocessError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["classify"])

_ACTION_TO_STATUS: dict[str, int] = {
    "ESCALATE_REVIEW": 422,
    "RETRY": 503,
}


@router.post(
    "/v1/classify",
    response_model=ClassifyResponse,
    responses={
        422: {"model": PreprocessError, "description": "Content or quality failure"},
        503: {"model": PreprocessError, "description": "Transient service failure"},
    },
    summary="Classify a single image's material type",
)
def classify(body: ClassifyRequest) -> ClassifyResponse | JSONResponse:
    try:
        return classify_single(body)
    except InferenceError as exc:
        err = exc.preprocess_error
        status = _ACTION_TO_STATUS.get(err.fallback_action, 500)
        logger.warning(
            "IEP0 classification failed job=%s page=%d code=%s action=%s",
            body.job_id,
            body.page_number,
            err.error_code,
            err.fallback_action,
        )
        return JSONResponse(status_code=status, content=err.model_dump())


@router.post(
    "/v1/classify-batch",
    response_model=BatchClassifyResponse,
    responses={
        422: {"model": PreprocessError, "description": "All images failed classification"},
        503: {"model": PreprocessError, "description": "Transient service failure"},
    },
    summary="Classify multiple images and return majority-voted material type",
)
def classify_batch_endpoint(body: BatchClassifyRequest) -> BatchClassifyResponse | JSONResponse:
    try:
        return classify_batch(body)
    except InferenceError as exc:
        err = exc.preprocess_error
        status = _ACTION_TO_STATUS.get(err.fallback_action, 500)
        logger.warning(
            "IEP0 batch classification failed job=%s code=%s action=%s",
            body.job_id,
            err.error_code,
            err.fallback_action,
        )
        return JSONResponse(status_code=status, content=err.model_dump())
