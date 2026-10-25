"""Uploaded course templates through the API, against a real database:
upload (any role), visibility (private by default, shared on request),
delete, the processed blueprint, and failure handling.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_review_workflow.py.
"""

from __future__ import annotations

import io
import os
from uuid import uuid4

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def template_client(monkeypatch):
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


def _make_admin(client, monkeypatch):
    admin_email = _email("tpl-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()
    admin_user = _register(client, admin_email)
    return admin_user, admin_email


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = DocxDocument()
    doc.add_heading("Underwriting Essentials", level=1)
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.add_paragraph("Bullet one", style="List Bullet")
    doc.add_paragraph("Bullet two", style="List Bullet")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_authenticated_user_can_upload_markdown_template(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    template_client.post("/auth/logout")
    author_email = _email("tpl-author-upload")
    _register(template_client, author_email)

    content = b"# Sample Template\n\nSome **bold** guidance.\n"
    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Sample MD", "template_type": "technical", "description": "A sample"},
        files={"file": ("sample.md", content, "text/markdown")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Sample MD"
    assert body["template_type"] == "technical"
    assert body["source_format"] == "md"
    assert body["content_format"] == "markdown"
    assert "Sample Template" in body["markdown_content"]
    assert body["created_by"] == author_email


def test_upload_docx_converts_to_markdown(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)

    docx_bytes = _docx_bytes(["This is the underwriting overview paragraph."])
    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Underwriting Essentials", "template_type": "non_technical", "description": ""},
        files={
            "file": (
                "underwriting.docx",
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_format"] == "docx"
    assert body["content_format"] == "markdown"
    assert "Underwriting Essentials" in body["markdown_content"]
    assert "underwriting overview paragraph" in body["markdown_content"]
    # Bulleted list structure is preserved as Markdown list markers.
    assert "Bullet one" in body["markdown_content"]


def test_every_role_can_upload(template_client, monkeypatch):
    """Upload is intentionally open to any authenticated role, not just
    admin - only viewing/listing requires authentication, uploading does
    too, but no particular role."""
    admin_user, admin_email = _make_admin(template_client, monkeypatch)

    for role in ("author", "editor_reviewer", "manager"):
        email = _email(f"tpl-role-{role}")
        role_user = _register(template_client, email)
        template_client.post("/auth/logout")

        _login(template_client, admin_email)
        role_update = template_client.patch(
            f"/admin/users/{role_user['id']}/role", json={"role": role}
        )
        assert role_update.status_code == 200, role_update.text
        template_client.post("/auth/logout")

        _login(template_client, email)
        response = template_client.post(
            "/api/course-template-documents",
            data={"name": f"Role Upload {role}", "template_type": "technical", "description": ""},
            files={"file": ("x.md", b"# X", "text/markdown")},
        )
        assert response.status_code == 201, response.text
        template_client.post("/auth/logout")


def test_unsupported_file_type_is_rejected(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)

    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Bad", "template_type": "technical", "description": ""},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 422, response.text


def test_empty_markdown_is_rejected(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)

    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Empty", "template_type": "technical", "description": ""},
        files={"file": ("empty.md", b"   \n\n  ", "text/markdown")},
    )

    assert response.status_code == 422, response.text


def test_invalid_template_type_is_rejected(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)

    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Bad Type", "template_type": "somethingelse", "description": ""},
        files={"file": ("x.md", b"# X", "text/markdown")},
    )

    assert response.status_code == 422, response.text


def test_any_authenticated_user_can_list_and_view_a_shared_template(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    upload = template_client.post(
        "/api/course-template-documents",
        data={"name": "Listed", "template_type": "technical", "description": "d", "visibility": "shared"},
        files={"file": ("x.md", b"# Listed Template", "text/markdown")},
    )
    template_id = upload.json()["id"]
    template_client.post("/auth/logout")

    _register(template_client, _email("tpl-viewer"))

    listing = template_client.get("/api/course-template-documents")
    assert listing.status_code == 200, listing.text
    names = [item["name"] for item in listing.json()["templates"]]
    assert "Listed" in names

    detail = template_client.get(f"/api/course-template-documents/{template_id}")
    assert detail.status_code == 200, detail.text
    assert "Listed Template" in detail.json()["markdown_content"]


def test_unauthenticated_requests_are_rejected(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    template_client.post("/auth/logout")

    listing = template_client.get("/api/course-template-documents")
    upload = template_client.post(
        "/api/course-template-documents",
        data={"name": "X", "template_type": "technical", "description": ""},
        files={"file": ("x.md", b"# X", "text/markdown")},
    )

    assert listing.status_code == 401, listing.text
    assert upload.status_code == 401, upload.text


def test_classify_type_returns_a_valid_template_type(template_client, monkeypatch):
    """Backs the hardcoded "Default Template" - available to any authenticated
    user, not just admin. The test suite runs against the offline mock AI
    client (see conftest.py), so the exact classification is deterministic
    rather than a real judgment call - this only asserts the contract."""
    _make_admin(template_client, monkeypatch)
    template_client.post("/auth/logout")
    _register(template_client, _email("tpl-classify"))

    response = template_client.post(
        "/api/course-template-documents/classify-type",
        json={"course_title": "Intro to Kubernetes", "target_audience": "Backend engineers"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_type"] in ("technical", "non_technical")


def test_classify_type_requires_a_course_title(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)

    response = template_client.post(
        "/api/course-template-documents/classify-type",
        json={"course_title": "", "target_audience": ""},
    )

    assert response.status_code == 422, response.text


def test_classify_type_requires_auth(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    template_client.post("/auth/logout")

    response = template_client.post(
        "/api/course-template-documents/classify-type",
        json={"course_title": "Intro to Kubernetes", "target_audience": ""},
    )

    assert response.status_code == 401, response.text


# --- visibility, delete, blueprint, failure handling -------------------------

_MD_BLUEPRINT = b"""# Support Desk Blueprint

## Writing rules
- Tone: calm and practical
- Every chapter must end with a summary of key takeaways.

## How It Works (required)
Explain the process step by step.
Add a flowchart labelled Request, Triage and Resolve after the explanation, with a caption.
"""


def _upload_md(client, name="Blueprint", visibility="private", content=_MD_BLUEPRINT):
    return client.post(
        "/api/course-template-documents",
        data={"name": name, "template_type": "non_technical", "description": "", "visibility": visibility},
        files={"file": ("blueprint.md", content, "text/markdown")},
    )


def test_upload_returns_the_processed_blueprint(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    response = _upload_md(template_client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["usable"] is True
    assert body["template_id"] == f"uploaded:{body['id']}"
    assert [section["key"] for section in body["sections"]] == ["how_it_works"]
    blueprint = body["blueprint"]
    assert blueprint["source_format"] == "md"
    assert blueprint["text"]["tone"] == "calm and practical"
    flowchart = blueprint["visuals"][0]
    assert flowchart["section_key"] == "how_it_works"
    assert flowchart["labels"] == ["Request", "Triage", "Resolve"]
    assert flowchart["required"] is True


def test_a_private_template_is_hidden_from_other_users_until_shared(template_client, monkeypatch):
    _make_admin(template_client, monkeypatch)
    template_client.post("/auth/logout")
    owner_email = _email("tpl-owner")
    _register(template_client, owner_email)
    template_id = _upload_md(template_client, name="Owner Private").json()["id"]
    template_client.post("/auth/logout")

    _register(template_client, _email("tpl-other"))
    names = [item["name"] for item in template_client.get("/api/course-template-documents").json()["templates"]]
    assert "Owner Private" not in names
    assert template_client.get(f"/api/course-template-documents/{template_id}").status_code == 404
    # Someone else cannot share or delete it either.
    assert template_client.patch(
        f"/api/course-template-documents/{template_id}", json={"visibility": "shared"}
    ).status_code == 404
    template_client.post("/auth/logout")

    _login(template_client, owner_email)
    shared = template_client.patch(f"/api/course-template-documents/{template_id}", json={"visibility": "shared"})
    assert shared.status_code == 200, shared.text
    assert shared.json()["visibility"] == "shared"
    template_client.post("/auth/logout")

    _register(template_client, _email("tpl-other-2"))
    listing = template_client.get("/api/course-template-documents").json()["templates"]
    item = next(entry for entry in listing if entry["name"] == "Owner Private")
    assert item["can_manage"] is False
    assert template_client.delete(f"/api/course-template-documents/{template_id}").status_code == 403


def test_a_deleted_template_cannot_start_a_course_but_still_resolves(template_client, monkeypatch):
    from app.course.templates.registry import load_template

    _make_admin(template_client, monkeypatch)
    body = _upload_md(template_client, name="To Delete").json()
    template_id = body["template_id"]
    assert template_client.delete(f"/api/course-template-documents/{body['id']}").status_code == 204

    names = [item["name"] for item in template_client.get("/api/course-template-documents").json()["templates"]]
    assert "To Delete" not in names
    created = template_client.post(
        "/api/courses",
        json={
            "course_title": "Anything",
            "toc": [{"title": "One"}],
            "target_audience": "Anyone",
            "template": "non_technical",
            "template_id_override": template_id,
            "run_planner": False,
        },
    )
    assert created.status_code == 422, created.text
    assert "deleted" in created.text
    # A course made before the delete still renders with its template.
    assert load_template(template_id).blueprint is not None


def test_a_corrupted_docx_is_rejected_and_leaves_no_files(template_client, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reset_settings_cache()
    _make_admin(template_client, monkeypatch)
    response = template_client.post(
        "/api/course-template-documents",
        data={"name": "Broken", "template_type": "technical"},
        files={"file": ("broken.docx", b"PK\x03\x04 this is not really a zip", "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert "corrupted" in response.text or "not a valid .docx" in response.text
    assert not (tmp_path / "templates").exists() or not any((tmp_path / "templates").iterdir())


def test_a_failed_database_insert_removes_the_stored_files(template_client, monkeypatch, tmp_path):
    from app.db.repositories.course_template_documents import CourseTemplateDocumentRepository

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reset_settings_cache()
    _make_admin(template_client, monkeypatch)

    async def explode(self, document):
        raise RuntimeError("database went away")

    monkeypatch.setattr(CourseTemplateDocumentRepository, "create", explode)
    with pytest.raises(RuntimeError):
        _upload_md(template_client, name="Will Fail")
    templates_dir = tmp_path / "templates"
    assert not templates_dir.exists() or not any(templates_dir.iterdir())
