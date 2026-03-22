"""Phase 5 — add ptiff_qa_approved to job_pages

Adds the boolean approval-tracking column required by the PTIFF QA gate
workflow (Packet 5.0a, spec Section 3.1).

Per-page approval intent is recorded in this column. The gate releases
when every ptiff_qa_pending page has ptiff_qa_approved=TRUE and no page
is in pending_human_correction.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_pages
            ADD COLUMN ptiff_qa_approved BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_pages
            DROP COLUMN ptiff_qa_approved
        """
    )
