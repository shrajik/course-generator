"""Authorization/IDOR audit: every course- and document-scoped endpoint must
reject a user who is neither the resource's owner, an admin, nor (for the
narrow set of actions that allow it) an editor/reviewer acting on a
genuinely-submitted course. Changing courseId/documentId in the URL must
never grant access to someone else's resource.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as the
other workflow test files.
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
def audit_client(monkeypatch):
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


def _create_course(client, title: str = "Audit Test Course"):
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


def _promote_to_role(client, monkeypatch, role: str) -> tuple[str, str]:
    """Registers an admin (via INITIAL_ADMIN_EMAIL) and a second user promoted
    to `role`. Returns (admin_email, promoted_user_email)."""
    admin_email = _email("audit-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(client, admin_email)
    client.post("/auth/logout")

    target_email = _email(f"audit-{role}")
    target_user = _register(client, target_email)
    client.post("/auth/logout")

    _login(client, admin_email)
    role_update = client.patch(f"/admin/users/{target_user['id']}/role", json={"role": role})
    assert role_update.status_code == 200, role_update.text
    client.post("/auth/logout")

    return admin_email, target_email


# --- the fix: generate is an author-only action, not a reviewer edit ---------


def test_generate_rejects_a_reviewer_who_is_not_the_owner(audit_client, monkeypatch):
    _, reviewer_email = _promote_to_role(audit_client, monkeypatch, "editor_reviewer")

    _register(audit_client, _email("audit-generate-owner"))
    course = _create_course(audit_client)
    submit = audit_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submit.status_code == 200, submit.text
    audit_client.post("/auth/logout")

    # The reviewer CAN view/approve this course (it's in_review) but must NOT
    # be able to trigger regeneration of its content.
    _login(audit_client, reviewer_email)
    view = audit_client.get(f"/api/courses/{course['course_id']}")
    generate = audit_client.post(
        f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"}
    )

    assert view.status_code == 200, view.text
    assert generate.status_code == 403, generate.text


def test_owner_can_still_trigger_generate_on_their_own_course(audit_client):
    _register(audit_client, _email("audit-generate-self"))
    course = _create_course(audit_client)

    response = audit_client.post(
        f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"}
    )

    assert response.status_code != 403
    assert response.status_code != 401


def test_admin_can_still_trigger_generate_on_any_course(audit_client, monkeypatch):
    admin_email = _email("audit-generate-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(audit_client, admin_email)
    audit_client.post("/auth/logout")

    _register(audit_client, _email("audit-generate-other-owner"))
    course = _create_course(audit_client)
    audit_client.post("/auth/logout")

    _login(audit_client, admin_email)
    response = audit_client.post(
        f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"}
    )

    assert response.status_code != 403
    assert response.status_code != 401


# --- IDOR sweep: every protected endpoint, cross-user -------------------------


def test_idor_sweep_rejects_cross_user_access_on_every_protected_endpoint(audit_client):
    """User A's course/document must be completely inaccessible to User B via
    every endpoint that takes a courseId or documentId, even though B is a
    fully authenticated, legitimate user (just not this resource's owner)."""
    _register(audit_client, _email("audit-victim"))
    course = _create_course(audit_client, "IDOR Victim Course")
    course_id = course["course_id"]
    document_id = course["document_id"]
    audit_client.post("/auth/logout")

    _register(audit_client, _email("audit-attacker"))

    # Course-scoped reads/writes.
    assert audit_client.get(f"/api/courses/{course_id}").status_code == 403
    assert audit_client.get(f"/api/courses/{course_id}/blueprint").status_code == 403
    assert audit_client.post(f"/api/courses/{course_id}/generate").status_code == 403
    assert audit_client.get(f"/api/courses/{course_id}/run").status_code == 403
    assert audit_client.get(f"/api/courses/{course_id}/document").status_code == 403
    assert audit_client.get(f"/api/courses/{course_id}/chapters/chapter_1").status_code == 403
    assert (
        audit_client.get(f"/api/courses/{course_id}/chapters/chapter_1/research").status_code
        == 403
    )
    assert audit_client.get(f"/api/courses/{course_id}/template").status_code == 403
    assert audit_client.get(f"/api/courses/{course_id}/review").status_code == 403
    assert (
        audit_client.post(f"/api/courses/{course_id}/submit-for-review").status_code == 403
    )
    assert audit_client.get(f"/api/courses/{course_id}/activity").status_code == 403

    # Document-scoped reads/writes, addressed via documentId instead.
    assert audit_client.get(f"/api/documents/{document_id}").status_code == 403
    assert (
        audit_client.put(
            f"/api/documents/{document_id}", json=_document_payload(course, "Hijacked")
        ).status_code
        == 403
    )
    assert (
        audit_client.post(
            f"/api/documents/{document_id}/ai-edit",
            json={"selected_block_ids": ["block_1"], "instruction": "hijack"},
        ).status_code
        == 403
    )
    assert audit_client.post(f"/api/documents/{document_id}/export/pdf").status_code == 403
    assert audit_client.get(f"/api/documents/{document_id}/preview").status_code == 403
    assert (
        audit_client.get(f"/api/documents/{document_id}/assets/whatever.png").status_code == 403
    )


def test_idor_sweep_reviewer_cannot_reach_a_still_draft_course(audit_client, monkeypatch):
    """A never-submitted draft is not yet the reviewer's to see - the broader
    editor/reviewer access only kicks in once a course is submitted."""
    _, reviewer_email = _promote_to_role(audit_client, monkeypatch, "editor_reviewer")

    _register(audit_client, _email("audit-draft-owner"))
    course = _create_course(audit_client, "Still A Draft")
    document_id = course["document_id"]
    audit_client.post("/auth/logout")

    _login(audit_client, reviewer_email)
    assert audit_client.get(f"/api/courses/{course['course_id']}").status_code == 403
    assert audit_client.get(f"/api/documents/{document_id}").status_code == 403


def test_manager_role_has_no_special_access_to_another_users_course(audit_client, monkeypatch):
    """Manager isn't granted the reviewer edit/view carve-out and doesn't own
    this course, so they get nothing - confirms no accidental over-grant."""
    _, manager_email = _promote_to_role(audit_client, monkeypatch, "manager")

    _register(audit_client, _email("audit-manager-owner"))
    course = _create_course(audit_client, "Manager Should Not See This")
    submit = audit_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submit.status_code == 200, submit.text
    audit_client.post("/auth/logout")

    _login(audit_client, manager_email)
    assert audit_client.get(f"/api/courses/{course['course_id']}").status_code == 403
    assert audit_client.get(f"/api/courses/{course['course_id']}/review").status_code == 403
    assert (
        audit_client.post(f"/api/courses/{course['course_id']}/approve").status_code == 403
    )


def test_reviewer_retains_view_and_edit_access_to_a_submitted_course(audit_client, monkeypatch):
    """Regression guard for the fix above: tightening `generate` must not
    also break the legitimate view/edit access reviewers already have."""
    _, reviewer_email = _promote_to_role(audit_client, monkeypatch, "editor_reviewer")

    _register(audit_client, _email("audit-reviewer-owner"))
    course = _create_course(audit_client, "Reviewer Can Still Edit This")
    submit = audit_client.post(f"/api/courses/{course['course_id']}/submit-for-review")
    assert submit.status_code == 200, submit.text
    audit_client.post("/auth/logout")

    _login(audit_client, reviewer_email)
    assert audit_client.get(f"/api/courses/{course['course_id']}").status_code == 200
    assert audit_client.get(f"/api/documents/{course['document_id']}").status_code == 404
    save = audit_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, "Reviewer edit"),
    )
    assert save.status_code == 200, save.text
    approve = audit_client.post(f"/api/courses/{course['course_id']}/approve")
    assert approve.status_code == 200, approve.text


def test_unauthenticated_request_is_rejected_not_silently_scoped(audit_client):
    """Every protected endpoint requires a real session; none of them should
    fall back to open/offline behaviour once Postgres-backed auth is active."""
    _register(audit_client, _email("audit-noauth-owner"))
    course = _create_course(audit_client, "No Session Should See This")
    audit_client.post("/auth/logout")

    response = audit_client.get(f"/api/courses/{course['course_id']}")

    assert response.status_code == 401
