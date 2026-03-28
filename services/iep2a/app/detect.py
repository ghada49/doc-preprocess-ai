"""
services/iep2a/app/detect.py
-----------------------------
IEP2A POST /v1/layout-detect router.

Supports two execution modes selected by the IEP2A_USE_REAL_MODEL env var:

  Stub mode  (IEP2A_USE_REAL_MODEL != "true", default):
    Deterministic mock regions — all Phase 6 tests run in this mode.
    Configurable via IEP2A_MOCK_FAIL / IEP2A_MOCK_CONFIDENCE /
    IEP2A_MOCK_NOT_READY.

  Real mode  (IEP2A_USE_REAL_MODEL=true):
    Runs the backend selected by IEP2A_LAYOUT_BACKEND (default: detectron2).
    Production serving loads only from local in-image artifacts and warms
    the model at startup so readiness reflects actual load success.
    Failure simulation flags (IEP2A_MOCK_*) are ignored in real mode.

Env vars — stub mode only:
    IEP2A_MOCK_FAIL         "true"  → HTTP 500 (failure simulation)
    IEP2A_MOCK_CONFIDENCE   float   → per-region confidence (default 0.87)
    IEP2A_MOCK_NOT_READY    "true"  → /ready returns not-ready

Env vars — real mode:
    IEP2A_USE_REAL_MODEL    "true"  → enable real inference
    IEP2A_LAYOUT_BACKEND    "detectron2" (default) | "paddleocr"

  Detectron2 backend env vars (see model.py for full list):
    IEP2A_WEIGHTS_PATH      local in-image checkpoint path
                             (default: /opt/models/iep2a/model_final.pth)
    IEP2A_LOCAL_WEIGHTS_PATH optional mounted local development override
    IEP2A_CONFIG_PATH       local Detectron2 config path override
    IEP2A_NUM_CLASSES       number of classes in the weights (default: 5)
    IEP2A_SCORE_THRESH      detection confidence threshold (default: 0.5)
    IEP2A_DEVICE            "cuda" or "cpu"
    IEP2A_MODEL_VERSION     optional validation/override input; baked
                            `<weights>.version` metadata is authoritative

  PaddleOCR backend env vars (see backends/paddleocr_backend.py):
    IEP2A_PADDLE_MODEL_DIR             local in-image PP-DocLayoutV2 model directory
    IEP2A_PADDLE_LOCAL_MODEL_DIR       optional mounted local development override
    IEP2A_PADDLE_MODEL_VERSION         optional validation/override input; baked
                                       `<model_dir>.version` metadata is authoritative
    IEP2A_PADDLE_ALLOW_ONLINE_DOWNLOAD development-only escape hatch; default false
    IEP2A_PADDLE_MODEL_SOURCE          optional online source selector (HF/BOS)
"""

from __future__ import annotations

import os
import time
from typing import Literal

from fastapi import APIRouter, HTTPException

from services.iep2a.app.postprocess import postprocess_regions
from shared.schemas.layout import (
    ColumnStructure,
    LayoutConfSummary,
    LayoutDetectRequest,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

router = APIRouter(tags=["layout"])

_DEFAULT_CONFIDENCE = 0.87


# ---------------------------------------------------------------------------
# Model-readiness check (exported for main.py health checks)
# ---------------------------------------------------------------------------


def is_model_ready() -> bool:
    """
    Return True when the model (stub or real) is ready to serve requests.

    Stub mode:  honour IEP2A_MOCK_NOT_READY env var.
    Real mode:  delegate to the active backend's is_ready(); returns False
                when no backend is initialized (startup still in progress or
                initialization failed).
    """
    if os.environ.get("IEP2A_USE_REAL_MODEL", "false").lower() != "true":
        return os.environ.get("IEP2A_MOCK_NOT_READY", "false").lower() != "true"
    # Lazy import — backends package can be imported without ML deps installed.
    from services.iep2a.app.backends.factory import get_active_backend_optional

    backend = get_active_backend_optional()
    return backend is not None and backend.is_ready()


# ---------------------------------------------------------------------------
# Deterministic mock region templates (stub mode only)
# ---------------------------------------------------------------------------
#
# Coordinate space: notional 1000×1000 px normalised page image.

_MOCK_REGION_TEMPLATES: list[tuple[str, RegionType, BoundingBox]] = [
    (
        "r1",
        RegionType.title,
        BoundingBox(x_min=50.0, y_min=30.0, x_max=950.0, y_max=120.0),
    ),
    (
        "r2",
        RegionType.text_block,
        BoundingBox(x_min=50.0, y_min=140.0, x_max=450.0, y_max=600.0),
    ),
    (
        "r3",
        RegionType.text_block,
        BoundingBox(x_min=510.0, y_min=140.0, x_max=950.0, y_max=600.0),
    ),
    (
        "r4",
        RegionType.image,
        BoundingBox(x_min=50.0, y_min=620.0, x_max=450.0, y_max=900.0),
    ),
    (
        "r5",
        RegionType.caption,
        BoundingBox(x_min=50.0, y_min=910.0, x_max=450.0, y_max=960.0),
    ),
    (
        "r6",
        RegionType.table,
        BoundingBox(x_min=510.0, y_min=620.0, x_max=950.0, y_max=960.0),
    ),
]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/v1/layout-detect",
    response_model=LayoutDetectResponse,
    summary="Run IEP2A layout detection on a page artifact",
)
def layout_detect(body: LayoutDetectRequest) -> LayoutDetectResponse:
    """
    Run IEP2A layout detection on the page image identified by body.image_uri.

    In stub mode (default): returns deterministic mock regions.
    In real mode (IEP2A_USE_REAL_MODEL=true): runs the backend selected by
    IEP2A_LAYOUT_BACKEND.

    Returns LayoutDetectResponse (200) with detector_type matching the selected
    backend.
    Returns HTTP 500 on detection failure or model load error.
    """
    t0 = time.monotonic()

    _use_real = os.environ.get("IEP2A_USE_REAL_MODEL", "false").lower() == "true"

    if not _use_real:
        return _stub_response(body, t0)
    else:
        return _real_response(body, t0)


