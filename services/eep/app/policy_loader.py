"""
services/eep/app/policy_loader.py
----------------------------------
Packet 9.2 — Policy loading and threshold wiring.

Provides ``load_gate_config(db) -> PreprocessingGateConfig``.

This function reads the most recently applied policy from the
``policy_versions`` table, parses the stored YAML, and constructs a
``PreprocessingGateConfig`` populated with the policy-defined threshold
values.  Any field absent from the active policy falls back to the
``PreprocessingGateConfig`` spec defaults (spec Section 8.4).  If no
policy has been applied yet the full default config is returned.

This is the canonical place where policy YAML → gate config translation
happens.  Gate modules (geometry_selection, artifact_validation) must
never read the DB or parse YAML themselves.  Callers (EEP worker, tests)
obtain a config by calling ``load_gate_config(db)`` and pass it to
``run_geometry_selection(config=...)`` / ``run_artifact_validation(config=...)``.

Field mapping — spec Section 8.4 YAML key → PreprocessingGateConfig field
--------------------------------------------------------------------------

Direct name matches (YAML key equals dataclass field name):
  preprocessing.split_confidence_threshold     → split_confidence_threshold
  preprocessing.tta_variance_ceiling           → tta_variance_ceiling
  preprocessing.page_area_preference_threshold → page_area_preference_threshold
  preprocessing.geometry_sanity_area_min_fraction → geometry_sanity_area_min_fraction
  preprocessing.geometry_sanity_area_max_fraction → geometry_sanity_area_max_fraction
  preprocessing.artifact_validation_threshold  → artifact_validation_threshold
  preprocessing.aspect_ratio_bounds            → aspect_ratio_bounds

Spec YAML aliases (spec uses a different key name than the dataclass field):
  preprocessing.quality_blur_score_max   → blur_score_bad_min
  preprocessing.quality_border_score_min → border_score_bad_max

Soft-signal fields (not in spec Section 8.4 YAML but loadable by exact
field name for operator tuning):
  preprocessing.skew_residual_good_max, skew_residual_bad_min
  preprocessing.blur_score_good_max, blur_score_bad_min
  preprocessing.border_score_bad_max, border_score_good_min
  preprocessing.foreground_good_lo, foreground_good_hi
  preprocessing.foreground_bad_lo, foreground_bad_hi
  preprocessing.geometry_confidence_good_min, geometry_confidence_bad_max
  preprocessing.tta_agreement_good_min, tta_agreement_bad_max
  preprocessing.weight_skew_residual, weight_blur_score, weight_border_score
  preprocessing.weight_foreground_coverage, weight_geometry_confidence
  preprocessing.weight_tta_agreement

Behavioral toggle:
  preprocessing.rectification_policy — "conditional" (default) or "disabled_direct_review"
    conditional:            attempt IEP1D whenever the first pass is not acceptable.
    disabled_direct_review: skip IEP1D; route non-acceptable pages directly to review.
    Any unrecognised value falls back to "conditional".

Note: The ``layout`` section of the policy YAML is parsed by this module
but no LayoutGateConfig is returned yet — Phase 6 (layout gate) is not
implemented.  Layout config loading will be added in Phase 6.

Exported:
  load_gate_config   — load PreprocessingGateConfig from active policy
  parse_gate_config  — pure function (no DB); parse from a pre-loaded dict
"""

from __future__ import annotations

import logging
from typing import Any

import yaml  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from services.eep.app.db.models import PolicyVersion
from services.eep.app.gates.geometry_selection import (
    _DEFAULT_ASPECT_RATIO_BOUNDS,
    PreprocessingGateConfig,
)

logger = logging.getLogger(__name__)

__all__ = ["load_gate_config", "parse_gate_config"]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _current_policy_yaml(db: Session) -> str | None:
    """Return config_yaml of the most recent PolicyVersion, or None."""
    pv: PolicyVersion | None = (
        db.query(PolicyVersion).order_by(PolicyVersion.applied_at.desc()).first()
    )
    return pv.config_yaml if pv is not None else None


