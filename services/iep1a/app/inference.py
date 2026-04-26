"""
services/iep1a/app/inference.py
--------------------------------
IEP1A — YOLOv8-seg geometry inference.

Loads the appropriate segmentation model based on material_type, runs inference
on the proxy image, extracts page corners from the segmentation mask contours,
and returns a GeometryResponse.

Model registry (material_type → weight file):
  book       → Book_segmentation.pt
  newspaper  → Newspaper_Segmentation.pt
  microfilm  → Segmentation_microfilm.pt

Environment variables:
  IEP1A_MODELS_DIR              path to model weights directory  (default: "models/iep1a")
  IEP1A_CONFIDENCE_THRESHOLD    detection confidence threshold   (default: "0.25")
  IEP1A_MOCK_MODE               "true" → fall back to mock behaviour for testing
  IEP1A_MOCK_FAIL               "true" → raise InferenceError (mock only)
  IEP1A_MOCK_FAIL_CODE          error_code for failure           (default: "GEOMETRY_FAILED")
  IEP1A_MOCK_FAIL_ACTION        fallback_action for failure      (default: "ESCALATE_REVIEW")
  IEP1A_MOCK_PAGE_COUNT         mock page count                  (default: "1")
  IEP1A_MOCK_CONFIDENCE         mock confidence                  (default: "0.95")
  IEP1A_MOCK_TTA_PASSES         mock TTA passes                 (default: "5")
  IEP1A_MOCK_NOT_READY          "true" → is_model_ready() False
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from services.iep1a.app.tta import compute_mock_tta_stats, compute_real_tta_stats
from shared.metrics import (
    IEP1A_GEOMETRY_CONFIDENCE,
    IEP1A_GPU_INFERENCE_SECONDS,
    IEP1A_PAGE_COUNT,
    IEP1A_SPLIT_DETECTION_RATE,
    IEP1A_TTA_PREDICTION_VARIANCE,
    IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE,
)
from shared.schemas.geometry import GeometryRequest, GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessError

logger = logging.getLogger(__name__)

# ── Model registry ──────────────────────────────────────────────────────────

_MODEL_FILES: dict[str, str] = {
    "book": "Book_segmentation.pt",
    "newspaper": "Newspaper_Segmentation.pt",
    "microfilm": "Segmentation_microfilm.pt",
}

_loaded_models: dict[str, object] = {}

# ── Reload tracking ─────────────────────────────────────────────────────────
# Monotonic startup time and wall-clock timestamps are both tracked so
# callers can distinguish "never reloaded" from "reloaded N times".

_startup_wall: float = time.time()
_reload_count: int = 0
_last_reload_wall: float | None = None
_last_version_tag: str | None = None


def _models_dir() -> Path:
    return Path(os.environ.get("IEP1A_MODELS_DIR", "models/iep1a"))


def _load_model(material_type: str) -> object:
    """Load and cache a YOLO segmentation model for the given material type."""
    model_file = _MODEL_FILES.get(material_type, _MODEL_FILES["book"])
    if model_file in _loaded_models:
        return _loaded_models[model_file]

    from ultralytics import YOLO

    model_path = _models_dir() / model_file
    if not model_path.exists():
        raise FileNotFoundError(f"IEP1A model not found: {model_path}")

    logger.info("IEP1A loading model: %s", model_path)
    model = YOLO(str(model_path))
    _loaded_models[model_file] = model
    return model


def _is_mock_mode() -> bool:
    return os.environ.get("IEP1A_MOCK_MODE", "false").lower() == "true"


class InferenceError(Exception):
    """Raised when geometry inference fails."""

    def __init__(self, error: PreprocessError) -> None:
        super().__init__(error.error_message)
        self.preprocess_error = error


def is_model_ready() -> bool:
    """Return True when models are available or in mock mode."""
    if os.environ.get("IEP1A_MOCK_NOT_READY", "false").lower() == "true":
        return False
    if _is_mock_mode():
        return True
    # Check that at least the default model file exists
    default_model = _models_dir() / _MODEL_FILES["book"]
    return default_model.exists()


def reload_models(version_tag: str | None = None) -> None:
    """Clear the in-process model cache so the next request reloads from disk."""
    global _reload_count, _last_reload_wall, _last_version_tag
    _loaded_models.clear()
    _reload_count += 1
    _last_reload_wall = time.time()
    _last_version_tag = version_tag or None
    logger.info(
        "iep1a: model cache cleared for hot-reload (reload_count=%d version_tag=%r)",
        _reload_count,
        _last_version_tag,
    )


def get_model_info() -> dict[str, Any]:
    """
    Return a snapshot of the current model-loading state for observability.

    version_tag is always None because iep1a loads weights from local .pt files
    and has no runtime mapping to the ModelVersion record in the EEP database.
    TODO: accept an injected version_tag from the reload signal message and
    store it here so callers can correlate this response with promotion-audit rows.
    """
    models_dir = _models_dir()
    loaded_entries = [
        {
            "material": mat,
            "weight_file": fname,
            "weight_path": str(models_dir / fname),
            "cached": fname in _loaded_models,
        }
        for mat, fname in _MODEL_FILES.items()
    ]
    last_reload_iso: str | None = None
    if _last_reload_wall is not None:
        last_reload_iso = datetime.fromtimestamp(_last_reload_wall, tz=timezone.utc).isoformat()

    return {
        "service": "iep1a",
        "mock_mode": _is_mock_mode(),
        "models_dir": str(models_dir),
        "loaded_models": loaded_entries,
        "reload_count": _reload_count,
        "last_reload_at": last_reload_iso,
        "reloaded_since_startup": _reload_count > 0,
        "version_tag": _last_version_tag,
    }


# ── Mask → corners conversion ──────────────────────────────────────────────


def _mask_to_corners(
    mask: np.ndarray,
    img_width: int,
    img_height: int,
) -> tuple[list[tuple[float, float]], tuple[int, int, int, int], float] | None:
    """
    Convert a binary segmentation mask to 4 corner points.

    Process:
      1. Find the largest contour in the binary mask.
      2. Approximate the contour to a polygon.
      3. If polygon has 4 vertices → use them directly as corners.
      4. Otherwise → use the oriented bounding rectangle (minAreaRect)
         to derive 4 corners.

    Returns (corners, bbox, area_fraction) or None if no valid contour found.
    """
    if mask.dtype != np.uint8:
        mask = (mask > 0).astype(np.uint8) * 255

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Use the largest contour
    contour = max(contours, key=cv2.contourArea)
    contour_area = cv2.contourArea(contour)
    if contour_area < 100:  # degenerate
        return None

    image_area = img_width * img_height
    area_fraction = contour_area / image_area if image_area > 0 else 0.0

    # Try polygon approximation
    epsilon = 0.02 * cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, epsilon, True)

    if len(approx) == 4:
        # Perfect quadrilateral
        corners = [(float(pt[0][0]), float(pt[0][1])) for pt in approx]
    else:
        # Fall back to minimum area rotated rectangle → 4 corners
        rect = cv2.minAreaRect(contour)
        box = cv2.boxPoints(rect)
        corners = [(float(pt[0]), float(pt[1])) for pt in box]

    # Order corners: top-left, top-right, bottom-right, bottom-left
    corners = _order_corners(corners)

    # Compute axis-aligned bounding box
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    bbox = (
        max(0, int(min(xs))),
        max(0, int(min(ys))),
        min(img_width, int(max(xs))),
        min(img_height, int(max(ys))),
    )

    return corners, bbox, round(area_fraction, 4)


def _order_corners(
    corners: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Order 4 corners as: top-left, top-right, bottom-right, bottom-left."""
    pts = np.array(corners, dtype="float32")
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).flatten()

    ordered = [
        tuple(pts[np.argmin(s)].tolist()),   # top-left: smallest x+y
        tuple(pts[np.argmin(diff)].tolist()), # top-right: smallest x-y
        tuple(pts[np.argmax(s)].tolist()),   # bottom-right: largest x+y
        tuple(pts[np.argmax(diff)].tolist()), # bottom-left: largest x-y
    ]
    return ordered


