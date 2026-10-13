"""AI model usage transparency (MOM Phase 2.2/2.3): a real generation run's
per-call model usage (see CallRecord) must be persisted and readable by an
admin, and refused to everyone else - enforced on the backend, not just
hidden in the frontend.

Same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_course_ownership.py - model usage is persisted via GenerationRun,
which only exists once Postgres-backed persistence does. Runs entirely
against the offline MockAIClient (MOCK_OPENAI=true, set globally in
conftest.py) - no live API calls, but a real end-to-end generation run
through the real pipeline, same as the ownership tests' own `/generate`
calls.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def usage_client(monkeypatch):
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


def _create_course(client, title: str = "Model Usage Test Course"):
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


def test_a_real_generation_run_persists_model_usage_and_admin_can_read_it(usage_client, monkeypatch):
    admin_email = _email("usage-admin")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    _register(usage_client, _email("usage-owner"))
    course = _create_course(usage_client)
    course_id = course["course_id"]

    generated = usage_client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert generated.status_code == 200, generated.text

    usage_client.post("/auth/logout")
    _register(usage_client, admin_email)  # role=admin via INITIAL_ADMIN_EMAIL

    response = usage_client.get(f"/admin/courses/{course_id}/model-usage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["course_id"] == course_id
    assert body["job_id"]
    assert body["run_state"] in ("done", "ready", "failed")
    # The offline mock client (see MockAIClient._simulate) still records one
    # CallRecord per AI call it stood in for - planning, research, writing,
    # reviewing - so a real (mocked) run must produce a non-empty list.
    assert len(body["calls"]) > 0
    for call in body["calls"]:
        assert call["model"]
        assert call["kind"] in ("structured", "research", "image", "embedding")
        # The mock client is honestly labelled, never disguised as a real
        # provider/model (see MockAIClient._simulate's provider="mock").
        assert call["provider"] == "mock"
        assert call["model"] == "mock"


def test_non_admin_owner_cannot_read_model_usage(usage_client):
    """Backend-enforced, not just hidden in the frontend - the course's own
    owner (who can see everything else about their course) still gets 403
    here, per MOM Phase 2.3's explicit requirement."""
    _register(usage_client, _email("usage-nonadmin-owner"))
    course = _create_course(usage_client)
    course_id = course["course_id"]

    response = usage_client.get(f"/admin/courses/{course_id}/model-usage")
    assert response.status_code == 403, response.text


def test_model_usage_for_a_course_with_no_generation_run_yet_is_empty_not_fabricated(usage_client, monkeypatch):
    admin_email = _email("usage-admin-empty")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    # A non-admin creates the course (so it exists) but never generates.
    _register(usage_client, _email("usage-owner-empty"))
    course = _create_course(usage_client, "Never Generated Course")
    course_id = course["course_id"]
    usage_client.post("/auth/logout")

    _register(usage_client, admin_email)
    response = usage_client.get(f"/admin/courses/{course_id}/model-usage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["course_id"] == course_id
    assert body["job_id"] is None
    assert body["calls"] == []


def test_model_usage_for_a_nonexistent_course_is_404(usage_client, monkeypatch):
    admin_email = _email("usage-admin-404")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()
    _register(usage_client, admin_email)

    response = usage_client.get("/admin/courses/crs_does_not_exist/model-usage")
    assert response.status_code == 404, response.text
