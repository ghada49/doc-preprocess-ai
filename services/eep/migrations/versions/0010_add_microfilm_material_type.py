"""Add microfilm to jobs material_type check constraint.

IEP0 classifies pages as 'microfilm' but the original constraint only
allowed 'book', 'newspaper', 'archival_document'.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES_WITH_MICROFILM = "'book', 'newspaper', 'archival_document', 'microfilm'"
_TYPES_WITHOUT_MICROFILM = "'book', 'newspaper', 'archival_document'"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE jobs
            DROP CONSTRAINT IF EXISTS jobs_material_type_check,
            ADD CONSTRAINT jobs_material_type_check
            CHECK (material_type IN ({_TYPES_WITH_MICROFILM}));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE jobs
        SET material_type = 'archival_document'
        WHERE material_type = 'microfilm';
        """
    )
    op.execute(
        f"""
        ALTER TABLE jobs
            DROP CONSTRAINT IF EXISTS jobs_material_type_check,
            ADD CONSTRAINT jobs_material_type_check
            CHECK (material_type IN ({_TYPES_WITHOUT_MICROFILM}));
        """
    )
