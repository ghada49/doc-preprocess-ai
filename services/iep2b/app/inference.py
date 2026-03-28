"""
services/iep2b/app/inference.py
---------------------------------
IEP2B inference helpers: image I/O and DocLayout-YOLO output → canonical Regions.

numpy and cv2 are imported at module level; both are available in the dev
and test environments (cv2 4.13, numpy 2.4).

All doclayout_yolo / torch imports are lazy (inside functions) so this
module can be imported without ML dependencies present.

DocLayout-YOLO uses the DocStructBench class vocabulary.  Each detection
carries a string class name that is passed to
class_mapping.map_native_class() to obtain the canonical RegionType.
Detections whose native class maps to None are silently excluded before
the postprocessing pipeline runs.

Exported:
    load_image_for_yolo        — storage URI → RGB np.ndarray (HxWx3 uint8)
    run_doclayout_yolo         — model + image → raw detection list
    raw_detections_to_regions  — raw detections → canonical Region list
"""

from __future__ import annotations

import cv2
import numpy as np

from services.iep2b.app.class_mapping import map_native_class
from shared.schemas.layout import Region
from shared.schemas.ucf import BoundingBox

# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------


def load_image_for_yolo(uri: str) -> np.ndarray:
    """
    Fetch an image from a storage URI and return it as an RGB numpy array
    (HxWx3 uint8), the format expected by DocLayout-YOLO's predict().

    Uses the shared storage backend (file:// or s3://) so the same URI
    conventions used elsewhere in the pipeline work here.

    Args:
        uri: Storage URI — file:// for local paths, s3:// for object store.

    Returns:
        RGB image as np.ndarray of shape (H, W, 3), dtype uint8.

    Raises:
        ValueError  — image bytes cannot be decoded by OpenCV.
        Any exception raised by StorageBackend.get_bytes() (e.g.
        FileNotFoundError for file:// URIs or botocore exceptions for
        s3:// URIs).
    """
    from shared.io.storage import get_backend

    raw_bytes = get_backend(uri).get_bytes(uri)
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"cv2.imdecode could not decode image bytes from URI: {uri!r}")
    # DocLayout-YOLO (ultralytics-based) accepts BGR or RGB arrays.
    # We convert to RGB so the colours match what the training data used.
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


# ---------------------------------------------------------------------------
# DocLayout-YOLO inference
# ---------------------------------------------------------------------------


def run_doclayout_yolo(
    model: object,
    image: np.ndarray,
    imgsz: int = 1024,
    conf_thresh: float = 0.2,
    device: str = "cpu",
) -> list[tuple[str, tuple[float, float, float, float], float]]:
    """
    Run a DocLayout-YOLO YOLOv10 model on an RGB image.

    Args:
        model:       Loaded YOLOv10 model from model.get_model().
        image:       RGB numpy array (HxWx3 uint8) from load_image_for_yolo().
        imgsz:       Inference image size passed to model.predict().
                     Default: 1024 (matches DocStructBench training resolution).
        conf_thresh: Minimum confidence threshold.  Default: 0.2.
        device:      "cuda" or "cpu".  Default: "cpu".

    Returns:
        List of (class_name, (x1, y1, x2, y2), confidence) tuples.
        Coordinates are in pixel space of the input image.
        The list may be empty if no instances pass the confidence threshold.
    """
    results = model.predict(  # type: ignore[attr-defined]
        image,
        imgsz=imgsz,
        conf=conf_thresh,
        device=device,
        verbose=False,
    )

    detections: list[tuple[str, tuple[float, float, float, float], float]] = []
    for result in results:
        if result.boxes is None:
            continue
        boxes = result.boxes
        for i in range(len(boxes)):
            cls_id = int(boxes.cls[i].item())
            class_name: str = result.names[cls_id]
            conf = float(boxes.conf[i].item())
            xyxy = boxes.xyxy[i].tolist()
            x1, y1, x2, y2 = float(xyxy[0]), float(xyxy[1]), float(xyxy[2]), float(xyxy[3])
            detections.append((class_name, (x1, y1, x2, y2), conf))

    return detections


# ---------------------------------------------------------------------------
# Raw detections → canonical Region list
# ---------------------------------------------------------------------------


def raw_detections_to_regions(
    detections: list[tuple[str, tuple[float, float, float, float], float]],
) -> list[Region]:
    """
    Convert raw (class_name, bbox, confidence) triples from DocLayout-YOLO
    to canonical Region objects via the IEP2B class mapping.

    Detections with a class_name that maps to None (explicitly excluded
    classes such as "abandon", "formula") are silently dropped.
    Degenerate bounding boxes (x1 >= x2 or y1 >= y2) are also excluded.

    IDs are assigned sequentially r1, r2, … in detection order.  The
    downstream postprocessing pipeline (postprocess_regions) will sort and
    reassign IDs by (y_min, x_min) after NMS.

    Args:
        detections: Output of run_doclayout_yolo().

    Returns:
        List of canonical Region objects.  Empty if no detections pass
        filtering.
    """
    regions: list[Region] = []
    rid = 1
    for class_name, (x1, y1, x2, y2), confidence in detections:
        region_type = map_native_class(class_name)
        if region_type is None:
            continue
        if x1 >= x2 or y1 >= y2:
            continue
        regions.append(
            Region(
                id=f"r{rid}",
                type=region_type,
                bbox=BoundingBox(x_min=x1, y_min=y1, x_max=x2, y_max=y2),
                confidence=max(0.0, min(1.0, confidence)),
            )
        )
        rid += 1
    return regions
