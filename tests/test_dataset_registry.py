from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from services.retraining_worker.app.dataset_registry import (
    DatasetSelectionDeferred,
    DatasetSelectionError,
    emit_default_registry,
    select_retraining_dataset,
)


def _write_registry(path: Path, datasets: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "generated_at": "2026-04-20T00:00:00+00:00", "datasets": datasets}),
        encoding="utf-8",
    )


def test_select_corrected_hybrid_prefers_latest_approved_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path
    reg = root / "training" / "preprocessing" / "dataset_registry.json"
    m1 = root / "d1.json"
    m2 = root / "d2.json"
    m1.write_text("{}", encoding="utf-8")
    m2.write_text("{}", encoding="utf-8")
    _write_registry(
        reg,
        [
            {
                "dataset_version": "ds-old",
                "dataset_checksum": "aaa",
                "manifest_path": str(m1),
                "approved": True,
                "created_at": "2026-04-19T00:00:00+00:00",
            },
            {
                "dataset_version": "ds-new",
                "dataset_checksum": "bbb",
                "manifest_path": str(m2),
                "approved": True,
                "created_at": "2026-04-20T00:00:00+00:00",
            },
        ],
    )
    monkeypatch.setenv("RETRAINING_DATASET_MODE", "corrected_hybrid")
    monkeypatch.setenv("RETRAINING_DATASET_REGISTRY_PATH", str(reg))
    sel = select_retraining_dataset(root)
    assert sel.dataset_version == "ds-new"
    assert sel.dataset_checksum == "bbb"
    assert sel.build_mode == "corrected_prebuilt"
    assert sel.manifest_path == m2


def test_select_corrected_hybrid_uses_builder_when_registry_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path
    manifest = root / "rebuilt_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    builder_script = root / "fake_builder.py"
    builder_script.write_text(
        "import json\n"
        f"print(json.dumps({{'dataset_version':'ds-r','dataset_checksum':'chk','manifest_path':r'{manifest}'}}))\n",
        encoding="utf-8",
    )
    builder = f"\"{sys.executable}\" \"{builder_script}\""
    monkeypatch.setenv("RETRAINING_DATASET_MODE", "corrected_hybrid")
    monkeypatch.setenv("RETRAINING_DATASET_REGISTRY_PATH", str(root / "missing.json"))
    monkeypatch.setenv("RETRAINING_DATASET_BUILDER_CMD", builder)
    sel = select_retraining_dataset(root)
    assert sel.build_mode == "corrected_export"
    assert sel.dataset_version == "ds-r"
    assert sel.manifest_path == manifest


def test_select_corrected_hybrid_without_registry_and_builder_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRAINING_DATASET_MODE", "corrected_hybrid")
    monkeypatch.setenv("RETRAINING_DATASET_REGISTRY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("RETRAINING_DATASET_BUILDER_CMD", f"\"{sys.executable}\" -c \"import sys; sys.exit(1)\"")
    with pytest.raises(DatasetSelectionError):
        select_retraining_dataset(tmp_path)


def test_emit_default_registry(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    manifest = tmp_path / "train_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    payload = emit_default_registry(reg, manifest)
    assert reg.is_file()
    assert payload["datasets"][0]["manifest_path"] == str(manifest)


def test_select_corrected_only_builder_min_samples_deferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path
    builder_script = root / "fake_builder.py"
    builder_script.write_text(
        "import json\n"
        "print(json.dumps({'status':'min_samples_not_met','counts':{'iep1a_book':10}}))\n",
        encoding="utf-8",
    )
    builder = f"\"{sys.executable}\" \"{builder_script}\""
    monkeypatch.setenv("RETRAINING_DATASET_MODE", "corrected_only")
    monkeypatch.setenv("RETRAINING_DATASET_BUILDER_CMD", builder)
    with pytest.raises(DatasetSelectionDeferred):
        select_retraining_dataset(root)
