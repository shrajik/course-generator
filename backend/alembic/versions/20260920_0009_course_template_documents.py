"""Uploaded course template documents (DOCX/Markdown -> normalized Markdown).

Admin-uploaded reference documents offered on Create Course as an optional
starting point. Deliberately separate from `templates` (the JSONB
block-schema table the AI pipeline reads via load_template()) - this table
holds prose reference content, not a generation schema, and nothing here is
wired into the pipeline yet.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260920_0009"
down_revision = "20260912_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    timestamp_type = sa.DateTime(timezone=True)

    op.create_table(
        "course_template_documents",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("template_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("content_format", sa.String(length=16), nullable=False, server_default="markdown"),
        sa.Column("markdown_content", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=400), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", uuid_type, nullable=True),
        sa.Column("created_by_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_course_template_documents_template_type",
        "course_template_documents",
        ["template_type"],
    )
    op.create_index(
        "ix_course_template_documents_is_active",
        "course_template_documents",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_course_template_documents_is_active", table_name="course_template_documents")
    op.drop_index("ix_course_template_documents_template_type", table_name="course_template_documents")
    op.drop_table("course_template_documents")
