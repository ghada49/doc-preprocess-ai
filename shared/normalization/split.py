"""
shared/normalization/split.py
-------------------------------
IEP1C split normalization path.

When split_required=True the full-resolution image is divided at split_x and
each half is normalized independently via normalize_single_page.

Coordinate convention:
  Left child  (sub_page_index=0): geometry.pages[0]; x-coordinates map
              directly into the left-half image (x in [0, split_x)).
  Right child (sub_page_index=1): geometry.pages[1]; x-coordinates are in
              full-image space, so split_x is subtracted before passing to
              normalize_single_page so they map into the right-half image.

Exported:
    split_and_normalize — normalize both children of a spread image
"""

from __future__ import annotations

import numpy as np

from shared.normalization.normalize import NormalizeResult, normalize_single_page
from shared.schemas.geometry import GeometryResponse, PageRegion


def split_and_normalize(
    image: np.ndarray,
    geometry: GeometryResponse,
) -> tuple[NormalizeResult, NormalizeResult]:
    """
    Split a spread image at split_x and normalize each child independently.

    Args:
        image:    H×W×C (or H×W) uint8 numpy array in full-resolution space.
                  Geometry coordinates must already be scaled to match image
                  dimensions.
        geometry: GeometryResponse with split_required=True and exactly 2
                  pages.  pages[0] = left child, pages[1] = right child.

    Returns:
        (left_result, right_result) — one NormalizeResult per child page,
        ordered left then right.

    Raises:
        ValueError: if split_required is False, split_x is None, or
                    geometry.pages does not have exactly 2 entries.
    """
    if not geometry.split_required:
        raise ValueError("split_and_normalize requires geometry.split_required=True")
    if geometry.split_x is None:
        raise ValueError("split_and_normalize requires geometry.split_x to be set")
    if len(geometry.pages) != 2:
        raise ValueError(f"split_and_normalize requires exactly 2 pages; got {len(geometry.pages)}")

    split_x = geometry.split_x

    # Slice the image along the column axis
    left_image: np.ndarray = image[:, :split_x]
    right_image: np.ndarray = image[:, split_x:]

    left_page = geometry.pages[0]
    # Right-child coordinates are in full-image space; shift into right-half space
    right_page = _shift_page_x(geometry.pages[1], -split_x)

    left_result = normalize_single_page(left_image, left_page, geometry)
    right_result = normalize_single_page(right_image, right_page, geometry)

    return left_result, right_result


def _shift_page_x(page: PageRegion, dx: int) -> PageRegion:
    """
    Return a new PageRegion with all x-coordinates shifted by dx.

    Used to convert right-child coordinates from full-image space into
    right-half image space (dx = -split_x).
    """
    new_corners: list[tuple[float, float]] | None = None
    if page.corners is not None:
        new_corners = [(x + dx, y) for x, y in page.corners]

    new_bbox: tuple[int, int, int, int] | None = None
    if page.bbox is not None:
        x_min, y_min, x_max, y_max = page.bbox
        new_bbox = (x_min + dx, y_min, x_max + dx, y_max)

    return PageRegion(
        region_id=page.region_id,
        geometry_type=page.geometry_type,
        corners=new_corners,
        bbox=new_bbox,
        confidence=page.confidence,
        page_area_fraction=page.page_area_fraction,
    )
