"""Enforce one original page row per job/page number.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_job_pages_original_page",
        "job_pages",
        ["job_id", "page_number"],
        unique=True,
        postgresql_where=sa.text("sub_page_index IS NULL"),
    )
    op.create_index(
        "uq_page_lineage_original_page",
        "page_lineage",
        ["job_id", "page_number"],
        unique=True,
        postgresql_where=sa.text("sub_page_index IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_page_lineage_original_page", table_name="page_lineage")
    op.drop_index("uq_job_pages_original_page", table_name="job_pages")
