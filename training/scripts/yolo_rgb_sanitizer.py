"""Utilities for normalizing YOLO dataset images before training."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_YOLO_IMAGE_EXTENSIONS = {
    ".bmp",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def _resolve_yolo_path(raw: Any, dataset_root: Path) -> list[Path]:
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    paths: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if not path.is_absolute():
            path = dataset_root / path
        paths.append(path)
    return paths


def _iter_yolo_image_files(data_yaml: Path) -> list[Path]:
    """
    Return local image files referenced by a YOLO data.yaml.

    Ultralytics accepts train/val/test fields as directories, files, or lists.
    Missing paths are left for the training script to report with its normal
    error; this helper only rewrites image files it can actually find.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to inspect YOLO data.yaml files") from exc

    payload = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid YOLO data.yaml format: {data_yaml}")

    root_raw = payload.get("path")
    if isinstance(root_raw, str) and root_raw.strip():
        dataset_root = Path(root_raw)
        if not dataset_root.is_absolute():
            dataset_root = (data_yaml.parent / dataset_root).resolve()
    else:
        dataset_root = data_yaml.parent

    files: list[Path] = []
    for split_key in ("train", "val", "test"):
        for split_path in _resolve_yolo_path(payload.get(split_key), dataset_root):
            if split_path.is_file() and split_path.suffix.lower() == ".txt":
                for line in split_path.read_text(encoding="utf-8").splitlines():
                    image_path = Path(line.strip())
                    if not image_path.is_absolute():
                        image_path = split_path.parent / image_path
                    if image_path.suffix.lower() in _YOLO_IMAGE_EXTENSIONS:
                        files.append(image_path)
                continue
            if split_path.is_file() and split_path.suffix.lower() in _YOLO_IMAGE_EXTENSIONS:
                files.append(split_path)
                continue
            if split_path.is_dir():
                files.extend(
                    p
                    for p in split_path.rglob("*")
                    if p.is_file() and p.suffix.lower() in _YOLO_IMAGE_EXTENSIONS
                )
    return sorted(set(files))


def _ensure_image_file_rgb(image_path: Path) -> bool:
    """
    Convert a single-channel image file to a 3-channel image in place.

    Returns True when the file was rewritten. Three-channel files are left
    untouched. Alpha-channel images are also left untouched because the YOLO
    failure this guards against is specifically one-channel input.
    """
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required to sanitize YOLO images") from exc

    image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        logger.warning("Skipping unreadable YOLO image during RGB check: %s", image_path)
        return False
    if image.ndim == 2:
        rgb_image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.ndim == 3 and image.shape[2] == 1:
        rgb_image = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    else:
        return False

    if not cv2.imwrite(str(image_path), rgb_image):
        raise RuntimeError(f"Failed to rewrite grayscale image as 3-channel: {image_path}")
    return True


def ensure_yolo_dataset_rgb(data_yaml: Path) -> dict[str, int]:
    return ensure_yolo_datasets_rgb([data_yaml])


def ensure_yolo_datasets_rgb(data_yamls: list[Path]) -> dict[str, int]:
    """
    Rewrite one-channel YOLO dataset images as 3-channel images before training.

    YOLO/Ultralytics assumes RGB-style inputs. Some archival TIFF exports are
    valid single-channel images, so training normalizes them in place before
    checksumming and launching YOLO.
    """
    checked = 0
    converted = 0
    seen: set[Path] = set()
    for data_yaml in data_yamls:
        for image_path in _iter_yolo_image_files(data_yaml):
            resolved = image_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            checked += 1
            if _ensure_image_file_rgb(resolved):
                converted += 1
    stats = {"checked": checked, "converted": converted}
    if checked:
        logger.info(
            "ensure_yolo_datasets_rgb: checked=%d converted=%d",
            checked,
            converted,
        )
    return stats
