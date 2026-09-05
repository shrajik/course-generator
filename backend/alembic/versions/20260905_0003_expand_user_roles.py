"""Expand user roles from user/admin to author/editor_reviewer/manager/admin."""

from alembic import op
import sqlalchemy as sa

revision = "20260905_0003"
down_revision = "20260904_0002"
branch_labels = None
depends_on = None

_VALID_ROLES = ("author", "editor_reviewer", "manager", "admin")


def upgrade() -> None:
    # Existing "admin" accounts stay admin; every other existing account
    # (previously just "user") becomes AUTHOR, the closest equivalent role.
    op.execute(sa.text("UPDATE users SET role = 'author' WHERE role <> 'admin'"))
    roles_sql = ", ".join(f"'{role}'" for role in _VALID_ROLES)
    op.create_check_constraint(
        "ck_users_role_valid",
        "users",
        f"role IN ({roles_sql})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_role_valid", "users", type_="check")
    op.execute(sa.text("UPDATE users SET role = 'user' WHERE role <> 'admin'"))
