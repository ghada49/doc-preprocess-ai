"""
services/iep1a/app/inference.py
--------------------------------
Mock inference module for IEP1A (YOLOv8-seg geometry service).

Provides deterministic, configurable mock behavior for testing.
Real YOLOv8-seg ML inference replaces this in Phase 12.

Configurable via environment variables (read at call time so tests can use
monkeypatch without restarting the process):

  IEP1A_MOCK_FAIL          "true"  → raise InferenceError
  IEP1A_MOCK_FAIL_CODE     error_code for failure  (default: "GEOMETRY_FAILED")
  IEP1A_MOCK_FAIL_ACTION   "RETRY" or "ESCALATE_REVIEW"  (default: "ESCALATE_REVIEW")
  IEP1A_MOCK_PAGE_COUNT    "1" or "2"  (default: "1")
  IEP1A_MOCK_CONFIDENCE    float in [0, 1]  (default: "0.95")
  IEP1A_MOCK_TTA_PASSES    int >= 1  (default: "5")
  IEP1A_MOCK_NOT_READY     "true"  → is_model_ready() returns False
"""

from __future__ import annotations

import os
import time

from shared.schemas.geometry import GeometryRequest, GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessError


class InferenceError(Exception):
    """Raised by run_mock_inference to simulate a geometry service failure."""

    def __init__(self, error: PreprocessError) -> None:
        super().__init__(error.error_message)
        self.preprocess_error = error


def is_model_ready() -> bool:
    """
    Return True when the mock 'model' is considered loaded and ready.

    In Phase 2 always True unless IEP1A_MOCK_NOT_READY="true".
    Phase 12 replaces this with a real CUDA + YOLOv8-seg model check.
    """
    return os.environ.get("IEP1A_MOCK_NOT_READY", "false").lower() != "true"


def run_mock_inference(req: GeometryRequest) -> GeometryResponse:
    """
    Return a deterministic mock GeometryResponse for the given request.

    Geometry is synthetic: quadrilateral corners derived from a notional
    1000×1000 proxy image.  For two-page spreads each child occupies one
    horizontal half.

    Raises:
        InferenceError: when IEP1A_MOCK_FAIL="true".
    """
    t0 = time.monotonic()

    # ── failure simulation ──────────────────────────────────────────────────
    if os.environ.get("IEP1A_MOCK_FAIL", "false").lower() == "true":
        error_code = os.environ.get("IEP1A_MOCK_FAIL_CODE", "GEOMETRY_FAILED")
        fallback_action = os.environ.get("IEP1A_MOCK_FAIL_ACTION", "ESCALATE_REVIEW")
        raise InferenceError(
            PreprocessError(
                error_code=error_code,  # type: ignore[arg-type]
                error_message=f"Mock IEP1A failure: {error_code}",
                fallback_action=fallback_action,  # type: ignore[arg-type]
            )
        )

    # ── mock geometry ───────────────────────────────────────────────────────
    page_count = int(os.environ.get("IEP1A_MOCK_PAGE_COUNT", "1"))
    confidence = float(os.environ.get("IEP1A_MOCK_CONFIDENCE", "0.95"))
    tta_passes = int(os.environ.get("IEP1A_MOCK_TTA_PASSES", "5"))

    split_required = page_count == 2
    split_x: int | None = 500 if split_required else None

    pages: list[PageRegion] = []
    for i in range(page_count):
        # Synthetic corners within a notional 1000×1000 proxy image.
        # Single page: full width with 20 px margin.
        # Two-page spread: each child occupies one 500 px half.
        half_w = 1000 // page_count
        x0 = i * half_w + 20
        x1 = (i + 1) * half_w - 20
        y0, y1 = 20, 980
        area_fraction = round((x1 - x0) * (y1 - y0) / (1000 * 1000), 4)
        pages.append(
            PageRegion(
                region_id=f"page_{i}",
                geometry_type="quadrilateral",
                corners=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                bbox=(x0, y0, x1, y1),
                confidence=confidence,
                page_area_fraction=area_fraction,
            )
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=confidence,
        tta_structural_agreement_rate=1.0,  # Packet 2.2 makes this configurable
        tta_prediction_variance=0.001,  # Packet 2.2 makes this configurable
        tta_passes=tta_passes,
        uncertainty_flags=[],
        warnings=[],
        processing_time_ms=elapsed_ms,
    )
