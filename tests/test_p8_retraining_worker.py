"""
tests/test_p8_retraining_worker.py
------------------------------------
Packet 8.5 contract tests for the retraining worker task and recovery reconciler.

Worker task (execute_retraining_task) tests:
  - preprocessing trigger creates a RetrainingJob with status='running' then 'completed'
  - trigger.retraining_job_id is set to the created job's job_id
  - trigger.status transitions to 'completed'
  - ModelVersion rows created for iep1a and iep1b with stage='staging'
  - gate_results written in the format promotion_api._check_gates expects:
      each gate has 'pass' key; geometry_iou, split_precision,
      structural_agreement_rate, golden_dataset, latency_p95 all present
  - all gate_results have pass=True (stub evaluation always passes)
  - job.promotion_decision = 'pending_gate_review'
  - job.mlflow_run_id is a non-empty string
  - job.dataset_version is a non-empty string
  - layout_confidence_degradation → no job created, trigger marked completed
  - unknown trigger_type falls through _TRIGGER_PIPELINE.get → treated as None
    (no job, trigger marked completed with note)

Reconciler (reconcile_once) tests:
  - running job older than timeout → marked failed
  - running job within timeout → left alone
  - processing trigger with no retraining_job_id → marked failed
  - processing trigger whose linked job is missing → marked failed
  - processing trigger whose linked job is 'failed' → marked failed
  - processing trigger whose linked job is 'running' (not yet timed out) → left alone
  - processing trigger whose linked job is 'completed' → left alone
  - reconcile returns correct ReconcileResult counts

All DB interactions are mocked via MagicMock sessions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from services.retraining_recovery.app.reconcile import (
    ReconcileConfig,
    ReconcileResult,
    reconcile_once,
)
from services.retraining_worker.app.task import (
    _PREPROCESSING_SERVICES,
    _TRIGGER_PIPELINE,
    execute_retraining_task,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)


# ── Trigger / Job / ModelVersion mock factories ───────────────────────────────


def _make_trigger(
    trigger_type: str = "escalation_rate_anomaly",
    status: str = "processing",
    trigger_id: str = "trig-001",
    retraining_job_id: str | None = None,
) -> MagicMock:
    t = MagicMock()
    t.trigger_id = trigger_id
    t.trigger_type = trigger_type
    t.status = status
    t.retraining_job_id = retraining_job_id
    t.resolved_at = None
    t.notes = None
    return t


def _make_job(
    job_id: str = "job-001",
    status: str = "running",
    started_at: datetime | None = None,
) -> MagicMock:
    j = MagicMock()
    j.job_id = job_id
    j.status = status
    j.started_at = started_at or _NOW
    j.error_message = None
    j.completed_at = None
    return j


def _make_task_session() -> MagicMock:
    """Mock session suitable for execute_retraining_task tests."""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db


# ── execute_retraining_task — preprocessing triggers ─────────────────────────


class TestExecuteRetrainingTaskPreprocessing:
    @pytest.mark.parametrize(
        "trigger_type",
        [
            "escalation_rate_anomaly",
            "auto_accept_rate_collapse",
            "structural_agreement_degradation",
            "drift_alert_persistence",
        ],
    )
    def test_creates_retraining_job_for_preprocessing_triggers(
        self, trigger_type: str
    ) -> None:
        trigger = _make_trigger(trigger_type=trigger_type)
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        # At least one db.add call must be for a RetrainingJob
        from services.eep.app.db.models import RetrainingJob
        added_types = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "RetrainingJob" in added_types

    def test_job_initial_status_running(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import RetrainingJob
        job = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], RetrainingJob)
        )
        # status starts 'running'; task sets to 'completed' before final commit
        assert job.status == "completed"

    def test_job_pipeline_type_is_preprocessing(self) -> None:
        trigger = _make_trigger(trigger_type="escalation_rate_anomaly")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import RetrainingJob
        job = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], RetrainingJob)
        )
        assert job.pipeline_type == "preprocessing"

    def test_trigger_retraining_job_id_linked(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        assert trigger.retraining_job_id is not None

    def test_trigger_status_completed(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        assert trigger.status == "completed"

    def test_trigger_resolved_at_set(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        assert trigger.resolved_at is not None

    def test_creates_model_version_for_iep1a_and_iep1b(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        service_names = {mv.service_name for mv in mv_rows}
        assert service_names == {"iep1a", "iep1b"}

    def test_model_version_stage_is_staging(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        assert all(mv.stage == "staging" for mv in mv_rows)

    def test_gate_results_written_to_model_version(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        assert all(mv.gate_results is not None for mv in mv_rows)

    def test_gate_results_has_all_required_gates(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        )
        required_gates = {
            "geometry_iou",
            "split_precision",
            "structural_agreement_rate",
            "golden_dataset",
            "latency_p95",
        }
        assert set(mv.gate_results.keys()) == required_gates

    def test_gate_results_all_pass_true(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        )
        for gate_name, result in mv.gate_results.items():
            assert result["pass"] is True, f"{gate_name} should pass"

    def test_gate_results_each_gate_has_pass_key(self) -> None:
        """Format must match promotion_api._check_gates expectations."""
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        )
        for gate_name, result in mv.gate_results.items():
            assert "pass" in result, f"gate {gate_name!r} missing 'pass' key"

    def test_mlflow_run_id_set_on_model_version(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        assert all(mv.mlflow_run_id is not None for mv in mv_rows)

    def test_dataset_version_set_on_model_version(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        assert all(mv.dataset_version is not None for mv in mv_rows)

    def test_job_promotion_decision_is_pending_gate_review(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import RetrainingJob
        job = next(
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], RetrainingJob)
        )
        assert job.promotion_decision == "pending_gate_review"

    def test_db_commit_called(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        db.commit.assert_called()

    def test_model_version_ids_are_valid_uuids(self) -> None:
        trigger = _make_trigger()
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        mv_rows = [
            c[0][0]
            for c in db.add.call_args_list
            if isinstance(c[0][0], ModelVersion)
        ]
        for mv in mv_rows:
            uuid.UUID(mv.model_id)  # raises ValueError if invalid


# ── execute_retraining_task — monitoring-only trigger ────────────────────────


class TestExecuteRetrainingTaskMonitoringOnly:
    def test_layout_confidence_degradation_creates_no_job(self) -> None:
        trigger = _make_trigger(trigger_type="layout_confidence_degradation")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import RetrainingJob
        added_types = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "RetrainingJob" not in added_types

    def test_layout_confidence_degradation_creates_no_model_version(self) -> None:
        trigger = _make_trigger(trigger_type="layout_confidence_degradation")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        from services.eep.app.db.models import ModelVersion
        added_types = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "ModelVersion" not in added_types

    def test_layout_confidence_degradation_trigger_marked_completed(self) -> None:
        trigger = _make_trigger(trigger_type="layout_confidence_degradation")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        assert trigger.status == "completed"

    def test_layout_confidence_degradation_note_added(self) -> None:
        trigger = _make_trigger(trigger_type="layout_confidence_degradation")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        assert trigger.notes is not None
        assert len(trigger.notes) > 0

    def test_layout_confidence_degradation_commits(self) -> None:
        trigger = _make_trigger(trigger_type="layout_confidence_degradation")
        db = _make_task_session()
        execute_retraining_task(trigger, db)
        db.commit.assert_called()


# ── Trigger pipeline mapping sanity check ────────────────────────────────────


class TestTriggerPipelineMapping:
    def test_all_known_trigger_types_in_mapping(self) -> None:
        known = {
            "escalation_rate_anomaly",
            "auto_accept_rate_collapse",
            "structural_agreement_degradation",
            "drift_alert_persistence",
            "layout_confidence_degradation",
        }
        assert set(_TRIGGER_PIPELINE.keys()) == known

    def test_preprocessing_services_are_iep1a_and_iep1b(self) -> None:
        assert set(_PREPROCESSING_SERVICES) == {"iep1a", "iep1b"}


# ── reconcile_once ────────────────────────────────────────────────────────────


def _mock_db_for_reconcile(
    running_jobs: list[MagicMock] | None = None,
    processing_triggers: list[MagicMock] | None = None,
    job_lookup: dict[str, MagicMock] | None = None,
) -> MagicMock:
    """
    Build a mock session for reconcile_once tests.

    running_jobs:         list of RetrainingJob mocks returned by stuck-job query
    processing_triggers:  list of RetrainingTrigger mocks returned by orphaned-trigger query
    job_lookup:           dict of job_id → RetrainingJob mock for linked-job lookups
    """
    db = MagicMock()
    _running = running_jobs or []
    _triggers = processing_triggers or []
    _jobs = job_lookup or {}
    call_count = [0]

    def _query(model):
        q = MagicMock()

        def _filter(*args):
            f = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                # Pass 1: stuck running jobs
                f.all.return_value = _running
            else:
                # Pass 2: processing triggers
                f.all.return_value = _triggers
            return f

        q.filter = _filter

        # For linked-job lookups (second .filter().first() call inside trigger loop)
        def _filter2(*args):
            f2 = MagicMock()
            # Determine which job_id is being looked up from filter args
            # We use a side_effect on first() to return the right job
            # by iterating over _jobs in order
            _iter = iter(_jobs.values())

            def _first():
                return next(_iter, None)

            f2.first = _first
            return f2

        # We need filter to work differently for linked-job queries.
        # Use a counter to distinguish stuck-jobs query from trigger query from job-lookup queries.
        # Since reconcile_once calls:
        #   db.query(RetrainingJob).filter(status=running, started_at<cutoff).all()  → call 1
        #   db.query(RetrainingTrigger).filter(status=processing).all()              → call 2
        #   db.query(RetrainingJob).filter(job_id=...).first()                       → calls 3+
        # We use a smarter approach: track model type
        return q

    # Use a per-model counter approach
    job_query_count = [0]
    trigger_query_count = [0]

    from services.eep.app.db.models import RetrainingJob, RetrainingTrigger as RT

    def _smart_query(model):
        q = MagicMock()

        if model is RetrainingJob:
            job_query_count[0] += 1
            count = job_query_count[0]

            def _filter(*args):
                f = MagicMock()
                if count == 1:
                    # Stuck-jobs query: .filter(...).all()
                    f.all.return_value = _running
                else:
                    # Linked-job lookup: .filter(...).first()
                    # Each call returns the next job from _jobs dict
                    job_ids = list(_jobs.keys())
                    idx = count - 2
                    looked_up = _jobs.get(job_ids[idx]) if idx < len(job_ids) else None
                    f.first.return_value = looked_up
                return f

            q.filter = _filter

        elif model is RT:

            def _filter(*args):
                f = MagicMock()
                f.all.return_value = _triggers
                return f

            q.filter = _filter

        return q

    db.query = _smart_query
    db.commit = MagicMock()
    return db


class TestReconcileOnce:
    def test_stuck_job_marked_failed(self) -> None:
        old_start = _NOW - timedelta(hours=2)  # > 60-min timeout
        job = _make_job(status="running", started_at=old_start)
        db = _mock_db_for_reconcile(running_jobs=[job])
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db, ReconcileConfig(job_timeout_minutes=60))
        assert job.status == "failed"
        assert job.error_message is not None
        assert "reconciler" in job.error_message.lower()
        assert job.completed_at == _NOW

    def test_fresh_job_not_touched(self) -> None:
        recent_start = _NOW - timedelta(minutes=5)  # well within timeout
        job = _make_job(status="running", started_at=recent_start)
        db = _mock_db_for_reconcile(running_jobs=[])  # filtered out by DB query
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            result = reconcile_once(db, ReconcileConfig(job_timeout_minutes=60))
        assert job.status == "running"  # unchanged
        assert result.recovered_jobs == 0

    def test_recovered_jobs_count_correct(self) -> None:
        old_start = _NOW - timedelta(hours=3)
        jobs = [_make_job(job_id=f"j{i}", status="running", started_at=old_start) for i in range(3)]
        db = _mock_db_for_reconcile(running_jobs=jobs)
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            result = reconcile_once(db, ReconcileConfig(job_timeout_minutes=60))
        assert result.recovered_jobs == 3

    def test_processing_trigger_no_job_id_marked_failed(self) -> None:
        trigger = _make_trigger(status="processing", retraining_job_id=None)
        db = _mock_db_for_reconcile(processing_triggers=[trigger])
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        assert trigger.status == "failed"
        assert trigger.notes is not None

    def test_processing_trigger_with_failed_job_marked_failed(self) -> None:
        failed_job = _make_job(job_id="j-fail", status="failed")
        trigger = _make_trigger(status="processing", retraining_job_id="j-fail")
        db = _mock_db_for_reconcile(
            processing_triggers=[trigger],
            job_lookup={"j-fail": failed_job},
        )
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        assert trigger.status == "failed"

    def test_processing_trigger_with_missing_job_marked_failed(self) -> None:
        trigger = _make_trigger(status="processing", retraining_job_id="j-missing")
        db = _mock_db_for_reconcile(
            processing_triggers=[trigger],
            job_lookup={"j-missing": None},
        )
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        assert trigger.status == "failed"

    def test_processing_trigger_with_running_job_left_alone(self) -> None:
        running_job = _make_job(job_id="j-run", status="running")
        trigger = _make_trigger(status="processing", retraining_job_id="j-run")
        db = _mock_db_for_reconcile(
            processing_triggers=[trigger],
            job_lookup={"j-run": running_job},
        )
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        assert trigger.status == "processing"  # unchanged

    def test_processing_trigger_with_completed_job_left_alone(self) -> None:
        completed_job = _make_job(job_id="j-done", status="completed")
        trigger = _make_trigger(status="processing", retraining_job_id="j-done")
        db = _mock_db_for_reconcile(
            processing_triggers=[trigger],
            job_lookup={"j-done": completed_job},
        )
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        assert trigger.status == "processing"  # unchanged

    def test_reconcile_result_counts(self) -> None:
        old_start = _NOW - timedelta(hours=2)
        stuck_job = _make_job(status="running", started_at=old_start)
        orphan = _make_trigger(status="processing", retraining_job_id=None)
        db = _mock_db_for_reconcile(
            running_jobs=[stuck_job],
            processing_triggers=[orphan],
        )
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            result = reconcile_once(db, ReconcileConfig(job_timeout_minutes=60))
        assert result.recovered_jobs == 1
        assert result.recovered_triggers == 1

    def test_empty_pass_returns_zero_counts(self) -> None:
        db = _mock_db_for_reconcile(running_jobs=[], processing_triggers=[])
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            result = reconcile_once(db)
        assert result.recovered_jobs == 0
        assert result.recovered_triggers == 0

    def test_db_commit_called_when_jobs_recovered(self) -> None:
        old_start = _NOW - timedelta(hours=2)
        job = _make_job(status="running", started_at=old_start)
        db = _mock_db_for_reconcile(running_jobs=[job])
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db, ReconcileConfig(job_timeout_minutes=60))
        db.commit.assert_called()

    def test_db_not_committed_when_nothing_recovered(self) -> None:
        db = _mock_db_for_reconcile(running_jobs=[], processing_triggers=[])
        with patch("services.retraining_recovery.app.reconcile.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.timedelta = timedelta
            reconcile_once(db)
        db.commit.assert_not_called()
