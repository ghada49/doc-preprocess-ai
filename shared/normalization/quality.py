"""
shared/normalization/quality.py
---------------------------------
Real quality metric computation for IEP1C normalized output images.

All metrics operate on the normalized array produced by normalize_single_page
or split_and_normalize.  Inputs are H×W×C or H×W uint8 ndarrays.

Metrics:
    blur_score          — sharpness via Laplacian variance, in [0, 1]
    border_score        — crop-border uniformity, in [0, 1]
    foreground_coverage — content fraction via Otsu thresholding, in [0, 1]
    skew_residual       — residual skew via Hough lines, in degrees (>= 0)

Calibration note: normalisation constants are conservative estimates typical
for scanned library documents.  Calibration against held-out AUB validation
data is deferred to Phase 9.

Exported:
    QualityMetricsResult   — dataclass holding all four metrics
    compute_blur_score
    compute_border_score
    compute_foreground_coverage
    compute_skew_residual
    compute_quality_metrics — convenience wrapper for all four
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# ── Calibration constants ─────────────────────────────────────────────────────

# Laplacian variance at which blur_score saturates to 1.0.
# A sharp, well-focused document scan typically reaches 500-2000.
_BLUR_SATURATE_AT: float = 2000.0

# Border strip as fraction of the shorter image dimension.
_BORDER_FRAC: float = 0.05

# Pixel-value standard-deviation of the border strip at which border_score = 0.
# A non-uniform border (e.g. includes part of an adjacent page) easily reaches 50.
_BORDER_STD_MAX: float = 50.0

# Minimum Hough accumulator votes to accept a line for skew estimation.
_HOUGH_THRESHOLD: int = 50


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QualityMetricsResult:
    """
    All four quality metrics for a normalised page image.

    Fields:
        blur_score          — sharpness signal in [0, 1]; higher = sharper
        border_score        — border uniformity in [0, 1]; higher = cleaner crop
        foreground_coverage — fraction of pixels classified as content in [0, 1]
        skew_residual       — remaining skew angle in degrees (>= 0)
    """

    blur_score: float
    border_score: float
    foreground_coverage: float
    skew_residual: float


# ── Public API ────────────────────────────────────────────────────────────────


def compute_blur_score(image: np.ndarray) -> float:
    """
    Estimate sharpness via the variance of the Laplacian.

    A sharp document (high edge contrast) produces high Laplacian variance and
    therefore a high score.  A blurry or uniform image produces a low score.

    Args:
        image: H×W×C or H×W uint8 ndarray

    Returns:
        float in [0, 1]; 1.0 = very sharp, 0.0 = completely uniform
    """
    gray = _to_gray(image)
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return min(1.0, variance / _BLUR_SATURATE_AT)


def compute_border_score(image: np.ndarray) -> float:
    """
    Estimate crop accuracy via border-strip pixel uniformity.

    A well-cropped page has a uniform border (background only).  Non-uniform
    borders suggest the crop includes content from an adjacent page or binding.

    Args:
        image: H×W×C or H×W uint8 ndarray

    Returns:
        float in [0, 1]; 1.0 = perfectly uniform borders, 0.0 = very variable
    """
    gray = _to_gray(image)
    h, w = gray.shape[:2]
    bw = max(1, int(min(h, w) * _BORDER_FRAC))

    border = np.concatenate(
        [
            gray[:bw, :].ravel(),
            gray[max(0, h - bw) :, :].ravel(),
            gray[:, :bw].ravel(),
            gray[:, max(0, w - bw) :].ravel(),
        ]
    )
    std = float(np.std(border.astype(np.float64)))
    return max(0.0, 1.0 - std / _BORDER_STD_MAX)


def compute_foreground_coverage(image: np.ndarray) -> float:
    """
    Estimate the fraction of the image covered by content (non-background).

    Uses Otsu thresholding on the grayscale image.  A uniform image (no
    variation between background and foreground) returns 0.0.

    Args:
        image: H×W×C or H×W uint8 ndarray

    Returns:
        float in [0, 1]; fraction of pixels classified as foreground
    """
    gray = _to_gray(image)
    # Otsu is ill-defined for a uniform image; return 0.0 immediately
    if int(gray.min()) == int(gray.max()):
        return 0.0
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return float(np.mean(binary > 0))


def compute_skew_residual(image: np.ndarray) -> float:
    """
    Estimate residual skew (degrees) via Hough line detection.

    Detects dominant near-horizontal lines and computes their mean angular
    deviation from horizontal.  Returns 0.0 when no lines are detected (e.g.
    blank or uniform image).

    Args:
        image: H×W×C or H×W uint8 ndarray

    Returns:
        float >= 0; estimated residual skew in degrees
    """
    gray = _to_gray(image)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, _HOUGH_THRESHOLD)
    if lines is None:
        return 0.0

    deviations: list[float] = []
    for line in lines:
        theta = float(line[0][1])
        # theta ∈ [0, π]; convert to signed angle from horizontal
        angle_deg = float(np.degrees(theta)) - 90.0
        if abs(angle_deg) <= 45.0:  # consider only near-horizontal lines
            deviations.append(abs(angle_deg))

    return float(np.mean(deviations)) if deviations else 0.0


def compute_quality_metrics(image: np.ndarray) -> QualityMetricsResult:
    """
    Compute all four quality metrics for a normalised page image.

    Args:
        image: H×W×C or H×W uint8 ndarray (normalised output)

    Returns:
        QualityMetricsResult
    """
    return QualityMetricsResult(
        blur_score=compute_blur_score(image),
        border_score=compute_border_score(image),
        foreground_coverage=compute_foreground_coverage(image),
        skew_residual=compute_skew_residual(image),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _to_gray(image: np.ndarray) -> np.ndarray:
    """Convert H×W×C to H×W grayscale; return as-is if already 2-D."""
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image
