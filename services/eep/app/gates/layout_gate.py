"""
services/eep/app/gates/layout_gate.py
--------------------------------------
EEP layout consensus gate (Packet 6.5).

Implements spec Section 7.4: greedy IoU-based one-to-one matching between
IEP2A (Detectron2) and IEP2B (DocLayout-YOLO) canonical region lists, yielding
a LayoutConsensusResult that drives the downstream routing decision.

Algorithm summary (spec Section 7.4):
  1. Greedy one-to-one matching by descending IoU.
     A match requires IoU >= match_iou_threshold AND same canonical RegionType.
  2. match_ratio = matched / max(len(iep2a), len(iep2b))
  3. type_histogram_match: for every RegionType in either histogram,
     |count_a - count_b| <= max_type_count_diff
  4. agreed = match_ratio >= min_match_ratio AND type_histogram_match
  5. consensus_confidence = 0.6*match_ratio + 0.2*mean_iou + 0.2*histogram_flag
     where histogram_flag = 1.0 if type_histogram_match else 0.0
  6. Single-model fallback (iep2b_regions is None): agreed=False unconditionally.

Routing after this gate (spec Section 8.2, Step 12–13):
  agreed == False                          → review / "layout_consensus_failed"
  consensus_confidence < min_conf (0.6)   → review / "layout_consensus_low_confidence"

Exported:
    LayoutGateConfig         — policy thresholds (spec Section 8.4 defaults)
    evaluate_layout_consensus — main entry point
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shared.schemas.layout import LayoutConsensusResult, Region, RegionType
from shared.schemas.ucf import BoundingBox

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LayoutGateConfig:
    """Policy thresholds for the layout consensus gate (spec Section 8.4)."""

    match_iou_threshold: float = 0.5
    min_match_ratio: float = 0.7
    max_type_count_diff: int = 1
    min_consensus_confidence: float = 0.6


# ---------------------------------------------------------------------------
# IoU utility
# ---------------------------------------------------------------------------


def _iou(a: BoundingBox, b: BoundingBox) -> float:
    """Intersection-over-Union of two axis-aligned bounding boxes."""
    ix_min = max(a.x_min, b.x_min)
    iy_min = max(a.y_min, b.y_min)
    ix_max = min(a.x_max, b.x_max)
    iy_max = min(a.y_max, b.y_max)

    if ix_min >= ix_max or iy_min >= iy_max:
        return 0.0

    inter = (ix_max - ix_min) * (iy_max - iy_min)
    area_a = (a.x_max - a.x_min) * (a.y_max - a.y_min)
    area_b = (b.x_max - b.x_min) * (b.y_max - b.y_min)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


# ---------------------------------------------------------------------------
# Greedy one-to-one matching (spec Section 7.4)
# ---------------------------------------------------------------------------


def _greedy_match(
    iep2a: Sequence[Region],
    iep2b: Sequence[Region],
    iou_threshold: float,
) -> list[tuple[int, int, float]]:
    """
    Greedy one-to-one matching by descending IoU.

    A match requires IoU >= iou_threshold AND same canonical RegionType.
    Each index (from either list) appears in at most one matched pair.

    Returns a list of (iep2a_idx, iep2b_idx, iou) triples.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, ra in enumerate(iep2a):
        for j, rb in enumerate(iep2b):
            if ra.type != rb.type:
                continue
            score = _iou(ra.bbox, rb.bbox)
            if score >= iou_threshold:
                candidates.append((score, i, j))

    # Process pairs in descending IoU order (greedy).
    candidates.sort(key=lambda t: t[0], reverse=True)

    matched_a: set[int] = set()
    matched_b: set[int] = set()
    matches: list[tuple[int, int, float]] = []

    for score, i, j in candidates:
        if i in matched_a or j in matched_b:
            continue
        matched_a.add(i)
        matched_b.add(j)
        matches.append((i, j, score))

    return matches


# ---------------------------------------------------------------------------
# type_histogram_match check
# ---------------------------------------------------------------------------


def _type_histogram_match(
    iep2a: Sequence[Region],
    iep2b: Sequence[Region],
    max_diff: int,
) -> bool:
    """
    True when, for every RegionType present in either list, the absolute
    per-type count difference is <= max_diff.
    """
    hist_a: dict[RegionType, int] = {}
    for r in iep2a:
        hist_a[r.type] = hist_a.get(r.type, 0) + 1

    hist_b: dict[RegionType, int] = {}
    for r in iep2b:
        hist_b[r.type] = hist_b.get(r.type, 0) + 1

    for rtype in set(hist_a) | set(hist_b):
        if abs(hist_a.get(rtype, 0) - hist_b.get(rtype, 0)) > max_diff:
            return False
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_layout_consensus(
    iep2a_regions: Sequence[Region],
    iep2b_regions: Sequence[Region] | None,
    config: LayoutGateConfig | None = None,
) -> LayoutConsensusResult:
    """
    Compare IEP2A and IEP2B canonical region lists and return a
    LayoutConsensusResult.

    Args:
        iep2a_regions:  Post-processed canonical regions from IEP2A (Detectron2).
        iep2b_regions:  Post-processed canonical regions from IEP2B
                        (DocLayout-YOLO), or None when IEP2B is unavailable.
        config:         Gate policy thresholds; defaults to LayoutGateConfig().

    Returns:
        LayoutConsensusResult.  agreed is True only in dual-model mode when
        match_ratio >= min_match_ratio AND type_histogram_match.
        agreed is always False in single-model fallback.
    """
    cfg = config if config is not None else LayoutGateConfig()
    n_a = len(iep2a_regions)

    # ------------------------------------------------------------------
    # Single-model fallback: IEP2B unavailable.
    # spec Section 7.4: "agreed = False unconditionally; single-model
    # auto-acceptance is prohibited."
    # ------------------------------------------------------------------
    if iep2b_regions is None:
        return LayoutConsensusResult(
            iep2a_region_count=n_a,
            iep2b_region_count=0,
            matched_regions=0,
            unmatched_iep2a=n_a,
            unmatched_iep2b=0,
            mean_matched_iou=0.0,
            type_histogram_match=False,
            agreed=False,
            consensus_confidence=0.0,
            single_model_mode=True,
        )

    # ------------------------------------------------------------------
    # Dual-model mode.
    # ------------------------------------------------------------------
    n_b = len(iep2b_regions)
    total = max(n_a, n_b)

    matches = _greedy_match(iep2a_regions, iep2b_regions, cfg.match_iou_threshold)
    matched_regions = len(matches)

    match_ratio = matched_regions / total if total > 0 else 0.0
    mean_iou = sum(m[2] for m in matches) / matched_regions if matched_regions > 0 else 0.0

    hist_match = _type_histogram_match(iep2a_regions, iep2b_regions, cfg.max_type_count_diff)
    histogram_flag = 1.0 if hist_match else 0.0

    agreed = match_ratio >= cfg.min_match_ratio and hist_match

    consensus_confidence = max(
        0.0,
        min(
            1.0,
            0.6 * match_ratio + 0.2 * mean_iou + 0.2 * histogram_flag,
        ),
    )

    return LayoutConsensusResult(
        iep2a_region_count=n_a,
        iep2b_region_count=n_b,
        matched_regions=matched_regions,
        unmatched_iep2a=n_a - matched_regions,
        unmatched_iep2b=n_b - matched_regions,
        mean_matched_iou=round(mean_iou, 6),
        type_histogram_match=hist_match,
        agreed=agreed,
        consensus_confidence=round(consensus_confidence, 6),
        single_model_mode=False,
    )
