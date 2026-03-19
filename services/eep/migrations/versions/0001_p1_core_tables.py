"""Phase 1 core tables

Creates the six core tables required for Phase 1:
  jobs, job_pages, page_lineage, service_invocations, quality_gate_log, users

All table definitions and CHECK constraints match spec Section 13 exactly.
MLOps tables (shadow_results, model_versions, policy_versions, etc.) are
intentionally excluded — they belong to the Phase 8 migration (Packet 8.1).

Revision ID: 0001
Revises: (none — initial migration)
Create Date: 2026-03-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────────────
    # Created first: no foreign-key dependencies.
    op.execute(
        """
        CREATE TABLE users (
            user_id          TEXT PRIMARY KEY,
            username         TEXT UNIQUE NOT NULL,
            hashed_password  TEXT NOT NULL,
            role             TEXT NOT NULL CHECK (role IN ('user', 'admin')),
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # ── jobs ───────────────────────────────────────────────────────────────────
    # jobs is the parent table referenced by job_pages and page_lineage.
    #
    # status derivation (exact, deterministic):
    #   queued:  all leaf pages in 'queued' state (no page has started processing)
    #   running: at least one leaf page is in a non-worker-terminal state
    #            ('queued', 'preprocessing', 'rectification', 'ptiff_qa_pending',
    #             'layout_detection')
    #   done:    all leaf pages are worker-terminal AND at least one is not 'failed'
    #   failed:  all leaf pages are worker-terminal AND all are 'failed'
    # Leaf pages = all job_pages where status != 'split' and no children exist,
    #              PLUS all sub-pages (sub_page_index IS NOT NULL).
    # Split-parent records (status='split') are excluded from all counts.
    #
    # Counter semantics: leaf-page outcomes only. Split parents never counted.
    # Split children (sub_page_index IS NOT NULL) count as leaf pages.
    # Reconciliation when all leaf pages are terminal:
    #   accepted_count + review_count + failed_count + pending_human_correction_count
    #   = total leaf pages
    op.execute(
        """
        CREATE TABLE jobs (
            job_id           TEXT PRIMARY KEY,
            collection_id    TEXT NOT NULL,
            material_type    TEXT NOT NULL CHECK (material_type IN (
                                 'book', 'newspaper', 'archival_document'
                             )),
            pipeline_mode    TEXT NOT NULL CHECK (pipeline_mode IN (
                                 'preprocess', 'layout'
                             )) DEFAULT 'layout',
            ptiff_qa_mode    TEXT NOT NULL CHECK (ptiff_qa_mode IN (
                                 'manual', 'auto_continue'
                             )) DEFAULT 'manual',
            policy_version   TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                                 'queued', 'running', 'done', 'failed'
                             )),
            page_count       INTEGER NOT NULL,
            accepted_count   INTEGER NOT NULL DEFAULT 0,
            review_count     INTEGER NOT NULL DEFAULT 0,
            failed_count     INTEGER NOT NULL DEFAULT 0,
            pending_human_correction_count INTEGER NOT NULL DEFAULT 0,
            shadow_mode      BOOLEAN NOT NULL DEFAULT FALSE,
            created_by       TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at     TIMESTAMPTZ
        )
        """
    )

    # ── job_pages ──────────────────────────────────────────────────────────────
    # Per-page processing record.
    # status CHECK constraint includes ptiff_qa_pending (spec Section 9.1 + 3.1).
    # acceptance_decision is set only when the page reaches a leaf-final state
    # (accepted, review, failed). Remains NULL in pending_human_correction.
    # layout_consensus_result: NULL for preprocess-only mode or before layout.
    op.execute(
        """
        CREATE TABLE job_pages (
            page_id           TEXT PRIMARY KEY,
            job_id            TEXT NOT NULL REFERENCES jobs(job_id),
            page_number       INTEGER NOT NULL,
            sub_page_index    INTEGER,
            status            TEXT NOT NULL DEFAULT 'queued'
                              CHECK (status IN (
                                  'queued', 'preprocessing', 'rectification',
                                  'ptiff_qa_pending', 'layout_detection',
                                  'pending_human_correction',
                                  'accepted', 'review', 'failed', 'split'
                              )),
            routing_path      TEXT,
            escalated_to_gpu  BOOLEAN NOT NULL DEFAULT FALSE,
            input_image_uri   TEXT NOT NULL,
            output_image_uri  TEXT,
            quality_summary   JSONB,
            layout_consensus_result JSONB,
            acceptance_decision TEXT CHECK (acceptance_decision IN (
                                    'accepted', 'review', 'failed'
                                )),
            review_reasons    JSONB,
            processing_time_ms REAL,
            status_updated_at TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at      TIMESTAMPTZ,
            output_layout_uri TEXT,
            UNIQUE (job_id, page_number, sub_page_index)
        )
        """
    )
    op.execute("CREATE INDEX idx_job_pages_job_id ON job_pages(job_id)")
    op.execute(
        "CREATE INDEX idx_job_pages_status_updated "
        "ON job_pages(status_updated_at) "
        "WHERE status_updated_at IS NOT NULL"
    )

    # ── page_lineage ───────────────────────────────────────────────────────────
    # Complete audit trail for every page.
    # preprocessed_artifact_state / layout_artifact_state default to 'pending':
    #   the DB-first write protocol sets 'pending' before writing to S3,
    #   then 'confirmed' after a successful write. 'recovery_failed' is set when
    #   cleanup_retry_count >= 3 and age exceeds 3× grace period.
    # ptiff_ssim: offline-only metric; MUST NOT influence routing decisions.
    op.execute(
        """
        CREATE TABLE page_lineage (
            lineage_id         TEXT PRIMARY KEY,
            job_id             TEXT NOT NULL,
            page_number        INTEGER NOT NULL,
            sub_page_index     INTEGER,
            correlation_id     TEXT NOT NULL,
            input_image_uri    TEXT NOT NULL,
            input_image_hash   TEXT,
            otiff_uri          TEXT NOT NULL,
            reference_ptiff_uri TEXT,
            ptiff_ssim         FLOAT,
            iep1a_used         BOOLEAN NOT NULL DEFAULT FALSE,
            iep1b_used         BOOLEAN NOT NULL DEFAULT FALSE,
            selected_geometry_model TEXT,
            structural_agreement BOOLEAN,
            iep1d_used         BOOLEAN NOT NULL DEFAULT FALSE,
            material_type      TEXT NOT NULL,
            routing_path       TEXT,
            policy_version     TEXT NOT NULL,
            acceptance_decision TEXT,
            acceptance_reason  TEXT,
            gate_results       JSONB,
            total_processing_ms REAL,
            shadow_eval_id     TEXT,
            cleanup_retry_count INT NOT NULL DEFAULT 0,
            preprocessed_artifact_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (preprocessed_artifact_state IN (
                    'pending', 'confirmed', 'recovery_failed'
                )),
            layout_artifact_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (layout_artifact_state IN (
                    'pending', 'confirmed', 'recovery_failed'
                )),
            output_image_uri   TEXT,
            parent_page_id     TEXT,
            split_source       BOOLEAN NOT NULL DEFAULT FALSE,
            human_corrected    BOOLEAN NOT NULL DEFAULT FALSE,
            human_correction_timestamp TIMESTAMPTZ,
            human_correction_fields JSONB,
            reviewed_by        TEXT,
            reviewed_at        TIMESTAMPTZ,
            reviewer_notes     TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at       TIMESTAMPTZ,
            UNIQUE (job_id, page_number, sub_page_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_lineage_job " "ON page_lineage(job_id, acceptance_decision, created_at)"
    )

    # ── service_invocations ────────────────────────────────────────────────────
    # Per-invocation record for every IEP call made during processing.
    op.execute(
        """
        CREATE TABLE service_invocations (
            id                SERIAL PRIMARY KEY,
            lineage_id        TEXT NOT NULL REFERENCES page_lineage(lineage_id),
            service_name      TEXT NOT NULL,
            service_version   TEXT,
            model_version     TEXT,
            model_source      TEXT,
            invoked_at        TIMESTAMPTZ NOT NULL,
            completed_at      TIMESTAMPTZ,
            processing_time_ms REAL,
            status            TEXT NOT NULL CHECK (status IN (
                                   'success', 'error', 'timeout', 'skipped'
                               )),
            error_message     TEXT,
            metrics           JSONB,
            config_snapshot   JSONB
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_invocations_lineage " "ON service_invocations(lineage_id, service_name)"
    )

    # ── quality_gate_log ───────────────────────────────────────────────────────
    # Immutable record of every quality gate decision.
    # gate_type covers geometry selection (both passes), artifact validation
    # (both passes), and layout consensus.
    # route_decision: the actual page status transition triggered by this gate.
    op.execute(
        """
        CREATE TABLE quality_gate_log (
            gate_id            TEXT PRIMARY KEY,
            job_id             TEXT NOT NULL,
            page_number        INTEGER NOT NULL,
            gate_type          TEXT NOT NULL CHECK (gate_type IN (
                                   'geometry_selection',
                                   'geometry_selection_post_rectification',
                                   'artifact_validation',
                                   'artifact_validation_final',
                                   'layout'
                               )),
            iep1a_geometry     JSONB,
            iep1b_geometry     JSONB,
            structural_agreement BOOLEAN,
            selected_model     TEXT,
            selection_reason   TEXT,
            sanity_check_results JSONB,
            split_confidence   JSONB,
            tta_variance       JSONB,
            artifact_validation_score FLOAT,
            route_decision     TEXT NOT NULL CHECK (route_decision IN (
                                   'accepted',
                                   'rectification',
                                   'pending_human_correction',
                                   'review'
                               )),
            review_reason      TEXT,
            processing_time_ms REAL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_quality_gate_job " "ON quality_gate_log(job_id, page_number)")
    op.execute("CREATE INDEX idx_quality_gate_route " "ON quality_gate_log(route_decision)")
    op.execute(
        "CREATE INDEX idx_quality_gate_agreement " "ON quality_gate_log(structural_agreement)"
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.execute("DROP TABLE IF EXISTS quality_gate_log")
    op.execute("DROP TABLE IF EXISTS service_invocations")
    op.execute("DROP TABLE IF EXISTS page_lineage")
    op.execute("DROP TABLE IF EXISTS job_pages")
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TABLE IF EXISTS users")
