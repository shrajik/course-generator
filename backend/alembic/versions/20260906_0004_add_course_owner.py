"""Add courses.owner_id (FK to users.id) for per-user course ownership."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_0004"
down_revision = "20260905_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: existing courses predate ownership and have no reliable user
    # to backfill from, so they stay ownerless (visible to admins only) rather
    # than being deleted or guessed at.
    op.add_column(
        "courses",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_courses_owner_id", "courses", ["owner_id"])
    op.create_foreign_key(
        "fk_courses_owner_id_users",
        "courses",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_courses_owner_id_users", "courses", type_="foreignkey")
    op.drop_index("ix_courses_owner_id", table_name="courses")
    op.drop_column("courses", "owner_id")
