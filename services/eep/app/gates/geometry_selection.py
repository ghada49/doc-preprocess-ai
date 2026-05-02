"""services.eep.app.gates.geometry_selection
-----------------------------------------
Geometry selection gate for the IEP1 preprocessing pipeline.

Implements the geometry selection cascade defined in spec Section 6.8.

Packet 3.1: structural agreement check + six per-model sanity checks.
Packet 3.2: split confidence filter, TTA variance filter, page area preference.
Packet 3.3: confidence-based selection, route-to-human logic, quality_gate_log writes.

Exported (Packet 3.1):
    PreprocessingGateConfig  — policy thresholds (spec Section 8.4 defaults)
    SanityCheckResult        — result of the six-check sanity gate
    check_structural_agreement  — mandatory agreement check (spec Section 6.8)
    check_sanity                — six hard sanity checks applied to one GeometryResponse

Exported (Packet 3.2):
    GeometryCandidate           — (model, response) carrier through the selection cascade
    apply_split_confidence_filter — remove candidates below split confidence threshold
    apply_tta_variance_filter     — remove candidates above TTA variance ceiling
    check_page_area_preference    — signal IEP1B preference for small-page tiebreaker

Exported (Packet 3.3):
    GeometrySelectionResult      — full result of the geometry selection cascade
    run_geometry_selection       — orchestrate the full cascade; returns GeometrySelectionResult
    build_geometry_gate_log_record — build a quality_gate_log insertion dict (no DB write)
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, field
from typing import Literal

from shared.schemas.eep import MaterialType
from shared.schemas.geometry import GeometryResponse, PageRegion

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_ASPECT_RATIO_BOUNDS: dict[str, tuple[float, float]] = {
    "book": (0.5, 2.5),
    "newspaper": (0.2, 8.0),
    "archival_document": (0.5, 3.0),
    "microfilm": (0.3, 5.0),
}

_DEFAULT_AREA_FRACTION_BOUNDS: dict[str, tuple[float, float]] = {
    "book": (0.15, 1.0),
    "newspaper": (0.05, 1.0),
    "archival_document": (0.15, 1.0),
    "microfilm": (0.15, 1.0),
}

_DEFAULT_ARTIFACT_VALIDATION_THRESHOLDS: dict[str, float] = {
    "book": 0.60,
    "newspaper": 0.50,
    "archival_document": 0.60,
    "microfilm": 0.60,
}


@dataclass
class PreprocessingGateConfig:
    """
    Policy-driven thresholds for the preprocessing gates.

    Defaults match the canonical values in spec Section 8.4 (libraryai-policy
    ConfigMap). In production these are loaded from the policy store (Phase 9).

    Fields used in Packet 3.1 (structural agreement + sanity checks):
        geometry_sanity_area_min_fraction — page region must be ≥ this fraction of image area
        geometry_sanity_area_max_fraction — page region must be ≤ this fraction of image area
        area_fraction_bounds              — per-material-type [min, max] page-area fraction
        aspect_ratio_bounds               — per-material-type [min, max] width/height ratio

    Fields declared for completeness (used from Packet 3.2 onward):
        split_confidence_threshold        — split decisions require higher confidence (3.2)
        tta_variance_ceiling              — models above this variance are unstable (3.2)
        page_area_preference_threshold    — below this fraction prefer IEP1B (3.2)

    Fields added for Packet 3.5 (artifact soft scoring):
        artifact_validation_threshold  — combined score must meet this to accept (3.5)
        skew_residual_good_max         — good if residual < this (°)
        skew_residual_bad_min          — suspicious if residual > this (°)
        blur_score_good_max            — good if blur_score < this
        blur_score_bad_min             — suspicious if blur_score > this
        border_score_bad_max           — suspicious if border_score < this
        border_score_good_min          — good if border_score > this
        foreground_good_lo             — good range lower bound for foreground_coverage
        foreground_good_hi             — good range upper bound for foreground_coverage
        foreground_bad_lo              — suspicious if foreground_coverage < this
        foreground_bad_hi              — suspicious if foreground_coverage > this
        geometry_confidence_good_min   — good if geometry_confidence > this
        geometry_confidence_bad_max    — suspicious if geometry_confidence < this
        tta_agreement_good_min         — good if tta_structural_agreement_rate > this
        tta_agreement_bad_max          — suspicious if tta_structural_agreement_rate < this
        weight_skew_residual           — weight for skew_residual in weighted sum
        weight_blur_score              — weight for blur_score in weighted sum
        weight_border_score            — weight for border_score in weighted sum
        weight_foreground_coverage     — weight for foreground_coverage in weighted sum
        weight_geometry_confidence     — weight for geometry_confidence in weighted sum
        weight_tta_agreement           — weight for tta_structural_agreement_rate in weighted sum
    """

    geometry_sanity_area_min_fraction: float = 0.15
    geometry_sanity_area_max_fraction: float = 1.0
    area_fraction_bounds: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(_DEFAULT_AREA_FRACTION_BOUNDS)
    )
    aspect_ratio_bounds: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(_DEFAULT_ASPECT_RATIO_BOUNDS)
    )
    newspaper_iep1b_mild_area_min_fraction: float = 0.08
    newspaper_strong_iep1a_geometry_confidence_min: float = 0.90
    newspaper_strong_iep1a_tta_agreement_min: float = 0.90
    newspaper_split_child_sliver_max_area_fraction: float = 0.12
    # Packet 3.2 fields — declared here so config is a single object.
    split_confidence_threshold: float = 0.75
    tta_variance_ceiling: float = 0.15
    page_area_preference_threshold: float = 0.30
    # Packet 3.5 fields — artifact soft signal scoring (spec Section 6.9 + 8.4).
    artifact_validation_threshold: float = 0.60
    artifact_validation_thresholds: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_ARTIFACT_VALIDATION_THRESHOLDS)
    )
    # Rectification routing policy (spec Section 8.4).
    # "conditional"            — attempt IEP1D whenever first pass is not acceptable (default).
    # "disabled_direct_review" — skip IEP1D; route first-pass non-acceptable pages directly
    #                            to pending_human_correction.
    rectification_policy: str = "conditional"
    skew_residual_good_max: float = 1.0
    skew_residual_bad_min: float = 5.0
    blur_score_good_max: float = 0.4
    blur_score_bad_min: float = 0.7
    border_score_bad_max: float = 0.3
    border_score_good_min: float = 0.5
    newspaper_border_score_bad_max: float = 0.2
    newspaper_border_score_good_min: float = 0.4
    foreground_good_lo: float = 0.2
    foreground_good_hi: float = 0.9
    foreground_bad_lo: float = 0.1
    foreground_bad_hi: float = 0.95
    newspaper_foreground_good_lo: float = 0.08
    newspaper_foreground_good_hi: float = 0.95
    newspaper_foreground_bad_lo: float = 0.03
    newspaper_foreground_bad_hi: float = 0.99
    geometry_confidence_good_min: float = 0.8
    geometry_confidence_bad_max: float = 0.5
    tta_agreement_good_min: float = 0.9
    tta_agreement_bad_max: float = 0.7
    newspaper_soft_signal_floor: float = 0.5
    weight_skew_residual: float = 1.0
    weight_blur_score: float = 1.0
    weight_border_score: float = 1.0
    weight_foreground_coverage: float = 1.0
    weight_geometry_confidence: float = 1.0
    weight_tta_agreement: float = 1.0


# ---------------------------------------------------------------------------
# Structural agreement  (spec Section 6.8, mandatory non-negotiable)
# ---------------------------------------------------------------------------


def check_structural_agreement(
    iep1a: GeometryResponse,
    iep1b: GeometryResponse,
) -> bool:
    """
    Return True if both models agree on page_count and split_required.

    Literal function from spec Section 6.8.

    First-pass disagreement is not immediately fatal: it lowers geometry trust
    and triggers rectification + mandatory second-pass agreement.  Second-pass
    disagreement is terminal and routes to pending_human_correction.

    The caller (EEP worker, Packet 4) is responsible for interpreting the result
    in the context of first vs. second pass.
    """
    return iep1a.page_count == iep1b.page_count and iep1a.split_required == iep1b.split_required


# ---------------------------------------------------------------------------
# Sanity check helpers
# ---------------------------------------------------------------------------


def _quadrilateral_area(corners: list[tuple[float, float]]) -> float:
    """Unsigned polygon area via the shoelace formula."""
    n = len(corners)
    area = 0.0
    for i in range(n):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _corners_convex_and_valid(corners: list[tuple[float, float]]) -> bool:
    """
    Return True if the corners form a convex (or near-convex) quadrilateral
    with no self-intersecting edges.

    Uses cross-product sign consistency: for a convex polygon traversed in
    one direction all z-components of consecutive edge cross products have the
    same sign.  Near-zero cross products (collinear edges, |cross| < 1e-9) are
    treated as neither CW nor CCW and are skipped — they do not indicate a
    sign reversal.  Only a clear sign reversal triggers failure.
    """
    n = len(corners)
    if n < 3:
        return False

    sign = 0  # 0 = undecided, 1 = CCW, -1 = CW
    for i in range(n):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % n]
        cx, cy = corners[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-9:
            continue  # collinear / near-collinear — allowed
        current_sign = 1 if cross > 0 else -1
        if sign == 0:
            sign = current_sign
        elif sign != current_sign:
            return False  # sign reversal → non-convex / self-intersecting
    return True


def _bbox_iou(
    b1: tuple[int, int, int, int],
    b2: tuple[int, int, int, int],
) -> float:
    """IoU between two axis-aligned bounding boxes (x_min, y_min, x_max, y_max)."""
    ix1 = max(b1[0], b2[0])
    iy1 = max(b1[1], b2[1])
    ix2 = min(b1[2], b2[2])
    iy2 = min(b1[3], b2[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    union = a1 + a2 - inter
    return inter / union if union > 0 else 0.0


def _area_fraction_bounds_for_material(
    config: PreprocessingGateConfig,
    material_type: MaterialType,
) -> tuple[float, float]:
    """
    Return page-area fraction bounds for a material type.

    ``geometry_sanity_area_min_fraction`` and
    ``geometry_sanity_area_max_fraction`` are legacy scalar policy fields. If a
    caller explicitly changes either scalar while leaving material bounds at
    defaults, preserve the old all-material override behavior.
    """
    scalar_changed = (
        config.geometry_sanity_area_min_fraction != 0.15
        or config.geometry_sanity_area_max_fraction != 1.0
    )
    if scalar_changed and config.area_fraction_bounds == _DEFAULT_AREA_FRACTION_BOUNDS:
        return (
            config.geometry_sanity_area_min_fraction,
            config.geometry_sanity_area_max_fraction,
        )
    return config.area_fraction_bounds.get(
        material_type,
        (
            config.geometry_sanity_area_min_fraction,
            config.geometry_sanity_area_max_fraction,
        ),
    )


def _region_width_height(region: PageRegion) -> tuple[float, float] | None:
    """
    Derive (width, height) of a page region for aspect-ratio checking.

    Prefers bbox (always present in practice).  Falls back to the bounding
    box of the quadrilateral corners.  Returns None if neither is available.
    """
    if region.bbox is not None:
        x_min, y_min, x_max, y_max = region.bbox
        return float(x_max - x_min), float(y_max - y_min)
    if region.corners is not None and len(region.corners) >= 2:
        xs = [c[0] for c in region.corners]
        ys = [c[1] for c in region.corners]
        return max(xs) - min(xs), max(ys) - min(ys)
    return None


# ---------------------------------------------------------------------------
# Sanity check result
# ---------------------------------------------------------------------------

#: Canonical names for the six sanity checks (spec Section 6.8).
SANITY_CHECK_NAMES: tuple[str, ...] = (
    "within_bounds",
    "non_degenerate",
    "area_fraction_plausible",
    "aspect_ratio_plausible",
    "corner_ordering_valid",
    "regions_non_overlapping",
)


@dataclass
class SanityCheckResult:
    """
    Result of running all six sanity checks against a single GeometryResponse.

    Attributes:
        passed        — True only if ALL six checks pass.
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
# Main sanity check function  (spec Section 6.8, Packet 3.1)
# ---------------------------------------------------------------------------


