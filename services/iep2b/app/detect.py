"""
services/iep2b/app/detect.py
-----------------------------
IEP2B POST /v1/layout-detect router.

Supports two execution modes selected by the IEP2B_USE_REAL_MODEL env var:

  Stub mode  (IEP2B_USE_REAL_MODEL != "true", default):
    Deterministic mock regions — all Phase 6 tests run in this mode.
    Configurable via IEP2B_MOCK_FAIL / IEP2B_MOCK_CONFIDENCE /
    IEP2B_MOCK_NOT_READY.

  Real mode  (IEP2B_USE_REAL_MODEL=true):
    Runs DocLayout-YOLO inference via the model loaded by model.py.
    Failure simulation flags (IEP2B_MOCK_*) are ignored in real mode.

Env vars — stub mode only:
    IEP2B_MOCK_FAIL         "true"  → HTTP 500 (failure simulation)
    IEP2B_MOCK_CONFIDENCE   float   → per-region confidence (default 0.87)
    IEP2B_MOCK_NOT_READY    "true"  → /ready returns not-ready

Env vars — real mode:
    IEP2B_USE_REAL_MODEL    "true"  → enable real inference
    IEP2B_WEIGHTS_PATH      local in-image checkpoint path
                             (default: /opt/models/iep2b/
                             doclayout_yolo_docstructbench_imgsz1024.pt)
    IEP2B_LOCAL_WEIGHTS_PATH optional mounted local development override
    IEP2B_MODEL_VERSION     optional validation/override input; baked
                            `<weights>.version` metadata is authoritative
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException

from services.iep2b.app.postprocess import postprocess_regions
from shared.metrics import (
    IEP2B_GPU_INFERENCE_SECONDS,
    IEP2B_MEAN_PAGE_CONFIDENCE,
    IEP2B_REGION_CONFIDENCE,
    IEP2B_REGIONS_PER_PAGE,
)
from shared.schemas.layout import (
    LayoutConfSummary,
    LayoutDetectRequest,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

router = APIRouter(prefix="/v1", tags=["layout-detection"])

_DEFAULT_CONFIDENCE = 0.87


# ---------------------------------------------------------------------------
# Model-readiness check (exported for main.py health checks)
# ---------------------------------------------------------------------------


def is_model_ready() -> bool:
    """
    Return True when the model (stub or real) is ready to serve requests.

    Stub mode:  honour IEP2B_MOCK_NOT_READY env var.
    Real mode:  delegate to is_real_model_loaded().
    """
    if os.environ.get("IEP2B_USE_REAL_MODEL", "false").lower() != "true":
        return os.environ.get("IEP2B_MOCK_NOT_READY", "false").lower() != "true"
    from services.iep2b.app.model import is_real_model_loaded

    return is_real_model_loaded()


# ---------------------------------------------------------------------------
# Deterministic mock region templates (stub mode only)
# ---------------------------------------------------------------------------
#
# Coordinate space: notional 1000×1000 px normalised page image.
# All five canonical RegionType values are represented.

_MOCK_REGION_TEMPLATES: list[tuple[str, RegionType, BoundingBox]] = [
    (
        "r1",
        RegionType.title,
        BoundingBox(x_min=60.0, y_min=40.0, x_max=960.0, y_max=130.0),
    ),
    (
        "r2",
        RegionType.text_block,
        BoundingBox(x_min=60.0, y_min=150.0, x_max=460.0, y_max=610.0),
    ),
    (
        "r3",
        RegionType.text_block,
        BoundingBox(x_min=520.0, y_min=150.0, x_max=960.0, y_max=610.0),
    ),
    (
        "r4",
        RegionType.image,
        BoundingBox(x_min=60.0, y_min=630.0, x_max=460.0, y_max=910.0),
    ),
    (
        "r5",
        RegionType.caption,
        BoundingBox(x_min=60.0, y_min=920.0, x_max=460.0, y_max=970.0),
    ),
    (
        "r6",
        RegionType.table,
        BoundingBox(x_min=520.0, y_min=630.0, x_max=960.0, y_max=970.0),
    ),
]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/layout-detect",
    response_model=LayoutDetectResponse,
    summary="Run IEP2B DocLayout-YOLO layout detection on a page artifact",
)
def layout_detect(body: LayoutDetectRequest) -> LayoutDetectResponse:
    """
    Run IEP2B DocLayout-YOLO layout detection on the page image identified by
    body.image_uri.

    In stub mode (default): returns deterministic mock regions.
    In real mode (IEP2B_USE_REAL_MODEL=true): runs DocLayout-YOLO inference.

    Returns LayoutDetectResponse (200) with detector_type="doclayout_yolo".
    Returns HTTP 500 on detection failure or model load error.
    """
    t0 = time.monotonic()

    if os.environ.get("IEP2B_USE_REAL_MODEL", "false").lower() != "true":
        return _stub_response(body, t0)
    return _real_response(body, t0)


# ---------------------------------------------------------------------------
# Stub response path
# ---------------------------------------------------------------------------


def _stub_response(body: LayoutDetectRequest, t0: float) -> LayoutDetectResponse:
    """Return the deterministic mock response (stub mode)."""
    if os.environ.get("IEP2B_MOCK_FAIL", "false").lower() == "true":
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "layout_detection_failed",
                "error_message": "IEP2B_MOCK_FAIL is set — simulated failure",
            },
        )

    try:
        confidence = float(os.environ.get("IEP2B_MOCK_CONFIDENCE", str(_DEFAULT_CONFIDENCE)))
        confidence = max(0.0, min(1.0, confidence))
    except ValueError:
        confidence = _DEFAULT_CONFIDENCE

    raw_regions = [
        Region(id=rid, type=rtype, bbox=bbox, confidence=confidence)
        for rid, rtype, bbox in _MOCK_REGION_TEMPLATES
    ]

    regions = postprocess_regions(raw_regions)
    return _assemble_response(regions, t0, model_version="mock-stub-iep2b-6.3")


# ---------------------------------------------------------------------------
# Real inference path
# ---------------------------------------------------------------------------


def _real_response(body: LayoutDetectRequest, t0: float) -> LayoutDetectResponse:
    """Run DocLayout-YOLO inference and return the response."""
    from services.iep2b.app.inference import (
        load_image_for_yolo,
        raw_detections_to_regions,
        run_doclayout_yolo,
    )
    from services.iep2b.app.model import get_loaded_model_version, get_model

    try:
        model = get_model()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "model_not_ready",
                "error_message": str(exc),
            },
        ) from exc

    try:
        image = load_image_for_yolo(body.image_uri)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "image_load_failed",
                "error_message": str(exc),
            },
        ) from exc

    try:
        detections = run_doclayout_yolo(model, image)
        raw_regions = raw_detections_to_regions(detections)
        regions = postprocess_regions(raw_regions)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "layout_detection_failed",
                "error_message": f"Layout detection failed: {exc}",
            },
        ) from exc

    return _assemble_response(regions, t0, model_version=get_loaded_model_version())


# ---------------------------------------------------------------------------
# Response assembly (shared between stub and real paths)
# ---------------------------------------------------------------------------


def _assemble_response(
    regions: list[Region],
    t0: float,
    model_version: str,
    warnings: list[str] | None = None,
) -> LayoutDetectResponse:
    n = len(regions)
    mean_conf = sum(r.confidence for r in regions) / n if n else 0.0
    low_conf_frac = sum(1 for r in regions if r.confidence < 0.5) / n if n else 0.0

    histogram: dict[str, int] = {rt.value: 0 for rt in RegionType}
    for r in regions:
        histogram[r.type.value] += 1

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    IEP2B_GPU_INFERENCE_SECONDS.observe(elapsed_ms / 1000.0)
    IEP2B_MEAN_PAGE_CONFIDENCE.observe(mean_conf)
    IEP2B_REGIONS_PER_PAGE.observe(n)
    for r in regions:
        IEP2B_REGION_CONFIDENCE.observe(r.confidence)

    return LayoutDetectResponse(
        region_schema_version="v1",
        regions=regions,
        layout_conf_summary=LayoutConfSummary(
            mean_conf=round(mean_conf, 6),
            low_conf_frac=round(low_conf_frac, 6),
        ),
        region_type_histogram=histogram,
        column_structure=None,
        model_version=model_version,
        detector_type="doclayout_yolo",
        processing_time_ms=elapsed_ms,
        warnings=warnings if warnings is not None else [],
    )
