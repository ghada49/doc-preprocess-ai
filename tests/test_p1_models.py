"""
tests/test_p1_models.py
------------------------
Packet 1.6 validator tests for services.eep.app.db.models and session.

Tests validate ORM model structure (table names, column names, column types,
nullability, FKs, unique constraints) without requiring a live PostgreSQL
connection.

Definition of done:
  - ORM models mirror the migration 0001_p1_core_tables.py exactly
  - Base.metadata carries all six tables
  - JSONB columns use postgresql.JSONB
  - DateTime columns are timezone-aware
  - ForeignKey constraints match the migration
  - UniqueConstraints present on job_pages and page_lineage
  - env.py target_metadata is Base.metadata
  - session.py exports engine, SessionLocal, get_session
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from sqlalchemy import DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from services.eep.app.db.models import (
    Base,
    Job,
    JobPage,
    PageLineage,
    QualityGateLog,
    ServiceInvocation,
    User,
)
from services.eep.app.db.session import SessionLocal, engine, get_session

# ── Helpers ────────────────────────────────────────────────────────────────────

_ALL_TABLE_NAMES = frozenset(
    {
        "users",
        "jobs",
        "job_pages",
        "page_lineage",
        "service_invocations",
        "quality_gate_log",
    }
)


def _col(model: Any, name: str) -> Any:
    return model.__table__.c[name]


def _col_names(model: Any) -> list[str]:
    return [c.name for c in model.__table__.columns]


def _unique_constraints(model: Any) -> list[UniqueConstraint]:
    return [c for c in model.__table__.constraints if isinstance(c, UniqueConstraint)]


# ── Base ───────────────────────────────────────────────────────────────────────


class TestBase:
    def test_base_metadata_has_phase1_tables(self) -> None:
        # Phase 8 (Packet 8.1) added 6 MLOps models to the same Base; the
        # count is no longer exactly 6. Assert Phase 1 tables are present.
        assert _ALL_TABLE_NAMES.issubset(set(Base.metadata.tables.keys()))

    def test_base_metadata_table_names(self) -> None:
        # Phase 8 MLOps tables share the same Base; use a subset check.
        assert _ALL_TABLE_NAMES.issubset(set(Base.metadata.tables.keys()))

    def test_all_models_share_same_base(self) -> None:
        for model in (User, Job, JobPage, PageLineage, ServiceInvocation, QualityGateLog):
            assert model.__table__ in Base.metadata.tables.values()


# ── User ───────────────────────────────────────────────────────────────────────


class TestUserModel:
    def test_tablename(self) -> None:
        assert User.__tablename__ == "users"

    @pytest.mark.parametrize(
        "col",
        ["user_id", "username", "hashed_password", "role", "is_active", "created_at"],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(User)

    def test_user_id_is_primary_key(self) -> None:
        assert _col(User, "user_id").primary_key

    def test_username_is_unique(self) -> None:
        assert _col(User, "username").unique

    def test_username_not_nullable(self) -> None:
        assert not _col(User, "username").nullable

    def test_created_at_timezone_aware(self) -> None:
        col_type = _col(User, "created_at").type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True


# ── Job ────────────────────────────────────────────────────────────────────────


class TestJobModel:
    def test_tablename(self) -> None:
        assert Job.__tablename__ == "jobs"

    @pytest.mark.parametrize(
        "col",
        [
            "job_id",
            "collection_id",
            "material_type",
            "pipeline_mode",
            "ptiff_qa_mode",
            "policy_version",
            "status",
            "page_count",
            "accepted_count",
            "review_count",
            "failed_count",
            "pending_human_correction_count",
            "shadow_mode",
            "created_by",
            "created_at",
            "updated_at",
            "completed_at",
        ],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(Job)

    def test_job_id_is_primary_key(self) -> None:
        assert _col(Job, "job_id").primary_key

    def test_ptiff_qa_mode_column_present(self) -> None:
        # Backward compat: ptiff_qa_mode column retained in ORM model (code no longer writes it)
        assert "ptiff_qa_mode" in _col_names(Job)

    def test_created_by_nullable(self) -> None:
        assert _col(Job, "created_by").nullable

    def test_completed_at_nullable(self) -> None:
        assert _col(Job, "completed_at").nullable

    def test_created_at_not_nullable(self) -> None:
        assert not _col(Job, "created_at").nullable

    def test_updated_at_not_nullable(self) -> None:
        assert not _col(Job, "updated_at").nullable

    def test_timestamps_timezone_aware(self) -> None:
        for ts_col in ("created_at", "updated_at", "completed_at"):
            col_type = _col(Job, ts_col).type
            assert isinstance(col_type, DateTime), f"{ts_col} should be DateTime"
            assert col_type.timezone is True, f"{ts_col} should be timezone-aware"

    def test_counter_columns_not_nullable(self) -> None:
        for counter in (
            "accepted_count",
            "review_count",
            "failed_count",
            "pending_human_correction_count",
        ):
            assert not _col(Job, counter).nullable, f"{counter} must be NOT NULL"


# ── JobPage ────────────────────────────────────────────────────────────────────


class TestJobPageModel:
    def test_tablename(self) -> None:
        assert JobPage.__tablename__ == "job_pages"

    @pytest.mark.parametrize(
        "col",
        [
            "page_id",
            "job_id",
            "page_number",
            "sub_page_index",
            "status",
            "routing_path",
            "escalated_to_gpu",
            "input_image_uri",
            "output_image_uri",
            "quality_summary",
            "layout_consensus_result",
            "acceptance_decision",
            "review_reasons",
            "processing_time_ms",
            "status_updated_at",
            "created_at",
            "completed_at",
            "output_layout_uri",
        ],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(JobPage)

    def test_job_id_foreign_key_to_jobs(self) -> None:
        fk_tables = {fk.column.table.name for fk in _col(JobPage, "job_id").foreign_keys}
        assert "jobs" in fk_tables

    def test_unique_constraint_on_job_page_subpage(self) -> None:
        constraints = _unique_constraints(JobPage)
        col_sets = [frozenset(c.name for c in uc.columns) for uc in constraints]
        assert frozenset({"job_id", "page_number", "sub_page_index"}) in col_sets

    @pytest.mark.parametrize(
        "col", ["quality_summary", "layout_consensus_result", "review_reasons"]
    )
    def test_jsonb_column_type(self, col: str) -> None:
        assert isinstance(_col(JobPage, col).type, JSONB), f"{col} must be JSONB"

    def test_sub_page_index_nullable(self) -> None:
        assert _col(JobPage, "sub_page_index").nullable

    def test_output_layout_uri_column(self) -> None:
        assert "output_layout_uri" in _col_names(JobPage)


# ── PageLineage ────────────────────────────────────────────────────────────────


class TestPageLineageModel:
    def test_tablename(self) -> None:
        assert PageLineage.__tablename__ == "page_lineage"

    @pytest.mark.parametrize(
        "col",
        [
            "lineage_id",
            "job_id",
            "page_number",
            "sub_page_index",
            "correlation_id",
            "input_image_uri",
            "input_image_hash",
            "otiff_uri",
            "reference_ptiff_uri",
            "ptiff_ssim",
            "iep1a_used",
            "iep1b_used",
            "selected_geometry_model",
            "structural_agreement",
            "iep1d_used",
            "material_type",
            "routing_path",
            "policy_version",
            "acceptance_decision",
            "acceptance_reason",
            "gate_results",
            "total_processing_ms",
            "shadow_eval_id",
            "cleanup_retry_count",
            "preprocessed_artifact_state",
            "layout_artifact_state",
            "output_image_uri",
            "parent_page_id",
            "split_source",
            "human_corrected",
            "human_correction_timestamp",
            "human_correction_fields",
            "reviewed_by",
            "reviewed_at",
            "reviewer_notes",
            "created_at",
            "completed_at",
        ],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(PageLineage)

    def test_unique_constraint_on_job_page_subpage(self) -> None:
        constraints = _unique_constraints(PageLineage)
        col_sets = [frozenset(c.name for c in uc.columns) for uc in constraints]
        assert frozenset({"job_id", "page_number", "sub_page_index"}) in col_sets

    def test_artifact_state_columns_present(self) -> None:
        assert "preprocessed_artifact_state" in _col_names(PageLineage)
        assert "layout_artifact_state" in _col_names(PageLineage)

    def test_artifact_state_columns_not_nullable(self) -> None:
        assert not _col(PageLineage, "preprocessed_artifact_state").nullable
        assert not _col(PageLineage, "layout_artifact_state").nullable

    def test_artifact_state_defaults_to_pending(self) -> None:
        for col in ("preprocessed_artifact_state", "layout_artifact_state"):
            default = _col(PageLineage, col).default
            assert default is not None, f"{col} must have a Python default"
            assert default.arg == "pending", f"{col} default must be 'pending'"

    def test_human_correction_fields_jsonb(self) -> None:
        assert isinstance(_col(PageLineage, "human_correction_fields").type, JSONB)

    def test_gate_results_jsonb(self) -> None:
        assert isinstance(_col(PageLineage, "gate_results").type, JSONB)

    def test_ptiff_ssim_nullable(self) -> None:
        # ptiff_ssim is offline-only; column must exist but must be nullable
        assert _col(PageLineage, "ptiff_ssim").nullable

    def test_human_correction_timestamp_timezone_aware(self) -> None:
        col_type = _col(PageLineage, "human_correction_timestamp").type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True

    def test_cleanup_retry_count_not_nullable(self) -> None:
        assert not _col(PageLineage, "cleanup_retry_count").nullable


# ── ServiceInvocation ──────────────────────────────────────────────────────────


class TestServiceInvocationModel:
    def test_tablename(self) -> None:
        assert ServiceInvocation.__tablename__ == "service_invocations"

    @pytest.mark.parametrize(
        "col",
        [
            "id",
            "lineage_id",
            "service_name",
            "service_version",
            "model_version",
            "model_source",
            "invoked_at",
            "completed_at",
            "processing_time_ms",
            "status",
            "error_message",
            "metrics",
            "config_snapshot",
        ],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(ServiceInvocation)

    def test_id_is_integer_primary_key(self) -> None:
        col = _col(ServiceInvocation, "id")
        assert col.primary_key
        assert col.autoincrement is True or col.autoincrement == "auto"

    def test_lineage_id_foreign_key_to_page_lineage(self) -> None:
        fk_tables = {
            fk.column.table.name for fk in _col(ServiceInvocation, "lineage_id").foreign_keys
        }
        assert "page_lineage" in fk_tables

    @pytest.mark.parametrize("col", ["metrics", "config_snapshot"])
    def test_jsonb_column_type(self, col: str) -> None:
        assert isinstance(_col(ServiceInvocation, col).type, JSONB), f"{col} must be JSONB"

    def test_service_name_not_nullable(self) -> None:
        assert not _col(ServiceInvocation, "service_name").nullable

    def test_invoked_at_timezone_aware(self) -> None:
        col_type = _col(ServiceInvocation, "invoked_at").type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True


# ── QualityGateLog ─────────────────────────────────────────────────────────────


class TestQualityGateLogModel:
    def test_tablename(self) -> None:
        assert QualityGateLog.__tablename__ == "quality_gate_log"

    @pytest.mark.parametrize(
        "col",
        [
            "gate_id",
            "job_id",
            "page_number",
            "gate_type",
            "iep1a_geometry",
            "iep1b_geometry",
            "structural_agreement",
            "selected_model",
            "selection_reason",
            "sanity_check_results",
            "split_confidence",
            "tta_variance",
            "artifact_validation_score",
            "route_decision",
            "review_reason",
            "processing_time_ms",
            "created_at",
        ],
    )
    def test_column_exists(self, col: str) -> None:
        assert col in _col_names(QualityGateLog)

    def test_gate_id_is_primary_key(self) -> None:
        assert _col(QualityGateLog, "gate_id").primary_key

    @pytest.mark.parametrize(
        "col",
        [
            "iep1a_geometry",
            "iep1b_geometry",
            "sanity_check_results",
            "split_confidence",
            "tta_variance",
        ],
    )
    def test_jsonb_column_type(self, col: str) -> None:
        assert isinstance(_col(QualityGateLog, col).type, JSONB), f"{col} must be JSONB"

    def test_structural_agreement_nullable(self) -> None:
        assert _col(QualityGateLog, "structural_agreement").nullable

    def test_gate_type_not_nullable(self) -> None:
        assert not _col(QualityGateLog, "gate_type").nullable

    def test_route_decision_not_nullable(self) -> None:
        assert not _col(QualityGateLog, "route_decision").nullable

    def test_created_at_timezone_aware(self) -> None:
        col_type = _col(QualityGateLog, "created_at").type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True


# ── session.py ─────────────────────────────────────────────────────────────────


class TestSessionModule:
    def test_engine_is_not_none(self) -> None:
        assert engine is not None

    def test_session_local_is_sessionmaker(self) -> None:
        from sqlalchemy.orm import sessionmaker

        assert isinstance(SessionLocal, sessionmaker)

    def test_get_session_is_callable(self) -> None:
        assert callable(get_session)

    def test_get_session_returns_generator(self) -> None:
        gen = get_session()
        assert isinstance(gen, types.GeneratorType)
        gen.close()  # clean up without advancing

    def test_session_local_creates_sessions(self) -> None:
        from sqlalchemy.orm import Session

        # Creating a session object doesn't require a live DB connection.
        db = SessionLocal()
        assert isinstance(db, Session)
        db.close()


# ── env.py target_metadata ─────────────────────────────────────────────────────


class TestEnvMetadata:
    def test_env_py_target_metadata_is_base_metadata(self) -> None:
        import pathlib

        env_path = (
            pathlib.Path(__file__).parent.parent / "services" / "eep" / "migrations" / "env.py"
        )
        src = env_path.read_text(encoding="utf-8")
        assert "Base.metadata" in src, "env.py must set target_metadata = Base.metadata"

    def test_env_py_imports_base(self) -> None:
        import pathlib

        env_path = (
            pathlib.Path(__file__).parent.parent / "services" / "eep" / "migrations" / "env.py"
        )
        src = env_path.read_text(encoding="utf-8")
        assert "from services.eep.app.db.models import Base" in src

    def test_env_py_no_longer_has_target_metadata_none(self) -> None:
        import pathlib

        env_path = (
            pathlib.Path(__file__).parent.parent / "services" / "eep" / "migrations" / "env.py"
        )
        src = env_path.read_text(encoding="utf-8")
        assert "target_metadata = None" not in src
