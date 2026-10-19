"""Gate 1: no chapter is written until the outline has been approved."""

from __future__ import annotations

import pytest

from app.core.config import reset_settings_cache
from app.services import course_service


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("REQUIRE_OUTLINE_APPROVAL", "true")
    reset_settings_cache()
    course_service.reset_course_service()


def _create(client):
    response = client.post(
        "/api/courses",
        json={
            "course_title": "Intro to RAG",
            "toc": [{"title": "What is RAG"}, {"title": "Chunking"}],
            "target_audience": "Backend engineers",
            "template": "technical",
            "run_planner": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_new_course_starts_with_an_unapproved_outline(client, gated):
    assert _create(client)["outline_approved"] is False


def test_generation_is_refused_until_the_outline_is_approved(client, gated):
    course_id = _create(client)["course_id"]
    refused = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert refused.status_code == 409
    assert "approve" in refused.json()["error"]["message"].lower()


def test_generation_runs_after_approval(client, gated):
    course_id = _create(client)["course_id"]
    approved = client.post(f"/api/courses/{course_id}/approve-outline")
    assert approved.status_code == 200
    body = approved.json()
    assert body["outline_approved"] is True and body["outline_approved_at"]
    ok = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert ok.status_code == 200, ok.text


def test_approving_twice_changes_nothing(client, gated):
    course_id = _create(client)["course_id"]
    first = client.post(f"/api/courses/{course_id}/approve-outline").json()
    second = client.post(f"/api/courses/{course_id}/approve-outline").json()
    assert first["outline_approved_at"] == second["outline_approved_at"]


def test_a_course_from_before_the_gate_is_never_blocked(client, gated):
    course_id = _create(client)["course_id"]
    service = course_service.get_course_service()
    import asyncio

    async def make_legacy():
        record = await service._load_course(course_id)
        record.outline_approved = None
        await service._save_course(record)

    asyncio.run(make_legacy())
    ok = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert ok.status_code == 200, ok.text


def test_the_gate_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_OUTLINE_APPROVAL", "false")
    reset_settings_cache()
    course_service.reset_course_service()
    course_id = _create(client)["course_id"]
    assert client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"}).status_code == 200
