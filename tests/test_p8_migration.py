"""
tests/test_p8_migration.py
--------------------------
Packet 8.1 validator tests for the Phase 8 MLOps DB migration.

Tests validate the migration file's SQL content against spec Section 13
and roadmap Packet 8.1 without requiring a live PostgreSQL connection.

Definition of done (Packet 8.1):
  - Phase 8 tables exist (all 6 MLOps tables created in 0003 migration)
  - Phase 1 tables are NOT modified by 0003 (separation invariant)
  - Revision chain is correct: 0003 → down_revision 0002
  - All required columns per table are present
  - All required CHECK constraints are present
  - ORM models for all 6 MLOps tables are importable from models.py
"""

from __future__ import annotations

import importlib
import pathlib
import types
from typing import Any

import pytest

# ── Paths ──────────────────────────────────────────────────────────────────────

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent
    / "services"
    / "eep"
    / "migrations"
    / "versions"
    / "0003_p8_mlops_tables.py"
)


def _migration_source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _load_migration() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0003", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Migration file structure ───────────────────────────────────────────────────


class TestMigrationFileStructure:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_PATH.exists(), f"Migration not found: {_MIGRATION_PATH}"

    def test_has_upgrade_function(self) -> None:
        mod = _load_migration()
        assert callable(getattr(mod, "upgrade", None))

    def test_has_downgrade_function(self) -> None:
        mod = _load_migration()
        assert callable(getattr(mod, "downgrade", None))

    def test_revision_is_0003(self) -> None:
        mod = _load_migration()
        assert getattr(mod, "revision", None) == "0003"

    def test_down_revision_is_0002(self) -> None:
        # Must chain from Phase 5 migration, not from 0001
        mod = _load_migration()
        assert getattr(mod, "down_revision", "NOT_SET") == "0002"


# ── Six MLOps tables present ───────────────────────────────────────────────────


_MLOPS_TABLES = [
    "model_versions",
    "policy_versions",
    "task_retry_states",
    "retraining_triggers",
    "retraining_jobs",
    "slo_audit_samples",
]

_PHASE1_TABLES = [
    "jobs",
    "job_pages",
    "page_lineage",
    "service_invocations",
    "quality_gate_log",
    "users",
]


class TestMLOpsTables:
    @pytest.mark.parametrize("table", _MLOPS_TABLES)
    def test_table_created(self, table: str) -> None:
        src = _migration_source()
        assert f"CREATE TABLE {table}" in src, f"CREATE TABLE {table} missing"

    @pytest.mark.parametrize("table", _MLOPS_TABLES)
    def test_table_dropped_in_downgrade(self, table: str) -> None:
        src = _migration_source()
        assert f"DROP TABLE IF EXISTS {table}" in src, (
            f"DROP TABLE IF EXISTS {table} missing from downgrade"
        )

    def test_exactly_six_tables_created(self) -> None:
        src = _migration_source()
        count = src.count("CREATE TABLE ")
        assert count == 6, f"Expected 6 CREATE TABLE statements; found {count}"

    @pytest.mark.parametrize("table", _PHASE1_TABLES)
    def test_phase1_tables_not_created(self, table: str) -> None:
        src = _migration_source()
        assert f"CREATE TABLE {table}" not in src, (
            f"Phase 1 table '{table}' must NOT be created in Phase 8 migration"
        )

    @pytest.mark.parametrize("table", _PHASE1_TABLES)
    def test_phase1_tables_not_dropped(self, table: str) -> None:
        src = _migration_source()
        assert f"DROP TABLE IF EXISTS {table}" not in src, (
            f"Phase 1 table '{table}' must NOT be dropped in Phase 8 migration"
        )

    @pytest.mark.parametrize("table", _PHASE1_TABLES)
    def test_phase1_tables_not_altered(self, table: str) -> None:
        src = _migration_source()
        assert f"ALTER TABLE {table}" not in src, (
            f"Phase 1 table '{table}' must NOT be altered in Phase 8 migration"
        )


# ── model_versions spec compliance ────────────────────────────────────────────


class TestModelVersionsTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in [
            "model_id",
            "service_name",
            "version_tag",
            "mlflow_run_id",
            "dataset_version",
            "stage",
            "gate_results",
            "promoted_at",
            "notes",
            "created_at",
        ]:
            assert col in src, f"column '{col}' missing from model_versions"

    def test_stage_check_constraint(self) -> None:
        src = _migration_source()
        for stage in ["'experimental'", "'staging'", "'shadow'", "'production'", "'archived'"]:
            assert stage in src, f"stage value {stage} missing from model_versions CHECK"

    def test_gate_results_jsonb(self) -> None:
        # gate_results must be JSONB (offline evaluation results)
        src = _migration_source()
        assert "gate_results" in src
        assert "JSONB" in src

    def test_index_on_service_stage(self) -> None:
        src = _migration_source()
        assert "idx_model_versions_service_stage" in src


# ── policy_versions spec compliance ───────────────────────────────────────────


class TestPolicyVersionsTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in ["version", "config_yaml", "applied_at", "applied_by", "justification"]:
            assert col in src, f"column '{col}' missing from policy_versions"

    def test_version_is_primary_key(self) -> None:
        src = _migration_source()
        # The PRIMARY KEY constraint should be present in the policy_versions block
        # (simple check: 'version' col appears before PRIMARY KEY in the table DDL)
        pos_table = src.index("CREATE TABLE policy_versions")
        pos_pk = src.index("PRIMARY KEY", pos_table)
        assert pos_pk > pos_table


# ── task_retry_states spec compliance ─────────────────────────────────────────


class TestTaskRetryStatesTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in [
            "task_id",
            "page_id",
            "job_id",
            "retry_count",
            "last_error",
            "final_error",
            "last_attempted_at",
            "created_at",
        ]:
            assert col in src, f"column '{col}' missing from task_retry_states"

    def test_retry_count_default_zero(self) -> None:
        src = _migration_source()
        assert "retry_count" in src
        assert "DEFAULT 0" in src

    def test_index_on_job_id(self) -> None:
        src = _migration_source()
        assert "idx_task_retry_states_job" in src


# ── retraining_triggers spec compliance ───────────────────────────────────────


class TestRetrainingTriggersTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in [
            "trigger_id",
            "trigger_type",
            "metric_name",
            "metric_value",
            "threshold_value",
            "persistence_hours",
            "fired_at",
            "cooldown_until",
            "status",
            "retraining_job_id",
            "mlflow_run_id",
            "resolved_at",
            "notes",
        ]:
            assert col in src, f"column '{col}' missing from retraining_triggers"

    def test_status_check_constraint(self) -> None:
        src = _migration_source()
        for val in ["'pending'", "'processing'", "'completed'", "'failed'"]:
            assert val in src, f"status value {val} missing from retraining_triggers CHECK"

    def test_status_defaults_to_pending(self) -> None:
        src = _migration_source()
        assert "DEFAULT 'pending'" in src

    def test_index_on_status(self) -> None:
        src = _migration_source()
        assert "idx_retraining_triggers_status" in src


# ── retraining_jobs spec compliance ───────────────────────────────────────────


class TestRetrainingJobsTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in [
            "job_id",
            "trigger_id",
            "pipeline_type",
            "status",
            "mlflow_experiment",
            "mlflow_run_id",
            "dataset_version",
            "started_at",
            "completed_at",
            "result_model_version",
            "result_mAP",
            "promotion_decision",
            "error_message",
            "created_at",
        ]:
            assert col in src, f"column '{col}' missing from retraining_jobs"

    def test_pipeline_type_check_constraint(self) -> None:
        src = _migration_source()
        for pt in [
            "'layout_detection'",
            "'doclayout_yolo'",
            "'rectification'",
            "'preprocessing'",
        ]:
            assert pt in src, f"pipeline_type value {pt} missing from retraining_jobs CHECK"

    def test_status_check_constraint(self) -> None:
        src = _migration_source()
        for val in ["'pending'", "'running'", "'completed'", "'failed'"]:
            assert val in src, f"status value {val} missing from retraining_jobs CHECK"

    def test_index_on_status(self) -> None:
        src = _migration_source()
        assert "idx_retraining_jobs_status" in src


# ── slo_audit_samples spec compliance ─────────────────────────────────────────


class TestSloAuditSamplesTable:
    def test_required_columns(self) -> None:
        src = _migration_source()
        for col in [
            "audit_id",
            "job_id",
            "page_number",
            "audit_week",
            "auditor_id",
            "auto_accepted",
            "auditor_would_flag",
            "disagreement_reason",
            "audited_at",
        ]:
            assert col in src, f"column '{col}' missing from slo_audit_samples"

    def test_boolean_columns(self) -> None:
        src = _migration_source()
        assert "auto_accepted" in src
        assert "auditor_would_flag" in src

    def test_index_on_audit_week(self) -> None:
        src = _migration_source()
        assert "idx_slo_audit_samples_week" in src


