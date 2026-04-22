"""Add layout_with_ocr to jobs.pipeline_mode CHECK constraint.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_pipeline_mode_check")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT jobs_pipeline_mode_check
            CHECK (pipeline_mode IN ('preprocess', 'layout', 'layout_with_ocr'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_pipeline_mode_check")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT jobs_pipeline_mode_check
            CHECK (pipeline_mode IN ('preprocess', 'layout'))
        """
    )
