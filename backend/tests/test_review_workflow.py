"""Course review/approval workflow:

    draft -> in_review -> approved
                        -> changes_requested -> in_review (resubmit) -> ...

Authors submit their own courses; editor/reviewers approve or request
changes (never their own courses, since roles are mutually exclusive here);
admin retains full access throughout.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_course_ownership.py and test_document_save.py, since the workflow only
exists once Postgres-backed user identity/roles do.
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
def review_client(monkeypatch):
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


def _create_course(client, title: str = "Review Workflow Test Course"):
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


def _make_admin_and_reviewer(client, monkeypatch):
    """Registers an admin (via INITIAL_ADMIN_EMAIL) and a second user promoted
    to editor_reviewer. Returns (admin_user, reviewer_user, reviewer_email)."""
    admin_email = _email("wf-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(client, admin_email)
    client.post("/auth/logout")

    reviewer_email = _email("wf-reviewer")
    reviewer_user = _register(client, reviewer_email)
    client.post("/auth/logout")

    _login(client, admin_email)
    role_update = client.patch(
        f"/admin/users/{reviewer_user['id']}/role", json={"role": "editor_reviewer"}
    )
    assert role_update.status_code == 200, role_update.text
    client.post("/auth/logout")

    return admin_user, reviewer_user, reviewer_email, admin_email


def test_author_can_submit_own_course_for_review(review_client):
    _register(review_client, _email("wf-author-submit"))
    course = _create_course(review_client)

    response = review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "in_review"
    assert body["history"][-1]["action"] == "submitted"


def test_author_cannot_approve_or_reject(review_client):
    _register(review_client, _email("wf-author-selfapprove"))
    course = _create_course(review_client)
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")

    approve = review_client.post(f"/api/courses/{course['course_id']}/approve")
    reject = review_client.post(
        f"/api/courses/{course['course_id']}/request-changes",
        json={"comment": "please fix"},
    )

    assert approve.status_code == 403, approve.text
    assert reject.status_code == 403, reject.text


def test_reviewer_can_view_and_approve_submitted_course(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    _register(review_client, _email("wf-author-approved"))
    course = _create_course(review_client, "Course To Approve")
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    view = review_client.get(f"/api/courses/{course['course_id']}/review")
    assert view.status_code == 200, view.text
    assert view.json()["review_status"] == "in_review"

    # Reviewer can also open the full course/document views, not just status.
    full_view = review_client.get(f"/api/courses/{course['course_id']}")
    assert full_view.status_code == 200, full_view.text

    approve = review_client.post(
        f"/api/courses/{course['course_id']}/approve", json={"comment": "Looks great"}
    )
    assert approve.status_code == 200, approve.text
    body = approve.json()
    assert body["review_status"] == "approved"
    assert body["review_comment"] == "Looks great"
    assert body["reviewed_at"] is not None
    assert body["history"][-1]["action"] == "approved"


def test_reviewer_can_request_changes_with_reason(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    _register(review_client, _email("wf-author-changes"))
    course = _create_course(review_client, "Course Needing Changes")
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    response = review_client.post(
        f"/api/courses/{course['course_id']}/request-changes",
        json={"comment": "The intro chapter needs more examples."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "changes_requested"
    assert body["review_comment"] == "The intro chapter needs more examples."


def test_request_changes_requires_a_reason(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    _register(review_client, _email("wf-author-empty-reason"))
    course = _create_course(review_client)
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    empty = review_client.post(
        f"/api/courses/{course['course_id']}/request-changes", json={"comment": ""}
    )
    missing = review_client.post(f"/api/courses/{course['course_id']}/request-changes", json={})

    assert empty.status_code == 422, empty.text
    assert missing.status_code == 422, missing.text


def test_author_sees_feedback_and_can_resubmit(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    author_email = _email("wf-author-resubmit")
    _register(review_client, author_email)
    course = _create_course(review_client, "Course To Resubmit")
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    review_client.post(
        f"/api/courses/{course['course_id']}/request-changes",
        json={"comment": "Fix the typo in chapter 1."},
    )
    review_client.post("/auth/logout")

    _login(review_client, author_email)
    feedback = review_client.get(f"/api/courses/{course['course_id']}/review")
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["review_status"] == "changes_requested"
    assert feedback.json()["review_comment"] == "Fix the typo in chapter 1."

    resubmit = review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert resubmit.status_code == 200, resubmit.text
    body = resubmit.json()
    assert body["review_status"] == "in_review"
    # The stale "changes requested" note is cleared on resubmission ...
    assert body["review_comment"] is None
    # ... but the audit trail keeps every step.
    actions = [entry["action"] for entry in body["history"]]
    assert actions == ["submitted", "changes_requested", "submitted"]


def test_unauthorized_user_cannot_manipulate_another_users_workflow(review_client):
    _register(review_client, _email("wf-victim"))
    course = _create_course(review_client, "Victim Workflow Course")
    review_client.post("/auth/logout")

    _register(review_client, _email("wf-attacker"))
    submit = review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    status = review_client.get(f"/api/courses/{course['course_id']}/review")

    assert submit.status_code == 403, submit.text
    assert status.status_code == 403, status.text


def test_reviewer_cannot_view_or_act_on_a_draft_course(review_client, monkeypatch):
    """Requirement: a reviewer can't access courses not authorized for review
    - an unsubmitted draft isn't theirs to see yet."""
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    _register(review_client, _email("wf-author-draft"))
    course = _create_course(review_client, "Still A Draft")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    view = review_client.get(f"/api/courses/{course['course_id']}/review")
    approve = review_client.post(f"/api/courses/{course['course_id']}/approve")

    assert view.status_code == 403, view.text
    # Approve is role-gated (passes the role check) but blocked by the state
    # machine - the course was never submitted.
    assert approve.status_code == 409, approve.text


