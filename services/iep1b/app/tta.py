"""
services/iep1b/app/tta.py
--------------------------
TTA (Test-Time Augmentation) mock for IEP1B.

In Phase 2 this module provides deterministic, configurable mock TTA statistics.
Phase 12 replaces this with real TTA augmentation passes over the proxy image.

Real TTA procedure (deferred to Phase 12):
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

Uncertainty flags are populated automatically based on fixed conservative
thresholds (spec Section 1 threshold-derivation rule; calibration from real
validation data deferred to Phase 9):
  "low_structural_agreement"  — when tta_structural_agreement_rate < 0.80
  "high_prediction_variance"  — when tta_prediction_variance > 0.10
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Conservative fixed thresholds (spec Section 1 threshold-derivation rule).
# Calibrated values will be derived from held-out AUB validation data in Phase 9.
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
    """

    structural_agreement_rate: float
    prediction_variance: float
    uncertainty_flags: list[str] = field(default_factory=list)


def compute_mock_tta_stats(n_passes: int) -> TTAStats:  # noqa: ARG001
    """
    Return mock TTA statistics for n_passes augmentation passes.

    In Phase 2 this reads env vars rather than running real augmentation.
    Phase 12 replaces this body with actual per-pass YOLOv8-pose inference
    and statistics computation over augmented proxy images.

    Args:
        n_passes: number of TTA passes performed (>= 1); informational only
                  in the mock — does not affect the returned statistics.

    Returns:
        TTAStats with configurable agreement rate, variance, and derived
        uncertainty flags.
    """
    agreement_rate = float(os.environ.get("IEP1B_MOCK_TTA_AGREEMENT_RATE", "1.0"))
    variance = float(os.environ.get("IEP1B_MOCK_TTA_VARIANCE", "0.001"))

    flags: list[str] = []
    if agreement_rate < _LOW_AGREEMENT_THRESHOLD:
        flags.append("low_structural_agreement")
    if variance > _HIGH_VARIANCE_THRESHOLD:
        flags.append("high_prediction_variance")

    return TTAStats(
        structural_agreement_rate=agreement_rate,
        prediction_variance=variance,
        uncertainty_flags=flags,
    )
