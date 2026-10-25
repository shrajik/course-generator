"""Who can see an uploaded template: private (uploader + admins) or shared.

New uploads default to private. Every row that already exists becomes
"shared", because until now every upload was visible to every user - nothing
anyone could see before this migration disappears after it.

Revision ID: 20261026_0011
Revises: 20260929_0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261026_0011"
down_revision = "20260929_0010"
branch_labels = None
depends_on = None

_TABLE = "course_template_documents"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="shared"),
    )
    # The server default only fills existing rows; the application sets the
    # value explicitly for new uploads (private unless the uploader shares).
    op.alter_column(_TABLE, "visibility", server_default="private")
    op.create_index("ix_course_template_documents_visibility", _TABLE, ["visibility"])


def downgrade() -> None:
    op.drop_index("ix_course_template_documents_visibility", table_name=_TABLE)
    op.drop_column(_TABLE, "visibility")
