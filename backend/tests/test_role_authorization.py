"""FRD role alignment: each of the four platform roles can authenticate,
/admin/* stays ADMIN-only against every other role, and the role-expansion
migration's data transform (user -> author, admin stays admin) is correct.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_admin_authorization.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)

ALL_ROLES = ("author", "editor_reviewer", "manager", "admin")
NON_ADMIN_ROLES = ("author", "editor_reviewer", "manager")


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    """Schema lifecycle lives in conftest.py's session-scoped db_schema
    fixture; this just keeps the JWT env vars set for the whole module."""
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def role_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("USE_DATABASE", "true")
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "")
    reset_settings_cache()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_settings_cache()


def _email(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}@example.com"


def _register(client, email: str, password: str = "CourseTest123!"):
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["user"]


def _login(client, email: str, password: str = "CourseTest123!"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _set_role(client, admin_email: str, user_id: str, role: str):
    _login(client, admin_email)
    response = client.patch(f"/admin/users/{user_id}/role", json={"role": role})
    assert response.status_code == 200, response.text
    client.post("/auth/logout")
    return response.json()


def test_login_authenticates_with_the_correct_role(role_client, monkeypatch):
    admin_email = _email("login-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    user = _register(role_client, admin_email)
    assert user["role"] == "admin"

    role_client.post("/auth/logout")
    me = _login(role_client, admin_email)
    assert me["role"] == "admin"


def test_new_registration_defaults_to_author(role_client):
    email = _email("default-role")
    user = _register(role_client, email)
    assert user["role"] == "author"


def test_each_of_the_four_roles_can_authenticate(role_client, monkeypatch):
    admin_email = _email("four-roles-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(role_client, admin_email)
    role_client.post("/auth/logout")

    for role in NON_ADMIN_ROLES:
        email = _email(f"role-{role}")
        user = _register(role_client, email)
        role_client.post("/auth/logout")
        _set_role(role_client, admin_email, user["id"], role)

        me = _login(role_client, email)
        assert me["role"] == role
        assert role_client.get("/auth/me").json()["user"]["role"] == role
        role_client.post("/auth/logout")

    admin_me = _login(role_client, admin_email)
    assert admin_me["role"] == "admin"
    assert admin_user["role"] == "admin"


def test_admin_endpoints_reject_every_non_admin_role(role_client, monkeypatch):
    admin_email = _email("gate-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(role_client, admin_email)
    role_client.post("/auth/logout")

    for role in ("author", "editor_reviewer", "manager"):
        email = _email(f"gate-{role}")
        user = _register(role_client, email)
        role_client.post("/auth/logout")
        _set_role(role_client, admin_email, user["id"], role)

        _login(role_client, email)
        assert role_client.get("/admin/dashboard").status_code == 403
        assert role_client.get("/admin/users").status_code == 403
        role_client.post("/auth/logout")


def test_admin_can_access_admin_endpoints(role_client, monkeypatch):
    admin_email = _email("access-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(role_client, admin_email)

    assert role_client.get("/admin/dashboard").status_code == 200
    assert role_client.get("/admin/users").status_code == 200


def test_admin_can_assign_any_of_the_four_roles(role_client, monkeypatch):
    admin_email = _email("assign-admin")
    target_email = _email("assign-target")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(role_client, admin_email)
    role_client.post("/auth/logout")
    target = _register(role_client, target_email)
    role_client.post("/auth/logout")
    _login(role_client, admin_email)

    for role in ALL_ROLES:
        response = role_client.patch(f"/admin/users/{target['id']}/role", json={"role": role})
        assert response.status_code == 200, response.text
        assert response.json()["role"] == role


def test_admin_cannot_change_own_role(role_client, monkeypatch):
    admin_email = _email("self-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(role_client, admin_email)

    response = role_client.patch(f"/admin/users/{admin_user['id']}/role", json={"role": "manager"})
    assert response.status_code == 403


def test_invalid_role_is_rejected(role_client, monkeypatch):
    admin_email = _email("invalid-admin")
    target_email = _email("invalid-target")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(role_client, admin_email)
    role_client.post("/auth/logout")
    target = _register(role_client, target_email)
    role_client.post("/auth/logout")
    _login(role_client, admin_email)

    response = role_client.patch(f"/admin/users/{target['id']}/role", json={"role": "superuser"})
    assert response.status_code == 422

    # The old two-role vocabulary is no longer accepted either.
    legacy_response = role_client.patch(f"/admin/users/{target['id']}/role", json={"role": "user"})
    assert legacy_response.status_code == 422


async def test_role_expansion_migration_converts_user_to_author_and_keeps_admin():
    """Replays the exact SQL from `20260905_0003_expand_user_roles` inside a
    transaction that's always rolled back, so it never mutates the shared
    test database that other modules in this session also rely on."""
    engine = create_async_engine(DATABASE_URL, future=True)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                now = datetime.now(timezone.utc)
                legacy_user_id = uuid.uuid4()
                legacy_admin_id = uuid.uuid4()

                # Simulate the pre-migration (20260904_0002) schema: no
                # role check constraint yet, so a legacy "user" row is legal.
                await conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role_valid"))
                for user_id, role in ((legacy_user_id, "user"), (legacy_admin_id, "admin")):
                    await conn.execute(
                        text(
                            "INSERT INTO users "
                            "(id, email, password_hash, role, is_active, is_verified, created_at, updated_at) "
                            "VALUES (:id, :email, 'x', :role, true, false, :now, :now)"
                        ),
                        {"id": user_id, "email": f"legacy-{user_id.hex}@example.com", "role": role, "now": now},
                    )

                # Replay 20260905_0003_expand_user_roles.upgrade() verbatim.
                await conn.execute(text("UPDATE users SET role = 'author' WHERE role <> 'admin'"))
                await conn.execute(
                    text(
                        "ALTER TABLE users ADD CONSTRAINT ck_users_role_valid "
                        "CHECK (role IN ('author', 'editor_reviewer', 'manager', 'admin'))"
                    )
                )

                user_role = (
                    await conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": legacy_user_id})
                ).scalar_one()
                admin_role = (
                    await conn.execute(text("SELECT role FROM users WHERE id = :id"), {"id": legacy_admin_id})
                ).scalar_one()

                assert user_role == "author"
                assert admin_role == "admin"
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()
