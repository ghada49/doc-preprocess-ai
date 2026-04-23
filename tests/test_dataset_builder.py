from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import sqlite3

from services.dataset_builder.app.main import (
    _canonicalize_corners,
    _extract_corners_abs,
    _resolve_dimensions,
    _rotate_point,
    _seg_line,
    _to_norm,
)


def _write_train_manifest(tmp_path: Path) -> Path:
    iep0 = tmp_path / "iep0"
    iep0.mkdir()
    (iep0 / "a.txt").write_text("x", encoding="utf-8")

    iep1a = {}
    iep1b = {}
    for family, target in (("iep1a", iep1a), ("iep1b", iep1b)):
        for mat in ("book", "newspaper", "microfilm"):
            d = tmp_path / family / mat
            d.mkdir(parents=True)
            y = d / "data.yaml"
            y.write_text("path: .\ntrain: train\nval: val\n", encoding="utf-8")
            target[mat] = str(y)

    manifest = tmp_path / "retraining_train_manifest.json"
    manifest.write_text(
        json.dumps({"iep0": {"data_root": str(iep0)}, "iep1a": iep1a, "iep1b": iep1b}),
        encoding="utf-8",
    )
    return manifest


def test_dataset_builder_writes_registry(tmp_path: Path) -> None:
    manifest = _write_train_manifest(tmp_path)
    registry = tmp_path / "dataset_registry.json"
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "services" / "dataset_builder" / "app" / "main.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "scheduled",
            "--approved",
            "--manifest",
            str(manifest),
            "--registry-path",
            str(registry),
            "--dataset-version",
            "ds-test-1",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["dataset_version"] == "ds-test-1"
    assert out["manifest_path"] == str(manifest)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert len(payload["datasets"]) == 1
    assert payload["datasets"][0]["approved"] is True


def test_dataset_builder_corrected_export_min_samples_not_met(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "services" / "dataset_builder" / "app" / "main.py"
    db_path = tmp_path / "eep.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE page_lineage (
                lineage_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                material_type TEXT NOT NULL,
                iep1a_used BOOLEAN NOT NULL DEFAULT 0,
                iep1b_used BOOLEAN NOT NULL DEFAULT 0,
                human_corrected BOOLEAN NOT NULL DEFAULT 0,
                human_correction_timestamp TEXT NULL,
                human_correction_fields TEXT NULL,
                acceptance_decision TEXT NULL,
                gate_results TEXT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    env = dict(**os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "corrected-export",
            "--source-window",
            "all",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "min_samples_not_met"


def test_resolve_dimensions_prefers_correction_wh() -> None:
    w, h = _resolve_dimensions(
        {"image_width": 800, "image_height": 600},
        {"downsample": {"downsampled_width": 99, "downsampled_height": 99}},
    )
    assert (w, h) == (800.0, 600.0)


def test_resolve_dimensions_falls_back_to_gate_downsample() -> None:
    w, h = _resolve_dimensions(
        {},
        {"downsample": {"downsampled_width": 1024, "downsampled_height": 1536}},
    )
    assert (w, h) == (1024.0, 1536.0)


def test_extract_corners_quad_mode() -> None:
    corners = _extract_corners_abs(
        {"quad_points": [[10, 20], [100, 20], [100, 200], [10, 200]]}
    )
    assert corners == [(10.0, 20.0), (100.0, 20.0), (100.0, 200.0), (10.0, 200.0)]


def test_extract_corners_rect_mode_no_deskew() -> None:
    corners = _extract_corners_abs({"crop_box": [10, 20, 110, 220]})
    assert corners == [(10.0, 20.0), (110.0, 20.0), (110.0, 220.0), (10.0, 220.0)]


def test_extract_corners_rect_mode_with_deskew_90() -> None:
    corners = _extract_corners_abs({"crop_box": [0, 0, 100, 100], "deskew_angle": 90.0})
    cx = cy = 50.0
    raw = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    expected = [_rotate_point(x, y, cx, cy, 90.0) for x, y in raw]
    for (x1, y1), (x2, y2) in zip(corners, expected):
        assert math.isclose(x1, x2, rel_tol=0, abs_tol=1e-9)
        assert math.isclose(y1, y2, rel_tol=0, abs_tol=1e-9)


def test_canonicalize_corners_tl_tr_br_bl() -> None:
    # Unordered input; canonical order TL, TR, BR, BL
    corners = [(100, 20), (10, 200), (100, 200), (10, 20)]
    ordered = _canonicalize_corners(corners)
    assert ordered[0] == (10, 20)  # TL
    assert ordered[1] == (100, 20)  # TR
    assert ordered[2] == (100, 200)  # BR
    assert ordered[3] == (10, 200)  # BL


def test_to_norm_and_seg_line() -> None:
    corners_abs = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    norm = _to_norm(corners_abs, 100.0, 200.0)
    assert norm == [(0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.0, 0.5)]
    line = _seg_line(norm)
    assert line.startswith("0 ")
    parts = line.split()[1:]
    assert len(parts) == 8
