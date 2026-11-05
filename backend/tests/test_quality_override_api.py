"""MOM quality-governance follow-up - the four admin-only override-approve
HTTP endpoints: role enforcement, reason validation, and one full
success path per category.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_review_workflow.py, since role-based authorization only exists once
Postgres-backed user identity/roles do. The quality-gate LOGIC itself
(which category blocks, which override lifts which gate) has its own
offline, DB-free coverage in test_quality_overrides.py - this file is only
about the HTTP/role layer on top of it.
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

OVERRIDE_PATHS = [
    "approve/override/blockers",
    "approve/override/verification-failed",
    "approve/override/evaluation-failure",
    "approve/override/incomplete-chapters",
]


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def override_client(monkeypatch):
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


def _create_course_with_incomplete_inventory(client, title: str):
    """A course whose blueprint plans chapters that were never generated -
    `record.chapters` is non-empty (from planning) but nothing exists on
    disk yet, so every expected chapter reads as "missing". The simplest
    reliable fixture for the incomplete-chapters override, with no need to
    exercise the mock generation pipeline through the HTTP layer."""
    payload = {
        "course_title": title,
        "toc": [{"title": "Foundations"}, {"title": "Advanced Topics"}],
        "target_audience": "Backend engineers",
        "dos": [],
        "donts": [],
        "template": "technical",
        "run_planner": True,
    }
    response = client.post("/api/courses", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_and_submit_complete_course(client, title: str):
    """A course that completes generation fully (every blueprint-planned
    chapter present and loadable) and then submits for review successfully
    - the correct starting point for tests about a chapter being lost or
    corrupted AFTER submission (approve-time re-verification), as opposed
    to `_create_course_with_incomplete_inventory` above, which is for
    testing submission-time rejection itself (see
    test_submit_for_review_rejects_an_incomplete_inventory_through_the_api).
    Asserting success at each step here is what the two corrected tests
    below were missing - they previously mutated a chapter file on a course
    that had never actually reached `in_review`, since its inventory was
    already incomplete before submission was ever attempted."""
    payload = {
        "course_title": title,
        "toc": [{"title": "Foundations"}, {"title": "Advanced Topics"}],
        "target_audience": "Backend engineers",
        "dos": [],
        "donts": [],
        "template": "technical",
        "run_planner": True,
    }
    created = client.post("/api/courses", json=payload)
    assert created.status_code == 201, created.text
    course = created.json()

    generated = client.post(
        f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"}
    )
    assert generated.status_code == 200, generated.text
    assert generated.json()["status"] == "ready", generated.text
    assert not generated.json()["chapters_failed"], generated.text

    submitted = client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["review_status"] == "in_review", submitted.text

    return course


def _make_admin_and_reviewer(client, monkeypatch):
    admin_email = _email("ovr-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(client, admin_email)
    client.post("/auth/logout")

    reviewer_email = _email("ovr-reviewer")
    reviewer_user = _register(client, reviewer_email)
    client.post("/auth/logout")

    _login(client, admin_email)
    role_update = client.patch(
        f"/admin/users/{reviewer_user['id']}/role", json={"role": "editor_reviewer"}
    )
    assert role_update.status_code == 200, role_update.text
    client.post("/auth/logout")

    return admin_email, reviewer_email


@pytest.mark.parametrize("path", OVERRIDE_PATHS)
def test_an_author_cannot_call_any_override_endpoint(override_client, path):
    _register(override_client, _email("ovr-author-self"))
    course = _create_course_with_incomplete_inventory(override_client, "Author Self Override")
    override_client.post(f"/api/courses/{course['course_id']}/submit-for-review")

    response = override_client.post(
        f"/api/courses/{course['course_id']}/{path}", json={"comment": "trying anyway"}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path", OVERRIDE_PATHS)
def test_a_plain_editor_reviewer_cannot_call_any_override_endpoint(
    override_client, monkeypatch, path
):
    """Approve/request-changes are reviewer-accessible; the four overrides
    are a STRICTER, admin-only tier on top of that - a reviewer alone must
    not be able to use them."""
    _, reviewer_email = _make_admin_and_reviewer(override_client, monkeypatch)

    _register(override_client, _email("ovr-author-for-reviewer"))
    course = _create_course_with_incomplete_inventory(override_client, "Reviewer Cannot Override")
    override_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    override_client.post("/auth/logout")

    _login(override_client, reviewer_email)
    response = override_client.post(
        f"/api/courses/{course['course_id']}/{path}", json={"comment": "trying as reviewer"}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path", OVERRIDE_PATHS)
def test_an_admin_gets_422_for_an_empty_override_reason(override_client, monkeypatch, path):
    admin_email, _ = _make_admin_and_reviewer(override_client, monkeypatch)

    _register(override_client, _email("ovr-author-empty-reason"))
    course = _create_course_with_incomplete_inventory(override_client, "Empty Reason Override")
    override_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    override_client.post("/auth/logout")

    _login(override_client, admin_email)
    empty = override_client.post(
        f"/api/courses/{course['course_id']}/{path}", json={"comment": ""}
    )
    missing = override_client.post(f"/api/courses/{course['course_id']}/{path}", json={})

    assert empty.status_code == 422, empty.text
    assert missing.status_code == 422, missing.text


def test_ordinary_approve_endpoint_rejects_an_incomplete_inventory(
    override_client, monkeypatch, storage
):
    """The plain /approve endpoint has no override parameter at all - it
    must still enforce the inventory gate normally, distinctly from the
    override endpoints below. The course is fully generated and submitted
    successfully FIRST (asserted), then a chapter is lost afterward - the
    realistic post-submission-corruption scenario approve_course's own
    independent re-check is meant to catch."""
    admin_email, _ = _make_admin_and_reviewer(override_client, monkeypatch)

    _register(override_client, _email("ovr-author-plain-approve"))
    course = _create_and_submit_complete_course(override_client, "Plain Approve Blocked")

    detail = override_client.get(f"/api/courses/{course['course_id']}")
    assert detail.status_code == 200, detail.text
    target = detail.json()["course"]["chapters"][-1]["chapter_id"]
    storage.chapter_path(course["course_id"], target).unlink()
    override_client.post("/auth/logout")

    _login(override_client, admin_email)
    response = override_client.post(f"/api/courses/{course['course_id']}/approve")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "incomplete_chapter_inventory", response.text


def test_admin_can_override_approve_an_incomplete_chapter_inventory(
    override_client, monkeypatch, storage
):
    admin_email, _ = _make_admin_and_reviewer(override_client, monkeypatch)

    _register(override_client, _email("ovr-author-success"))
    course = _create_and_submit_complete_course(override_client, "Successful Override")

    detail = override_client.get(f"/api/courses/{course['course_id']}")
    assert detail.status_code == 200, detail.text
    target = detail.json()["course"]["chapters"][-1]["chapter_id"]
    storage.chapter_path(course["course_id"], target).unlink()
    override_client.post("/auth/logout")

    _login(override_client, admin_email)
    response = override_client.post(
        f"/api/courses/{course['course_id']}/approve/override/incomplete-chapters",
        json={"comment": "Approving with known-missing chapters; tracked separately."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "approved"
    assert body["history"][-1]["action"] == "override_approved_incomplete_chapters"
    affected = body["history"][-1]["override_evidence"]["affected_chapters"]
    assert affected
    assert affected[0]["chapter_id"] == target
    assert affected[0]["status"] == "missing"


def test_submit_for_review_rejects_an_incomplete_inventory_through_the_api(override_client):
    """Decision B's submission-time gate, exercised through the real HTTP
    endpoint rather than the service directly (see test_chapter_inventory.py
    for the equivalent service-level coverage)."""
    _register(override_client, _email("ovr-author-submit-incomplete"))
    course = _create_course_with_incomplete_inventory(override_client, "Submit Incomplete")

    response = override_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "incomplete_chapter_inventory", response.text
