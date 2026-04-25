"""Create shadow_evaluations table

Records written by the shadow-worker for every page that completes
processing in a shadow_mode=True job.  Enables tracking of shadow
pipeline coverage and confidence deltas once a shadow model version
is promoted.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE shadow_evaluations (
            eval_id          TEXT PRIMARY KEY,
            job_id           TEXT NOT NULL,
            page_id          TEXT NOT NULL,
            page_status      TEXT NOT NULL,
            confidence_delta FLOAT,
            status           TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN (
                                 'pending', 'completed', 'failed', 'no_shadow_model'
                             )),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at     TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_shadow_evaluations_job ON shadow_evaluations(job_id)"
    )
    op.execute(
        "CREATE INDEX idx_shadow_evaluations_status ON shadow_evaluations(status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS shadow_evaluations")
