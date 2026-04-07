"""
Post-processing: validate page_left / page_right assignments after
YOLOv8-seg inference.

The model was trained on all 4 rotations (0°, 90°, 180°, 270°) with
CONSISTENT labels — class IDs track physical page identity, not spatial
position. The model learned to distinguish page_left from page_right
using content/text orientation cues.

Therefore: NO orientation detection or class swapping is needed.
The model's output directly gives the correct physical page identity.

Post-processing only performs validation:
  - Verify page count (0, 1, or 2)
  - If 2 pages: check both classes are present (one 0, one 1)
  - Flag duplicates or low confidence for human review
"""

import numpy as np
from typing import List, Tuple, Optional
from dataclasses import dataclass, field


# ── Data structures ────────────────────────────────────────────────
@dataclass
class PageDetection:
    class_id: int           # 0 = page_left, 1 = page_right
    confidence: float
    polygon: np.ndarray     # shape (N, 2), normalized [0,1]
    mask: Optional[np.ndarray] = None

    @property
    def centroid(self) -> Tuple[float, float]:
        return float(self.polygon[:, 0].mean()), float(self.polygon[:, 1].mean())

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (
            float(self.polygon[:, 0].min()),
            float(self.polygon[:, 1].min()),
            float(self.polygon[:, 0].max()),
            float(self.polygon[:, 1].max()),
        )


@dataclass
class PostProcessResult:
    detections: List[PageDetection]
    page_count: int
    split_required: bool
    layout: str = "unknown"         # "horizontal", "vertical", "single", "empty"
    needs_review: bool = False
    warnings: List[str] = field(default_factory=list)


# ── Layout classification ─────────────────────────────────────────
def classify_layout(det_a: PageDetection, det_b: PageDetection) -> str:
    cx_a, cy_a = det_a.centroid
    cx_b, cy_b = det_b.centroid
    dx = abs(cx_a - cx_b)
    dy = abs(cy_a - cy_b)
    if dx < 1e-6 and dy < 1e-6:
        return "ambiguous"
    return "horizontal" if dx >= dy else "vertical"


# ── Core post-processing (validation only) ─────────────────────────
CONFIDENCE_THRESHOLD = 0.50


def validate_page_assignments(
    detections: List[PageDetection],
) -> PostProcessResult:
    """
    Validate model output. No swapping — model directly predicts
    physical page identity across all orientations.

    Checks:
      - Page count is 0, 1, or 2
      - If 2 pages: both classes present (0 and 1)
      - Confidence is above threshold
    """
    warnings = []
    needs_review = False

    # ── 0 pages ───────────────────────────────────────────────
    if len(detections) == 0:
        return PostProcessResult(
            [], 0, False, layout="empty",
            needs_review=True,
            warnings=["No pages detected."],
        )

    # ── 1 page ────────────────────────────────────────────────
    if len(detections) == 1:
        det = detections[0]
        if det.confidence < CONFIDENCE_THRESHOLD:
            needs_review = True
            warnings.append(
                f"Low confidence ({det.confidence:.2f}). Needs review."
            )
        return PostProcessResult(
            detections, 1, False, layout="single",
            needs_review=needs_review, warnings=warnings,
        )

    # ── >2 pages: keep top 2 ─────────────────────────────────
    if len(detections) > 2:
        warnings.append(
            f"Expected 1-2 pages, got {len(detections)}. Using top 2."
        )
        detections = sorted(
            detections, key=lambda d: d.confidence, reverse=True
        )[:2]

    # ── 2 pages: validate ─────────────────────────────────────
    det_a, det_b = detections[0], detections[1]
    layout = classify_layout(det_a, det_b)

    # Check: both classes must be present
    classes = {det_a.class_id, det_b.class_id}
    if classes != {0, 1}:
        needs_review = True
        warnings.append(
            f"Duplicate classes detected: both are class {det_a.class_id}. "
            f"Needs review."
        )

    # Check: confidence
    min_conf = min(det_a.confidence, det_b.confidence)
    if min_conf < CONFIDENCE_THRESHOLD:
        needs_review = True
        warnings.append(
            f"Low confidence ({min_conf:.2f} < {CONFIDENCE_THRESHOLD}). "
            f"Needs review."
        )

    # Order detections: page_left first, page_right second
    ordered = sorted(detections, key=lambda d: d.class_id)

    return PostProcessResult(
        detections=ordered,
        page_count=2,
        split_required=True,
        layout=layout,
        needs_review=needs_review,
        warnings=warnings,
    )


# ── Utility: parse YOLO prediction ────────────────────────────────
def parse_yolo_predictions(results, conf_threshold: float = 0.25) -> List[PageDetection]:
    detections = []
    if results.masks is None:
        return detections
    for mask_xy, box in zip(results.masks.xyn, results.boxes):
        conf = float(box.conf[0])
        if conf < conf_threshold:
            continue
        detections.append(PageDetection(
            class_id=int(box.cls[0]),
            confidence=conf,
            polygon=np.array(mask_xy),
        ))
    return detections


# ── Demo ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    def show(label, r):
        review = " ⚠ REVIEW" if r.needs_review else " ✓"
        print(f"  Layout={r.layout}, Pages={r.page_count}, Split={r.split_required}{review}")
        print(f"  Classes: {[d.class_id for d in r.detections]}")
        for w in r.warnings:
            print(f"    → {w}")

    print("=== 2 pages, correct classes, high confidence ===")
    dets = [
        PageDetection(0, 0.95, np.array([[.1,.1],[.45,.1],[.45,.9],[.1,.9]])),
        PageDetection(1, 0.93, np.array([[.55,.1],[.9,.1],[.9,.9],[.55,.9]])),
    ]
    show("good", validate_page_assignments(dets))

    print("\n=== 2 pages, duplicate classes (both page_left) ===")
    dets = [
        PageDetection(0, 0.90, np.array([[.1,.1],[.45,.1],[.45,.9],[.1,.9]])),
        PageDetection(0, 0.85, np.array([[.55,.1],[.9,.1],[.9,.9],[.55,.9]])),
    ]
    show("dup", validate_page_assignments(dets))

    print("\n=== 2 pages, low confidence ===")
    dets = [
        PageDetection(0, 0.40, np.array([[.1,.1],[.45,.1],[.45,.9],[.1,.9]])),
        PageDetection(1, 0.35, np.array([[.55,.1],[.9,.1],[.9,.9],[.55,.9]])),
    ]
    show("low conf", validate_page_assignments(dets))

    print("\n=== Single page, high confidence ===")
    dets = [
        PageDetection(0, 0.97, np.array([[.15,.1],[.85,.1],[.85,.9],[.15,.9]])),
    ]
    show("single", validate_page_assignments(dets))

    print("\n=== No pages ===")
    show("empty", validate_page_assignments([]))

    print("\n=== 3 detections (keeps top 2) ===")
    dets = [
        PageDetection(0, 0.95, np.array([[.1,.1],[.45,.1],[.45,.9],[.1,.9]])),
        PageDetection(1, 0.90, np.array([[.55,.1],[.9,.1],[.9,.9],[.55,.9]])),
        PageDetection(0, 0.30, np.array([[.2,.2],[.4,.2],[.4,.8],[.2,.8]])),
    ]
    show("3 dets", validate_page_assignments(dets))
