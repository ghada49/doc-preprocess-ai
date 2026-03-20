"""
shared/normalization/perspective.py
------------------------------------
Perspective correction via four-point quadrilateral warp.

Uses cv2.getPerspectiveTransform + cv2.warpPerspective to map an arbitrary
quadrilateral region of the source image to an axis-aligned rectangle.

Exported:
    four_point_transform — warp a quadrilateral region to a rectangle
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np


def four_point_transform(
    image: np.ndarray,
    corners: Sequence[tuple[float, float]],
) -> tuple[np.ndarray, tuple[float, float, float, float], np.ndarray]:
    """
    Apply a perspective transform to straighten a quadrilateral region.

    Args:
        image:   H×W×C or H×W uint8 ndarray in full-resolution coordinates
        corners: exactly 4 (x, y) corner coordinates in image space; may be
                 in any order (top-left, top-right, bottom-right, bottom-left
                 is re-established internally)

    Returns:
        (warped, source_bbox, homography_matrix) where:
            warped            — perspective-corrected output image ndarray
            source_bbox       — (x_min, y_min, x_max, y_max) bounding box of
                                the input corners in source-image space
            homography_matrix — 3×3 float64 homography from source → warped
    """
    pts = np.array(corners, dtype="float32")

    # Bounding box of the source corners
    x_min = float(pts[:, 0].min())
    y_min = float(pts[:, 1].min())
    x_max = float(pts[:, 0].max())
    y_max = float(pts[:, 1].max())
    source_bbox: tuple[float, float, float, float] = (x_min, y_min, x_max, y_max)

    # Output rectangle dimensions (at least 1×1)
    width = max(1, int(round(x_max - x_min)))
    height = max(1, int(round(y_max - y_min)))

    # Order source corners: top-left, top-right, bottom-right, bottom-left
    rect = _order_corners(pts)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype="float32",
    )

    homography = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, homography, (width, height))

    return warped, source_bbox, homography


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """
    Re-order 4 corner points into (top-left, top-right, bottom-right, bottom-left).

    Uses the sum (x+y) and difference (x-y) of coordinates to identify corners
    regardless of input order.
    """
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)  # x+y per point
    diff = np.diff(pts, axis=1)  # x-y per point

    rect[0] = pts[np.argmin(s)]  # top-left:     smallest x+y
    rect[2] = pts[np.argmax(s)]  # bottom-right: largest  x+y
    rect[1] = pts[np.argmin(diff)]  # top-right:    smallest x-y
    rect[3] = pts[np.argmax(diff)]  # bottom-left:  largest  x-y
    return rect