def check_sanity(
    response: GeometryResponse,
    material_type: MaterialType,
    proxy_width: int,
    proxy_height: int,
    config: PreprocessingGateConfig,
) -> SanityCheckResult:
    """
    Apply all six hard sanity checks to a single GeometryResponse.

    Checks are applied across every page region in the response.  The response
    fails sanity if any check fails on any of its regions, or if the
    regions_non_overlapping check fails at the response level.

    Returns SanityCheckResult(passed=True, failed_checks=[]) when all six
    checks pass; otherwise passed=False with the names of failing checks.

    Spec reference: Section 6.8 "Sanity Checks (per model)".

    Args:
        response      — GeometryResponse from IEP1A or IEP1B
        material_type — job material type (book / newspaper / archival_document)
        proxy_width   — pixel width of the proxy image used for inference
        proxy_height  — pixel height of the proxy image used for inference
        config        — policy thresholds (defaults per spec Section 8.4)
    """
    failed: list[str] = []

    for region in response.pages:
        # -------------------------------------------------------------------
        # Check 1: Page region within image bounds
        # All corner coordinates ≥ 0 and within proxy dimensions.
        # Applied to both corners (when available) and bbox (when available).
        # -------------------------------------------------------------------
        within = True
        if region.corners is not None:
            for x, y in region.corners:
                if not (0.0 <= x <= proxy_width and 0.0 <= y <= proxy_height):
                    within = False
                    break
        if within and region.bbox is not None:
            x_min, y_min, x_max, y_max = region.bbox
            if not (x_min >= 0 and y_min >= 0 and x_max <= proxy_width and y_max <= proxy_height):
                within = False
        if not within and "within_bounds" not in failed:
            failed.append("within_bounds")

        # -------------------------------------------------------------------
        # Check 2: Non-degenerate geometry
        # Quadrilateral area > 0; bbox width > 0 and height > 0.
        # -------------------------------------------------------------------
        non_degenerate = True
        if region.geometry_type == "quadrilateral" and region.corners is not None:
            if _quadrilateral_area(region.corners) <= 0.0:
                non_degenerate = False
        if non_degenerate and region.bbox is not None:
            x_min, y_min, x_max, y_max = region.bbox
            if not (x_max - x_min > 0 and y_max - y_min > 0):
                non_degenerate = False
        if not non_degenerate and "non_degenerate" not in failed:
            failed.append("non_degenerate")

        # -------------------------------------------------------------------
        # Check 3: Page area fraction plausible
        # For 2-page spreads each page is naturally smaller, so halve the
        # minimum area threshold when the response reports page_count == 2.
        # -------------------------------------------------------------------
        area_min, area_max = _area_fraction_bounds_for_material(config, material_type)
        if response.page_count == 2:
            area_min = area_min / 2.0
        if not (area_min <= region.page_area_fraction <= area_max):
            if "area_fraction_plausible" not in failed:
                failed.append("area_fraction_plausible")

        # -------------------------------------------------------------------
        # Check 4: Aspect ratio plausible
        # width / height ratio must be within the material_type bounds.
        # -------------------------------------------------------------------
        wh = _region_width_height(region)
        if wh is not None:
            w, h = wh
            if h > 0.0:
                aspect = w / h
                lo, hi = config.aspect_ratio_bounds.get(material_type, (0.0, float("inf")))
                if not (lo <= aspect <= hi):
                    if "aspect_ratio_plausible" not in failed:
                        failed.append("aspect_ratio_plausible")
            else:
                # height is zero — degenerate case (also caught by check 2, but
                # flag here so all applicable checks fire independently)
                if "aspect_ratio_plausible" not in failed:
                    failed.append("aspect_ratio_plausible")

        # -------------------------------------------------------------------
        # Check 5: Corner ordering valid  (quadrilateral geometry only)
        # Corners must form a convex (or near-convex) quadrilateral with no
        # self-intersecting edges.
        # -------------------------------------------------------------------
        if region.geometry_type == "quadrilateral" and region.corners is not None:
            if not _corners_convex_and_valid(region.corners):
                if "corner_ordering_valid" not in failed:
                    failed.append("corner_ordering_valid")

    # -----------------------------------------------------------------------
    # Check 6: Page regions non-overlapping  (only when page_count == 2)
    # IoU between the two page-region bboxes must be < 0.1.
    # -----------------------------------------------------------------------
    if response.page_count == 2 and len(response.pages) == 2:
        b1 = response.pages[0].bbox
        b2 = response.pages[1].bbox
        if b1 is not None and b2 is not None:
            if _bbox_iou(b1, b2) >= 0.1:
                failed.append("regions_non_overlapping")

    return SanityCheckResult(passed=len(failed) == 0, failed_checks=failed)


