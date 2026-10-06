"""Store the parsed template + parse report on uploaded template documents.

Additive and nullable: rows uploaded before the DOCX parser existed keep
working and simply fall back to their built-in template, so this migration is
safe to apply to a live database with no backfill.

Revision ID: 20260929_0010
Revises: 20260920_0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929_0010"
down_revision = "20260920_0009"
branch_labels = None
depends_on = None

_TABLE = "course_template_documents"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("template_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("parse_report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(_TABLE, sa.Column("parser_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "parser_version")
    op.drop_column(_TABLE, "parse_report_json")
    op.drop_column(_TABLE, "template_json")
