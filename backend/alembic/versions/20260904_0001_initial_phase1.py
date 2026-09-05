"""Initial Phase 1 PostgreSQL foundation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260904_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    timestamp_type = sa.DateTime(timezone=True)

    op.create_table(
        "courses",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("template_id", sa.String(length=100), nullable=False),
        sa.Column("input_json", json_type, nullable=False),
        sa.Column("metadata_json", json_type, nullable=False),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_id"),
        sa.UniqueConstraint("document_id"),
    )
    op.create_index("ix_courses_course_id", "courses", ["course_id"])
    op.create_index("ix_courses_document_id", "courses", ["document_id"])

    op.create_table(
        "documents",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("course_pk", uuid_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document_json", json_type, nullable=False),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
        sa.UniqueConstraint("course_pk"),
    )
    op.create_index("ix_documents_document_id", "documents", ["document_id"])

    op.create_table(
        "blueprints",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_pk", uuid_type, nullable=False),
        sa.Column("blueprint_json", json_type, nullable=False),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_pk", name="uq_blueprints_course_pk"),
    )

    op.create_table(
        "generation_runs",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_pk", uuid_type, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("payload_json", json_type, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_generation_runs_course_pk", "generation_runs", ["course_pk"])


def downgrade() -> None:
    op.drop_index("ix_generation_runs_course_pk", table_name="generation_runs")
    op.drop_table("generation_runs")
    op.drop_table("blueprints")
    op.drop_index("ix_documents_document_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_courses_document_id", table_name="courses")
    op.drop_index("ix_courses_course_id", table_name="courses")
    op.drop_table("courses")