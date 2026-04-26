"""
services/iep1b/app/inference.py
--------------------------------
IEP1B — YOLOv8-pose geometry inference.

Loads the appropriate keypoint model based on material_type, runs inference
on the proxy image, extracts page corners directly from keypoint predictions,
and returns a GeometryResponse.

Model registry (material_type → weight file):
  book       → Book_keypoint.pt
  newspaper  → Newspaper_Keypoints.pt
  microfilm  → Microfilm_Keypoints.pt

Environment variables:
  IEP1B_MODELS_DIR              path to model weights directory  (default: "models/iep1b")
  IEP1B_CONFIDENCE_THRESHOLD    detection confidence threshold   (default: "0.25")
  IEP1B_MOCK_MODE               "true" → fall back to mock behaviour for testing
  IEP1B_MOCK_FAIL               "true" → raise InferenceError (mock only)
  IEP1B_MOCK_FAIL_CODE          error_code for failure           (default: "GEOMETRY_FAILED")
  IEP1B_MOCK_FAIL_ACTION        fallback_action for failure      (default: "ESCALATE_REVIEW")
  IEP1B_MOCK_PAGE_COUNT         mock page count                  (default: "1")
  IEP1B_MOCK_CONFIDENCE         mock confidence                  (default: "0.92")
  IEP1B_MOCK_TTA_PASSES         mock TTA passes                 (default: "5")
  IEP1B_MOCK_NOT_READY          "true" → is_model_ready() False
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

from services.iep1b.app.tta import compute_mock_tta_stats, compute_real_tta_stats
from shared.metrics import (
    IEP1B_GEOMETRY_CONFIDENCE,
    IEP1B_GPU_INFERENCE_SECONDS,
    IEP1B_PAGE_COUNT,
    IEP1B_SPLIT_DETECTION_RATE,
    IEP1B_TTA_PREDICTION_VARIANCE,
    IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE,
)
from shared.schemas.geometry import GeometryRequest, GeometryResponse, PageRegion
from shared.schemas.preprocessing import PreprocessError

logger = logging.getLogger(__name__)

# ── Model registry ──────────────────────────────────────────────────────────

_MODEL_FILES: dict[str, str] = {
    "book": "Book_keypoint.pt",
    "newspaper": "Newspaper_Keypoints.pt",
    "microfilm": "Microfilm_Keypoints.pt",
}

_loaded_models: dict[str, object] = {}

# ── Reload tracking ─────────────────────────────────────────────────────────

_startup_wall: float = time.time()
_reload_count: int = 0
_last_reload_wall: float | None = None
_last_version_tag: str | None = None


def _models_dir() -> Path:
    return Path(os.environ.get("IEP1B_MODELS_DIR", "models/iep1b"))


def _load_model(material_type: str) -> object:
    """Load and cache a YOLO pose model for the given material type."""
    model_file = _MODEL_FILES.get(material_type, _MODEL_FILES["book"])
    if model_file in _loaded_models:
        return _loaded_models[model_file]

    from ultralytics import YOLO

    model_path = _models_dir() / model_file
    if not model_path.exists():
        raise FileNotFoundError(f"IEP1B model not found: {model_path}")

    logger.info("IEP1B loading model: %s", model_path)
    model = YOLO(str(model_path))
    _loaded_models[model_file] = model
    return model


def _is_mock_mode() -> bool:
    return os.environ.get("IEP1B_MOCK_MODE", "false").lower() == "true"


def reload_models(version_tag: str | None = None) -> None:
    """Clear the in-process model cache so the next request reloads from disk."""
    global _reload_count, _last_reload_wall, _last_version_tag
    _loaded_models.clear()
    _reload_count += 1
    _last_reload_wall = time.time()
    _last_version_tag = version_tag or None
    logger.info(
        "iep1b: model cache cleared for hot-reload (reload_count=%d version_tag=%r)",
        _reload_count,
        _last_version_tag,
    )


def get_model_info() -> dict[str, Any]:
    """
    Return a snapshot of the current model-loading state for observability.

    version_tag is always None — iep1b loads weights from local .pt files
    and has no runtime mapping to the ModelVersion record in the EEP database.
    TODO: persist the version_tag from the Redis reload signal and return it
    here so operators can correlate with promotion-audit rows.
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
        "service": "iep1b",
        "mock_mode": _is_mock_mode(),
        "models_dir": str(models_dir),
        "loaded_models": loaded_entries,
        "reload_count": _reload_count,
        "last_reload_at": last_reload_iso,
        "reloaded_since_startup": _reload_count > 0,
        "version_tag": _last_version_tag,
    }