# ---------------------------------------------------------------------------
# Packet 3.2 — Split confidence filter, TTA variance filter, page area preference
# ---------------------------------------------------------------------------

#: Valid model name identifiers used throughout the selection cascade.
ModelName = Literal["iep1a", "iep1b"]


@dataclass
class GeometryCandidate:
    """
    A single candidate in the geometry selection cascade.

    Carries the model identity and its GeometryResponse together so that
    filters can operate on (model, response) pairs without losing provenance.

    Attributes:
        model    — "iep1a" or "iep1b"
        response — the GeometryResponse returned by that model
    """

    model: ModelName
    response: GeometryResponse


def _compute_split_confidence(response: GeometryResponse) -> float:
    """
    Compute split confidence for a candidate.

    Spec Section 6.8:
        split_confidence = min(weakest_instance_confidence,
                               tta_structural_agreement_rate)

    geometry_confidence is already defined as "min confidence across all
    detected instances" (spec schema), so it equals weakest_instance_confidence.
    """
    return min(response.geometry_confidence, response.tta_structural_agreement_rate)


def apply_split_confidence_filter(
    candidates: list[GeometryCandidate],
    config: PreprocessingGateConfig,
) -> list[GeometryCandidate]:
    """
    Remove candidates that fail the split confidence check.

    Applied only to candidates where split_required=True.  Candidates where
    split_required=False pass through unchanged — split confidence is irrelevant
    for single-page geometry.

    split_confidence = min(geometry_confidence, tta_structural_agreement_rate)

    A candidate is removed when split_confidence < config.split_confidence_threshold.

    If both candidates are removed the caller (Packet 3.3 route-to-human logic)
    must route to pending_human_correction with review_reasons=["split_confidence_low"].

    Spec reference: Section 6.8 "Split Confidence Filter".
    """
    result: list[GeometryCandidate] = []
    for candidate in candidates:
        if not candidate.response.split_required:
            result.append(candidate)
            continue
        sc = _compute_split_confidence(candidate.response)
        if sc >= config.split_confidence_threshold:
            result.append(candidate)
    return result


