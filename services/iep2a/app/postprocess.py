"""
services/iep2a/app/postprocess.py
-----------------------------------
IEP2A postprocessing pipeline (Packet 6.2).

Applied to the raw region list from Detectron2 before assembling
LayoutDetectResponse.  The stub path in detect.py calls this on its mock
regions so that the full contract (calibrated confidences, column structure,
sequential IDs) is exercised end-to-end.

Pipeline steps (applied in order):

  1. Merge overlapping same-type regions
     Greedy NMS per RegionType: regions sorted by confidence descending;
     a candidate is suppressed when its IoU with any already-kept same-type
     region exceeds 0.5.  The keeper is always the higher-confidence region.

  2. Recalibrate confidence
     - small region  (area < 1 % of page area)     → confidence × 0.8
     - edge region   (any bbox side within 5 % of   → confidence × 0.9
                      the corresponding page border)
     Penalties compound when both conditions hold; result clamped to [0, 1].

  3. Infer column structure via 1D DBSCAN on text_block x-centroids
     eps = dbscan_eps_fraction × page_width  (default 0.08, from config)
     Returns None when no text_block regions survive postprocessing.
     Column boundaries are expressed as fractions of page_width in [0, 1],
     sorted ascending (satisfies ColumnStructure schema invariants).

  4. Reassign region IDs sequentially (r1, r2, …) sorted by (y_min, x_min).
     IDs are stable within a single response; they are not guaranteed to be
     stable across invocations on the same page.

Exported:
    postprocess_regions — run the full pipeline; returns
                          (regions, column_structure | None)
"""

from __future__ import annotations

from shared.schemas.layout import ColumnStructure, Region, RegionType
from shared.schemas.ucf import BoundingBox

# ---------------------------------------------------------------------------
# Tunable constants (all from spec Section 7.1 / Section 8.4)
# ---------------------------------------------------------------------------

# IoU threshold for same-type NMS.
_NMS_IOU_THRESHOLD: float = 0.5

# Fraction of a page dimension used to classify "edge" regions.
_EDGE_MARGIN_FRAC: float = 0.05

# Fraction of page area below which a region is classified "small".
_SMALL_AREA_FRAC: float = 0.01

# Confidence penalty multipliers.
_SMALL_PENALTY: float = 0.8
_EDGE_PENALTY: float = 0.9

# Default DBSCAN eps fraction (config-injectable via postprocess_regions arg).
_DEFAULT_DBSCAN_EPS_FRACTION: float = 0.08


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
# Step 1 — greedy NMS per RegionType
# ---------------------------------------------------------------------------


