from __future__ import annotations

import cv2
import numpy as np

from shared.schemas.layout import Region, RegionType
from shared.schemas.ucf import BoundingBox

PUBLAYNET_CLASS_MAP: dict[str, RegionType] = {
    "text": RegionType.text_block,
    "title": RegionType.title,
    "list": RegionType.text_block,
    "table": RegionType.table,
    "figure": RegionType.image,
}


def load_image_from_uri(uri: str) -> np.ndarray:
    from shared.io.storage import get_backend

    raw_bytes = get_backend(uri).get_bytes(uri)
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"cv2.imdecode could not decode image bytes from URI: {uri!r}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def run_detectron2(
    predictor: object,
    image: np.ndarray,
) -> list[tuple[str, tuple[float, float, float, float], float]]:
    layout = predictor.detect(image)  # type: ignore[attr-defined]
    detections: list[tuple[str, tuple[float, float, float, float], float]] = []

    for block in layout:
        label = str(getattr(block, "type", "")).strip()
        score = float(getattr(block, "score", 0.0) or 0.0)
        x1 = float(block.block.x_1)
        y1 = float(block.block.y_1)
        x2 = float(block.block.x_2)
        y2 = float(block.block.y_2)
        detections.append((label, (x1, y1, x2, y2), score))

    return detections


def raw_detections_to_regions(
    detections: list[tuple[str, tuple[float, float, float, float], float]],
    class_map: dict[str, RegionType],
) -> list[Region]:
    regions: list[Region] = []
    rid = 1
    for label, (x1, y1, x2, y2), confidence in detections:
        region_type = class_map.get(label.strip().lower())
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
