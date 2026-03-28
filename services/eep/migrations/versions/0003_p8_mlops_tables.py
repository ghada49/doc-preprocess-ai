"""Phase 8 MLOps tables

Creates the six MLOps tables required for Phase 8 (Packet 8.1):
  model_versions, policy_versions, task_retry_states,
  retraining_triggers, retraining_jobs, slo_audit_samples

All column definitions match spec Section 13 exactly.
Phase 1 core tables (jobs, job_pages, page_lineage,
service_invocations, quality_gate_log, users) are intentionally
untouched — they remain owned by migration 0001.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── model_versions ─────────────────────────────────────────────────────────
    # Tracks every known model version per IEP service.
    # stage CHECK: experimental → staging → shadow → production → archived
    # gate_results: JSONB populated by the offline evaluation worker after a
    #   retraining job completes; read by the promotion gate check.
    op.execute(
        """
        CREATE TABLE model_versions (
            model_id         TEXT PRIMARY KEY,
            service_name     TEXT NOT NULL,
            version_tag      TEXT NOT NULL,
            mlflow_run_id    TEXT,
            dataset_version  TEXT,
            stage            TEXT NOT NULL CHECK (stage IN (
                                 'experimental', 'staging', 'shadow',
                                 'production', 'archived'
                             )),
            gate_results     JSONB,
            promoted_at      TIMESTAMPTZ,
            notes            TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_model_versions_service_stage "
        "ON model_versions(service_name, stage)"
    )

    # ── policy_versions ────────────────────────────────────────────────────────
    # Immutable log of every applied policy config.
    # version is a human-readable identifier (e.g. 'v1', 'v2').
    op.execute(
        """
        CREATE TABLE policy_versions (
            version       TEXT PRIMARY KEY,
            config_yaml   TEXT NOT NULL,
            applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_by    TEXT NOT NULL,
            justification TEXT NOT NULL
        )
        """
    )

    # ── task_retry_states ──────────────────────────────────────────────────────
    # Tracks per-task retry counters and last-error state for worker tasks.
    # page_id / job_id reference Phase 1 tables but are stored as plain TEXT
    # to keep Phase 8 migration independent of Phase 1 schema.
    op.execute(
        """
        CREATE TABLE task_retry_states (
            task_id           TEXT PRIMARY KEY,
            page_id           TEXT,
            job_id            TEXT,
            retry_count       INTEGER NOT NULL DEFAULT 0,
            last_error        TEXT,
            final_error       TEXT,
            last_attempted_at TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_task_retry_states_job "
        "ON task_retry_states(job_id) "
        "WHERE job_id IS NOT NULL"
    )

    # ── retraining_triggers ────────────────────────────────────────────────────
    # One row per auto-retraining trigger event.
    # status CHECK: pending → processing → completed | failed
    # cooldown_until: set after firing; next trigger of same type is suppressed
    #   until this timestamp passes (7-day cooldown, spec Section 16.3).
    # retraining_job_id: populated once the retraining worker creates a job.
    op.execute(
        """
        CREATE TABLE retraining_triggers (
            trigger_id        TEXT PRIMARY KEY,
            trigger_type      TEXT NOT NULL,
            metric_name       TEXT NOT NULL,
            metric_value      FLOAT NOT NULL,
            threshold_value   FLOAT NOT NULL,
            persistence_hours FLOAT NOT NULL,
            fired_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cooldown_until    TIMESTAMPTZ,
            status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                                  'pending', 'processing', 'completed', 'failed'
                              )),
            retraining_job_id TEXT,
            mlflow_run_id     TEXT,
            resolved_at       TIMESTAMPTZ,
            notes             TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_retraining_triggers_status "
        "ON retraining_triggers(status, fired_at)"
    )

    # ── retraining_jobs ────────────────────────────────────────────────────────
    # One row per retraining execution dispatched to the retraining worker.
    # pipeline_type CHECK: which IEP pipeline is being retrained.
    # status CHECK: pending → running → completed | failed
    # gate_results written to model_versions (not here); this table records
    # the outcome summary (result_mAP, promotion_decision).
    op.execute(
        """
        CREATE TABLE retraining_jobs (
            job_id               TEXT PRIMARY KEY,
            trigger_id           TEXT,
            pipeline_type        TEXT NOT NULL CHECK (pipeline_type IN (
                                     'layout_detection', 'doclayout_yolo',
                                     'rectification', 'preprocessing'
                                 )),
            status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                                     'pending', 'running', 'completed', 'failed'
                                 )),
            mlflow_experiment    TEXT,
            mlflow_run_id        TEXT,
            dataset_version      TEXT,
            started_at           TIMESTAMPTZ,
            completed_at         TIMESTAMPTZ,
            result_model_version TEXT,
            result_mAP           FLOAT,
            promotion_decision   TEXT,
            error_message        TEXT,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_retraining_jobs_status "
        "ON retraining_jobs(status, created_at)"
    )

    # ── slo_audit_samples ──────────────────────────────────────────────────────
    # Weekly human audit sample records for bad auto-accept rate measurement.
    # audit_week: ISO week string (e.g. '2026-W12').
    # auto_accepted: was this page auto-accepted by the pipeline?
    # auditor_would_flag: would the human auditor have flagged this page?
    op.execute(
        """
        CREATE TABLE slo_audit_samples (
            audit_id            TEXT PRIMARY KEY,
            job_id              TEXT NOT NULL,
            page_number         INTEGER NOT NULL,
            audit_week          TEXT NOT NULL,
            auditor_id          TEXT NOT NULL,
            auto_accepted       BOOLEAN NOT NULL,
            auditor_would_flag  BOOLEAN NOT NULL,
            disagreement_reason TEXT,
            audited_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_slo_audit_samples_week "
        "ON slo_audit_samples(audit_week, job_id)"
    )


def downgrade() -> None:
    # Drop in reverse dependency order (no FK constraints between Phase 8 tables,
    # so order is arbitrary — most recently created first).
    op.execute("DROP TABLE IF EXISTS slo_audit_samples")
    op.execute("DROP TABLE IF EXISTS retraining_jobs")
    op.execute("DROP TABLE IF EXISTS retraining_triggers")
    op.execute("DROP TABLE IF EXISTS task_retry_states")
    op.execute("DROP TABLE IF EXISTS policy_versions")
    op.execute("DROP TABLE IF EXISTS model_versions")
