"""Per-user course ownership: a course belongs to the user who created it;
only that owner (or an admin) can view, edit, generate, or export it - and a
user can't get at someone else's course by changing the id in the URL.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_admin_authorization.py and test_role_authorization.py, since ownership
only exists once Postgres-backed user identity does (see
`get_current_user_if_db_enabled` in app/api/dependencies.py).
"""

from __future__ import annotations

import os
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
def owner_client(monkeypatch):
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


def _create_course(client, title: str = "Ownership Test Course"):
    payload = {
        "course_title": title,
        "toc": [{"title": "Foundations"}],
        "target_audience": "Backend engineers",
        "dos": [],
        "donts": [],
        "template": "technical",
        "run_planner": False,
    }
    response = client.post("/api/courses", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_course_creation_assigns_authenticated_user_as_owner(owner_client):
    user = _register(owner_client, _email("owner-assign"))

    course = _create_course(owner_client)

    assert course["owner_id"] == user["id"]


def test_owner_id_cannot_be_spoofed_from_the_request_body(owner_client):
    """CreateCourseRequest forbids unknown fields, so `owner_id` in the body
    is rejected outright rather than silently accepted or ignored."""
    _register(owner_client, _email("owner-spoof"))

    response = owner_client.post(
        "/api/courses",
        json={
            "course_title": "Spoof Attempt",
            "toc": [{"title": "Foundations"}],
            "target_audience": "Backend engineers",
            "dos": [],
            "donts": [],
            "template": "technical",
            "run_planner": False,
            "owner_id": str(uuid4()),
        },
    )
    assert response.status_code == 422, response.text


def test_owner_can_access_their_own_course(owner_client):
    _register(owner_client, _email("owner-access"))
    course = _create_course(owner_client)
    course_id = course["course_id"]

    assert owner_client.get(f"/api/courses/{course_id}").status_code == 200
    assert owner_client.get(f"/api/courses/{course_id}/template").status_code == 200
    assert owner_client.get(f"/api/courses/{course_id}/run").status_code == 200
    # No blueprint/document yet (run_planner=False, generation never run) - a
    # 404 here still proves the ownership gate let the owner through to the
    # actual lookup, rather than blocking them.
    assert owner_client.get(f"/api/courses/{course_id}/blueprint").status_code == 404
    assert owner_client.get(f"/api/courses/{course_id}/document").status_code == 404


def test_user_cannot_access_another_users_course(owner_client):
    _register(owner_client, _email("owner-a"))
    course = _create_course(owner_client, "User A's Course")
    course_id = course["course_id"]
    document_id = course["document_id"]
    owner_client.post("/auth/logout")

    _register(owner_client, _email("owner-b"))

    assert owner_client.get(f"/api/courses/{course_id}").status_code == 403
    assert owner_client.get(f"/api/courses/{course_id}/template").status_code == 403
    assert owner_client.get(f"/api/courses/{course_id}/run").status_code == 403
    assert owner_client.get(f"/api/courses/{course_id}/blueprint").status_code == 403
    assert owner_client.get(f"/api/courses/{course_id}/document").status_code == 403
    assert (
        owner_client.get(f"/api/courses/{course_id}/chapters/chapter_1").status_code == 403
    )
    assert (
        owner_client.get(f"/api/courses/{course_id}/chapters/chapter_1/research").status_code
        == 403
    )
    assert (
        owner_client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"}).status_code
        == 403
    )

    assert owner_client.get(f"/api/documents/{document_id}").status_code == 403
    assert owner_client.get(f"/api/documents/{document_id}/preview").status_code == 403
    assert (
        owner_client.post(
            f"/api/documents/{document_id}/ai-edit",
            json={"selected_block_ids": ["block_1"], "instruction": "Make this clearer"},
        ).status_code
        == 403
    )
    assert owner_client.post(f"/api/documents/{document_id}/export/pdf").status_code == 403
    assert (
        owner_client.get(f"/api/documents/{document_id}/assets/whatever.png").status_code == 403
    )


def test_generation_endpoint_enforces_ownership(owner_client):
    _register(owner_client, _email("gen-owner"))
    course = _create_course(owner_client)
    course_id = course["course_id"]
    owner_client.post("/auth/logout")

    _register(owner_client, _email("gen-other"))
    blocked = owner_client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert blocked.status_code == 403


def test_admin_can_access_any_users_course(owner_client, monkeypatch):
    admin_email = _email("owner-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(owner_client, _email("owner-c"))
    course = _create_course(owner_client, "User C's Course")
    course_id = course["course_id"]
    document_id = course["document_id"]
    owner_client.post("/auth/logout")

    _register(owner_client, admin_email)  # role=admin via INITIAL_ADMIN_EMAIL

    assert owner_client.get(f"/api/courses/{course_id}").status_code == 200
    assert owner_client.get(f"/api/courses/{course_id}/template").status_code == 200
    assert owner_client.get(f"/api/courses/{course_id}/run").status_code == 200
    # Ownership check passes for the admin; 404 is the document genuinely
    # not existing yet (run_planner=False), not an authorization failure.
    assert owner_client.get(f"/api/documents/{document_id}").status_code == 404


def test_list_courses_only_shows_owned_courses_for_non_admin(owner_client):
    _register(owner_client, _email("list-a"))
    course_a = _create_course(owner_client, f"List A {uuid4().hex}")
    owner_client.post("/auth/logout")

    _register(owner_client, _email("list-b"))
    course_b = _create_course(owner_client, f"List B {uuid4().hex}")

    listed = owner_client.get("/api/courses").json()["courses"]
    ids = {c["course_id"] for c in listed}
    assert course_b["course_id"] in ids
    assert course_a["course_id"] not in ids


def test_admin_list_courses_sees_every_course(owner_client, monkeypatch):
    admin_email = _email("list-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(owner_client, admin_email)
    owner_client.post("/auth/logout")

    _register(owner_client, _email("list-owned"))
    course = _create_course(owner_client, f"Admin Sees All {uuid4().hex}")
    owner_client.post("/auth/logout")

    _login(owner_client, admin_email)
    listed = owner_client.get("/api/courses").json()["courses"]
    ids = {c["course_id"] for c in listed}
    assert course["course_id"] in ids


def test_nonexistent_course_is_404_not_403(owner_client):
    _register(owner_client, _email("missing-course"))

    response = owner_client.get(f"/api/courses/crs_{uuid4().hex[:12]}")

    assert response.status_code == 404
