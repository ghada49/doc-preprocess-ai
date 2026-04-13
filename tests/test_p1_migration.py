"""
tests/test_p1_migration.py
--------------------------
Packet 1.5 validator tests for the Phase 1 core DB migration.

Tests validate the migration file's SQL content against spec Section 13
without requiring a live PostgreSQL connection.

Definition of done:
  - schema matches spec for these six core tables only
  - job_pages supports ptiff_qa_pending as a valid page state
  - jobs table stores ptiff_qa_mode
"""

from __future__ import annotations

import importlib
import pathlib
import types

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent
    / "services"
    / "eep"
    / "migrations"
    / "versions"
    / "0001_p1_core_tables.py"
)


def _migration_source() -> str:
    """Return the full source text of the migration file."""
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _load_migration() -> types.ModuleType:
    """Import the migration module dynamically."""
    spec = importlib.util.spec_from_file_location("migration_0001", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Migration file existence and structure ─────────────────────────────────────


class TestMigrationFileStructure:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_PATH.exists(), f"Migration not found: {_MIGRATION_PATH}"

    def test_migration_file_is_python(self) -> None:
        assert _MIGRATION_PATH.suffix == ".py"

    def test_has_upgrade_function(self) -> None:
        mod = _load_migration()
        assert callable(getattr(mod, "upgrade", None)), "upgrade() not found"

    def test_has_downgrade_function(self) -> None:
        mod = _load_migration()
        assert callable(getattr(mod, "downgrade", None)), "downgrade() not found"

    def test_revision_is_0001(self) -> None:
        mod = _load_migration()
        assert getattr(mod, "revision", None) == "0001"

    def test_down_revision_is_none(self) -> None:
        mod = _load_migration()
        assert getattr(mod, "down_revision", "NOT_SET") is None

    def test_alembic_ini_exists(self) -> None:
        ini = _MIGRATION_PATH.parent.parent.parent / "alembic.ini"
        assert ini.exists(), f"alembic.ini not found at {ini}"

    def test_env_py_exists(self) -> None:
        env = _MIGRATION_PATH.parent.parent / "env.py"
        assert env.exists(), f"migrations/env.py not found at {env}"


# ── Six core tables present ────────────────────────────────────────────────────


class TestSixCoreTables:
    @pytest.mark.parametrize(
        "table",
        [
            "jobs",
            "job_pages",
            "page_lineage",
            "service_invocations",
            "quality_gate_log",
            "users",
        ],
    )
    def test_table_created(self, table: str) -> None:
        src = _migration_source()
        assert f"CREATE TABLE {table}" in src, f"CREATE TABLE {table} not found in migration"

    @pytest.mark.parametrize(
        "table",
        [
            "jobs",
            "job_pages",
            "page_lineage",
            "service_invocations",
            "quality_gate_log",
            "users",
        ],
    )
    def test_table_dropped_in_downgrade(self, table: str) -> None:
        src = _migration_source()
        assert (
            f"DROP TABLE IF EXISTS {table}" in src
        ), f"DROP TABLE IF EXISTS {table} not found in downgrade"

    def test_exactly_six_tables_created(self) -> None:
        src = _migration_source()
        count = src.count("CREATE TABLE ")
        assert count == 6, f"Expected 6 CREATE TABLE statements; found {count}"

    def test_no_mlops_tables(self) -> None:
        src = _migration_source()
        # MLOps tables must NOT appear in Phase 1 migration (spec constraint 11)
        for forbidden in [
            "shadow_results",
            "model_versions",
            "policy_versions",
            "task_retry_states",
            "retraining_triggers",
            "retraining_jobs",
            "slo_audit_samples",
        ]:
            assert (
                f"CREATE TABLE {forbidden}" not in src
            ), f"MLOps table '{forbidden}' must not appear in Phase 1 migration"


# ── jobs table spec compliance ─────────────────────────────────────────────────


class TestJobsTable:
    def test_ptiff_qa_mode_column(self) -> None:
        # DoD: jobs table stores ptiff_qa_mode
        src = _migration_source()
        assert "ptiff_qa_mode" in src

    def test_ptiff_qa_mode_check_constraint(self) -> None:
        src = _migration_source()
        assert "'manual'" in src
        assert "'auto_continue'" in src

    def test_pipeline_mode_column(self) -> None:
        src = _migration_source()
        assert "pipeline_mode" in src

    def test_pipeline_mode_values(self) -> None:
        src = _migration_source()
        assert "'preprocess'" in src
        assert "'layout'" in src

    def test_material_type_check_constraint(self) -> None:
        src = _migration_source()
        assert "'book'" in src
        assert "'newspaper'" in src
        assert "'archival_document'" in src

    def test_job_status_check_constraint(self) -> None:
        src = _migration_source()
        # job-level status values
        assert "'queued'" in src
        assert "'running'" in src
        assert "'done'" in src

    def test_counter_columns(self) -> None:
        src = _migration_source()
        assert "accepted_count" in src
        assert "review_count" in src
        assert "failed_count" in src
        assert "pending_human_correction_count" in src

    def test_shadow_mode_column(self) -> None:
        src = _migration_source()
        assert "shadow_mode" in src

    def test_timestamps(self) -> None:
        src = _migration_source()
        assert "created_at" in src
        assert "updated_at" in src
        assert "completed_at" in src


# ── job_pages table spec compliance ───────────────────────────────────────────


class TestJobPagesTable:
    def test_ptiff_qa_pending_in_status_check(self) -> None:
        # DoD: job_pages supports ptiff_qa_pending as a valid page state
        src = _migration_source()
        assert "'ptiff_qa_pending'" in src

    def test_all_ten_page_states_present(self) -> None:
        src = _migration_source()
        for state in [
            "'queued'",
            "'preprocessing'",
            "'rectification'",
            "'ptiff_qa_pending'",
            "'layout_detection'",
            "'pending_human_correction'",
            "'accepted'",
            "'review'",
            "'failed'",
            "'split'",
        ]:
            assert state in src, f"Page state {state} missing from migration"

    def test_acceptance_decision_check(self) -> None:
        src = _migration_source()
        assert "acceptance_decision" in src

    def test_jsonb_columns(self) -> None:
        src = _migration_source()
        assert "quality_summary" in src
        assert "layout_consensus_result" in src
        assert "review_reasons" in src

    def test_job_id_foreign_key(self) -> None:
        src = _migration_source()
        assert "REFERENCES jobs(job_id)" in src

    def test_unique_constraint(self) -> None:
        src = _migration_source()
        assert "UNIQUE (job_id, page_number, sub_page_index)" in src

    def test_indexes(self) -> None:
        src = _migration_source()
        assert "idx_job_pages_job_id" in src
        assert "idx_job_pages_status_updated" in src

    def test_output_layout_uri_column(self) -> None:
        src = _migration_source()
        assert "output_layout_uri" in src


# ── page_lineage table spec compliance ────────────────────────────────────────


class TestPageLineageTable:
    def test_artifact_states(self) -> None:
        src = _migration_source()
        assert "preprocessed_artifact_state" in src
        assert "layout_artifact_state" in src

    def test_artifact_state_values(self) -> None:
        src = _migration_source()
        assert "'pending'" in src
        assert "'confirmed'" in src
        assert "'recovery_failed'" in src

    def test_artifact_state_defaults_to_pending(self) -> None:
        src = _migration_source()
        # Both artifact state columns must default to 'pending'
        assert src.count("DEFAULT 'pending'") == 2

    def test_human_correction_fields(self) -> None:
        src = _migration_source()
        assert "human_corrected" in src
        assert "human_correction_timestamp" in src
        assert "human_correction_fields" in src

    def test_ptiff_ssim_column(self) -> None:
        # ptiff_ssim is offline-only; must exist but must not affect routing
        src = _migration_source()
        assert "ptiff_ssim" in src

    def test_parent_page_id_column(self) -> None:
        src = _migration_source()
        assert "parent_page_id" in src

    def test_split_source_column(self) -> None:
        src = _migration_source()
        assert "split_source" in src

    def test_cleanup_retry_count_column(self) -> None:
        src = _migration_source()
        assert "cleanup_retry_count" in src

    def test_unique_constraint(self) -> None:
        src = _migration_source()
        assert "UNIQUE (job_id, page_number, sub_page_index)" in src

    def test_lineage_index(self) -> None:
        src = _migration_source()
        assert "idx_lineage_job" in src


# ── service_invocations table spec compliance ──────────────────────────────────


class TestServiceInvocationsTable:
    def test_status_check_constraint(self) -> None:
        src = _migration_source()
        assert "'success'" in src
        assert "'error'" in src
        assert "'timeout'" in src
        assert "'skipped'" in src

    def test_lineage_id_foreign_key(self) -> None:
        src = _migration_source()
        assert "REFERENCES page_lineage(lineage_id)" in src

    def test_jsonb_columns(self) -> None:
        src = _migration_source()
        assert "metrics" in src
        assert "config_snapshot" in src

    def test_index(self) -> None:
        src = _migration_source()
        assert "idx_invocations_lineage" in src


# ── quality_gate_log table spec compliance ────────────────────────────────────


class TestQualityGateLogTable:
    def test_gate_type_check_constraint(self) -> None:
        src = _migration_source()
        assert "'geometry_selection'" in src
        assert "'geometry_selection_post_rectification'" in src
        assert "'artifact_validation'" in src
        assert "'artifact_validation_final'" in src
        assert "'layout'" in src

    def test_route_decision_check_constraint(self) -> None:
        src = _migration_source()
        assert "'rectification'" in src
        assert "'pending_human_correction'" in src

    def test_structural_agreement_column(self) -> None:
        src = _migration_source()
        assert "structural_agreement" in src

    def test_indexes(self) -> None:
        src = _migration_source()
        assert "idx_quality_gate_job" in src
        assert "idx_quality_gate_route" in src
        assert "idx_quality_gate_agreement" in src


# ── users table spec compliance ────────────────────────────────────────────────


class TestUsersTable:
    def test_role_check_constraint(self) -> None:
        src = _migration_source()
        assert "'user'" in src
        assert "'admin'" in src

    def test_hashed_password_column(self) -> None:
        src = _migration_source()
        assert "hashed_password" in src

    def test_is_active_column(self) -> None:
        src = _migration_source()
        assert "is_active" in src

    def test_username_unique(self) -> None:
        src = _migration_source()
        assert "username" in src
        assert "UNIQUE" in src


# ── Downgrade order ────────────────────────────────────────────────────────────


class TestDowngradeOrder:
    def test_job_pages_dropped_before_jobs(self) -> None:
        src = _migration_source()
        pos_pages = src.index("DROP TABLE IF EXISTS job_pages")
        pos_jobs = src.index("DROP TABLE IF EXISTS jobs")
        assert pos_pages < pos_jobs, "job_pages must be dropped before jobs (FK dependency)"

    def test_service_invocations_dropped_before_page_lineage(self) -> None:
        src = _migration_source()
        pos_inv = src.index("DROP TABLE IF EXISTS service_invocations")
        pos_lin = src.index("DROP TABLE IF EXISTS page_lineage")
        assert (
            pos_inv < pos_lin
        ), "service_invocations must be dropped before page_lineage (FK dependency)"

    def test_page_lineage_dropped_before_jobs(self) -> None:
        src = _migration_source()
        pos_lin = src.index("DROP TABLE IF EXISTS page_lineage")
        pos_jobs = src.index("DROP TABLE IF EXISTS jobs")
        assert pos_lin < pos_jobs
