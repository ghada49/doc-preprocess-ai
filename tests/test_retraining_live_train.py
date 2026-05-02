from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from services.retraining_worker.app.live_train import _resolve_train_paths
from training.scripts.yolo_rgb_sanitizer import ensure_yolo_datasets_rgb


def _write_data_yaml(root: Path) -> Path:
    data_yaml = root / "data.yaml"
    data_yaml.write_text(
        "path: .\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        "  0: page\n"
        "nc: 1\n",
        encoding="utf-8",
    )
    return data_yaml


def test_ensure_yolo_datasets_rgb_converts_grayscale_images(tmp_path: Path) -> None:
    image_dir = tmp_path / "images" / "train"
    image_dir.mkdir(parents=True)
    data_yaml = _write_data_yaml(tmp_path)
    image_path = image_dir / "gray.png"

    gray = np.arange(100, dtype=np.uint8).reshape(10, 10)
    assert cv2.imwrite(str(image_path), gray)

    stats = ensure_yolo_datasets_rgb([data_yaml])

    rewritten = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    assert stats == {"checked": 1, "converted": 1}
    assert rewritten is not None
    assert rewritten.ndim == 3
    assert rewritten.shape[2] == 3
    assert np.array_equal(rewritten[:, :, 0], gray)
    assert np.array_equal(rewritten[:, :, 1], gray)
    assert np.array_equal(rewritten[:, :, 2], gray)


def test_ensure_yolo_datasets_rgb_leaves_color_images_unchanged(tmp_path: Path) -> None:
    image_dir = tmp_path / "images" / "train"
    image_dir.mkdir(parents=True)
    data_yaml = _write_data_yaml(tmp_path)
    image_path = image_dir / "color.png"

    color = np.zeros((8, 8, 3), dtype=np.uint8)
    color[:, :, 0] = 10
    color[:, :, 1] = 20
    color[:, :, 2] = 30
    assert cv2.imwrite(str(image_path), color)

    stats = ensure_yolo_datasets_rgb([data_yaml])

    rewritten = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    assert stats == {"checked": 1, "converted": 0}
    assert rewritten is not None
    assert np.array_equal(rewritten, color)


def test_resolve_train_paths_accepts_corrected_export_partial_manifest(tmp_path: Path) -> None:
    iep0_root = tmp_path / "iep0_placeholder"
    iep0_root.mkdir()

    iep1a_book = tmp_path / "iep1a" / "book"
    iep1b_newspaper = tmp_path / "iep1b" / "newspaper"
    iep1b_microfilm = tmp_path / "iep1b" / "microfilm"
    for root in (iep1a_book, iep1b_newspaper, iep1b_microfilm):
        root.mkdir(parents=True)
        _write_data_yaml(root)

    manifest = tmp_path / "retraining_train_manifest.json"
    manifest.write_text(
        "{\n"
        f'  "iep0": {{"data_root": "{iep0_root.as_posix()}"}},\n'
        f'  "iep1a": {{"book": "{(iep1a_book / "data.yaml").as_posix()}"}},\n'
        f'  "iep1b": {{"newspaper": "{(iep1b_newspaper / "data.yaml").as_posix()}", '
        f'"microfilm": "{(iep1b_microfilm / "data.yaml").as_posix()}"}}\n'
        "}\n",
        encoding="utf-8",
    )

    resolved_iep0, iep1a, iep1b = _resolve_train_paths(manifest_path=manifest)

    assert resolved_iep0 == iep0_root
    assert set(iep1a) == {"book"}
    assert set(iep1b) == {"newspaper", "microfilm"}


def test_resolve_train_paths_accepts_single_service_material_manifest(tmp_path: Path) -> None:
    iep0_root = tmp_path / "iep0_placeholder"
    iep0_root.mkdir()

    iep1a_book = tmp_path / "iep1a" / "book"
    iep1a_book.mkdir(parents=True)
    data_yaml = _write_data_yaml(iep1a_book)

    manifest = tmp_path / "retraining_train_manifest.json"
    manifest.write_text(
        "{\n"
        f'  "iep0": {{"data_root": "{iep0_root.as_posix()}"}},\n'
        f'  "iep1a": {{"book": "{data_yaml.as_posix()}"}},\n'
        '  "iep1b": {}\n'
        "}\n",
        encoding="utf-8",
    )

    _, iep1a, iep1b = _resolve_train_paths(manifest_path=manifest)

    assert set(iep1a) == {"book"}
    assert iep1b == {}
