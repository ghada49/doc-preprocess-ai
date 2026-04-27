"""Fix retraining_jobs result_map column name.

Revision ID: 0004a
Revises: 0004
Create Date: 2026-04-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0004a"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Normalize to `result_mAP` (camelCase) — the canonical column name.
    # Older DBs that were previously migrated to `result_map` are renamed back.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'retraining_jobs'
                  AND column_name = 'result_map'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'retraining_jobs'
                  AND column_name = 'result_mAP'
            )
            THEN
                ALTER TABLE retraining_jobs RENAME COLUMN result_map TO "result_mAP";
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'retraining_jobs'
                  AND column_name = 'result_mAP'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'retraining_jobs'
                  AND column_name = 'result_map'
            )
            THEN
                ALTER TABLE retraining_jobs RENAME COLUMN "result_mAP" TO result_map;
            END IF;
        END $$;
        """
    )
