"""Create model_promotion_audit table

Immutable audit log for every model promote and rollback action.
Enables querying who promoted/rolled-back each model version and whether
gate checks were bypassed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_promotion_audit (
            audit_id                TEXT PRIMARY KEY,
            action                  TEXT NOT NULL
                                    CHECK (action IN ('promote', 'rollback')),
            service_name            TEXT NOT NULL,
            candidate_model_id      TEXT NOT NULL,
            previous_model_id       TEXT,
            promoted_by_user_id     TEXT NOT NULL,
            forced                  BOOLEAN NOT NULL DEFAULT FALSE,
            failed_gates_bypassed   JSONB,
            reason                  TEXT,
            notes                   TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_promotion_audit_service ON model_promotion_audit(service_name)"
    )
    op.execute(
        "CREATE INDEX idx_promotion_audit_candidate ON model_promotion_audit(candidate_model_id)"
    )
    op.execute(
        "CREATE INDEX idx_promotion_audit_user ON model_promotion_audit(promoted_by_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_promotion_audit")
