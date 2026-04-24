"""Semantic reading-order persistence

Adds:
  job_pages.reading_order   INT NULLABLE   — semantic sequence (1=first page reader sees)
  jobs.reading_direction    VARCHAR(12) NULLABLE — 'ltr' | 'rtl' | 'unresolved'

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_pages
            ADD COLUMN IF NOT EXISTS reading_order INTEGER;
        """
    )
    op.execute(
        """
        ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS reading_direction VARCHAR(12);
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE job_pages DROP COLUMN IF EXISTS reading_order;")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS reading_direction;")
