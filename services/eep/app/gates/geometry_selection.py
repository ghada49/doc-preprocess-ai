"""services.eep.app.gates.geometry_selection
-----------------------------------------
Geometry selection gate for the IEP1 preprocessing pipeline.

Implements the geometry selection cascade defined in spec Section 6.8.

Packet 3.1: structural agreement check + six per-model sanity checks.
Packet 3.2: split confidence filter, TTA variance filter, page area preference.
Packet 3.3: confidence-based selection, route-to-human logic, quality_gate_log writes.  [pending]

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from shared.schemas.eep import MaterialType
from shared.schemas.geometry import GeometryResponse, PageRegion

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_ASPECT_RATIO_BOUNDS: dict[str, tuple[float, float]] = {
    "book": (0.5, 2.5),
    "newspaper": (0.3, 5.0),
    "archival_document": (0.5, 3.0),
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
        aspect_ratio_bounds               — per-material-type [min, max] width/height ratio

    Fields declared for completeness (used from Packet 3.2 onward):
        split_confidence_threshold        — split decisions require higher confidence (3.2)
        tta_variance_ceiling              — models above this variance are unstable (3.2)
        page_area_preference_threshold    — below this fraction prefer IEP1B (3.2)
    """

    geometry_sanity_area_min_fraction: float = 0.15
    geometry_sanity_area_max_fraction: float = 0.98
    aspect_ratio_bounds: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(_DEFAULT_ASPECT_RATIO_BOUNDS)
    )
    # Packet 3.2 fields — declared here so config is a single object.
    split_confidence_threshold: float = 0.75
    tta_variance_ceiling: float = 0.15
    page_area_preference_threshold: float = 0.30


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
        # -------------------------------------------------------------------
        if not (
            config.geometry_sanity_area_min_fraction
            <= region.page_area_fraction
            <= config.geometry_sanity_area_max_fraction
        ):
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