# ── Real inference ──────────────────────────────────────────────────────────


def _run_real_inference_single(
    model: object,
    image: np.ndarray,
    conf_threshold: float,
) -> list[dict]:
    """
    Run YOLOv8-seg on a single image and return per-detection dicts.

    Each dict has keys: corners, bbox, confidence, area_fraction.
    """
    results = model(image, conf=conf_threshold, verbose=False)  # type: ignore[operator]
    if not results or len(results) == 0:
        return []

    result = results[0]
    img_h, img_w = image.shape[:2]
    detections: list[dict] = []

    if result.masks is None or result.boxes is None:
        return []

    for i, mask_data in enumerate(result.masks.data):
        confidence = float(result.boxes.conf[i])
        mask_np = mask_data.cpu().numpy().astype(np.uint8)

        # Resize mask to image dimensions if needed
        if mask_np.shape[0] != img_h or mask_np.shape[1] != img_w:
            mask_np = cv2.resize(mask_np, (img_w, img_h), interpolation=cv2.INTER_NEAREST)

        conversion = _mask_to_corners(mask_np, img_w, img_h)
        if conversion is None:
            continue

        corners, bbox, area_fraction = conversion
        detections.append({
            "corners": corners,
            "bbox": bbox,
            "confidence": confidence,
            "area_fraction": area_fraction,
        })

    # Sort by area_fraction descending (largest pages first)
    detections.sort(key=lambda d: d["area_fraction"], reverse=True)
    return detections