def apply_tta_variance_filter(
    candidates: list[GeometryCandidate],
    config: PreprocessingGateConfig,
) -> list[GeometryCandidate]:
    """
    Remove candidates whose TTA prediction variance exceeds the ceiling.

    High TTA variance means the model's predictions are unstable across
    augmented inputs — the geometry is not reliable even if the primary
    prediction has high confidence.

    A candidate is removed when tta_prediction_variance > config.tta_variance_ceiling.

    If both candidates are removed the caller (Packet 3.3 route-to-human logic)
    must route to pending_human_correction with review_reasons=["tta_variance_high"].

    Spec reference: Section 6.8 "TTA Variance Filter".
    """
    return [
        c for c in candidates if c.response.tta_prediction_variance <= config.tta_variance_ceiling
    ]


def check_page_area_preference(
    candidates: list[GeometryCandidate],
    config: PreprocessingGateConfig,
) -> bool:
    """
    Return True if IEP1B should be preferred as a tiebreaker for small pages.

    When any detected page region across the surviving candidates reports
    page_area_fraction below config.page_area_preference_threshold, IEP1B is
    preferred because IEP1A mask resolution degrades for small pages relative
    to the full image (spec Section 6.8 "Page Area Preference").

    This function only signals the preference — it does NOT select a candidate.
    Packet 3.3 (confidence selection) applies this preference as a tiebreaker
    when both candidates survive all preceding filters.

    Returns False when no candidate reports a small page area fraction, or when
    fewer than two candidates are present (preference is only meaningful as a
    tiebreaker between both models).

    Spec reference: Section 6.8 "Page Area Preference".
    """
    if len(candidates) < 2:
        return False
    for candidate in candidates:
        for region in candidate.response.pages:
            if region.page_area_fraction < config.page_area_preference_threshold:
                return True
    return False


