"""Role/permission plumbing that needs no database: the `Role` enum, the
`require_roles` dependency factory, and the `has_permission` helper.

DB-backed behaviour (login issuing the right role, admin endpoints enforcing
roles, the migration's data transform) lives in test_role_authorization.py,
gated behind TEST_DATABASE_URL like the rest of the Postgres-backed suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import ForbiddenError
from app.core.roles import DEFAULT_ROLE, Permission, ROLE_PERMISSIONS, ROLE_VALUES, Role, has_permission


def test_role_enum_matches_frd_roles():
    assert set(ROLE_VALUES) == {"author", "editor_reviewer", "manager", "admin"}
    assert DEFAULT_ROLE is Role.AUTHOR


def test_every_role_has_at_least_one_permission():
    for role in Role:
        assert ROLE_PERMISSIONS[role], f"{role} has no permissions defined"


def test_author_cannot_approve_or_reject():
    assert has_permission(Role.AUTHOR, Permission.APPROVE_REJECT_CONTENT) is False
    assert has_permission(Role.EDITOR_REVIEWER, Permission.APPROVE_REJECT_CONTENT) is True


def test_manager_cannot_edit_content():
    assert has_permission(Role.MANAGER, Permission.EDIT_CONTENT_UNDER_REVIEW) is False
    assert has_permission(Role.MANAGER, Permission.VIEW_PROJECTS_AND_REPORTS) is True


def test_only_admin_manages_users_and_roles():
    for role in Role:
        expected = role is Role.ADMIN
        assert has_permission(role, Permission.MANAGE_USERS_AND_ROLES) is expected


def test_has_permission_rejects_unknown_role_string():
    assert has_permission("not-a-role", Permission.VIEW_PUBLISHED_CONTENT) is False


async def test_require_roles_allows_matching_role():
    from app.api.dependencies import require_roles

    dependency = require_roles(Role.MANAGER, Role.ADMIN)
    fake_manager = SimpleNamespace(role="manager")

    result = await dependency(current_user=fake_manager)

    assert result is fake_manager


async def test_require_roles_rejects_other_roles():
    from app.api.dependencies import require_roles

    dependency = require_roles(Role.MANAGER, Role.ADMIN)
    fake_author = SimpleNamespace(role="author")

    with pytest.raises(ForbiddenError):
        await dependency(current_user=fake_author)


async def test_get_current_admin_is_admin_only():
    from app.api.dependencies import get_current_admin

    fake_admin = SimpleNamespace(role="admin")
    fake_manager = SimpleNamespace(role="manager")

    assert await get_current_admin(current_user=fake_admin) is fake_admin
    with pytest.raises(ForbiddenError):
        await get_current_admin(current_user=fake_manager)


def test_role_expansion_migration_sql_covers_all_roles():
    """Offline SQL generation needs no live database (mirrors
    test_alembic_initial_migration_generates_sql in test_database_foundation.py)."""
    backend_dir = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ck_users_role_valid" in result.stdout
    for role in ROLE_VALUES:
        assert role in result.stdout