def _detections_to_response(
    detections: list[dict],
    tta_stats: object,
    elapsed_ms: float,
) -> GeometryResponse:
    """Convert raw detections list into a GeometryResponse."""
    # Limit to at most 2 page detections (area sort already applied by caller).
    #  # Re-sort the final 2 by x_min so that pages[0] is always the physical left
  #  page and pages[1] is always the physical right page.  This guarantees the
    # split_x midpoint calculation and split.py coordinate assumptions hold.
    detections = sorted(detections[:2], key=lambda d: d["bbox"][0])

    page_count = len(detections) if detections else 1

    if not detections:
        # No detection — return a fallback full-image region with low confidence
        return GeometryResponse(
            page_count=1,
            pages=[
                PageRegion(
                    region_id="page_0",
                    geometry_type="bbox",
                    corners=None,
                    bbox=(0, 0, 1, 1),
                    confidence=0.0,
                    page_area_fraction=1.0,
                )
            ],
            split_required=False,
            split_x=None,
            geometry_confidence=0.0,
            tta_structural_agreement_rate=tta_stats.structural_agreement_rate,
            tta_prediction_variance=tta_stats.prediction_variance,
            tta_passes=tta_stats.tta_passes if hasattr(tta_stats, "tta_passes") else 1,
            uncertainty_flags=tta_stats.uncertainty_flags,
            warnings=["no_detection"],
            processing_time_ms=elapsed_ms,
        )

    pages: list[PageRegion] = []
    min_conf = 1.0
    for i, det in enumerate(detections):
        min_conf = min(min_conf, det["confidence"])
        pages.append(
            PageRegion(
                region_id=f"page_{i}",
                geometry_type="quadrilateral",
                corners=det["corners"],
                bbox=det["bbox"],
                confidence=det["confidence"],
                page_area_fraction=det["area_fraction"],
            )
        )

    split_required = page_count == 2
    split_x: int | None = None
    if split_required:
        # Split point = midpoint between right edge of page_0 and left edge of page_1
        bbox_0 = detections[0]["bbox"]
        bbox_1 = detections[1]["bbox"]
        split_x = (bbox_0[2] + bbox_1[0]) // 2

    return GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=min_conf,
        tta_structural_agreement_rate=tta_stats.structural_agreement_rate,
        tta_prediction_variance=tta_stats.prediction_variance,
        tta_passes=tta_stats.tta_passes if hasattr(tta_stats, "tta_passes") else 1,
        uncertainty_flags=tta_stats.uncertainty_flags,
        warnings=[],
        processing_time_ms=elapsed_ms,
    )


