from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException

from services.iep1d.app.model import get_model_status, get_rectifier
from shared.io.storage import get_backend
from shared.metrics import (
    IEP1D_GPU_INFERENCE_SECONDS,
    IEP1D_RECTIFICATION_CONFIDENCE,
    IEP1D_RECTIFICATION_TRIGGERED,
)
from shared.normalization.quality import QualityMetricsResult, compute_quality_metrics
from shared.schemas.iep1d import RectifyRequest, RectifyResponse

router = APIRouter()
logger = logging.getLogger(__name__)
_QUALITY_METRICS_MAX_DIMENSION = 2048


def _encode_tiff(image: np.ndarray) -> bytes:
    success, buffer = cv2.imencode(".tiff", image)
    if not success:
        raise ValueError("cv2.imencode failed: could not encode rectified artifact to TIFF")
    return bytes(buffer.tobytes())


def _quality_metrics_image(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    max_dimension = max(height, width)
    if max_dimension <= _QUALITY_METRICS_MAX_DIMENSION:
        return image

    scale = _QUALITY_METRICS_MAX_DIMENSION / float(max_dimension)
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    return cv2.resize(
        image,
        (resized_width, resized_height),
        interpolation=cv2.INTER_AREA,
    )


def _file_uri(path: Path) -> str:
    return f"file://{path.as_posix()}"


def _rectified_output_uri(image_uri: str, job_id: str) -> str:
    parsed = urlparse(image_uri)

    if parsed.scheme == "s3":
        input_name = Path(parsed.path).stem or f"page_{job_id}"
        return f"s3://{parsed.netloc}/jobs/{job_id}/rectified/{input_name}.tiff"

    if parsed.scheme == "file":
        raw_path = image_uri[len("file://") :]
        input_path = Path(raw_path)
        input_name = input_path.stem or f"page_{job_id}"

        if input_path.parent.name == "output":
            rectified_path = input_path.parent.parent / "rectified" / f"{input_name}.tiff"
        else:
            rectified_path = input_path.parent / "rectified" / f"{input_name}.tiff"
        return _file_uri(rectified_path)

    raise ValueError(
        f"Unsupported artifact scheme for input URI {image_uri!r}. "
        "IEP1D only supports file:// and s3:// artifacts."
    )


def _normalized_improvement(before: float, after: float, *, higher_is_better: bool) -> float:
    delta = (after - before) if higher_is_better else (before - after)
    scale = max(abs(before), abs(after), 1.0)
    return max(-1.0, min(1.0, delta / scale))


def _rectification_confidence(
    before: QualityMetricsResult,
    after: QualityMetricsResult,
) -> float:
    score = 0.5
    score += 0.35 * _normalized_improvement(
        before.border_score,
        after.border_score,
        higher_is_better=True,
    )
    score += 0.35 * _normalized_improvement(
        before.skew_residual,
        after.skew_residual,
        higher_is_better=False,
    )
    score += 0.15 * _normalized_improvement(
        before.blur_score,
        after.blur_score,
        higher_is_better=True,
    )
    score += 0.15 * _normalized_improvement(
        before.foreground_coverage,
        after.foreground_coverage,
        higher_is_better=True,
    )
    return max(0.0, min(1.0, score))


def _warnings(before: QualityMetricsResult, after: QualityMetricsResult) -> list[str]:
    warnings: list[str] = []
    if after.border_score <= before.border_score:
        warnings.append("border_score_not_improved")
    if after.skew_residual >= before.skew_residual:
        warnings.append("skew_residual_not_improved")
    if after.blur_score + 0.05 < before.blur_score:
        warnings.append("blur_score_regressed")
    return warnings


@router.get("/v1/model-status")
async def model_status() -> dict[str, str | bool | None]:
    return get_model_status()


@router.post("/v1/rectify", response_model=RectifyResponse)
async def rectify(request: RectifyRequest) -> RectifyResponse:
    started_at = time.monotonic()
    IEP1D_RECTIFICATION_TRIGGERED.inc()

    try:
        rectifier = get_rectifier()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "model_not_ready",
                "error_message": str(exc),
            },
        ) from exc

    try:
        input_storage = get_backend(request.image_uri)
        raw_bytes = input_storage.get_bytes(request.image_uri)
        source_image = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if source_image is None:
            raise ValueError(f"cv2.imdecode returned None for {request.image_uri!r}")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "image_load_failed",
                "error_message": str(exc),
            },
        ) from exc

    before_metrics = compute_quality_metrics(_quality_metrics_image(source_image))
    inference_started = time.monotonic()
    try:
        rectified_image = rectifier.rectify(source_image)
    except Exception as exc:
        logger.exception(
            "IEP1D UVDoc inference failed",
            extra={
                "job_id": request.job_id,
                "page_number": request.page_number,
                "image_uri": request.image_uri,
                "material_type": request.material_type,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "rectification_failed",
                "error_message": str(exc),
            },
        ) from exc
    IEP1D_GPU_INFERENCE_SECONDS.observe(time.monotonic() - inference_started)

    output_uri = _rectified_output_uri(request.image_uri, request.job_id)
    try:
        output_storage = get_backend(output_uri)
        output_storage.put_bytes(output_uri, _encode_tiff(rectified_image))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "artifact_write_failed",
                "error_message": str(exc),
            },
        ) from exc

    after_metrics = compute_quality_metrics(_quality_metrics_image(rectified_image))
    confidence = _rectification_confidence(before_metrics, after_metrics)
    IEP1D_RECTIFICATION_CONFIDENCE.observe(confidence)

    return RectifyResponse(
        rectified_image_uri=output_uri,
        rectification_confidence=confidence,
        skew_residual_before=before_metrics.skew_residual,
        skew_residual_after=after_metrics.skew_residual,
        border_score_before=before_metrics.border_score,
        border_score_after=after_metrics.border_score,
        processing_time_ms=(time.monotonic() - started_at) * 1000.0,
        warnings=_warnings(before_metrics, after_metrics),
    )
