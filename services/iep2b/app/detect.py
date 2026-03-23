"""
services/iep2b/app/detect.py
-----------------------------
IEP2B POST /v1/layout-detect router.

Mock stub implementation (Packets 6.3 / 6.4).
Real DocLayout-YOLO inference is deferred to Phase 12.

The stub:
  - Accepts a valid LayoutDetectRequest and returns a valid LayoutDetectResponse.
  - Returns deterministic mock regions already in the canonical 5-class schema.
    In the stub the mock bypasses the native-to-canonical class mapping step
    (class_mapping.map_native_class) because there is no real model output to
    map; regions are constructed directly with RegionType values.
  - Runs the IEP2B postprocessing pipeline (NMS + ID reassignment) via
    postprocess_regions (Packet 6.4).
  - detector_type is always "doclayout_yolo".
  - column_structure is None (IEP2B does not infer column structure).
  - Supports failure simulation via IEP2B_MOCK_FAIL env var.

Configurable env vars (read at call time so tests can monkeypatch freely):
    IEP2B_MOCK_FAIL         — "true"  → HTTP 500
    IEP2B_MOCK_CONFIDENCE   — float in [0, 1]  (default: 0.84)
    IEP2B_MOCK_NOT_READY    — "true"  → is_model_ready() returns False
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException

from services.iep2b.app.postprocess import postprocess_regions
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

_DEFAULT_CONFIDENCE = 0.84


def is_model_ready() -> bool:
    """
    Return True when the mock 'model' is considered loaded and ready.

    Phase 12 replaces this with a real CUDA + DocLayout-YOLO model-loaded check.
    """
    return os.environ.get("IEP2B_MOCK_NOT_READY", "false").lower() != "true"


# ---------------------------------------------------------------------------
# Deterministic mock region templates (all 5 canonical types represented)
# ---------------------------------------------------------------------------
#
# At the stub stage regions are already in canonical form.
# Packet 6.4 introduces the native DocStructBench → canonical mapping layer
# that sits between raw model output and this response assembly.
#
# Coordinate space: notional 1000×1000 px normalised page image.
# Bbox positions are intentionally distinct from IEP2A's templates to
# exercise the consensus gate's matching logic realistically.

_MOCK_REGION_TEMPLATES: list[tuple[str, RegionType, BoundingBox]] = [
    (
        "r1",
        RegionType.title,
        BoundingBox(x_min=45.0, y_min=25.0, x_max=955.0, y_max=115.0),
    ),
    (
        "r2",
        RegionType.text_block,
        BoundingBox(x_min=45.0, y_min=135.0, x_max=455.0, y_max=610.0),
    ),
    (
        "r3",
        RegionType.text_block,
        BoundingBox(x_min=505.0, y_min=135.0, x_max=955.0, y_max=610.0),
    ),
    (
        "r4",
        RegionType.image,
        BoundingBox(x_min=45.0, y_min=625.0, x_max=455.0, y_max=905.0),
    ),
    (
        "r5",
        RegionType.caption,
        BoundingBox(x_min=45.0, y_min=915.0, x_max=455.0, y_max=965.0),
    ),
    (
        "r6",
        RegionType.table,
        BoundingBox(x_min=505.0, y_min=625.0, x_max=955.0, y_max=965.0),
    ),
]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/v1/layout-detect",
    response_model=LayoutDetectResponse,
    summary="Run IEP2B DocLayout-YOLO layout detection on a page artifact",
)
def layout_detect(body: LayoutDetectRequest) -> LayoutDetectResponse:
    """
    Run IEP2B (DocLayout-YOLO) layout detection on the page image identified
    by body.image_uri.

    Returns LayoutDetectResponse (200) with detector_type="doclayout_yolo".
    Returns HTTP 500 when IEP2B_MOCK_FAIL="true".
    """
    # Start the clock at request receipt per the global processing_time_ms rule.
    t0 = time.monotonic()

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
        column_structure=None,
        model_version="mock-stub-6.4",
        detector_type="doclayout_yolo",
        processing_time_ms=elapsed_ms,
        warnings=[],
    )
