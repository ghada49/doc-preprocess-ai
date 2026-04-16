"""
services/iep1b/app/tta.py
--------------------------
TTA (Test-Time Augmentation) for IEP1B.

Provides both mock TTA stats (for testing) and real TTA computation
that applies augmentations to the proxy image and measures keypoint
prediction stability across passes.

Real TTA procedure:
  1. Apply N augmentations to the proxy image (horizontal flip, ±2–3° rotation,
     ±5–10% scale).
  2. Run YOLOv8-pose inference on each augmented input.
  3. Map keypoint predictions back to original image coordinates.
  4. Compute mode of page_count and split_required across passes.
  5. tta_structural_agreement_rate = fraction of passes matching the mode.
  6. tta_prediction_variance = inter-pass variance of keypoint predictions.

Configurable via environment variables (read at call time):
  IEP1B_MOCK_TTA_AGREEMENT_RATE   float in [0, 1]   (default: "1.0")
  IEP1B_MOCK_TTA_VARIANCE         float >= 0        (default: "0.001")
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field

import cv2
import numpy as np

# Conservative fixed thresholds (spec Section 1 threshold-derivation rule).
_LOW_AGREEMENT_THRESHOLD: float = 0.80
_HIGH_VARIANCE_THRESHOLD: float = 0.10


@dataclass(frozen=True)
class TTAStats:
    """
    TTA statistics for a single geometry inference call.

    Fields:
        structural_agreement_rate — fraction of TTA passes agreeing on
                                    page_count and split_required [0, 1]
        prediction_variance       — inter-pass variance of keypoint predictions
                                    [>= 0]
        uncertainty_flags         — advisory flags; populated when agreement or
                                    variance cross configured thresholds
        tta_passes                — number of TTA passes performed
    """

    structural_agreement_rate: float
    prediction_variance: float
    uncertainty_flags: list[str] = field(default_factory=list)
    tta_passes: int = 1


def _derive_flags(agreement_rate: float, variance: float) -> list[str]:
    flags: list[str] = []
    if agreement_rate < _LOW_AGREEMENT_THRESHOLD:
        flags.append("low_structural_agreement")
    if variance > _HIGH_VARIANCE_THRESHOLD:
        flags.append("high_prediction_variance")
    return flags


def compute_mock_tta_stats(n_passes: int) -> TTAStats:
    """Return mock TTA statistics from environment variables."""
    agreement_rate = float(os.environ.get("IEP1B_MOCK_TTA_AGREEMENT_RATE", "1.0"))
    variance = float(os.environ.get("IEP1B_MOCK_TTA_VARIANCE", "0.001"))

    return TTAStats(
        structural_agreement_rate=agreement_rate,
        prediction_variance=variance,
        uncertainty_flags=_derive_flags(agreement_rate, variance),
        tta_passes=n_passes,
    )


# ── TTA augmentation transforms ─────────────────────────────────────────────


def _apply_augmentation(image: np.ndarray, pass_idx: int) -> tuple[np.ndarray, dict]:
    """
    Apply a deterministic augmentation based on pass index.

    Returns (augmented_image, transform_info).
    """
    h, w = image.shape[:2]
    info: dict = {"type": "none", "h": h, "w": w}

    if pass_idx == 0:
        return image.copy(), info

    if pass_idx % 3 == 1:
        aug = cv2.flip(image, 1)
        info["type"] = "hflip"
        return aug, info

    if pass_idx % 3 == 2:
        angle = 2.5 if (pass_idx % 2 == 0) else -2.5
        cx, cy = w / 2.0, h / 2.0
        mat = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
        aug = cv2.warpAffine(image, mat, (w, h), borderMode=cv2.BORDER_REPLICATE)
        info["type"] = "rotate"
        info["angle"] = angle
        info["center"] = (cx, cy)
        return aug, info

    scale = 1.05 if (pass_idx % 2 == 0) else 0.95
    new_w, new_h = int(w * scale), int(h * scale)
    aug = cv2.resize(image, (new_w, new_h))
    info["type"] = "scale"
    info["scale"] = scale
    return aug, info


def _reverse_map_keypoints(
    keypoints: list[tuple[float, float]],
    info: dict,
) -> list[tuple[float, float]]:
    """Map detected keypoints back to the original image coordinate space."""
    if info["type"] == "none":
        return keypoints

    if info["type"] == "hflip":
        w = info["w"]
        return [(w - x, y) for x, y in keypoints]

    if info["type"] == "rotate":
        import math

        angle_rad = math.radians(-info["angle"])
        cx, cy = info["center"]
        result = []
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
        for x, y in keypoints:
            dx, dy = x - cx, y - cy
            nx = cos_a * dx - sin_a * dy + cx
            ny = sin_a * dx + cos_a * dy + cy
            result.append((nx, ny))
        return result

    if info["type"] == "scale":
        scale = info["scale"]
        return [(x / scale, y / scale) for x, y in keypoints]

    return keypoints


def compute_real_tta_stats(
    model: object,
    image: np.ndarray,
    conf_threshold: float,
    n_passes: int,
) -> TTAStats:
    """
    Run real TTA: apply augmentations, infer, and compute stability metrics.

    Args:
        model:          Loaded YOLO pose model
        image:          Original proxy image (H×W×C)
        conf_threshold: Confidence threshold for detection
        n_passes:       Number of TTA passes (>= 1)

    Returns:
        TTAStats with real structural agreement and prediction variance.
    """
    if n_passes < 1:
        n_passes = 1

    page_counts: list[int] = []
    all_keypoints: list[list[tuple[float, float]]] = []

    for pass_idx in range(n_passes):
        aug_image, aug_info = _apply_augmentation(image, pass_idx)

        results = model(aug_image, conf=conf_threshold, verbose=False)  # type: ignore[operator]

        if not results or len(results) == 0 or results[0].keypoints is None:
            page_counts.append(0)
            all_keypoints.append([])
            continue

        result = results[0]
        n_detections = len(result.keypoints.data)
        page_counts.append(min(n_detections, 2))

        pass_kps: list[tuple[float, float]] = []
        for i in range(min(n_detections, 2)):
            kp_np = result.keypoints.data[i].cpu().numpy()
            for j in range(min(4, len(kp_np))):
                kp = kp_np[j]
                pass_kps.append((float(kp[0]), float(kp[1])))

        # Reverse map to original coords
        pass_kps = _reverse_map_keypoints(pass_kps, aug_info)
        all_keypoints.append(pass_kps)

    # Compute structural agreement rate
    if page_counts:
        mode_count = Counter(page_counts).most_common(1)[0][0]
        agreement_rate = sum(1 for c in page_counts if c == mode_count) / len(page_counts)
    else:
        agreement_rate = 0.0

    # Compute keypoint variance across passes
    variance = _compute_keypoint_variance(all_keypoints)

    return TTAStats(
        structural_agreement_rate=round(agreement_rate, 4),
        prediction_variance=round(variance, 6),
        uncertainty_flags=_derive_flags(agreement_rate, variance),
        tta_passes=n_passes,
    )


def _compute_keypoint_variance(all_keypoints: list[list[tuple[float, float]]]) -> float:
    """
    Compute inter-pass variance of keypoint coordinates.

    For each pass, compute the centroid of all keypoints. Then compute the
    variance of centroids across passes.
    """
    centroids: list[tuple[float, float]] = []
    for kps in all_keypoints:
        if not kps:
            continue
        cx = sum(x for x, _ in kps) / len(kps)
        cy = sum(y for _, y in kps) / len(kps)
        centroids.append((cx, cy))

    if len(centroids) < 2:
        return 0.0

    mean_cx = sum(c[0] for c in centroids) / len(centroids)
    mean_cy = sum(c[1] for c in centroids) / len(centroids)

    variance = sum(
        (c[0] - mean_cx) ** 2 + (c[1] - mean_cy) ** 2 for c in centroids
    ) / len(centroids)

    return variance
