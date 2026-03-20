"""
tests/test_p2_quality.py
-------------------------
Contract tests for Packet 2.7: shared/normalization/quality.py.

Tests cover:
  - compute_blur_score
  - compute_border_score
  - compute_foreground_coverage
  - compute_skew_residual
  - compute_quality_metrics (wrapper)

Uses synthetic numpy images to exercise predictable code paths.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from shared.normalization.quality import (
    QualityMetricsResult,
    compute_blur_score,
    compute_border_score,
    compute_foreground_coverage,
    compute_quality_metrics,
    compute_skew_residual,
)

# ── Image helpers ──────────────────────────────────────────────────────────────


def _uniform(h: int = 200, w: int = 300, value: int = 128) -> np.ndarray:
    """Uniform gray H×W uint8 image."""
    return np.full((h, w), value, dtype=np.uint8)


def _uniform_color(h: int = 200, w: int = 300, value: int = 128) -> np.ndarray:
    """Uniform gray H×W×3 uint8 image."""
    return np.full((h, w, 3), value, dtype=np.uint8)


def _checkerboard(h: int = 200, w: int = 200, cell: int = 20) -> np.ndarray:
    """Black-and-white checkerboard H×W uint8 image."""
    img = np.zeros((h, w), dtype=np.uint8)
    for i in range(h // cell):
        for j in range(w // cell):
            if (i + j) % 2 == 0:
                img[i * cell : (i + 1) * cell, j * cell : (j + 1) * cell] = 255
    return img


def _white_with_dark_center(h: int = 200, w: int = 300) -> np.ndarray:
    """White image with a dark rectangle in the centre."""
    img = np.full((h, w), 240, dtype=np.uint8)
    cy, cx = h // 2, w // 2
    img[cy - 30 : cy + 30, cx - 60 : cx + 60] = 20
    return img


def _dark_border_image(h: int = 200, w: int = 300) -> np.ndarray:
    """White interior with a dark border strip (simulates bad crop)."""
    img = np.full((h, w), 240, dtype=np.uint8)
    img[:15, :] = 10  # dark top strip
    img[h - 15 :, :] = 10  # dark bottom strip
    return img


def _horizontal_lines(h: int = 300, w: int = 500) -> np.ndarray:
    """White image with evenly spaced horizontal black lines."""
    img = np.full((h, w), 255, dtype=np.uint8)
    for y in range(20, h, 40):
        img[y : y + 4, :] = 0
    return img


# ── TestComputeBlurScore ───────────────────────────────────────────────────────


class TestComputeBlurScore:
    """Tests for compute_blur_score."""

    def test_uniform_image_returns_zero(self) -> None:
        assert compute_blur_score(_uniform()) == pytest.approx(0.0)

    def test_uniform_color_image_returns_zero(self) -> None:
        assert compute_blur_score(_uniform_color()) == pytest.approx(0.0)

    def test_checkerboard_returns_positive(self) -> None:
        assert compute_blur_score(_checkerboard()) > 0.0

    def test_result_in_unit_range(self) -> None:
        score = compute_blur_score(_checkerboard())
        assert 0.0 <= score <= 1.0

    def test_blurred_image_lower_than_sharp(self) -> None:
        sharp = _checkerboard(200, 200, cell=10)
        blurred = cv2.GaussianBlur(sharp, (15, 15), 5)
        assert compute_blur_score(sharp) > compute_blur_score(blurred)

    def test_grayscale_input_accepted(self) -> None:
        score = compute_blur_score(_uniform(100, 100, value=200))
        assert isinstance(score, float)

    def test_white_image_returns_zero(self) -> None:
        assert compute_blur_score(_uniform(100, 100, value=255)) == pytest.approx(0.0)

    def test_black_image_returns_zero(self) -> None:
        assert compute_blur_score(_uniform(100, 100, value=0)) == pytest.approx(0.0)


# ── TestComputeBorderScore ─────────────────────────────────────────────────────


class TestComputeBorderScore:
    """Tests for compute_border_score."""

    def test_uniform_image_returns_one(self) -> None:
        # Uniform border → std = 0 → score = 1.0
        assert compute_border_score(_uniform()) == pytest.approx(1.0)

    def test_dark_border_returns_lower_than_one(self) -> None:
        # Non-uniform border (dark strips) → std > 0 → score < 1.0
        assert compute_border_score(_dark_border_image()) < 1.0

    def test_dark_border_lower_than_uniform(self) -> None:
        assert compute_border_score(_dark_border_image()) < compute_border_score(_uniform())

    def test_result_in_unit_range(self) -> None:
        score = compute_border_score(_dark_border_image())
        assert 0.0 <= score <= 1.0

    def test_color_image_accepted(self) -> None:
        score = compute_border_score(_uniform_color())
        assert isinstance(score, float)
        assert score == pytest.approx(1.0)

    def test_very_noisy_border_near_zero(self) -> None:
        # Image where every other border pixel alternates 0/255 → high std
        img = np.full((200, 300), 128, dtype=np.uint8)
        # Make the top strip alternate 0/255
        img[:10, ::2] = 0
        img[:10, 1::2] = 255
        score = compute_border_score(img)
        assert score < 0.5


# ── TestComputeForegroundCoverage ──────────────────────────────────────────────


class TestComputeForegroundCoverage:
    """Tests for compute_foreground_coverage."""

    def test_uniform_image_returns_zero(self) -> None:
        # Uniform → min == max → special-cased to 0.0
        assert compute_foreground_coverage(_uniform()) == pytest.approx(0.0)

    def test_all_white_returns_zero(self) -> None:
        assert compute_foreground_coverage(_uniform(value=255)) == pytest.approx(0.0)

    def test_dark_center_returns_positive(self) -> None:
        assert compute_foreground_coverage(_white_with_dark_center()) > 0.0

    def test_result_in_unit_range(self) -> None:
        score = compute_foreground_coverage(_white_with_dark_center())
        assert 0.0 <= score <= 1.0

    def test_color_image_accepted(self) -> None:
        score = compute_foreground_coverage(_uniform_color())
        assert isinstance(score, float)

    def test_mostly_dark_image_high_coverage(self) -> None:
        # Mostly dark image on a white image → most pixels classified as foreground
        img = np.full((200, 300), 240, dtype=np.uint8)
        img[10:190, 10:290] = 10  # large dark region
        coverage = compute_foreground_coverage(img)
        assert coverage > 0.5


# ── TestComputeSkewResidual ────────────────────────────────────────────────────


class TestComputeSkewResidual:
    """Tests for compute_skew_residual."""

    def test_uniform_image_returns_zero(self) -> None:
        # No edges → no Hough lines → 0.0
        assert compute_skew_residual(_uniform()) == pytest.approx(0.0)

    def test_returns_nonnegative(self) -> None:
        assert compute_skew_residual(_checkerboard()) >= 0.0

    def test_horizontal_lines_returns_near_zero(self) -> None:
        # Perfectly horizontal lines → deviation from horizontal ≈ 0°.
        # Allow up to 3° for Hough 1°-resolution quantisation artefacts.
        residual = compute_skew_residual(_horizontal_lines())
        assert residual < 3.0

    def test_returns_float(self) -> None:
        assert isinstance(compute_skew_residual(_uniform()), float)

    def test_color_image_accepted(self) -> None:
        assert isinstance(compute_skew_residual(_uniform_color()), float)


# ── TestComputeQualityMetrics ──────────────────────────────────────────────────


class TestComputeQualityMetrics:
    """Tests for compute_quality_metrics (wrapper)."""

    def test_returns_quality_metrics_result(self) -> None:
        result = compute_quality_metrics(_uniform())
        assert isinstance(result, QualityMetricsResult)

    def test_blur_score_in_range(self) -> None:
        r = compute_quality_metrics(_checkerboard())
        assert 0.0 <= r.blur_score <= 1.0

    def test_border_score_in_range(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert 0.0 <= r.border_score <= 1.0

    def test_foreground_coverage_in_range(self) -> None:
        r = compute_quality_metrics(_white_with_dark_center())
        assert 0.0 <= r.foreground_coverage <= 1.0

    def test_skew_residual_nonnegative(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert r.skew_residual >= 0.0

    def test_uniform_blur_is_zero(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert r.blur_score == pytest.approx(0.0)

    def test_uniform_border_is_one(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert r.border_score == pytest.approx(1.0)

    def test_uniform_foreground_is_zero(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert r.foreground_coverage == pytest.approx(0.0)

    def test_uniform_skew_is_zero(self) -> None:
        r = compute_quality_metrics(_uniform())
        assert r.skew_residual == pytest.approx(0.0)

    def test_color_image_accepted(self) -> None:
        r = compute_quality_metrics(_uniform_color())
        assert isinstance(r, QualityMetricsResult)