def _safe_float(value: Any, default: float) -> float:
    """Return float(value) if value is a real number, else default."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_aspect_ratio_bounds(
    raw: Any,
    default: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """
    Parse aspect_ratio_bounds from policy YAML value.

    Expected shape:
        book: [0.5, 2.5]
        newspaper: [0.3, 5.0]
        archival_document: [0.5, 3.0]

    Returns the default dict unchanged if the value is missing or
    malformed.  Partial overrides (only some material types present)
    are merged with the defaults so no material type is left without
    bounds.
    """
    if not isinstance(raw, dict):
        return dict(default)
    result: dict[str, tuple[float, float]] = dict(default)
    for material_type, bounds in raw.items():
        if isinstance(bounds, list | tuple) and len(bounds) == 2:
            try:
                lo = float(bounds[0])
                hi = float(bounds[1])
                result[material_type] = (lo, hi)
            except (TypeError, ValueError):
                pass  # keep default for this material type
    return result


# ── Public API ────────────────────────────────────────────────────────────────


def parse_gate_config(
    policy_config: dict[str, Any],
) -> PreprocessingGateConfig:
    """
    Construct a PreprocessingGateConfig from a pre-parsed policy dict.

    ``policy_config`` is the top-level parsed policy YAML (a dict).
    The ``preprocessing`` sub-dict is extracted and each recognised field
    is applied.  Missing fields fall back to PreprocessingGateConfig spec
    defaults.

    This is a pure function (no DB access) — useful for unit testing and
    for callers that already have the parsed config in memory.
    """
    defaults = PreprocessingGateConfig()
    pre: dict[str, Any] = policy_config.get("preprocessing") or {}

    # ── Direct name matches (spec Section 8.4 + soft-signal fields) ──────────

    split_confidence_threshold = _safe_float(
        pre.get("split_confidence_threshold"),
        defaults.split_confidence_threshold,
    )
    tta_variance_ceiling = _safe_float(
        pre.get("tta_variance_ceiling"),
        defaults.tta_variance_ceiling,
    )
    page_area_preference_threshold = _safe_float(
        pre.get("page_area_preference_threshold"),
        defaults.page_area_preference_threshold,
    )
    geometry_sanity_area_min_fraction = _safe_float(
        pre.get("geometry_sanity_area_min_fraction"),
        defaults.geometry_sanity_area_min_fraction,
    )
    geometry_sanity_area_max_fraction = _safe_float(
        pre.get("geometry_sanity_area_max_fraction"),
        defaults.geometry_sanity_area_max_fraction,
    )
    artifact_validation_threshold = _safe_float(
        pre.get("artifact_validation_threshold"),
        defaults.artifact_validation_threshold,
    )
    aspect_ratio_bounds = _parse_aspect_ratio_bounds(
        pre.get("aspect_ratio_bounds"),
        _DEFAULT_ASPECT_RATIO_BOUNDS,
    )

    # ── Spec YAML aliases (spec key ≠ dataclass field name) ──────────────────
    # quality_blur_score_max → blur_score_bad_min
    # (spec: "above this → quality poor"; dataclass: "suspicious if > this")
    blur_score_bad_min = _safe_float(
        pre.get("quality_blur_score_max") or pre.get("blur_score_bad_min"),
        defaults.blur_score_bad_min,
    )
    # quality_border_score_min → border_score_bad_max
    # (spec: "below this → quality poor"; dataclass: "suspicious if < this")
    border_score_bad_max = _safe_float(
        pre.get("quality_border_score_min") or pre.get("border_score_bad_max"),
        defaults.border_score_bad_max,
    )

    # ── Soft-signal fields (exact field name in YAML) ─────────────────────────
    skew_residual_good_max = _safe_float(
        pre.get("skew_residual_good_max"), defaults.skew_residual_good_max
    )
    skew_residual_bad_min = _safe_float(
        pre.get("skew_residual_bad_min"), defaults.skew_residual_bad_min
    )
    blur_score_good_max = _safe_float(pre.get("blur_score_good_max"), defaults.blur_score_good_max)
    border_score_good_min = _safe_float(
        pre.get("border_score_good_min"), defaults.border_score_good_min
    )
    foreground_good_lo = _safe_float(pre.get("foreground_good_lo"), defaults.foreground_good_lo)
    foreground_good_hi = _safe_float(pre.get("foreground_good_hi"), defaults.foreground_good_hi)
    foreground_bad_lo = _safe_float(pre.get("foreground_bad_lo"), defaults.foreground_bad_lo)
    foreground_bad_hi = _safe_float(pre.get("foreground_bad_hi"), defaults.foreground_bad_hi)
    geometry_confidence_good_min = _safe_float(
        pre.get("geometry_confidence_good_min"), defaults.geometry_confidence_good_min
    )
    geometry_confidence_bad_max = _safe_float(
        pre.get("geometry_confidence_bad_max"), defaults.geometry_confidence_bad_max
    )
    tta_agreement_good_min = _safe_float(
        pre.get("tta_agreement_good_min"), defaults.tta_agreement_good_min
    )
    tta_agreement_bad_max = _safe_float(
        pre.get("tta_agreement_bad_max"), defaults.tta_agreement_bad_max
    )
    weight_skew_residual = _safe_float(
        pre.get("weight_skew_residual"), defaults.weight_skew_residual
    )
    weight_blur_score = _safe_float(pre.get("weight_blur_score"), defaults.weight_blur_score)
    weight_border_score = _safe_float(pre.get("weight_border_score"), defaults.weight_border_score)
    weight_foreground_coverage = _safe_float(
        pre.get("weight_foreground_coverage"), defaults.weight_foreground_coverage
    )
    weight_geometry_confidence = _safe_float(
        pre.get("weight_geometry_confidence"), defaults.weight_geometry_confidence
    )
    weight_tta_agreement = _safe_float(
        pre.get("weight_tta_agreement"), defaults.weight_tta_agreement
    )

    # ── Behavioral toggle: rectification routing policy ───────────────────────
    _VALID_RECTIFICATION_POLICIES = {"conditional", "disabled_direct_review"}
    _raw_rectification_policy = pre.get("rectification_policy")
    rectification_policy = (
        _raw_rectification_policy
        if _raw_rectification_policy in _VALID_RECTIFICATION_POLICIES
        else defaults.rectification_policy
    )

    return PreprocessingGateConfig(
        geometry_sanity_area_min_fraction=geometry_sanity_area_min_fraction,
        geometry_sanity_area_max_fraction=geometry_sanity_area_max_fraction,
        aspect_ratio_bounds=aspect_ratio_bounds,
        split_confidence_threshold=split_confidence_threshold,
        tta_variance_ceiling=tta_variance_ceiling,
        page_area_preference_threshold=page_area_preference_threshold,
        artifact_validation_threshold=artifact_validation_threshold,
        skew_residual_good_max=skew_residual_good_max,
        skew_residual_bad_min=skew_residual_bad_min,
        blur_score_good_max=blur_score_good_max,
        blur_score_bad_min=blur_score_bad_min,
        border_score_bad_max=border_score_bad_max,
        border_score_good_min=border_score_good_min,
        foreground_good_lo=foreground_good_lo,
        foreground_good_hi=foreground_good_hi,
        foreground_bad_lo=foreground_bad_lo,
        foreground_bad_hi=foreground_bad_hi,
        geometry_confidence_good_min=geometry_confidence_good_min,
        geometry_confidence_bad_max=geometry_confidence_bad_max,
        tta_agreement_good_min=tta_agreement_good_min,
        tta_agreement_bad_max=tta_agreement_bad_max,
        weight_skew_residual=weight_skew_residual,
        weight_blur_score=weight_blur_score,
        weight_border_score=weight_border_score,
        weight_foreground_coverage=weight_foreground_coverage,
        weight_geometry_confidence=weight_geometry_confidence,
        weight_tta_agreement=weight_tta_agreement,
        rectification_policy=rectification_policy,
    )


def load_gate_config(db: Session) -> PreprocessingGateConfig:
    """
    Load and return a PreprocessingGateConfig from the active policy in the DB.

    Reads the most recently applied ``PolicyVersion`` row, parses its
    ``config_yaml``, and delegates to ``parse_gate_config``.

    Falls back to spec defaults (``PreprocessingGateConfig()``) when:
    - no policy version exists in the DB (fresh deploy / no policy applied yet)
    - the stored YAML cannot be parsed (defensive: corrupt or truncated row)
    - any individual threshold field is absent or non-numeric

    Callers must not cache the result across requests — policy may be updated
    between page processing runs.  The DB round-trip cost is negligible
    relative to GPU inference latency.

    Args:
        db — SQLAlchemy Session (read-only; no commit performed).

    Returns:
        PreprocessingGateConfig populated with policy thresholds (or defaults).
    """
    config_yaml = _current_policy_yaml(db)

    if config_yaml is None:
        logger.debug("load_gate_config: no active policy — using spec defaults")
        return PreprocessingGateConfig()

    try:
        parsed = yaml.safe_load(config_yaml)
    except yaml.YAMLError as exc:
        logger.warning(
            "load_gate_config: failed to parse active policy YAML (%s) — using spec defaults",
            exc,
        )
        return PreprocessingGateConfig()

    if not isinstance(parsed, dict):
        logger.warning(
            "load_gate_config: active policy YAML top-level is not a mapping — using spec defaults"
        )
        return PreprocessingGateConfig()

    cfg = parse_gate_config(parsed)
    logger.debug("load_gate_config: loaded gate config from active policy")
    return cfg