# ---------------------------------------------------------------------------
# Stub response path
# ---------------------------------------------------------------------------


def _stub_response(body: LayoutDetectRequest, t0: float) -> LayoutDetectResponse:
    """Return the deterministic mock response (stub mode)."""
    if os.environ.get("IEP2A_MOCK_FAIL", "false").lower() == "true":
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "layout_detection_failed",
                "error_message": "IEP2A_MOCK_FAIL is set — simulated failure",
            },
        )

    try:
        confidence = float(os.environ.get("IEP2A_MOCK_CONFIDENCE", str(_DEFAULT_CONFIDENCE)))
        confidence = max(0.0, min(1.0, confidence))
    except ValueError:
        confidence = _DEFAULT_CONFIDENCE

    raw_regions = [
        Region(id=rid, type=rtype, bbox=bbox, confidence=confidence)
        for rid, rtype, bbox in _MOCK_REGION_TEMPLATES
    ]

    regions, col_struct = postprocess_regions(raw_regions)
    return _assemble_response(regions, col_struct, t0, model_version="mock-stub-6.1")


# ---------------------------------------------------------------------------
# Real inference path
# ---------------------------------------------------------------------------


def _real_response(body: LayoutDetectRequest, t0: float) -> LayoutDetectResponse:
    """Run real backend inference and return the response."""
    from services.iep2a.app.backends.base import ImageLoadError
    from services.iep2a.app.backends.factory import get_active_backend

    try:
        backend = get_active_backend()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "model_not_ready",
                "error_message": str(exc),
            },
        ) from exc

    try:
        result = backend.detect(body.image_uri)
    except ImageLoadError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "image_load_failed",
                "error_message": str(exc),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "model_not_ready",
                "error_message": str(exc),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "layout_detection_failed",
                "error_message": f"Layout detection failed: {exc}",
            },
        ) from exc

    return _assemble_response(
        result.regions,
        result.column_structure,
        t0,
        model_version=result.model_version,
        detector_type=result.detector_type,
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# Response assembly (shared between stub and real paths)
# ---------------------------------------------------------------------------


def _assemble_response(
    regions: list[Region],
    col_struct: ColumnStructure | None,
    t0: float,
    model_version: str,
    detector_type: Literal["detectron2", "doclayout_yolo", "paddleocr"] = "detectron2",
    warnings: list[str] | None = None,
) -> LayoutDetectResponse:
    n = len(regions)
    mean_conf = sum(r.confidence for r in regions) / n if n else 0.0
    low_conf_frac = sum(1 for r in regions if r.confidence < 0.5) / n if n else 0.0

    histogram: dict[str, int] = {rt.value: 0 for rt in RegionType}
    for r in regions:
        histogram[r.type.value] += 1

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=regions,
        layout_conf_summary=LayoutConfSummary(
            mean_conf=round(mean_conf, 6),
            low_conf_frac=round(low_conf_frac, 6),
        ),
        region_type_histogram=histogram,
        column_structure=col_struct,
        model_version=model_version,
        detector_type=detector_type,
        processing_time_ms=elapsed_ms,
        warnings=warnings if warnings is not None else [],
    )
