"""services.eep.app.gates.artifact_validation
--------------------------------------------
Artifact validation gate for the IEP1 preprocessing pipeline.

Implements artifact validation as defined in spec Section 6.9.

Packet 3.4: hard requirements — five checks that must all pass before scoring.
Packet 3.5: soft signal scoring — weighted score + threshold + gate log record.

Exported (Packet 3.4):
    ArtifactImageDimensions    — (width, height) carrier returned by image_loader
    ArtifactHardCheckResult    — result of the five hard requirement checks
    ARTIFACT_HARD_CHECK_NAMES  — canonical names for the five hard checks
    check_artifact_hard_requirements — run all five hard checks against one artifact
    make_cv2_image_loader      — production image loader factory (cv2 / OpenCV)

Exported (Packet 3.5):
    ArtifactValidationResult   — combined hard + soft validation result
    compute_artifact_soft_score — compute per-signal and combined weighted score
    run_artifact_validation    — orchestrate hard checks then soft scoring
    build_artifact_gate_log_record — build quality_gate_log insertion dict (no DB write)
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from services.eep.app.gates.geometry_selection import PreprocessingGateConfig
from shared.schemas.geometry import GeometryResponse
from shared.schemas.preprocessing import PreprocessBranchResponse, QualityMetrics

# ---------------------------------------------------------------------------
# Image dimensions carrier
# ---------------------------------------------------------------------------


@dataclass
class ArtifactImageDimensions:
    """
    Width and height of a decoded artifact image.

    Returned by the image_loader callable passed to
    check_artifact_hard_requirements.

    The image_loader contract:
      - Return ArtifactImageDimensions on success.
      - Raise FileNotFoundError if the URI does not resolve to a readable file.
      - Raise any other exception (ValueError, OSError, etc.) if the file
        exists but cannot be decoded as a valid image.
    """

    width: int
    height: int


# ---------------------------------------------------------------------------
# Hard check names and result
# ---------------------------------------------------------------------------

#: Canonical names for the five artifact hard checks (spec Section 6.9).
ARTIFACT_HARD_CHECK_NAMES: tuple[str, ...] = (
    "file_exists",
    "valid_image",
    "non_degenerate",
    "bounds_consistent",
    "dimensions_consistent",
)


@dataclass
class ArtifactHardCheckResult:
    """
    Result of running all five hard requirement checks against one artifact.

    Attributes:
        passed        — True only if ALL five checks pass.
        failed_checks — list of canonical check names that failed (empty when
                        passed=True).  Suitable for storage in quality_gate_log
                        JSONB column sanity_check_results.
    """

    passed: bool
    failed_checks: list[str]

    def as_dict(self) -> dict[str, object]:
        """Serialize to a plain dict suitable for JSONB storage."""
        return {"passed": self.passed, "failed_checks": list(self.failed_checks)}


# ---------------------------------------------------------------------------
# Hard check implementation  (spec Section 6.9 "Hard Requirements")
# ---------------------------------------------------------------------------


def check_artifact_hard_requirements(
    response: PreprocessBranchResponse,
    image_loader: Callable[[str], ArtifactImageDimensions],
    dimension_tolerance: int = 2,
) -> ArtifactHardCheckResult:
    """
    Apply all five hard requirement checks to a single normalized artifact.

    Any failure → artifact is invalid; soft scoring must not be performed.

    The five checks (spec Section 6.9 "Hard Requirements"):
      1. file_exists          — URI resolves to a readable file.
      2. valid_image          — file decodes as a valid image without error.
      3. non_degenerate       — decoded image width > 0 and height > 0.
      4. bounds_consistent    — crop box coordinates within original image bounds.
      5. dimensions_consistent — actual artifact dimensions match the expected
                                 post_preprocessing_dimensions (within tolerance).

    Checks 1–3 require the image to be loaded.  If the file is missing (check 1
    fails), the function returns early — no further checks are possible.  If the
    file exists but cannot be decoded (check 2 fails), checks 3 and 5 are skipped
    because they require valid dimensions; check 4 is always evaluated since it
    is data-only.

    Args:
        response            — PreprocessBranchResponse from IEP1C normalization
        image_loader        — callable ``(uri) → ArtifactImageDimensions``; must
                              raise ``FileNotFoundError`` for missing files and
                              any other exception for decode failures
        dimension_tolerance — allowed pixel difference (per axis) between actual
                              and expected post_preprocessing_dimensions; accounts
                              for TIFF rounding.  Default: 2 pixels.
    """
    failed: list[str] = []
    dims: ArtifactImageDimensions | None = None

    # -------------------------------------------------------------------
    # Checks 1 & 2: File exists + valid image
    # -------------------------------------------------------------------
    try:
        dims = image_loader(response.processed_image_uri)
    except FileNotFoundError:
        failed.append("file_exists")
        # Cannot continue — no file to inspect for remaining checks.
        return ArtifactHardCheckResult(passed=False, failed_checks=failed)
    except Exception:
        # File is accessible but cannot be decoded as a valid image.
        failed.append("valid_image")

    # -------------------------------------------------------------------
    # Check 3: Non-degenerate dimensions
    # -------------------------------------------------------------------
    if dims is not None:
        if not (dims.width > 0 and dims.height > 0):
            failed.append("non_degenerate")

    # -------------------------------------------------------------------
    # Check 4: Bounds consistency  (data-only — no I/O needed)
    # Crop box must lie within the original image dimensions recorded in the
    # TransformRecord.  This is already enforced by the Pydantic schema
    # validator; the explicit gate check is a defense-in-depth redundancy.
    # -------------------------------------------------------------------
    crop = response.transform.crop_box
    orig = response.transform.original_dimensions
    if not (
        crop.x_min >= 0
        and crop.y_min >= 0
        and crop.x_max <= orig.width
        and crop.y_max <= orig.height
    ):
        failed.append("bounds_consistent")

    # -------------------------------------------------------------------
    # Check 5: Dimension consistency
    # The actual decoded image dimensions must match post_preprocessing_dimensions
    # within the rounding tolerance.  Only performed when we have valid dims
    # (check 2 passed) and those dims are non-degenerate (check 3 passed).
    # -------------------------------------------------------------------
    if dims is not None and "non_degenerate" not in failed:
        expected = response.transform.post_preprocessing_dimensions
        if not (
            abs(dims.width - expected.width) <= dimension_tolerance
            and abs(dims.height - expected.height) <= dimension_tolerance
        ):
            failed.append("dimensions_consistent")

    return ArtifactHardCheckResult(passed=len(failed) == 0, failed_checks=failed)


# ---------------------------------------------------------------------------
# Production image loader factory
# ---------------------------------------------------------------------------


def make_cv2_image_loader(
    storage: object,
) -> Callable[[str], ArtifactImageDimensions]:
    """
    Build an image_loader callable backed by a StorageBackend and OpenCV.

    The returned callable reads raw bytes from the storage backend and decodes
    them with cv2.imdecode.  TIFF and common raster formats are supported.

    Args:
        storage — a StorageBackend instance (shared.io.storage); must expose
                  ``get_bytes(uri: str) -> bytes``.

    Returns:
        Callable[[str], ArtifactImageDimensions] suitable for passing to
        check_artifact_hard_requirements.

    Raises:
        ImportError if cv2 (opencv-python-headless) or numpy are not installed.
    """
    import cv2  # noqa: PLC0415 — deferred; not available in all environments
    import numpy as np  # noqa: PLC0415

    def _load(uri: str) -> ArtifactImageDimensions:
        try:
            data = storage.get_bytes(uri)  # type: ignore[attr-defined]
        except (FileNotFoundError, KeyError, Exception) as exc:
            if isinstance(exc, FileNotFoundError | KeyError):
                raise FileNotFoundError(uri) from exc
            raise
        buf = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"cv2.imdecode returned None for URI: {uri}")
        h, w = img.shape[:2]
        return ArtifactImageDimensions(width=w, height=h)

    return _load


# ---------------------------------------------------------------------------
# Packet 3.5 — Soft signal scoring, combined validation, gate log record
# ---------------------------------------------------------------------------


def _normalize_decreasing(value: float, good_max: float, bad_min: float) -> float:
    """
    Normalize a signal where lower values are better.

    Returns 1.0 when value ≤ good_max, 0.0 when value ≥ bad_min, and a linear
    interpolation in between.
    """
    if value <= good_max:
        return 1.0
    if value >= bad_min:
        return 0.0
    return (bad_min - value) / (bad_min - good_max)


def _normalize_increasing(value: float, bad_max: float, good_min: float) -> float:
    """
    Normalize a signal where higher values are better.

    Returns 1.0 when value ≥ good_min, 0.0 when value ≤ bad_max, and a linear
    interpolation in between.
    """
    if value >= good_min:
        return 1.0
    if value <= bad_max:
        return 0.0
    return (value - bad_max) / (good_min - bad_max)


def _normalize_range(
    value: float,
    bad_lo: float,
    good_lo: float,
    good_hi: float,
    bad_hi: float,
) -> float:
    """
    Normalize a range-bounded signal (good in [good_lo, good_hi]).

    Returns 1.0 in the good range, 0.0 at or beyond the suspicious bounds, and
    linear ramps between the bad and good boundaries on each side.
    """
    if good_lo <= value <= good_hi:
        return 1.0
    if value <= bad_lo or value >= bad_hi:
        return 0.0
    if value < good_lo:
        return (value - bad_lo) / (good_lo - bad_lo)
    # value > good_hi
    return (bad_hi - value) / (bad_hi - good_hi)


def compute_artifact_soft_score(
    quality: QualityMetrics,
    geometry: GeometryResponse | None,
    config: PreprocessingGateConfig,
) -> tuple[float, dict[str, float]]:
    """
    Compute the weighted soft validation score for a normalized artifact.

    Each signal is normalized to [0, 1] where 1.0 = good quality.  The combined
    score is the weighted mean of per-signal scores.

    When geometry is None the two geometry signals (geometry_confidence and
    tta_agreement) are omitted from the weighted sum — the score is computed
    over the four IEP1C quality signals only.

    Args:
        quality  — QualityMetrics from PreprocessBranchResponse
        geometry — selected GeometryResponse; may be None
        config   — PreprocessingGateConfig with signal normalization bounds and weights

    Returns:
        (combined_score, signal_scores) where combined_score ∈ [0.0, 1.0] and
        signal_scores maps signal name → normalized [0, 1] score.
    """
    cfg = config

    signal_scores: dict[str, float] = {}
    weights: dict[str, float] = {}

    signal_scores["skew_residual"] = _normalize_decreasing(
        quality.skew_residual,
        cfg.skew_residual_good_max,
        cfg.skew_residual_bad_min,
    )
    weights["skew_residual"] = cfg.weight_skew_residual

    signal_scores["blur_score"] = _normalize_decreasing(
        quality.blur_score,
        cfg.blur_score_good_max,
        cfg.blur_score_bad_min,
    )
    weights["blur_score"] = cfg.weight_blur_score

    signal_scores["border_score"] = _normalize_increasing(
        quality.border_score,
        cfg.border_score_bad_max,
        cfg.border_score_good_min,
    )
    weights["border_score"] = cfg.weight_border_score

    signal_scores["foreground_coverage"] = _normalize_range(
        quality.foreground_coverage,
        cfg.foreground_bad_lo,
        cfg.foreground_good_lo,
        cfg.foreground_good_hi,
        cfg.foreground_bad_hi,
    )
    weights["foreground_coverage"] = cfg.weight_foreground_coverage

    if geometry is not None:
        signal_scores["geometry_confidence"] = _normalize_increasing(
            geometry.geometry_confidence,
            cfg.geometry_confidence_bad_max,
            cfg.geometry_confidence_good_min,
        )
        weights["geometry_confidence"] = cfg.weight_geometry_confidence

        signal_scores["tta_agreement"] = _normalize_increasing(
            geometry.tta_structural_agreement_rate,
            cfg.tta_agreement_bad_max,
            cfg.tta_agreement_good_min,
        )
        weights["tta_agreement"] = cfg.weight_tta_agreement

    total_weight = sum(weights.values())
    if total_weight <= 0.0:
        combined_score = 0.0
    else:
        combined_score = sum(weights[k] * signal_scores[k] for k in signal_scores) / total_weight

    return combined_score, signal_scores


@dataclass
class ArtifactValidationResult:
    """
    Combined result of artifact validation (hard requirements + soft scoring).

    Attributes:
        hard_result   — result of the five hard requirement checks
        soft_score    — combined weighted score in [0, 1]; None when hard checks failed
        signal_scores — per-signal normalized [0, 1] scores; None when hard checks failed
        soft_passed   — True when soft_score >= threshold; None when hard checks failed
        passed        — True only when hard_result.passed AND soft_passed is True
    """

    hard_result: ArtifactHardCheckResult
    soft_score: float | None
    signal_scores: dict[str, float] | None
    soft_passed: bool | None
    passed: bool


def run_artifact_validation(
    response: PreprocessBranchResponse,
    geometry: GeometryResponse | None,
    image_loader: Callable[[str], ArtifactImageDimensions],
    config: PreprocessingGateConfig | None = None,
    dimension_tolerance: int = 2,
) -> ArtifactValidationResult:
    """
    Orchestrate artifact validation: hard requirements then soft scoring.

    Hard requirements are checked first.  If any hard check fails, the artifact
    is invalid and soft scoring is skipped (soft_score=None, passed=False).

    When hard checks pass, compute_artifact_soft_score is called and the result
    is compared against config.artifact_validation_threshold.

    Args:
        response            — PreprocessBranchResponse from IEP1C normalization
        geometry            — selected GeometryResponse; pass None when not available
        image_loader        — callable for loading artifact image dimensions
        config              — PreprocessingGateConfig; defaults to PreprocessingGateConfig()
        dimension_tolerance — pixel tolerance for dimension consistency check
    """
    if config is None:
        config = PreprocessingGateConfig()

    hard_result = check_artifact_hard_requirements(response, image_loader, dimension_tolerance)

    if not hard_result.passed:
        return ArtifactValidationResult(
            hard_result=hard_result,
            soft_score=None,
            signal_scores=None,
            soft_passed=None,
            passed=False,
        )

    soft_score, signal_scores = compute_artifact_soft_score(response.quality, geometry, config)
    soft_passed = soft_score >= config.artifact_validation_threshold

    return ArtifactValidationResult(
        hard_result=hard_result,
        soft_score=soft_score,
        signal_scores=signal_scores,
        soft_passed=soft_passed,
        passed=soft_passed,
    )


def build_artifact_gate_log_record(
    result: ArtifactValidationResult,
    job_id: str,
    page_number: int,
    gate_type: Literal["artifact_validation", "artifact_validation_final"],
    route_decision: Literal["accepted", "rectification", "pending_human_correction"],
    review_reason: str | None,
    processing_time_ms: float,
) -> dict[str, object]:
    """
    Build a dict ready for insertion into the quality_gate_log table.

    Does NOT write to the database — the caller (EEP worker, Packet 4) performs
    the actual insert.  A fresh gate_id (UUID4) is generated on each call.

    The route_decision and review_reason are provided by the caller because they
    depend on pipeline context (first validation vs. post-rectification final
    validation) which the gate itself does not know.

    Geometry columns (iep1a_geometry, iep1b_geometry, structural_agreement,
    selected_model, selection_reason, split_confidence, tta_variance) are set
    to None here — they are populated by the geometry selection gate record for
    the same page.
    """
    return {
        "gate_id": str(_uuid.uuid4()),
        "job_id": job_id,
        "page_number": page_number,
        "gate_type": gate_type,
        "iep1a_geometry": None,
        "iep1b_geometry": None,
        "structural_agreement": None,
        "selected_model": None,
        "selection_reason": None,
        "sanity_check_results": result.hard_result.as_dict(),
        "split_confidence": None,
        "tta_variance": None,
        "artifact_validation_score": result.soft_score,
        "route_decision": route_decision,
        "review_reason": review_reason,
        "processing_time_ms": int(processing_time_ms),
    }
