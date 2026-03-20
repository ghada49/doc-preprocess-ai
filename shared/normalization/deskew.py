"""
shared/normalization/deskew.py
--------------------------------
Affine deskew for axis-aligned bounding boxes.

Used as the normalization fallback when geometry_type != "quadrilateral".
Phase 12 may refine the angle-estimation heuristic; the affine warp mechanics
remain the same.

Exported:
    compute_deskew_angle — derive skew angle (degrees) from 4 corner points
    apply_affine_deskew  — apply affine rotation and crop to a bbox region
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import cv2
import numpy as np


def compute_deskew_angle(corners: Sequence[tuple[float, float]]) -> float:
    """
    Estimate the skew angle (degrees) from a set of 4 corner points.

    Uses the mean of the two near-horizontal edge angles (top pair and bottom
    pair).  Returns a value in (-90, 90] degrees; typically small for real
    documents.

    Args:
        corners: 4 (x, y) corner coordinates in any order

    Returns:
        Estimated skew angle in degrees.
    """
    # Sort by y ascending; split into top/bottom pairs, each sorted by x
    pts = sorted(corners, key=lambda p: p[1])
    top_l, top_r = sorted(pts[:2], key=lambda p: p[0])
    bot_l, bot_r = sorted(pts[2:], key=lambda p: p[0])

    angle_top = math.degrees(math.atan2(top_r[1] - top_l[1], top_r[0] - top_l[0]))
    angle_bot = math.degrees(math.atan2(bot_r[1] - bot_l[1], bot_r[0] - bot_l[0]))

    return (angle_top + angle_bot) / 2.0


def apply_affine_deskew(
    image: np.ndarray,
    angle_deg: float,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, float]:
    """
    Apply affine rotation to correct skew, then crop to the bbox region.

    Rotation is around the image centre with BORDER_REPLICATE fill to avoid
    black border artefacts.  The bbox is clamped to image dimensions before
    cropping.

    Args:
        image:     H×W×C (or H×W) uint8 ndarray
        angle_deg: skew angle to correct in degrees (positive = CCW in OpenCV)
        bbox:      (x_min, y_min, x_max, y_max) crop region in original coords

    Returns:
        (cropped, residual_deg) where:
            cropped      — deskewed and cropped output image ndarray
            residual_deg — remaining skew after correction (placeholder 0.0;
                           real computation deferred to Packet 2.7)
    """
    h, w = image.shape[:2]
    cx, cy = w / 2.0, h / 2.0

    rotation_matrix = cv2.getRotationMatrix2D((cx, cy), angle_deg, 1.0)
    rotated = cv2.warpAffine(
        image, rotation_matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )

    # Clamp bbox to image bounds
    x_min = max(0, int(round(bbox[0])))
    y_min = max(0, int(round(bbox[1])))
    x_max = min(w, int(round(bbox[2])))
    y_max = min(h, int(round(bbox[3])))

    # Ensure non-empty crop
    if x_max <= x_min:
        x_max = min(w, x_min + 1)
    if y_max <= y_min:
        y_max = min(h, y_min + 1)

    cropped = rotated[y_min:y_max, x_min:x_max]

    # Residual is placeholder 0.0; real computation deferred to Packet 2.7
    residual_deg = 0.0

    return cropped, residual_deg
