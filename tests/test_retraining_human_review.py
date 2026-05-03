from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from services.eep.app.retraining_api import _check_training_data_sufficiency
from services.eep.app.scaling.runpod_scaler import _runpod_env
from services.retraining_worker.app.main import _build_live_callback_payload


def _session_for_rows(rows: list[tuple[bool, bool, str]]) -> MagicMock:
    session = MagicMock()
    query = session.query.return_value
    query.filter.return_value = query
    query.all.return_value = rows
    return session


def _clear_minimum_env(monkeypatch: Any) -> None:
    for family in ("iep1a", "iep1b"):
        for material in ("book", "newspaper", "microfilm"):
            monkeypatch.delenv(
                f"RETRAINING_MIN_CORRECTED_{family.upper()}_{material.upper()}",
                raising=False,
            )


def test_human_review_retraining_default_minimum_is_ten(monkeypatch: Any) -> None:
    _clear_minimum_env(monkeypatch)
    db = _session_for_rows([(True, False, "book")] * 10)

    assert _check_training_data_sufficiency(db) is None


def test_human_review_retraining_blocks_below_default_minimum(monkeypatch: Any) -> None:
    _clear_minimum_env(monkeypatch)
    db = _session_for_rows([(True, False, "book")] * 9)

    result = _check_training_data_sufficiency(db)

    assert result is not None
    assert result["reason"] == "insufficient_training_data"
    assert result["breakdown"]["iep1a/book"] == {"have": 9, "need": 10}


def test_human_review_retraining_allows_each_material_independently(monkeypatch: Any) -> None:
    _clear_minimum_env(monkeypatch)
    db = _session_for_rows([(False, True, "newspaper")] * 10)

    assert _check_training_data_sufficiency(db) is None


def test_runpod_retraining_env_inherits_live_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("RETRAINING_CALLBACK_BASE_URL", "https://eep.example.test")
    monkeypatch.setenv("RETRAINING_CALLBACK_SECRET", "secret")
    monkeypatch.setenv("LIBRARYAI_RETRAINING_TRAIN", "live")
    monkeypatch.setenv("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "live")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    monkeypatch.setenv("RETRAINING_MIN_CORRECTED_IEP1A_BOOK", "10")

    env = _runpod_env(
        "trigger-1",
        job_id="job-1",
        extra_env={
            "RETRAINING_DATASET_ARCHIVE_URI": "s3://bucket/retraining/datasets/job-1.tar.gz",
            "RETRAINING_TRAIN_MANIFEST": "/workspace/retraining_dataset/job-1/retraining_train_manifest.json",
        },
    )

    assert env["RETRAINING_WORKER_MODE"] == "callback_once"
    assert env["LIBRARYAI_RETRAINING_TRAIN"] == "live"
    assert env["LIBRARYAI_RETRAINING_GOLDEN_EVAL"] == "live"
    assert "DATABASE_URL" not in env
    assert env["MLFLOW_TRACKING_URI"] == "http://mlflow:5000"
    assert env["RETRAINING_MIN_CORRECTED_IEP1A_BOOK"] == "10"
    assert env["RETRAINING_DATASET_ARCHIVE_URI"] == "s3://bucket/retraining/datasets/job-1.tar.gz"
    assert (
        env["RETRAINING_TRAIN_MANIFEST"]
        == "/workspace/retraining_dataset/job-1/retraining_train_manifest.json"
    )


def test_runpod_live_callback_uses_corrected_only_dataset(monkeypatch: Any) -> None:
    monkeypatch.setenv("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub")
    selection = MagicMock()
    selection.dataset_version = "ds-human-review"
    selection.dataset_checksum = "checksum-1"
    selection.manifest_path = Path("manifest.json")

    trained = MagicMock()
    trained.iep1a_weights = {"book": Path("iep1a.pt")}
    trained.iep1b_weights = {}
    trained.mlflow_run_ids = ["run-1"]
    trained.s3_uris = ["s3://bucket/retraining/weights/job-1/iep1a_book.pt"]

    with (
        patch(
            "services.retraining_worker.app.main.select_retraining_dataset",
            return_value=selection,
        ) as select_mock,
        patch(
            "services.retraining_worker.app.main.run_live_preprocessing_training",
            return_value=trained,
        ),
    ):
        payload = _build_live_callback_payload("trigger-1", "job-1")

    select_mock.assert_called_once()
    assert select_mock.call_args.kwargs["prefer_mode"] == "corrected_only"
    assert payload["status"] == "completed"
    assert payload["dataset_version"] == "ds-human-review"
    assert payload["mlflow_run_id"] == "run-1"
    assert payload["result_model_version"] == "rt-job1-iep1a"
    assert payload["model_versions"][0]["service_name"] == "iep1a"
    assert (
        "s3_weights:s3://bucket/retraining/weights/job-1/iep1a_book.pt"
        in payload["model_versions"][0]["notes"]
    )


def test_runpod_live_callback_prefers_provided_manifest(monkeypatch: Any) -> None:
    monkeypatch.setenv("LIBRARYAI_RETRAINING_GOLDEN_EVAL", "stub")
    monkeypatch.setenv("RETRAINING_TRAIN_MANIFEST", "/workspace/retraining_dataset/job-1/retraining_train_manifest.json")
    monkeypatch.setenv("RETRAINING_DATASET_VERSION", "ds-prebuilt")
    monkeypatch.setenv("RETRAINING_DATASET_CHECKSUM", "checksum-prebuilt")

    trained = MagicMock()
    trained.iep1a_weights = {"book": Path("iep1a.pt")}
    trained.iep1b_weights = {}
    trained.mlflow_run_ids = ["run-1"]
    trained.s3_uris = ["s3://bucket/retraining/weights/job-1/iep1a_book.pt"]

    with (
        patch("services.retraining_worker.app.main.select_retraining_dataset") as select_mock,
        patch(
            "services.retraining_worker.app.main.run_live_preprocessing_training",
            return_value=trained,
        ) as train_mock,
    ):
        payload = _build_live_callback_payload("trigger-1", "job-1")

    select_mock.assert_not_called()
    assert train_mock.call_args.kwargs["manifest_path"] == Path(
        "/workspace/retraining_dataset/job-1/retraining_train_manifest.json"
    )
    assert payload["dataset_version"] == "ds-prebuilt"
    assert "dataset_checksum=checksum-prebuilt" in payload["model_versions"][0]["notes"]