# ---------------------------------------------------------------------------
# Packet 3.3 — Confidence-based selection, route-to-human, gate log record
# ---------------------------------------------------------------------------


@dataclass
class GeometrySelectionResult:
    """
    Full result of the geometry selection cascade.

    Attributes:
        selected                    — winning GeometryCandidate; None if routed to human
        geometry_trust              — "high" only if both models were provided,
                                      structural agreement held, and either both survived
                                      all filters or the narrow newspaper IEP1B mild
                                      area-fraction exception applied; "low" if any other
                                      dropout or disagreement occurred; None when no
                                      candidate was selected
        selection_reason            — short label explaining why this candidate was chosen
                                      (e.g. "higher_confidence"); None when no candidate
                                      survived
        route_decision              — "accepted" (high trust, proceed to normalization),
                                      "rectification" (low trust, mandatory IEP1D pass),
                                      or "pending_human_correction" (no usable geometry)
        review_reason               — canonical reason string when route_decision is
                                      "pending_human_correction"; None otherwise
        structural_agreement        — result of check_structural_agreement; None when only
                                      one model was provided (single-model mode)
        sanity_results              — per-model SanityCheckResult dicts keyed by model
                                      name ("iep1a" / "iep1b")
        split_confidence_per_model  — per-model split confidence scores for models that
                                      reported split_required=True; None when no model
                                      reported a split
        tta_variance_per_model      — per-model tta_prediction_variance values (all
                                      initial models, before any filtering)
        page_area_preference_triggered — True if the IEP1B small-page tiebreaker fired
    """

    selected: GeometryCandidate | None
    geometry_trust: Literal["high", "low"] | None
    selection_reason: str | None
    route_decision: Literal["accepted", "rectification", "pending_human_correction"]
    review_reason: str | None
    structural_agreement: bool | None
    sanity_results: dict[str, dict[str, object]]
    split_confidence_per_model: dict[str, float] | None
    tta_variance_per_model: dict[str, float]
    page_area_preference_triggered: bool


