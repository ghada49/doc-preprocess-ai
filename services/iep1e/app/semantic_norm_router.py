"""
services/iep1e/app/semantic_norm_router.py
-------------------------------------------
IEP1E — POST /v1/semantic-norm endpoint.

Request:  SemanticNormRequest
Response: SemanticNormResponse

Processing:
  1. Load each page image from storage.
  2. For each page: score four rotations via PaddleOCR.
  3. Select best orientation per page; rotate and store if rotation != 0.
  4. Determine reading direction from combined script evidence.
  5. Assign reading order from direction + physical x_centers.
  6. Return SemanticNormResponse.

Mock mode (IEP1E_MOCK_MODE=true):
  All pages are returned unrotated, reading_direction="unresolved",
  fallback_used=True.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException

from services.iep1e.app.model import get_ocr_engine, is_model_ready
from shared.io.storage import get_backend
from shared.metrics import (
    IEP1E_FALLBACK_TOTAL,
    IEP1E_ORIENTATION_DECISIONS,
    IEP1E_PROCESSING_SECONDS,
    IEP1E_READING_DIRECTION,
)
from shared.schemas.semantic_norm import (
    PageOrientationResult,
    ScriptEvidence,
    SemanticNormPageResult,
    SemanticNormRequest,
    SemanticNormResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_ROTATIONS = (0, 90, 180, 270)
_CV2_ROTATION = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


# ── Storage helpers ───────────────────────────────────────────────────────────


def _load_image(uri: str) -> np.ndarray:
    """Load a storage artifact as a BGR ndarray.  Raises ValueError on failure."""
    storage = get_backend(uri)
    raw = storage.get_bytes(uri)
    arr = np.frombuffer(raw, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"iep1e: cv2.imdecode returned None for {uri!r}")
    return image


def _encode_tiff(image: np.ndarray) -> bytes:
    success, buf = cv2.imencode(".tiff", image)
    if not success:
        raise ValueError("iep1e: cv2.imencode failed")
    return bytes(buf.tobytes())


def _oriented_uri(original_uri: str, job_id: str, sub_page_index: int) -> str:
    """
    Derive an output URI for the oriented artifact in the 'oriented' section.

    Pattern mirrors the 'rectified' section used by IEP1D.
    """
    parsed = urlparse(original_uri)
    stem = Path(parsed.path).stem or f"page_{job_id}_{sub_page_index}"

    if parsed.scheme == "s3":
        return f"s3://{parsed.netloc}/jobs/{job_id}/oriented/{stem}.tiff"

    if parsed.scheme == "file":
        raw_path = original_uri[len("file://"):]
        input_path = Path(raw_path)
        parent = input_path.parent
        # If under a named section dir (output, output_rectified, …), sibling it
        oriented_path = parent.parent / "oriented" / f"{stem}.tiff"
        return f"file://{oriented_path.as_posix()}"

    raise ValueError(
        f"iep1e: unsupported URI scheme {parsed.scheme!r}; "
        "only file:// and s3:// are supported"
    )


def _store_oriented(image: np.ndarray, uri: str) -> None:
    storage = get_backend(uri)
    storage.put_bytes(uri, _encode_tiff(image))


# ── Mock fallback ─────────────────────────────────────────────────────────────


def _zero_evidence() -> ScriptEvidence:
    return ScriptEvidence(
        arabic_ratio=0.0, latin_ratio=0.0, garbage_ratio=0.0,
        n_boxes=0, n_chars=0, mean_conf=0.0,
    )


def _mock_orientation() -> PageOrientationResult:
    return PageOrientationResult(
        best_rotation_deg=0,
        orientation_confident=False,
        score_ratio=0.0,
        score_diff=0.0,
        script_evidence=_zero_evidence(),
    )


def _mock_response(request: SemanticNormRequest) -> SemanticNormResponse:
    pages = [
        SemanticNormPageResult(
            original_uri=uri,
            oriented_uri=uri,
            sub_page_index=request.sub_page_indices[i],
            orientation=_mock_orientation(),
        )
        for i, uri in enumerate(request.page_uris)
    ]
    return SemanticNormResponse(
        pages=pages,
        reading_direction="unresolved",
        ordered_page_uris=list(request.page_uris),
        fallback_used=True,
        processing_time_ms=0.0,
        warnings=["mock_mode_active"],
    )


# ── Real processing ───────────────────────────────────────────────────────────


def _process_page(
    uri: str,
    sub_page_index: int,
    job_id: str,
    ocr: Any,
) -> SemanticNormPageResult:
    """
    Score all four rotations for one page, rotate+store if needed.

    Returns SemanticNormPageResult.  Never raises — falls back to rotation=0
    if any storage or OCR step fails.
    """
    from shared.semantic_norm.ocr_scorer import (
        score_rotation,
        select_orientation,
    )

    try:
        image = _load_image(uri)
    except Exception as exc:
        logger.warning("iep1e: failed to load image %r: %s — using fallback", uri, exc)
        return SemanticNormPageResult(
            original_uri=uri,
            oriented_uri=uri,
            sub_page_index=sub_page_index,
            orientation=_mock_orientation(),
        )

    try:
        scores = score_rotation(image, ocr)
        orientation = select_orientation(scores)
    except Exception as exc:
        logger.warning("iep1e: OCR scoring failed for %r: %s — using fallback", uri, exc)
        return SemanticNormPageResult(
            original_uri=uri,
            oriented_uri=uri,
            sub_page_index=sub_page_index,
            orientation=_mock_orientation(),
        )

    IEP1E_ORIENTATION_DECISIONS.labels(
        confident=str(orientation.orientation_confident).lower()
    ).inc()

    if orientation.best_rotation_deg == 0:
        # No rotation needed — return original URI
        return SemanticNormPageResult(
            original_uri=uri,
            oriented_uri=uri,
            sub_page_index=sub_page_index,
            orientation=orientation,
        )

    # Rotate image, store, return new URI
    try:
        rotated = cv2.rotate(image, _CV2_ROTATION[orientation.best_rotation_deg])
        out_uri = _oriented_uri(uri, job_id, sub_page_index)
        _store_oriented(rotated, out_uri)
        logger.info(
            "iep1e: page sub=%d rotated %d° → %s",
            sub_page_index,
            orientation.best_rotation_deg,
            out_uri,
        )
    except Exception as exc:
        logger.warning(
            "iep1e: failed to store rotated artifact for %r: %s — using original",
            uri,
            exc,
        )
        out_uri = uri

    return SemanticNormPageResult(
        original_uri=uri,
        oriented_uri=out_uri,
        sub_page_index=sub_page_index,
        orientation=orientation,
    )


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/v1/semantic-norm", response_model=SemanticNormResponse)
async def semantic_norm(request: SemanticNormRequest) -> SemanticNormResponse:
    """
    Resolve orientation and reading order for 1 or 2 page crops.

    - Blank pages (no OCR text) are handled gracefully; orientation defaults
      to 0° with orientation_confident=False.
    - If both pages produce no usable OCR, reading_direction="unresolved"
      and physical left-to-right order is preserved (fallback_used=True).
    """
    started_at = time.monotonic()

    if len(request.page_uris) == 0 or len(request.page_uris) != len(request.x_centers):
        raise HTTPException(
            status_code=422,
            detail="page_uris and x_centers must be non-empty lists of equal length",
        )

    ocr = get_ocr_engine()
    if ocr is None:
        # mock mode
        return _mock_response(request)

    # ── Per-page orientation scoring ─────────────────────────────────────────
    page_results: list[SemanticNormPageResult] = []
    for i, uri in enumerate(request.page_uris):
        sub_idx = request.sub_page_indices[i] if i < len(request.sub_page_indices) else i
        result = _process_page(uri, sub_idx, request.job_id, ocr)
        page_results.append(result)

    # ── Reading direction ────────────────────────────────────────────────────
    from shared.semantic_norm.ocr_scorer import (
        assign_reading_order,
        determine_reading_direction,
    )

    evidences = [r.orientation.script_evidence for r in page_results]
    reading_direction = determine_reading_direction(evidences)

    oriented_uris = [r.oriented_uri for r in page_results]
    ordered_uris = assign_reading_order(oriented_uris, request.x_centers, reading_direction)

    all_blank = all(r.orientation.script_evidence.n_boxes == 0 for r in page_results)
    fallback_used = all_blank

    processing_time_ms = (time.monotonic() - started_at) * 1000.0

    IEP1E_PROCESSING_SECONDS.observe(processing_time_ms / 1000.0)
    IEP1E_READING_DIRECTION.labels(direction=reading_direction).inc()
    if fallback_used:
        IEP1E_FALLBACK_TOTAL.inc()

    logger.info(
        "iep1e: semantic-norm complete job=%s page=%d pages=%d "
        "direction=%s fallback=%s ms=%.1f",
        request.job_id,
        request.page_number,
        len(page_results),
        reading_direction,
        fallback_used,
        processing_time_ms,
    )

    return SemanticNormResponse(
        pages=page_results,
        reading_direction=reading_direction,  # type: ignore[arg-type]
        ordered_page_uris=ordered_uris,
        fallback_used=fallback_used,
        processing_time_ms=processing_time_ms,
        warnings=[],
    )