class InferenceError(Exception):
    """Raised when geometry inference fails."""

    def __init__(self, error: PreprocessError) -> None:
        super().__init__(error.error_message)
        self.preprocess_error = error


def is_model_ready() -> bool:
    """Return True when models are available or in mock mode."""
    if os.environ.get("IEP1B_MOCK_NOT_READY", "false").lower() == "true":
        return False
    if _is_mock_mode():
        return True
    default_model = _models_dir() / _MODEL_FILES["book"]
    return default_model.exists()


# ── Keypoints → corners conversion ─────────────────────────────────────────


def _keypoints_to_corners(
    keypoints: np.ndarray,
    img_width: int,
    img_height: int,
) -> tuple[list[tuple[float, float]], tuple[int, int, int, int], float] | None:
    """
    Convert YOLOv8-pose keypoint predictions to 4 corner points.

    The pose model predicts keypoints as (x, y, visibility) triplets.
    We expect exactly 4 keypoints representing the 4 corners of the page.

    Returns (corners, bbox, area_fraction) or None if invalid.
    """
    if keypoints is None or len(keypoints) < 4:
        return None

    # Extract the first 4 keypoints (x, y pairs)
    corners: list[tuple[float, float]] = []
    for i in range(4):
        kp = keypoints[i]
        x, y = float(kp[0]), float(kp[1])

        # Check visibility if available (3rd element)
        if len(kp) >= 3:
            visibility = float(kp[2])
            if visibility < 0.1:  # not visible
                return None

        # Clamp to image bounds
        x = max(0.0, min(float(img_width), x))
        y = max(0.0, min(float(img_height), y))
        corners.append((x, y))

    # Order corners: top-left, top-right, bottom-right, bottom-left
    corners = _order_corners(corners)

    # Compute bounding box
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    bbox = (
        max(0, int(min(xs))),
        max(0, int(min(ys))),
        min(img_width, int(max(xs))),
        min(img_height, int(max(ys))),
    )

    # Compute area fraction using shoelace formula
    area = _quad_area(corners)
    image_area = img_width * img_height
    area_fraction = area / image_area if image_area > 0 else 0.0

    if area < 100:  # degenerate
        return None

    return corners, bbox, round(area_fraction, 4)


