"""Add course review/approval workflow columns.

draft -> in_review -> approved
                    -> changes_requested -> in_review (resubmit) -> ...
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_0005"
down_revision = "20260906_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default backfills every existing course to "draft" (not yet
    # submitted) with no data loss and no separate UPDATE pass.
    op.add_column(
        "courses",
        sa.Column("review_status", sa.String(length=32), nullable=False, server_default="draft"),
    )
    op.create_index("ix_courses_review_status", "courses", ["review_status"])

    op.add_column("courses", sa.Column("review_comment", sa.Text(), nullable=True))

    op.add_column(
        "courses",
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_courses_reviewer_id", "courses", ["reviewer_id"])
    op.create_foreign_key(
        "fk_courses_reviewer_id_users",
        "courses",
        "users",
        ["reviewer_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "courses",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("courses", "reviewed_at")
    op.drop_constraint("fk_courses_reviewer_id_users", "courses", type_="foreignkey")
    op.drop_index("ix_courses_reviewer_id", table_name="courses")
    op.drop_column("courses", "reviewer_id")
    op.drop_column("courses", "review_comment")
    op.drop_index("ix_courses_review_status", table_name="courses")
    op.drop_column("courses", "review_status")
