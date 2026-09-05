"""PUT /api/documents/{document_id}: persisting manual editor edits.

Only the course's owner (or an admin) may save; the document_id/course_id in
the URL are authoritative over anything the request body claims; invalid
document shapes are rejected; a successful save is retrievable afterwards.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_course_ownership.py, since ownership - and therefore who may save -
only exists once Postgres-backed user identity does.
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
def doc_client(monkeypatch):
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


def _create_course(client, title: str = "Editor Save Test Course"):
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


def _document_payload(course: dict, *, heading_text: str = "Chapter 1", pages: list | None = None) -> dict:
    """A minimal, schema-valid CourseDocument - what the editor would send."""
    return {
        "document_id": course["document_id"],
        "course_id": course["course_id"],
        "course_title": course["input"]["course_title"],
        "template_id": course["template_id"],
        "version": 1,
        "meta": {},
        "pages": pages
        if pages is not None
        else [
            {
                "id": "page_1",
                "page_number": 1,
                "kind": "content",
                "blocks": [
                    {
                        "type": "heading",
                        "content": {"text": heading_text, "level": 2},
                    }
                ],
            }
        ],
    }


def test_owner_can_save_their_document(doc_client):
    _register(doc_client, _email("save-owner"))
    course = _create_course(doc_client)

    response = doc_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, heading_text="Edited by owner"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == course["document_id"]
    assert body["course_id"] == course["course_id"]
    assert body["pages"][0]["blocks"][0]["content"]["text"] == "Edited by owner"
    assert body["version"] == 1  # first save of a document that didn't exist yet


def test_another_user_cannot_save_it(doc_client):
    _register(doc_client, _email("save-a"))
    course = _create_course(doc_client, "User A's Save Target")
    doc_client.post("/auth/logout")

    _register(doc_client, _email("save-b"))
    response = doc_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, heading_text="Hijacked"),
    )

    assert response.status_code == 403, response.text


def test_document_id_and_course_id_in_body_cannot_redirect_a_save(doc_client):
    """Even a legitimate owner can't point their save at someone else's
    document/course by editing those fields in the body - the URL wins."""
    _register(doc_client, _email("save-victim"))
    victim_course = _create_course(doc_client, "Victim Course")
    doc_client.post("/auth/logout")

    _register(doc_client, _email("save-attacker"))
    attacker_course = _create_course(doc_client, "Attacker Course")

    payload = _document_payload(victim_course, heading_text="Attempted hijack")
    response = doc_client.put(
        f"/api/documents/{attacker_course['document_id']}",
        json=payload,
    )

    # Saved under the attacker's own document/course (from the URL), not the
    # victim's (from the body) - and it succeeds because it's their own.
    assert response.status_code == 200, response.text
    assert response.json()["document_id"] == attacker_course["document_id"]
    assert response.json()["course_id"] == attacker_course["course_id"]


def test_admin_can_save_any_users_document(doc_client, monkeypatch):
    admin_email = _email("save-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(doc_client, _email("save-owner-2"))
    course = _create_course(doc_client, "Owned By Someone Else")
    doc_client.post("/auth/logout")

    _register(doc_client, admin_email)  # role=admin via INITIAL_ADMIN_EMAIL
    response = doc_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, heading_text="Edited by admin"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["pages"][0]["blocks"][0]["content"]["text"] == "Edited by admin"


def test_document_with_no_pages_is_rejected(doc_client):
    _register(doc_client, _email("save-empty"))
    course = _create_course(doc_client)

    response = doc_client.put(
        f"/api/documents/{course['document_id']}",
        json=_document_payload(course, pages=[]),
    )

    assert response.status_code == 422, response.text


def test_document_missing_required_field_is_rejected(doc_client):
    _register(doc_client, _email("save-malformed"))
    course = _create_course(doc_client)

    payload = _document_payload(course)
    del payload["template_id"]  # required by CourseDocument

    response = doc_client.put(f"/api/documents/{course['document_id']}", json=payload)

    assert response.status_code == 422, response.text


def test_block_with_invalid_content_is_rejected(doc_client):
    """A heading block's content requires `text`; omitting it must fail
    schema validation rather than silently saving a broken block."""
    _register(doc_client, _email("save-bad-block"))
    course = _create_course(doc_client)

    payload = _document_payload(
        course,
        pages=[
            {
                "id": "page_1",
                "page_number": 1,
                "kind": "content",
                "blocks": [{"type": "heading", "content": {}}],
            }
        ],
    )

    response = doc_client.put(f"/api/documents/{course['document_id']}", json=payload)

    assert response.status_code == 422, response.text


def test_saved_changes_can_be_retrieved_after_refresh(doc_client):
    _register(doc_client, _email("save-refresh"))
    course = _create_course(doc_client)
    document_id = course["document_id"]

    save_response = doc_client.put(
        f"/api/documents/{document_id}",
        json=_document_payload(course, heading_text="Persisted across reload"),
    )
    assert save_response.status_code == 200, save_response.text
    saved_version = save_response.json()["version"]

    reloaded = doc_client.get(f"/api/documents/{document_id}")
    assert reloaded.status_code == 200, reloaded.text
    body = reloaded.json()
    assert body["pages"][0]["blocks"][0]["content"]["text"] == "Persisted across reload"
    assert body["version"] == saved_version


def test_second_save_bumps_version_and_preserves_created_at(doc_client):
    _register(doc_client, _email("save-version"))
    course = _create_course(doc_client)
    document_id = course["document_id"]

    first = doc_client.put(
        f"/api/documents/{document_id}",
        json=_document_payload(course, heading_text="First edit"),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()

    second = doc_client.put(
        f"/api/documents/{document_id}",
        json=_document_payload(course, heading_text="Second edit"),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()

    assert second_body["version"] == first_body["version"] + 1
    assert second_body["created_at"] == first_body["created_at"]
    assert second_body["pages"][0]["blocks"][0]["content"]["text"] == "Second edit"