def _quad_area(corners: list[tuple[float, float]]) -> float:
    """Compute polygon area using the shoelace formula."""
    n = len(corners)
    area = 0.0
    for i in range(n):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


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
    Run YOLOv8-pose on a single image and return per-detection dicts.

    Each dict has keys: corners, bbox, confidence, area_fraction.
    """
    results = model(image, conf=conf_threshold, verbose=False)  # type: ignore[operator]
    if not results or len(results) == 0:
        return []

    result = results[0]
    img_h, img_w = image.shape[:2]
    detections: list[dict] = []

    if result.keypoints is None or result.boxes is None:
        return []

    for i, kp_data in enumerate(result.keypoints.data):
        confidence = float(result.boxes.conf[i])
        kp_np = kp_data.cpu().numpy()

        conversion = _keypoints_to_corners(kp_np, img_w, img_h)
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
    # Re-sort the final 2 by the dominant split axis:
    #   - Horizontal spread (x_min values differ more than y_min values):
    #     sort by x_min ascending → pages[0]=physical left, pages[1]=physical right.
    #   - Vertical stack (y_min values differ more than x_min values):
    #     sort by y_min ascending → pages[0]=top page, pages[1]=bottom page.
    # The worker applies a post-IEP1E rotation-aware swap to finalize left/right.
    top2 = detections[:2]
    if len(top2) == 2:
        x_sep = abs(top2[0]["bbox"][0] - top2[1]["bbox"][0])
        y_sep = abs(top2[0]["bbox"][1] - top2[1]["bbox"][1])
        if y_sep > x_sep:
            detections = sorted(top2, key=lambda d: d["bbox"][1])  # top-to-bottom
        else:
            detections = sorted(top2, key=lambda d: d["bbox"][0])  # left-to-right
    else:
        detections = top2
    page_count = len(detections) if detections else 1

    if not detections:
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
    Run IEP1B geometry inference.

    Uses real YOLOv8-pose models when available, falls back to mock when
    IEP1B_MOCK_MODE=true or models are not found.
    """
    if _is_mock_mode():
        return run_mock_inference(req)

    t0 = time.monotonic()
    conf_threshold = float(os.environ.get("IEP1B_CONFIDENCE_THRESHOLD", "0.25"))
    tta_passes = int(os.environ.get("IEP1B_TTA_PASSES", "5"))

    try:
        model = _load_model(req.material_type)
    except FileNotFoundError:
        logger.warning(
            "IEP1B model not found for material_type=%s, falling back to mock",
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
    IEP1B_GPU_INFERENCE_SECONDS.observe(elapsed_ms / 1000.0)
    IEP1B_GEOMETRY_CONFIDENCE.observe(resp.geometry_confidence)
    IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE.observe(resp.tta_structural_agreement_rate)
    IEP1B_TTA_PREDICTION_VARIANCE.observe(resp.tta_prediction_variance)
    IEP1B_PAGE_COUNT.observe(resp.page_count)
    if resp.split_required:
        IEP1B_SPLIT_DETECTION_RATE.inc()
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
        logger.exception("IEP1B: failed to load image from %s", image_uri)
        return None


# ── Mock inference (for testing) ────────────────────────────────────────────


def run_mock_inference(req: GeometryRequest) -> GeometryResponse:
    """
    Return a deterministic mock GeometryResponse for the given request.

    Geometry is synthetic: quadrilateral corners derived from a notional
    1000×1000 proxy image.
    """
    t0 = time.monotonic()

    if os.environ.get("IEP1B_MOCK_FAIL", "false").lower() == "true":
        error_code = os.environ.get("IEP1B_MOCK_FAIL_CODE", "GEOMETRY_FAILED")
        fallback_action = os.environ.get("IEP1B_MOCK_FAIL_ACTION", "ESCALATE_REVIEW")
        raise InferenceError(
            PreprocessError(
                error_code=error_code,  # type: ignore[arg-type]
                error_message=f"Mock IEP1B failure: {error_code}",
                fallback_action=fallback_action,  # type: ignore[arg-type]
            )
        )

    page_count = int(os.environ.get("IEP1B_MOCK_PAGE_COUNT", "1"))
    confidence = float(os.environ.get("IEP1B_MOCK_CONFIDENCE", "0.92"))
    tta_passes = int(os.environ.get("IEP1B_MOCK_TTA_PASSES", "5"))

    split_required = page_count == 2
    split_x: int | None = 500 if split_required else None

    pages: list[PageRegion] = []
    for i in range(page_count):
        half_w = 1000 // page_count
        x0 = i * half_w + 15
        x1 = (i + 1) * half_w - 15
        y0, y1 = 15, 985
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
    IEP1B_GPU_INFERENCE_SECONDS.observe(elapsed_ms / 1000.0)
    IEP1B_GEOMETRY_CONFIDENCE.observe(resp.geometry_confidence)
    IEP1B_TTA_STRUCTURAL_AGREEMENT_RATE.observe(resp.tta_structural_agreement_rate)
    IEP1B_TTA_PREDICTION_VARIANCE.observe(resp.tta_prediction_variance)
    IEP1B_PAGE_COUNT.observe(resp.page_count)
    if resp.split_required:
        IEP1B_SPLIT_DETECTION_RATE.inc()
    return resp