def _select_candidate(
    candidates: list[GeometryCandidate],
    page_area_preference: bool,
) -> tuple[GeometryCandidate, str]:
    """
    Select the winning candidate from the surviving list.

    Returns (candidate, selection_reason).  Assumes len(candidates) >= 1.

    Priority:
      1. Sole survivor — only one candidate remains.
      2. Page area preference — IEP1B preferred for small pages.
      3. Higher geometry_confidence — pick the more confident model.
      4. Default IEP1A — explicit tie-breaking default per spec.
    """
    if len(candidates) == 1:
        return candidates[0], "sole_survivor"

    # Page area preference: prefer IEP1B when triggered.
    if page_area_preference:
        iep1b = next((c for c in candidates if c.model == "iep1b"), None)
        if iep1b is not None:
            return iep1b, "page_area_preference"

    # Confidence tie check (handles exactly-equal floats).
    confs = [c.response.geometry_confidence for c in candidates]
    if len(set(confs)) == 1:
        # All tied — fall back to IEP1A.
        iep1a = next((c for c in candidates if c.model == "iep1a"), None)
        return (iep1a if iep1a is not None else candidates[0]), "default_iep1a"

    best = max(candidates, key=lambda c: c.response.geometry_confidence)
    return best, "higher_confidence"


def _page_area_fractions(response: GeometryResponse | None) -> list[float]:
    if response is None:
        return []
    return [region.page_area_fraction for region in response.pages]


def _only_failed_area_fraction(sanity_result: dict[str, object] | None) -> bool:
    if not sanity_result:
        return False
    failed = sanity_result.get("failed_checks")
    return failed == ["area_fraction_plausible"]


def _newspaper_iep1b_mild_area_dropout_is_acceptable(
    *,
    material_type: MaterialType,
    structural_agreement: bool | None,
    iep1a_response: GeometryResponse | None,
    iep1b_response: GeometryResponse | None,
    sanity_results: dict[str, dict[str, object]],
    surviving_candidates: list[GeometryCandidate],
    config: PreprocessingGateConfig,
) -> bool:
    """
    Narrow newspaper exception for IEP1B keypoint area underestimation.

    It only applies when IEP1A is a strong surviving candidate, both models
    structurally agree, and IEP1B failed exactly the area-fraction sanity check
    by a mild margin. Other sanity failures, structural disagreement, unstable
    TTA, single-model mode, and severe newspaper area failures still take the
    normal rectification/review path.
    """
    if material_type != "newspaper" or structural_agreement is not True:
        return False
    if iep1a_response is None or iep1b_response is None:
        return False
    if sanity_results.get("iep1a", {}).get("passed") is not True:
        return False
    if not _only_failed_area_fraction(sanity_results.get("iep1b")):
        return False
    if not any(candidate.model == "iep1a" for candidate in surviving_candidates):
        return False
    if iep1a_response.geometry_confidence < config.newspaper_strong_iep1a_geometry_confidence_min:
        return False
    if (
        iep1a_response.tta_structural_agreement_rate
        < config.newspaper_strong_iep1a_tta_agreement_min
    ):
        return False
    if iep1a_response.tta_prediction_variance > config.tta_variance_ceiling:
        return False
    if iep1b_response.tta_prediction_variance > config.tta_variance_ceiling:
        return False

    iep1b_area_fractions = _page_area_fractions(iep1b_response)
    if not iep1b_area_fractions:
        return False
    return min(iep1b_area_fractions) >= config.newspaper_iep1b_mild_area_min_fraction


