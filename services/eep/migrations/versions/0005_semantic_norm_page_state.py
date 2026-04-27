"""Allow semantic_norm page state

Adds ``semantic_norm`` to the ``job_pages.status`` check constraint so pages
submitted from human correction can run the post-correction IEP1E pass before
continuing to layout detection or acceptance.

Revision ID: 0005
Revises: 0004a
Create Date: 2026-04-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers
revision: str = "0005"
down_revision: str | None = "0004a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATES_WITH_SEMANTIC_NORM = (
    "'queued', 'preprocessing', 'rectification', 'ptiff_qa_pending', "
    "'layout_detection', 'semantic_norm', 'pending_human_correction', "
    "'accepted', 'review', 'failed', 'split'"
)

_STATES_WITHOUT_SEMANTIC_NORM = (
    "'queued', 'preprocessing', 'rectification', 'ptiff_qa_pending', "
    "'layout_detection', 'pending_human_correction', "
    "'accepted', 'review', 'failed', 'split'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE job_pages
            DROP CONSTRAINT IF EXISTS job_pages_status_check,
            ADD CONSTRAINT job_pages_status_check
            CHECK (status IN ({_STATES_WITH_SEMANTIC_NORM}));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE job_pages
        SET status = 'pending_human_correction'
        WHERE status = 'semantic_norm';
        """
    )
    op.execute(
        f"""
        ALTER TABLE job_pages
            DROP CONSTRAINT IF EXISTS job_pages_status_check,
            ADD CONSTRAINT job_pages_status_check
            CHECK (status IN ({_STATES_WITHOUT_SEMANTIC_NORM}));
        """
    )
