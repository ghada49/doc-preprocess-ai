"""
shared/normalization/normalize.py
-----------------------------------
IEP1C normalization orchestrator for a single page region.

Callers (EEP Phase 4 worker) are responsible for:
  - loading the full-resolution image as a numpy array
  - scaling geometry coordinates from proxy-image space to full-res space
    before calling normalize_single_page
  - writing the result image to storage and constructing processed_image_uri

Exported:
    NormalizeResult       — normalization output dataclass
    normalize_single_page — main normalization entry point
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from shared.normalization.deskew import apply_affine_deskew, compute_deskew_angle
from shared.normalization.perspective import four_point_transform
from shared.normalization.quality import compute_quality_metrics
from shared.schemas.geometry import GeometryResponse, PageRegion
from shared.schemas.preprocessing import CropResult, DeskewResult, QualityMetrics, SplitResult
from shared.schemas.ucf import BoundingBox, Dimensions, TransformRecord


@dataclass
class NormalizeResult:
    """
    Output of normalize_single_page().

    Fields:
        image              — normalized output image (H×W×C uint8 ndarray)
        deskew             — deskew operation record
        crop               — crop operation record
        split              — split metadata derived from the selected geometry
        quality            — artifact quality metrics
        transform          — full geometric transform record
        warnings           — advisory messages
        processing_time_ms — wall-clock elapsed time in ms
    """

    image: np.ndarray
    deskew: DeskewResult
    crop: CropResult
    split: SplitResult
    quality: QualityMetrics
    transform: TransformRecord
    warnings: list[str] = field(default_factory=list)
    processing_time_ms: float = 0.0


def normalize_single_page(
    image: np.ndarray,
    page: PageRegion,
    geometry: GeometryResponse,
) -> NormalizeResult:
    """
    Normalize a single page region from a full-resolution image array.

    Applies perspective correction when corners are available
    (geometry_type == "quadrilateral"), or falls back to affine deskew when
    only a bbox is available (geometry_type "bbox" / "mask_ref").
    Quality metrics are computed on the normalized output image.

    Args:
        image:    H×W×C (or H×W) uint8 numpy array in full-resolution space.
                  Geometry coordinates must already be scaled to match image
                  dimensions.
        page:     PageRegion from the selected GeometryResponse for this page
        geometry: full GeometryResponse (provides split metadata)

    Returns:
        NormalizeResult with normalized image + all metadata records.
    """
    t0 = time.monotonic()
    warnings: list[str] = []

    src_h, src_w = image.shape[:2]
    original_dims = Dimensions(width=src_w, height=src_h)

    # ── Choose normalization path ──────────────────────────────────────────────
    if page.geometry_type == "quadrilateral" and page.corners:
        result_image, source_bbox, _ = four_point_transform(image, page.corners)
        angle_deg = compute_deskew_angle(page.corners)
        x_min, y_min, x_max, y_max = source_bbox
        method = "geometry_quad"
    else:
        # Bbox fallback (geometry_type "bbox" or "mask_ref", or no corners)
        if page.bbox is not None:
            x_min = float(page.bbox[0])
            y_min = float(page.bbox[1])
            x_max = float(page.bbox[2])
            y_max = float(page.bbox[3])
        else:
            # Degenerate: no geometry at all — use full image extent
            x_min, y_min = 0.0, 0.0
            x_max, y_max = float(src_w), float(src_h)
            warnings.append("no geometry available; using full image extent")

        angle_deg = 0.0
        result_image, _ = apply_affine_deskew(image, angle_deg, (x_min, y_min, x_max, y_max))
        method = "geometry_bbox"

    # ── Quality metrics on the normalized output ───────────────────────────────
    qm = compute_quality_metrics(result_image)

    # ── Clamp crop box to source image bounds ─────────────────────────────────
    x_min_c = max(0.0, x_min)
    y_min_c = max(0.0, y_min)
    x_max_c = min(float(src_w), x_max)
    y_max_c = min(float(src_h), y_max)

    # Guard: ensure non-degenerate box (BoundingBox requires x_min < x_max)
    if x_max_c <= x_min_c:
        x_max_c = min(float(src_w), x_min_c + 1.0)
    if y_max_c <= y_min_c:
        y_max_c = min(float(src_h), y_min_c + 1.0)

    out_h, out_w = result_image.shape[:2]
    crop_box = BoundingBox(x_min=x_min_c, y_min=y_min_c, x_max=x_max_c, y_max=y_max_c)
    transform = TransformRecord(
        original_dimensions=original_dims,
        crop_box=crop_box,
        deskew_angle_deg=angle_deg,
        post_preprocessing_dimensions=Dimensions(width=out_w, height=out_h),
    )

    # ── Split metadata ─────────────────────────────────────────────────────────
    # split_confidence = min(weakest instance confidence, TTA agreement rate)
    split_confidence: float | None = None
    if geometry.split_required:
        split_confidence = min(page.confidence, geometry.tta_structural_agreement_rate)

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    return NormalizeResult(
        image=result_image,
        deskew=DeskewResult(
            angle_deg=angle_deg,
            residual_deg=qm.skew_residual,
            method=method,
        ),
        crop=CropResult(
            crop_box=crop_box,
            border_score=qm.border_score,
            method=method,
        ),
        split=SplitResult(
            split_required=geometry.split_required,
            split_x=geometry.split_x,
            split_confidence=split_confidence,
            method="instance_boundary" if geometry.split_required else "none",
        ),
        quality=QualityMetrics(
            skew_residual=qm.skew_residual,
            blur_score=qm.blur_score,
            border_score=qm.border_score,
            split_confidence=split_confidence,
            foreground_coverage=qm.foreground_coverage,
        ),
        transform=transform,
        warnings=warnings,
        processing_time_ms=elapsed_ms,
    )
