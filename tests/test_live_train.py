"""Unit tests for retraining_worker live_train helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.retraining_worker.app.live_train import (
    RetrainingTrainConfigError,
    parse_train_script_stdout,
)


def test_parse_train_script_stdout_extracts_run_id_and_weights() -> None:
    out = """noise
LIBRARYAI_MLFLOW_RUN_ID=abc123def
LIBRARYAI_BEST_WEIGHTS=/tmp/best.pt
Done.
"""
    rid, w = parse_train_script_stdout(out)
    assert rid == "abc123def"
    assert w == Path("/tmp/best.pt")


def test_parse_train_script_stdout_empty() -> None:
    rid, w = parse_train_script_stdout("no markers\n")
    assert rid is None
    assert w is None


def test_manifest_missing_iep1a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.retraining_worker.app import live_train as lt

    manifest = {"iep0": {"data_root": str(tmp_path / "cls")}, "iep1a": {}, "iep1b": {}}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("RETRAINING_TRAIN_MANIFEST", str(p))
    (tmp_path / "cls").mkdir()

    monkeypatch.delenv("RETRAINING_IEP0_DATA_ROOT", raising=False)
    with pytest.raises(RetrainingTrainConfigError, match="at least one"):
        lt._resolve_train_paths()
