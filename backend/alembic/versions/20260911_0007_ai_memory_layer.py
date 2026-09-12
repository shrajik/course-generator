"""AI memory layer: templates (versioned), visual_knowledge, course_samples.

generation_runs already exists (see 20260904_0001) and only needed a
repository/service to actually be written to - no migration required for it.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260911_0007"
down_revision = "20260906_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    timestamp_type = sa.DateTime(timezone=True)
    tags_type = postgresql.ARRAY(sa.String(length=64))

    op.create_table(
        "templates",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("template_json", json_type, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("created_by", uuid_type, nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id"),
        sa.UniqueConstraint("slug", "version", name="uq_templates_slug_version"),
    )
    op.create_index("ix_templates_slug", "templates", ["slug"])
    op.create_index("ix_templates_template_id", "templates", ["template_id"])
    op.create_index("ix_templates_is_current", "templates", ["is_current"])

    op.create_table(
        "visual_knowledge",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_pk", uuid_type, nullable=True),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("asset_path", sa.String(length=300), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("diagram_kind", sa.String(length=32), nullable=True),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", tags_type, nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("created_by", uuid_type, nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_visual_knowledge_course_pk", "visual_knowledge", ["course_pk"])
    op.create_index("ix_visual_knowledge_course_id", "visual_knowledge", ["course_id"])
    op.create_index("ix_visual_knowledge_approved", "visual_knowledge", ["approved"])

    op.create_table(
        "course_samples",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_pk", uuid_type, nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", tags_type, nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("created_by", uuid_type, nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_pk"),
    )
    op.create_index("ix_course_samples_course_pk", "course_samples", ["course_pk"])
    op.create_index("ix_course_samples_course_id", "course_samples", ["course_id"])
    op.create_index("ix_course_samples_approved", "course_samples", ["approved"])


def downgrade() -> None:
    op.drop_index("ix_course_samples_approved", table_name="course_samples")
    op.drop_index("ix_course_samples_course_id", table_name="course_samples")
    op.drop_index("ix_course_samples_course_pk", table_name="course_samples")
    op.drop_table("course_samples")

    op.drop_index("ix_visual_knowledge_approved", table_name="visual_knowledge")
    op.drop_index("ix_visual_knowledge_course_id", table_name="visual_knowledge")
    op.drop_index("ix_visual_knowledge_course_pk", table_name="visual_knowledge")
    op.drop_table("visual_knowledge")

    op.drop_index("ix_templates_is_current", table_name="templates")
    op.drop_index("ix_templates_template_id", table_name="templates")
    op.drop_index("ix_templates_slug", table_name="templates")
    op.drop_table("templates")
