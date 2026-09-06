"""Add course_activities: a lightweight append-only audit log."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_0006"
down_revision = "20260906_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    timestamp_type = sa.DateTime(timezone=True)

    op.create_table(
        "course_activities",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("course_pk", uuid_type, nullable=False),
        sa.Column("user_id", uuid_type, nullable=True),
        sa.Column("user_email", sa.String(length=320), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.ForeignKeyConstraint(["course_pk"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_course_activities_course_pk", "course_activities", ["course_pk"])
    op.create_index("ix_course_activities_created_at", "course_activities", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_course_activities_created_at", table_name="course_activities")
    op.drop_index("ix_course_activities_course_pk", table_name="course_activities")
    op.drop_table("course_activities")
