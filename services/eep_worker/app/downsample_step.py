"""
services/eep_worker/app/downsample_step.py
-------------------------------------------
Pre-IEP0 downsampling stage.

Produces a downsampled TIFF artifact from the original full-resolution image
for use by the future IEP0 material-type classifier and optionally IEP2.

Behavior (mirrors offline training logic):
  - Preserves aspect ratio.
  - Resizes with LANCZOS4 when any dimension exceeds max_dimension_px (default 2096).
  - Saves output as TIFF using OpenCV.
  - The original artifact at source_artifact_uri is never touched.

Exported:
    DownsampleResult    — metadata produced by this stage
    run_downsample_step — main entry point
"""

from __future__ import annotations

import dataclasses
import logging
import time

import cv2
import numpy as np

from shared.io.storage import StorageBackend

__all__ = ["DownsampleResult", "run_downsample_step"]

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DIMENSION_PX: int = 2096


@dataclasses.dataclass(frozen=True)
class DownsampleResult:
    """
    Metadata produced by the pre-IEP0 downsampling stage.

    All fields needed by later stages (IEP0, IEP2) to reference and interpret
    the downsampled artifact.

    Attributes:
        source_artifact_uri:      URI of the original uploaded image (read-only input).
        downsampled_artifact_uri: URI where the downsampled TIFF was written.
        original_width:           Width of the source image in pixels.
        original_height:          Height of the source image in pixels.
        downsampled_width:        Width of the downsampled image in pixels.
        downsampled_height:       Height of the downsampled image in pixels.
        scale_factor:             Ratio applied to both dimensions (1.0 if no resize needed).
        processing_time_ms:       Wall-clock time for this stage in milliseconds.
    """

    source_artifact_uri: str
    downsampled_artifact_uri: str
    original_width: int
    original_height: int
    downsampled_width: int
    downsampled_height: int
    scale_factor: float
    processing_time_ms: float


def _encode_tiff(image: np.ndarray) -> bytes:
    """Encode a numpy BGR array to TIFF bytes via OpenCV."""
    success, buf = cv2.imencode(".tiff", image)
    if not success:
        raise ValueError("cv2.imencode(.tiff) failed: could not encode image to TIFF bytes")
    return bytes(buf.tobytes())


def run_downsample_step(
    *,
    full_res_image: np.ndarray,
    source_artifact_uri: str,
    output_uri: str,
    storage: StorageBackend,
    max_dimension_px: int = _DEFAULT_MAX_DIMENSION_PX,
) -> DownsampleResult:
    """
    Downsample the full-resolution image and write it as a TIFF artifact.

    If either dimension of *full_res_image* exceeds *max_dimension_px*, the
    image is resized (aspect ratio preserved) using LANCZOS4 interpolation,
    then saved as TIFF.  Otherwise it is saved at original resolution.

    The source artifact at *source_artifact_uri* is never touched.

    Args:
        full_res_image:      H×W×C uint8 BGR numpy array from decode_otiff().
        source_artifact_uri: URI of the original image (metadata only; never read or
                             written here).
        output_uri:          Destination URI for the downsampled TIFF artifact.
        storage:             StorageBackend (caller owns lifecycle).
        max_dimension_px:    Maximum pixel length of any single dimension.
                             Default: 2096.

    Returns:
        DownsampleResult with all required metadata fields populated.

    Raises:
        ValueError: If TIFF encoding fails (cv2.imencode returns False).
        Exception:  Any exception from storage.put_bytes() propagates unchanged.
    """
    t0 = time.monotonic()

    orig_h, orig_w = full_res_image.shape[:2]
    scale = min(max_dimension_px / orig_w, max_dimension_px / orig_h, 1.0)

    if scale < 1.0:
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        downsampled = cv2.resize(full_res_image, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    else:
        new_w, new_h = orig_w, orig_h
        downsampled = full_res_image

    tiff_bytes = _encode_tiff(downsampled)
    storage.put_bytes(output_uri, tiff_bytes)

    duration_ms = (time.monotonic() - t0) * 1000.0

    logger.info(
        "downsample_step: artifact written orig=%dx%d -> %dx%d scale=%.4f uri=%s ms=%.1f",
        orig_w,
        orig_h,
        new_w,
        new_h,
        scale,
        output_uri,
        duration_ms,
    )

    return DownsampleResult(
        source_artifact_uri=source_artifact_uri,
        downsampled_artifact_uri=output_uri,
        original_width=orig_w,
        original_height=orig_h,
        downsampled_width=new_w,
        downsampled_height=new_h,
        scale_factor=round(scale, 6),
        processing_time_ms=round(duration_ms, 2),
    )