def run_inference(req: GeometryRequest) -> GeometryResponse:
    """
    Run IEP1A geometry inference.

    Uses real YOLOv8-seg models when available, falls back to mock when
    IEP1A_MOCK_MODE=true or models are not found.
    """
    if _is_mock_mode():
        return run_mock_inference(req)

    t0 = time.monotonic()
    conf_threshold = float(os.environ.get("IEP1A_CONFIDENCE_THRESHOLD", "0.25"))
    tta_passes = int(os.environ.get("IEP1A_TTA_PASSES", "5"))

    try:
        model = _load_model(req.material_type)
    except FileNotFoundError:
        logger.warning(
            "IEP1A model not found for material_type=%s, falling back to mock",
            req.material_type,
        )
        return run_mock_inference(req)

    # Load proxy image from URI
    image = _load_image(req.image_uri)
    if image is None:
        raise InferenceError(
            PreprocessError(
                error_code="GEOMETRY_FAILED",  # type: ignore[arg-type]
                error_message=f"Could not load image from {req.image_uri}",
                fallback_action="RETRY",  # type: ignore[arg-type]
            )
        )

    # Run primary inference
    detections = _run_real_inference_single(model, image, conf_threshold)

    # Run TTA
    tta_stats = compute_real_tta_stats(model, image, conf_threshold, tta_passes)

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    resp = _detections_to_response(detections, tta_stats, elapsed_ms)
    IEP1A_GPU_INFERENCE_SECONDS.observe(elapsed_ms / 1000.0)
    IEP1A_GEOMETRY_CONFIDENCE.observe(resp.geometry_confidence)
    IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE.observe(resp.tta_structural_agreement_rate)
    IEP1A_TTA_PREDICTION_VARIANCE.observe(resp.tta_prediction_variance)
    IEP1A_PAGE_COUNT.observe(resp.page_count)
    if resp.split_required:
        IEP1A_SPLIT_DETECTION_RATE.inc()
    return resp


def _load_image(image_uri: str) -> np.ndarray | None:
    """Load an image from a storage URI into a numpy array."""
    from shared.io.storage import get_backend

    try:
        backend = get_backend(image_uri)
        image_bytes = backend.get_bytes(image_uri)
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        return image
    except Exception:
        logger.exception("IEP1A: failed to load image from %s", image_uri)
        return None


# ── Mock inference (for testing) ────────────────────────────────────────────


def run_mock_inference(req: GeometryRequest) -> GeometryResponse:
    """
    Return a deterministic mock GeometryResponse for the given request.

    Geometry is synthetic: quadrilateral corners derived from a notional
    1000×1000 proxy image.
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

    tta = compute_mock_tta_stats(tta_passes)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    resp = GeometryResponse(
        page_count=page_count,
        pages=pages,
        split_required=split_required,
        split_x=split_x,
        geometry_confidence=confidence,
        tta_structural_agreement_rate=tta.structural_agreement_rate,
        tta_prediction_variance=tta.prediction_variance,
        tta_passes=tta_passes,
        uncertainty_flags=tta.uncertainty_flags,
        warnings=[],
        processing_time_ms=elapsed_ms,
    )
    IEP1A_GPU_INFERENCE_SECONDS.observe(elapsed_ms / 1000.0)
    IEP1A_GEOMETRY_CONFIDENCE.observe(resp.geometry_confidence)
    IEP1A_TTA_STRUCTURAL_AGREEMENT_RATE.observe(resp.tta_structural_agreement_rate)
    IEP1A_TTA_PREDICTION_VARIANCE.observe(resp.tta_prediction_variance)
    IEP1A_PAGE_COUNT.observe(resp.page_count)
    if resp.split_required:
        IEP1A_SPLIT_DETECTION_RATE.inc()
    return resp
