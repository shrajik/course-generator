"""Course activity log: a lightweight, append-only audit trail recording who
did what to a course and when (created, updated/saved, submitted for review,
changes requested, approved, exported).

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as the
other workflow test files, since the log only exists once Postgres-backed
user identity does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    os.environ["DATABASE_URL"] = DATABASE_URL or ""
    os.environ["USE_DATABASE"] = "true"
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0


@pytest.fixture
def activity_client(monkeypatch):
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


def _create_course(client, title: str = "Activity Log Test Course"):
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


def _document_payload(course: dict, heading_text: str = "Chapter 1") -> dict:
    return {
        "document_id": course["document_id"],
        "course_id": course["course_id"],
        "course_title": course["input"]["course_title"],
        "template_id": course["template_id"],
        "version": 1,
        "meta": {},
        "pages": [
            {
                "id": "page_1",
                "page_number": 1,
                "kind": "content",
                "blocks": [{"type": "heading", "content": {"text": heading_text, "level": 2}}],
            }
        ],
    }


def _actions(activity_response) -> list[str]:
    return [entry["action"] for entry in activity_response.json()["activities"]]


def _make_admin_and_reviewer(client, monkeypatch):
    admin_email = _email("act-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(client, admin_email)
    client.post("/auth/logout")

    reviewer_email = _email("act-reviewer")
    reviewer_user = _register(client, reviewer_email)
    client.post("/auth/logout")

    _login(client, admin_email)
    role_update = client.patch(
        f"/admin/users/{reviewer_user['id']}/role", json={"role": "editor_reviewer"}
    )
    assert role_update.status_code == 200, role_update.text
    client.post("/auth/logout")

    return admin_email, reviewer_email


def test_activity_is_created_after_course_creation(activity_client):
    user = _register(activity_client, _email("act-create"))
    course = _create_course(activity_client)

    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")

    assert response.status_code == 200, response.text
    activities = response.json()["activities"]
    assert len(activities) == 1
    assert activities[0]["action"] == "created"
    assert activities[0]["user_id"] == user["id"]
    assert activities[0]["user_email"] == user["email"]


def test_activity_is_created_after_save(activity_client):
    _register(activity_client, _email("act-save"))
    course = _create_course(activity_client)

    save = activity_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, "Edited heading"),
    )
    assert save.status_code == 200, save.text

    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")
    assert response.status_code == 200, response.text
    # Newest first.
    assert _actions(response) == ["updated", "created"]


def test_activity_is_created_after_export(activity_client):
    _register(activity_client, _email("act-export"))
    course = _create_course(activity_client)
    activity_client.put(
        f"/api/documents/{course['document_id']}", json=_document_payload(course)
    )

    export = activity_client.post(f"/api/documents/{course['document_id']}/export/pdf")
    assert export.status_code == 200, export.text

    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")
    assert _actions(response) == ["exported", "updated", "created"]


def test_activity_records_submit_request_changes_and_approve(activity_client, monkeypatch):
    admin_email, reviewer_email = _make_admin_and_reviewer(activity_client, monkeypatch)

    author_email = _email("act-author-workflow")
    _register(activity_client, author_email)
    course = _create_course(activity_client)

    submit = activity_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submit.status_code == 200, submit.text
    activity_client.post("/auth/logout")

    _login(activity_client, reviewer_email)
    changes = activity_client.post(
        f"/api/courses/{course['course_id']}/request-changes",
        json={"comment": "Needs more detail."},
    )
    assert changes.status_code == 200, changes.text
    activity_client.post("/auth/logout")

    _login(activity_client, author_email)
    resubmit = activity_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert resubmit.status_code == 200, resubmit.text
    activity_client.post("/auth/logout")

    _login(activity_client, reviewer_email)
    approve = activity_client.post(f"/api/courses/{course['course_id']}/approve")
    assert approve.status_code == 200, approve.text
    activity_client.post("/auth/logout")

    # /activity is owner-or-admin only (stricter than viewing the course
    # itself) - log back in as the author to read it.
    _login(activity_client, author_email)
    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")
    assert response.status_code == 200, response.text
    assert _actions(response) == [
        "approved",
        "submitted_for_review",
        "changes_requested",
        "submitted_for_review",
        "created",
    ]
    entries = response.json()["activities"]
    changes_entry = next(e for e in entries if e["action"] == "changes_requested")
    assert changes_entry["message"] == "Needs more detail."
    assert changes_entry["user_email"] == reviewer_email


def test_unauthorized_user_cannot_view_another_users_activity(activity_client):
    _register(activity_client, _email("act-victim"))
    course = _create_course(activity_client, "Victim Activity Course")
    activity_client.post("/auth/logout")

    _register(activity_client, _email("act-attacker"))
    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")

    assert response.status_code == 403, response.text


def test_admin_can_view_any_courses_activity(activity_client, monkeypatch):
    admin_email = _email("act-admin-view")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(activity_client, admin_email)
    activity_client.post("/auth/logout")

    _register(activity_client, _email("act-owner-for-admin"))
    course = _create_course(activity_client, "Admin Can See This")
    activity_client.post("/auth/logout")

    _login(activity_client, admin_email)
    response = activity_client.get(f"/api/courses/{course['course_id']}/activity")

    assert response.status_code == 200, response.text
    assert _actions(response) == ["created"]
