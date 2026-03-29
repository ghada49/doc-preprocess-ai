"""
services/iep2b/app/postprocess.py
-----------------------------------
IEP2B postprocessing pipeline (Packet 6.4).

Applied after native-to-canonical class mapping to produce the final
LayoutDetectResponse region list.

Per spec Section 7.2, IEP2B postprocessing steps are:

  1. (Class mapping — applied upstream by the caller, not here.)

  2. Merge overlapping same-type canonical regions
     Greedy NMS per RegionType: regions sorted by confidence descending;
     a candidate is suppressed when its IoU with any already-kept region
     of the same type exceeds 0.5.  The keeper is always the higher-
     confidence region.

  3. Reassign region IDs sequentially (r1, r2, …) sorted by (y_min, x_min).
     IDs are stable within a single response; not guaranteed stable across
     invocations.

Note: IEP2B does NOT apply confidence recalibration (small/edge penalties)
or DBSCAN column inference — those are IEP2A-specific steps.  IEP2B's
postprocessing is intentionally simpler to keep the services architecturally
independent and to avoid coupling their internal quality signals.

Exported:
    postprocess_regions — run the IEP2B postprocessing pipeline
"""

from __future__ import annotations

from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

# IoU threshold for same-type NMS (spec Section 7.2).
_NMS_IOU_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# IoU
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
# NMS per RegionType
# ---------------------------------------------------------------------------


def _nms_per_type(regions: list[Region]) -> list[Region]:
    """
    Apply greedy NMS independently per RegionType.

    Regions are processed in descending confidence order within each type.
    A candidate is kept only if its IoU with every already-kept region of
    the same type is <= _NMS_IOU_THRESHOLD.
    """
    by_type: dict[RegionType, list[Region]] = {}
    for r in regions:
        by_type.setdefault(r.type, []).append(r)

    kept: list[Region] = []
    for region_list in by_type.values():
        sorted_by_conf = sorted(region_list, key=lambda r: r.confidence, reverse=True)
        survivors: list[Region] = []
        for candidate in sorted_by_conf:
            if all(_iou(candidate.bbox, s.bbox) <= _NMS_IOU_THRESHOLD for s in survivors):
                survivors.append(candidate)
        kept.extend(survivors)
    return kept


# ---------------------------------------------------------------------------
# Sequential ID reassignment
# ---------------------------------------------------------------------------


def _reassign_ids(regions: list[Region]) -> list[Region]:
    """
    Sort regions by (y_min, x_min) and assign IDs r1, r2, …
    Returns new Region instances; originals are not mutated.
    """
    sorted_regions = sorted(regions, key=lambda r: (r.bbox.y_min, r.bbox.x_min))
    return [r.model_copy(update={"id": f"r{i + 1}"}) for i, r in enumerate(sorted_regions)]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def postprocess_regions(regions: list[Region]) -> list[Region]:
    """
    Run the IEP2B postprocessing pipeline on a list of canonically-typed regions.

    Expects regions whose types have already been mapped from native DocLayout-
    YOLO classes to canonical RegionType values (via class_mapping.map_native_class),
    with non-canonical regions already excluded.

    Steps:
        1. Greedy NMS per RegionType (IoU threshold 0.5).
        2. Sequential ID reassignment sorted by (y_min, x_min).

    Args:
        regions: Canonical Region list after class mapping.

    Returns:
        Postprocessed Region list with unique sequential IDs.
    """
    after_nms = _nms_per_type(regions)
    return _reassign_ids(after_nms)