def _nms_per_type(regions: list[Region]) -> list[Region]:
    """
    Apply greedy NMS independently per RegionType.

    Within each type group, regions are processed in descending confidence
    order.  A candidate is kept only if its IoU with every already-kept
    region of the same type is <= _NMS_IOU_THRESHOLD.
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
# Step 2 — confidence recalibration
# ---------------------------------------------------------------------------


def _recalibrate(
    regions: list[Region],
    page_width: float,
    page_height: float,
) -> list[Region]:
    """
    Apply small-region and edge-region confidence penalties.

    Returns new Region instances; originals are not mutated.
    Penalty factors compound when both conditions hold.
    """
    page_area = page_width * page_height
    edge_x = _EDGE_MARGIN_FRAC * page_width
    edge_y = _EDGE_MARGIN_FRAC * page_height

    result: list[Region] = []
    for r in regions:
        b = r.bbox
        region_area = (b.x_max - b.x_min) * (b.y_max - b.y_min)

        factor = 1.0
        if region_area < _SMALL_AREA_FRAC * page_area:
            factor *= _SMALL_PENALTY
        if (
            b.x_min <= edge_x
            or b.x_max >= page_width - edge_x
            or b.y_min <= edge_y
            or b.y_max >= page_height - edge_y
        ):
            factor *= _EDGE_PENALTY

        new_conf = max(0.0, min(1.0, r.confidence * factor))
        result.append(r.model_copy(update={"confidence": new_conf}))
    return result


# ---------------------------------------------------------------------------
# Step 3 — column structure inference via 1D DBSCAN
# ---------------------------------------------------------------------------


def _dbscan_1d(values: list[float], eps: float) -> list[int]:
    """
    1D DBSCAN with min_samples=1.

    Every point belongs to a cluster.  Points within eps of each other
    (directly or transitively via sorted adjacency) are in the same cluster.
    Returns cluster labels in the same order as the input values.
    """
    n = len(values)
    if n == 0:
        return []

    order = sorted(range(n), key=lambda i: values[i])
    sorted_vals = [values[i] for i in order]

    cluster_id = 0
    sorted_labels: list[int] = [0] * n
    sorted_labels[0] = cluster_id
    for i in range(1, n):
        if sorted_vals[i] - sorted_vals[i - 1] > eps:
            cluster_id += 1
        sorted_labels[i] = cluster_id

    # Map sorted labels back to original order.
    labels: list[int] = [0] * n
    for rank, orig_idx in enumerate(order):
        labels[orig_idx] = sorted_labels[rank]
    return labels


def _infer_column_structure(
    regions: list[Region],
    page_width: float,
    dbscan_eps_fraction: float,
) -> ColumnStructure | None:
    """
    Infer column structure from text_block x-centroids via 1D DBSCAN.

    Returns None when no text_block regions are present.
    Column boundaries are expressed as fractions of page_width, sorted
    ascending, and satisfy ColumnStructure schema invariants.
    """
    text_blocks = [r for r in regions if r.type == RegionType.text_block]
    if not text_blocks:
        return None

    centroids = [(r.bbox.x_min + r.bbox.x_max) / 2.0 for r in text_blocks]
    eps = dbscan_eps_fraction * page_width
    labels = _dbscan_1d(centroids, eps)

    # Compute mean centroid per cluster.
    cluster_sums: dict[int, float] = {}
    cluster_counts: dict[int, int] = {}
    for centroid, label in zip(centroids, labels):
        cluster_sums[label] = cluster_sums.get(label, 0.0) + centroid
        cluster_counts[label] = cluster_counts.get(label, 0) + 1

    cluster_means = sorted(cluster_sums[cid] / cluster_counts[cid] for cid in cluster_sums)
    n_columns = len(cluster_means)

    # Boundaries: midpoint between adjacent cluster means, as fraction of page_width.
    boundaries: list[float] = []
    for i in range(n_columns - 1):
        mid = (cluster_means[i] + cluster_means[i + 1]) / 2.0
        boundaries.append(max(0.0, min(1.0, mid / page_width)))

    return ColumnStructure(column_count=n_columns, column_boundaries=boundaries)


# ---------------------------------------------------------------------------
# Step 4 — sequential ID reassignment
# ---------------------------------------------------------------------------


def _reassign_ids(regions: list[Region]) -> list[Region]:
    """
    Sort regions by (y_min, x_min) and assign IDs r1, r2, …
    Returns new Region instances; originals are not mutated.
    """
    sorted_regions = sorted(regions, key=lambda r: (r.bbox.y_min, r.bbox.x_min))
    return [r.model_copy(update={"id": f"r{i + 1}"}) for i, r in enumerate(sorted_regions)]


# ---------------------------------------------------------------------------
# Page-dimension inference
# ---------------------------------------------------------------------------


def _infer_page_dimensions(regions: list[Region]) -> tuple[float, float]:
    """
    Derive page dimensions from the union of all region bounding boxes.
    Falls back to 1000×1000 when no regions are present.

    NOTE: This is a fallback only.  Region bounding boxes never extend to
    the true page margins, so the inferred width will be systematically
    underestimated.  That causes two compounding errors:
      1. eps = dbscan_eps_fraction × inferred_width is too small → columns
         that should merge remain split.
      2. column_boundaries expressed as fractions of inferred_width are
         shifted right relative to the true page → boundary values are too
         large.
    Phase 12 must pass actual image pixel dimensions (from the Detectron2
    input tensor) to eliminate both errors.
    """
    if not regions:
        return 1000.0, 1000.0
    page_width = max(r.bbox.x_max for r in regions)
    page_height = max(r.bbox.y_max for r in regions)
    return max(page_width, 1.0), max(page_height, 1.0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def postprocess_regions(
    regions: list[Region],
    page_width: float | None = None,
    page_height: float | None = None,
    dbscan_eps_fraction: float = _DEFAULT_DBSCAN_EPS_FRACTION,
) -> tuple[list[Region], ColumnStructure | None]:
    """
    Run the full IEP2A postprocessing pipeline.

    Args:
        regions:              Raw detected regions (before postprocessing).
        page_width:           Actual page width in pixels from the input image.
                              When None, falls back to bbox-union inference —
                              acceptable for the stub path but produces
                              systematically underestimated column boundaries
                              (see _infer_page_dimensions).  Phase 12 must
                              supply this from the Detectron2 input tensor.
        page_height:          Actual page height in pixels from the input image.
                              Same caveats as page_width.
        dbscan_eps_fraction:  DBSCAN eps = this × page_width (default 0.08).

    Returns:
        (postprocessed_regions, column_structure)

        postprocessed_regions: NMS-deduped, confidence-recalibrated,
                               sequentially-IDed Region list.
        column_structure:      Inferred ColumnStructure, or None when no
                               text_block regions survive postprocessing.
    """
    inferred_w, inferred_h = _infer_page_dimensions(regions)
    pw = page_width if page_width is not None else inferred_w
    ph = page_height if page_height is not None else inferred_h

    after_nms = _nms_per_type(regions)
    after_recal = _recalibrate(after_nms, pw, ph)
    col_struct = _infer_column_structure(after_recal, pw, dbscan_eps_fraction)
    final = _reassign_ids(after_recal)

    return final, col_struct