# ── ORM model imports ──────────────────────────────────────────────────────────


class TestMLOpsOrmModels:
    def test_all_mlops_models_importable(self) -> None:
        from services.eep.app.db.models import (
            ModelVersion,
            PolicyVersion,
            RetrainingJob,
            RetrainingTrigger,
            SloAuditSample,
            TaskRetryState,
        )
        for cls in [
            ModelVersion,
            PolicyVersion,
            TaskRetryState,
            RetrainingTrigger,
            RetrainingJob,
            SloAuditSample,
        ]:
            assert cls.__tablename__ in {
                "model_versions",
                "policy_versions",
                "task_retry_states",
                "retraining_triggers",
                "retraining_jobs",
                "slo_audit_samples",
            }

    def test_base_metadata_contains_mlops_tables(self) -> None:
        from services.eep.app.db.models import Base
        table_names = set(Base.metadata.tables.keys())
        for t in _MLOPS_TABLES:
            assert t in table_names, f"ORM Base.metadata missing table '{t}'"

    def test_base_metadata_still_contains_phase1_tables(self) -> None:
        from services.eep.app.db.models import Base
        table_names = set(Base.metadata.tables.keys())
        for t in _PHASE1_TABLES:
            assert t in table_names, f"ORM Base.metadata lost Phase 1 table '{t}'"

    def test_model_version_table_columns(self) -> None:
        from services.eep.app.db.models import ModelVersion
        cols = {c.name for c in ModelVersion.__table__.columns}
        for col in ["model_id", "service_name", "version_tag", "stage", "gate_results"]:
            assert col in cols, f"ModelVersion missing column '{col}'"

    def test_policy_version_table_columns(self) -> None:
        from services.eep.app.db.models import PolicyVersion
        cols = {c.name for c in PolicyVersion.__table__.columns}
        for col in ["version", "config_yaml", "applied_by", "justification"]:
            assert col in cols, f"PolicyVersion missing column '{col}'"

    def test_task_retry_state_table_columns(self) -> None:
        from services.eep.app.db.models import TaskRetryState
        cols = {c.name for c in TaskRetryState.__table__.columns}
        for col in ["task_id", "page_id", "job_id", "retry_count", "last_error", "final_error"]:
            assert col in cols, f"TaskRetryState missing column '{col}'"

    def test_retraining_trigger_table_columns(self) -> None:
        from services.eep.app.db.models import RetrainingTrigger
        cols = {c.name for c in RetrainingTrigger.__table__.columns}
        for col in [
            "trigger_id", "trigger_type", "metric_name", "metric_value",
            "threshold_value", "persistence_hours", "fired_at", "cooldown_until",
            "status", "retraining_job_id",
        ]:
            assert col in cols, f"RetrainingTrigger missing column '{col}'"

    def test_retraining_job_table_columns(self) -> None:
        from services.eep.app.db.models import RetrainingJob
        cols = {c.name for c in RetrainingJob.__table__.columns}
        for col in [
            "job_id", "trigger_id", "pipeline_type", "status",
            "result_mAP", "promotion_decision",
        ]:
            assert col in cols, f"RetrainingJob missing column '{col}'"

    def test_slo_audit_sample_table_columns(self) -> None:
        from services.eep.app.db.models import SloAuditSample
        cols = {c.name for c in SloAuditSample.__table__.columns}
        for col in [
            "audit_id", "job_id", "page_number", "audit_week",
            "auditor_id", "auto_accepted", "auditor_would_flag",
        ]:
            assert col in cols, f"SloAuditSample missing column '{col}'"


# ── Downgrade order ────────────────────────────────────────────────────────────


class TestDowngradeOrder:
    def test_all_six_tables_dropped(self) -> None:
        src = _migration_source()
        for table in _MLOPS_TABLES:
            assert f"DROP TABLE IF EXISTS {table}" in src

    def test_slo_audit_samples_before_retraining_jobs(self) -> None:
        # No FK dependencies between Phase 8 tables, but slo_audit_samples
        # references job_id (logical), so drop it before core MLOps tables
        src = _migration_source()
        pos_slo = src.index("DROP TABLE IF EXISTS slo_audit_samples")
        pos_ret = src.index("DROP TABLE IF EXISTS retraining_jobs")
        assert pos_slo < pos_ret
