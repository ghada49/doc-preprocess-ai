"""Tests for EEP-side MLflow tracking of RunPod retraining callbacks."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.eep.app.db.models import RetrainingJob
from services.eep.app.retraining_api import (
    RunPodModelVersionPayload,
    RunPodRetrainingCallbackRequest,
    _callback_mlflow_run_id,
)


def _payload(worker_run_id: str | None = "stub-run-worker") -> RunPodRetrainingCallbackRequest:
    return RunPodRetrainingCallbackRequest(
        trigger_id="trigger-1",
        job_id="job-1",
        status="completed",
        mlflow_run_id=worker_run_id,
        dataset_version="dataset-v1",
        result_model_version="rt-job-iep1a,rt-job-iep1b",
        result_mAP=0.84,
        promotion_decision="pending_gate_review",
        model_versions=[
            RunPodModelVersionPayload(
                service_name="iep1a",
                version_tag="rt-job-iep1a",
                dataset_version="dataset-v1",
                gate_results={
                    "geometry_iou": {"pass": True, "value": 0.84},
                    "golden_dataset": {"pass": True, "regressions": 0},
                },
            )
        ],
    )


def _job() -> RetrainingJob:
    return RetrainingJob(
        job_id="job-1",
        trigger_id="trigger-1",
        pipeline_type="preprocessing",
        status="running",
    )


def test_callback_mlflow_run_id_returns_worker_id_when_tracking_uri_missing(monkeypatch) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    assert _callback_mlflow_run_id(job=_job(), payload=_payload("worker-run")) == "worker-run"


def test_callback_mlflow_run_id_logs_real_mlflow_run(monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow.libraryai.local:5000")

    active_run = SimpleNamespace(info=SimpleNamespace(run_id="real-mlflow-run"))
    context = MagicMock()
    context.__enter__.return_value = active_run
    context.__exit__.return_value = False

    mlflow = MagicMock()
    mlflow.start_run.return_value = context

    with patch.dict(sys.modules, {"mlflow": mlflow}):
        run_id = _callback_mlflow_run_id(job=_job(), payload=_payload())

    assert run_id == "real-mlflow-run"
    mlflow.set_tracking_uri.assert_called_once_with("http://mlflow.libraryai.local:5000")
    mlflow.set_experiment.assert_called_once_with("libraryai_preprocessing")
    mlflow.log_metric.assert_any_call("result_mAP", 0.84)
    mlflow.log_param.assert_any_call("dataset_version", "dataset-v1")
    mlflow.log_dict.assert_called_once()
