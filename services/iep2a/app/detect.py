"""
services/iep2a/app/detect.py
-----------------------------
IEP2A POST /v1/layout-detect router.

Mock stub implementation (Packet 6.1).
Real Detectron2 Faster R-CNN inference is deferred to Phase 12.

The stub:
  - Accepts a valid LayoutDetectRequest and returns a valid LayoutDetectResponse.
  - Returns deterministic mock regions covering all 5 canonical region types.
  - detector_type is always "detectron2".
  - column_structure is None (DBSCAN inference deferred to Packet 6.2).
  - Supports failure simulation via IEP2A_MOCK_FAIL env var.

Configurable env vars (read at call time so tests can monkeypatch freely):
    IEP2A_MOCK_FAIL         — "true"  → HTTP 500
    IEP2A_MOCK_CONFIDENCE   — float in [0, 1]  (default: 0.87)
    IEP2A_MOCK_NOT_READY    — "true"  → is_model_ready() returns False
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException

from shared.schemas.layout import (
    LayoutConfSummary,
    LayoutDetectRequest,
    LayoutDetectResponse,
    Region,
    RegionType,
)
from shared.schemas.ucf import BoundingBox

router = APIRouter(tags=["layout"])

# ---------------------------------------------------------------------------
# Model-readiness check
# ---------------------------------------------------------------------------

_DEFAULT_CONFIDENCE = 0.87


def is_model_ready() -> bool:
    """
    Return True when the mock 'model' is considered loaded and ready.

    Phase 12 replaces this with a real CUDA + Detectron2 model-loaded check.
    """
    return os.environ.get("IEP2A_MOCK_NOT_READY", "false").lower() != "true"


# ---------------------------------------------------------------------------
# Deterministic mock region templates (all 5 canonical types represented)
# ---------------------------------------------------------------------------
#
# Templates are defined as (id, type, bbox) tuples; confidence is applied
# uniformly at request time from IEP2A_MOCK_CONFIDENCE.
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
    summary="Run IEP2A Detectron2 layout detection on a page artifact",
)
def layout_detect(body: LayoutDetectRequest) -> LayoutDetectResponse:
    """
    Run IEP2A (Detectron2) layout detection on the page image identified by
    body.image_uri.

    Returns LayoutDetectResponse (200) with detector_type="detectron2".
    Returns HTTP 500 when IEP2A_MOCK_FAIL="true".
    """
    # Start the clock at request receipt per the global processing_time_ms rule.
    t0 = time.monotonic()

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

    regions = [
        Region(id=rid, type=rtype, bbox=bbox, confidence=confidence)
        for rid, rtype, bbox in _MOCK_REGION_TEMPLATES
    ]

    mean_conf = sum(r.confidence for r in regions) / len(regions)
    low_conf_frac = sum(1 for r in regions if r.confidence < 0.5) / len(regions)

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
        column_structure=None,
        model_version="mock-stub-6.1",
        detector_type="detectron2",
        processing_time_ms=elapsed_ms,
        warnings=[],
    )
