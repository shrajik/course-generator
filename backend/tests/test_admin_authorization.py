"""Admin authorization behaviour backed by the existing cookie auth flow."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    """Schema lifecycle lives in conftest.py's session-scoped db_schema
    fixture; this just keeps the JWT env vars set for the whole module."""
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def admin_client(monkeypatch):
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


def _register(client, email: str, password: str = "CourseTest123!", **extra):
    payload = {"email": email, "password": password, **extra}
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response


def _login(client, email: str, password: str = "CourseTest123!"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _create_course(client, title: str):
    response = client.post(
        "/api/courses",
        json={
            "course_title": title,
            "toc": [{"title": "Foundations"}],
            "target_audience": "Admins",
            "dos": [],
            "donts": [],
            "template": "technical",
            "run_planner": False,
        },
    )
    assert response.status_code == 201, response.text
    return response


def _insert_admin_course(
    client,
    *,
    title: str,
    status: str = "created",
    owner: str | None = None,
    metadata: dict | None = None,
) -> dict[str, str]:
    async def insert() -> dict[str, str]:
        from app.db.models import Course
        from app.db.session import get_session_factory

        now = datetime.now(timezone.utc)
        suffix = uuid4().hex
        course_metadata = {
            "owner": owner,
            "has_blueprint": True,
            "has_document": False,
            "chapters": [{"chapter_id": "chapter_1", "title": "Foundations"}],
            "warnings": [],
            **(metadata or {}),
        }
        async with get_session_factory()() as session:
            async with session.begin():
                course = Course(
                    course_id=f"crs_{suffix}",
                    document_id=f"doc_{suffix}",
                    title=title,
                    status=status,
                    template_id="technical_v1",
                    input_json={
                        "course_title": title,
                        "toc": [{"title": "Foundations"}],
                        "target_audience": "Admins",
                        "dos": [],
                        "donts": [],
                        "template": "technical",
                        "language": "en",
                        "tone": "",
                    },
                    metadata_json=course_metadata,
                    created_at=now,
                    updated_at=now,
                )
                session.add(course)
                await session.flush()
                return {
                    "id": str(course.id),
                    "course_id": course.course_id,
                    "document_id": course.document_id,
                    "title": course.title,
                    "owner": owner or "",
                }

    # The TestClient drives the ASGI app on a dedicated anyio event loop
    # (its `.portal`). asyncpg connections are bound to the loop they were
    # opened on, and `get_engine()` caches a single engine for the process,
    # so setup work that touches the database must run on that same loop
    # instead of a separate one (e.g. via `anyio.run`), or the pooled
    # connections break the next time the app uses them.
    return client.portal.call(insert)


def test_admin_health_requires_authentication(admin_client):
    response = admin_client.get("/admin/health")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_admin_health_rejects_authenticated_normal_user(admin_client):
    _register(admin_client, _email("normal"))

    response = admin_client.get("/admin/health")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_initial_admin_email_can_access_admin_health(admin_client, monkeypatch):
    admin_email = _email("admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    response = _register(admin_client, admin_email)
    assert response.json()["user"]["role"] == "admin"

    admin_health = admin_client.get("/admin/health")
    assert admin_health.status_code == 200
    assert admin_health.json() == {"status": "ok"}


def test_public_registration_cannot_choose_admin_role(admin_client):
    response = _register(admin_client, _email("selfpromote"), role="admin")
    body = response.json()

    assert body["user"]["role"] == "author"
    assert "password_hash" not in body["user"]


def test_auth_regression_login_me_logout(admin_client):
    email = _email("auth")
    password = "CourseTest123!"
    _register(admin_client, email, password)
    admin_client.post("/auth/logout")

    login = admin_client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["email"] == email
    assert "password_hash" not in login.json()["user"]

    me = admin_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email

    logout = admin_client.post("/auth/logout")
    assert logout.status_code == 200
    assert admin_client.get("/auth/me").status_code == 401


def test_admin_step3_endpoints_require_authentication(admin_client):
    assert admin_client.get("/admin/dashboard").status_code == 401
    assert admin_client.get("/admin/users").status_code == 401
    assert admin_client.get("/admin/courses").status_code == 401


def test_admin_step3_endpoints_reject_normal_user(admin_client):
    normal = _register(admin_client, _email("normal-step3")).json()["user"]

    assert admin_client.get("/admin/dashboard").status_code == 403
    assert admin_client.get("/admin/users").status_code == 403
    assert admin_client.get("/admin/courses").status_code == 403
    assert admin_client.get(f"/admin/courses/{uuid4()}").status_code == 403
    assert admin_client.get(f"/admin/users/{normal['id']}").status_code == 403
    assert admin_client.patch(f"/admin/users/{normal['id']}/role", json={"role": "admin"}).status_code == 403
    assert (
        admin_client.patch(f"/admin/users/{normal['id']}/status", json={"is_active": False}).status_code
        == 403
    )


def test_admin_dashboard_users_courses_and_role_update(admin_client, monkeypatch):
    admin_email = _email("step3-admin")
    target_email = _email("step3-target")
    course_title = f"Admin Visible Course {uuid4().hex}"
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(admin_client, admin_email).json()["user"]
    admin_client.post("/auth/logout")
    target_user = _register(admin_client, target_email).json()["user"]
    admin_client.post("/auth/logout")
    _login(admin_client, admin_email)
    course = _create_course(admin_client, course_title).json()

    dashboard = admin_client.get("/admin/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["total_users"] >= 2
    assert dashboard.json()["total_courses"] >= 1
    assert dashboard.json()["system_status"] == "ok"

    users = admin_client.get("/admin/users")
    assert users.status_code == 200, users.text
    listed_users = users.json()["users"]
    assert any(user["id"] == admin_user["id"] for user in listed_users)
    assert any(user["email"] == target_email for user in listed_users)
    assert all("password_hash" not in user for user in listed_users)

    role_update = admin_client.patch(
        f"/admin/users/{target_user['id']}/role",
        json={"role": "admin"},
    )
    assert role_update.status_code == 200, role_update.text
    assert role_update.json()["role"] == "admin"

    self_update = admin_client.patch(
        f"/admin/users/{admin_user['id']}/role",
        json={"role": "author"},
    )
    assert self_update.status_code == 403

    courses = admin_client.get("/admin/courses")
    assert courses.status_code == 200, courses.text
    listed_courses = courses.json()["courses"]
    assert any(item["course_id"] == course["course_id"] for item in listed_courses)
    visible_course = next(item for item in listed_courses if item["course_id"] == course["course_id"])
    assert visible_course["title"] == course_title
    assert visible_course["status"] == "created"


def test_admin_users_search_filter_detail_and_status(admin_client, monkeypatch):
    admin_email = _email("step4-admin")
    target_email = _email("step4-target")
    inactive_email = _email("step4-inactive")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(admin_client, admin_email).json()["user"]
    admin_client.post("/auth/logout")
    target_user = _register(admin_client, target_email).json()["user"]
    admin_client.post("/auth/logout")
    inactive_user = _register(admin_client, inactive_email).json()["user"]
    admin_client.post("/auth/logout")
    _login(admin_client, admin_email)

    users = admin_client.get("/admin/users")
    assert users.status_code == 200, users.text
    assert any(user["id"] == target_user["id"] for user in users.json()["users"])

    search_by_email = admin_client.get("/admin/users", params={"search": target_email})
    assert search_by_email.status_code == 200, search_by_email.text
    assert search_by_email.json()["total"] == 1
    assert search_by_email.json()["users"][0]["id"] == target_user["id"]

    search_by_id = admin_client.get("/admin/users", params={"search": target_user["id"]})
    assert search_by_id.status_code == 200, search_by_id.text
    assert search_by_id.json()["total"] == 1
    assert search_by_id.json()["users"][0]["email"] == target_email

    role_filter = admin_client.get("/admin/users", params={"role": "admin", "search": admin_email})
    assert role_filter.status_code == 200, role_filter.text
    assert role_filter.json()["total"] == 1
    assert role_filter.json()["users"][0]["id"] == admin_user["id"]

    inactive_update = admin_client.patch(
        f"/admin/users/{inactive_user['id']}/status",
        json={"is_active": False},
    )
    assert inactive_update.status_code == 200, inactive_update.text
    assert inactive_update.json()["is_active"] is False

    inactive_filter = admin_client.get(
        "/admin/users",
        params={"is_active": False, "search": inactive_email},
    )
    assert inactive_filter.status_code == 200, inactive_filter.text
    assert inactive_filter.json()["total"] == 1
    assert inactive_filter.json()["users"][0]["id"] == inactive_user["id"]

    active_update = admin_client.patch(
        f"/admin/users/{inactive_user['id']}/status",
        json={"is_active": True},
    )
    assert active_update.status_code == 200, active_update.text
    assert active_update.json()["is_active"] is True

    detail = admin_client.get(f"/admin/users/{target_user['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["email"] == target_email
    assert detail.json()["role"] == "author"
    assert detail.json()["is_active"] is True
    assert "password_hash" not in detail.json()


def test_admin_user_self_protection_and_validation(admin_client, monkeypatch):
    admin_email = _email("step4-protect-admin")
    target_email = _email("step4-protect-target")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(admin_client, admin_email).json()["user"]
    admin_client.post("/auth/logout")
    target_user = _register(admin_client, target_email).json()["user"]
    admin_client.post("/auth/logout")
    _login(admin_client, admin_email)

    self_status = admin_client.patch(
        f"/admin/users/{admin_user['id']}/status",
        json={"is_active": False},
    )
    assert self_status.status_code == 403

    self_role = admin_client.patch(
        f"/admin/users/{admin_user['id']}/role",
        json={"role": "author"},
    )
    assert self_role.status_code == 403

    invalid_role = admin_client.patch(
        f"/admin/users/{target_user['id']}/role",
        json={"role": "owner"},
    )
    assert invalid_role.status_code == 422

    missing_user = admin_client.get(f"/admin/users/{uuid4()}")
    assert missing_user.status_code == 404

    invalid_uuid = admin_client.get("/admin/users/not-a-uuid")
    assert invalid_uuid.status_code == 422


def test_admin_courses_search_filters_pagination_and_detail(admin_client, monkeypatch):
    admin_email = _email("step5-admin")
    owner_email = _email("step5-owner")
    other_owner_email = _email("step5-other-owner")
    title_prefix = f"Step 5 Course {uuid4().hex}"
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(admin_client, admin_email)
    first = _insert_admin_course(
        admin_client,
        title=f"{title_prefix} One",
        status="ready",
        owner=owner_email,
        metadata={
            "has_document": True,
            "run": {"state": "done"},
            "warnings": ["Reviewed by admin test"],
        },
    )
    second = _insert_admin_course(
        admin_client,
        title=f"{title_prefix} Two",
        status="failed",
        owner=other_owner_email,
        metadata={"last_error": "chapter failed", "run": {"state": "failed"}},
    )

    listed = admin_client.get("/admin/courses", params={"search": title_prefix, "limit": 1, "offset": 0})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 2
    assert len(listed.json()["courses"]) == 1
    assert {"ready", "failed"} <= set(listed.json()["statuses"])

    next_page = admin_client.get("/admin/courses", params={"search": title_prefix, "limit": 1, "offset": 1})
    assert next_page.status_code == 200, next_page.text
    assert len(next_page.json()["courses"]) == 1
    assert next_page.json()["courses"][0]["id"] != listed.json()["courses"][0]["id"]

    search_by_course_id = admin_client.get("/admin/courses", params={"search": first["course_id"]})
    assert search_by_course_id.status_code == 200, search_by_course_id.text
    assert search_by_course_id.json()["total"] == 1
    assert search_by_course_id.json()["courses"][0]["id"] == first["id"]

    status_filter = admin_client.get(
        "/admin/courses",
        params={"search": title_prefix, "status": "failed"},
    )
    assert status_filter.status_code == 200, status_filter.text
    assert status_filter.json()["total"] == 1
    assert status_filter.json()["courses"][0]["id"] == second["id"]

    owner_filter = admin_client.get("/admin/courses", params={"owner": owner_email})
    assert owner_filter.status_code == 200, owner_filter.text
    assert owner_filter.json()["total"] == 1
    assert owner_filter.json()["courses"][0]["id"] == first["id"]

    detail = admin_client.get(f"/admin/courses/{first['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["course_id"] == first["course_id"]
    assert body["document_id"] == first["document_id"]
    assert body["title"] == first["title"]
    assert body["owner"] == owner_email
    assert body["status"] == "ready"
    assert body["target_audience"] == "Admins"
    assert body["toc_count"] == 1
    assert body["chapters_count"] == 1
    assert body["has_blueprint"] is True
    assert body["has_document"] is True
    assert body["run_state"] == "done"
    assert body["warnings"] == ["Reviewed by admin test"]


def test_admin_course_detail_validation(admin_client, monkeypatch):
    admin_email = _email("step5-validation-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(admin_client, admin_email)

    missing_course = admin_client.get(f"/admin/courses/{uuid4()}")
    assert missing_course.status_code == 404

    invalid_uuid = admin_client.get("/admin/courses/not-a-uuid")
    assert invalid_uuid.status_code == 422

    invalid_status = admin_client.get("/admin/courses", params={"status": "archived"})
    assert invalid_status.status_code == 422