def run_geometry_selection(
    iep1a_response: GeometryResponse | None,
    iep1b_response: GeometryResponse | None,
    material_type: MaterialType,
    proxy_width: int,
    proxy_height: int,
    config: PreprocessingGateConfig | None = None,
) -> GeometrySelectionResult:
    """
    Orchestrate the full geometry selection cascade (spec Section 6.8, Step 3).

    The cascade proceeds in this order:
      1. Structural agreement check (if both models present).
      2. Per-model sanity checks — models that fail are dropped.
      3. Split confidence filter (for split_required candidates).
      4. TTA variance filter.
      5. Page area preference signal (tiebreaker hint).
      6. Confidence-based selection among surviving candidates.

    Geometry trust is HIGH only when both models were provided, both survived
    all filters, and structural agreement held.  Any dropout or disagreement
    lowers trust to LOW, triggering mandatory rectification
    (route_decision="rectification").

    When no candidates survive all filters the result routes to human correction
    (route_decision="pending_human_correction") with a canonical review_reason.

    Args:
        iep1a_response — response from IEP1A; pass None for single-model mode
        iep1b_response — response from IEP1B; pass None for single-model mode
        material_type  — job material type (book / newspaper / archival_document)
        proxy_width    — pixel width of the proxy image used for inference
        proxy_height   — pixel height of the proxy image used for inference
        config         — policy thresholds; defaults to PreprocessingGateConfig()
    """
    if config is None:
        config = PreprocessingGateConfig()

    # --- Build initial candidate list (preserved for logging) ---
    all_models: list[GeometryCandidate] = []
    if iep1a_response is not None:
        all_models.append(GeometryCandidate(model="iep1a", response=iep1a_response))
    if iep1b_response is not None:
        all_models.append(GeometryCandidate(model="iep1b", response=iep1b_response))

    initial_count = len(all_models)
    both_present = initial_count == 2

    # --- Structural agreement (only meaningful when both models present) ---
    structural_agreement: bool | None = None
    if both_present and iep1a_response is not None and iep1b_response is not None:
        structural_agreement = check_structural_agreement(iep1a_response, iep1b_response)

    # --- Collect TTA variance before any filtering ---
    tta_variance_per_model: dict[str, float] = {
        c.model: c.response.tta_prediction_variance for c in all_models
    }

    # --- Collect split confidence for models that reported a split ---
    split_confidence_per_model: dict[str, float] | None = None
    if any(c.response.split_required for c in all_models):
        split_confidence_per_model = {
            c.model: _compute_split_confidence(c.response)
            for c in all_models
            if c.response.split_required
        }

    # --- Per-model sanity checks ---
    sanity_results: dict[str, dict[str, object]] = {}
    candidates: list[GeometryCandidate] = []
    for candidate in all_models:
        sr = check_sanity(candidate.response, material_type, proxy_width, proxy_height, config)
        sanity_results[candidate.model] = sr.as_dict()
        if sr.passed:
            candidates.append(candidate)

    all_failed_sanity = (len(candidates) == 0) and (initial_count > 0)
    dropped_at_sanity = len(candidates) < initial_count

    # --- Split confidence filter ---
    post_split = apply_split_confidence_filter(candidates, config)
    all_failed_split = (len(post_split) == 0) and (len(candidates) > 0)
    dropped_at_split = len(post_split) < len(candidates)
    candidates = post_split

    # --- TTA variance filter ---
    post_tta = apply_tta_variance_filter(candidates, config)
    all_failed_tta = (len(post_tta) == 0) and (len(candidates) > 0)
    dropped_at_tta = len(post_tta) < len(candidates)
    candidates = post_tta

    # --- Page area preference ---
    page_area_preference_triggered = check_page_area_preference(candidates, config)
    newspaper_iep1b_area_tolerated = _newspaper_iep1b_mild_area_dropout_is_acceptable(
        material_type=material_type,
        structural_agreement=structural_agreement,
        iep1a_response=iep1a_response,
        iep1b_response=iep1b_response,
        sanity_results=sanity_results,
        surviving_candidates=candidates,
        config=config,
    )

    # --- Route to human when no candidates survive ---
    if not candidates:
        if all_failed_sanity:
            review_reason: str | None = "geometry_sanity_failed"
        elif all_failed_split:
            review_reason = "split_confidence_low"
        elif all_failed_tta:
            review_reason = "tta_variance_high"
        else:
            review_reason = "geometry_selection_failed"

        return GeometrySelectionResult(
            selected=None,
            geometry_trust=None,
            selection_reason=None,
            route_decision="pending_human_correction",
            review_reason=review_reason,
            structural_agreement=structural_agreement,
            sanity_results=sanity_results,
            split_confidence_per_model=split_confidence_per_model,
            tta_variance_per_model=tta_variance_per_model,
            page_area_preference_triggered=page_area_preference_triggered,
        )

    # --- Select winner ---
    any_dropped = dropped_at_sanity or dropped_at_split or dropped_at_tta
    high_trust = both_present and (structural_agreement is True) and (
        not any_dropped or newspaper_iep1b_area_tolerated
    )
    geometry_trust: Literal["high", "low"] | None = "high" if high_trust else "low"

    if newspaper_iep1b_area_tolerated:
        selected = next(c for c in candidates if c.model == "iep1a")
        selection_reason = "newspaper_iep1a_mild_iep1b_area_fallback"
    else:
        selected, selection_reason = _select_candidate(candidates, page_area_preference_triggered)

    route_decision: Literal["accepted", "rectification", "pending_human_correction"] = (
        "accepted" if high_trust else "rectification"
    )

    return GeometrySelectionResult(
        selected=selected,
        geometry_trust=geometry_trust,
        selection_reason=selection_reason,
        route_decision=route_decision,
        review_reason=None,
        structural_agreement=structural_agreement,
        sanity_results=sanity_results,
        split_confidence_per_model=split_confidence_per_model,
        tta_variance_per_model=tta_variance_per_model,
        page_area_preference_triggered=page_area_preference_triggered,
    )