def test_cannot_submit_an_already_approved_course_again(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    author_email = _email("wf-author-final")
    _register(review_client, author_email)
    course = _create_course(review_client, "Finalized Course")
    review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    review_client.post("/auth/logout")

    _login(review_client, reviewer_email)
    review_client.post(f"/api/courses/{course['course_id']}/approve")
    review_client.post("/auth/logout")

    _login(review_client, author_email)
    resubmit = review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")

    assert resubmit.status_code == 409, resubmit.text


def test_reviewer_cannot_approve_a_course_not_ready_for_review(review_client, monkeypatch):
    _, _, reviewer_email, _ = _make_admin_and_reviewer(review_client, monkeypatch)

    author_email = _email("wf-author-notready")
    _register(review_client, author_email)
    course = _create_course(review_client, "Not Submitted Yet")
    review_client.post("/auth/logout")

    # Promote this author to reviewer-adjacent access isn't needed - just log
    # in as the actual reviewer and try to approve a never-submitted course.
    _login(review_client, reviewer_email)
    approve = review_client.post(f"/api/courses/{course['course_id']}/approve")

    assert approve.status_code == 409, approve.text


def test_admin_retains_full_access_throughout_the_workflow(review_client, monkeypatch):
    admin_email = _email("wf-admin-access")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    admin_user = _register(review_client, admin_email)
    review_client.post("/auth/logout")

    _register(review_client, _email("wf-author-adminflow"))
    course = _create_course(review_client, "Admin Full Access Course")
    review_client.post("/auth/logout")

    _login(review_client, admin_email)

    # Admin can submit on behalf of the author, view, approve - all of it.
    submit = review_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submit.status_code == 200, submit.text

    view = review_client.get(f"/api/courses/{course['course_id']}/review")
    assert view.status_code == 200, view.text

    approve = review_client.post(f"/api/courses/{course['course_id']}/approve")
    assert approve.status_code == 200, approve.text
    assert approve.json()["reviewer_id"] == admin_user["id"]