def build_geometry_gate_log_record(
    result: GeometrySelectionResult,
    job_id: str,
    page_number: int,
    gate_type: Literal["geometry_selection", "geometry_selection_post_rectification"],
    iep1a_response: GeometryResponse | None,
    iep1b_response: GeometryResponse | None,
    processing_time_ms: float,
) -> dict[str, object]:
    """
    Build a dict ready for insertion into the quality_gate_log table.

    Does NOT write to the database — the caller (EEP worker, Packet 4) performs
    the actual insert.  A fresh gate_id (UUID4) is generated on each call.

    Columns populated:
        gate_id, job_id, page_number, gate_type,
        iep1a_geometry, iep1b_geometry, structural_agreement,
        selected_model, selection_reason, sanity_check_results,
        split_confidence, tta_variance, artifact_validation_score,
        route_decision, review_reason, processing_time_ms

    artifact_validation_score is always None here — it is populated by the
    artifact validation gate (Packets 3.4–3.5) in the same log row when the
    worker updates the record after normalization.
    """
    return {
        "gate_id": str(_uuid.uuid4()),
        "job_id": job_id,
        "page_number": page_number,
        "gate_type": gate_type,
        "iep1a_geometry": iep1a_response.model_dump() if iep1a_response is not None else None,
        "iep1b_geometry": iep1b_response.model_dump() if iep1b_response is not None else None,
        "structural_agreement": result.structural_agreement,
        "selected_model": result.selected.model if result.selected is not None else None,
        "selection_reason": result.selection_reason,
        "sanity_check_results": result.sanity_results,
        "split_confidence": result.split_confidence_per_model,
        "tta_variance": result.tta_variance_per_model,
        "artifact_validation_score": None,
        "route_decision": result.route_decision,
        "review_reason": result.review_reason,
        "processing_time_ms": int(processing_time_ms),
    }
