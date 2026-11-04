"""The artifact memory API: who may do what, and the review flow end to end."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.api.dependencies import get_current_user
from app.artifacts.service import ArtifactService
from app.core.config import get_settings, reset_settings_cache
from app.db.session import get_session_factory
from tests.artifact_helpers import wipe_artifact_memory
from app.main import app
from tests.test_artifact_reuse import REFUNDS, chapter_doc

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")

USER = uuid.uuid4()
QUERY = "refund eligibility for returned products"


@pytest.fixture
async def api(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    await wipe_artifact_memory()
    async with get_session_factory()() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email, password_hash, role, is_active, is_verified, created_at, updated_at) VALUES (:i, :e, 'x', 'editor_reviewer', true, true, now(), now()) ON CONFLICT (id) DO NOTHING"),
            {"i": USER, "e": f"sme-{USER}@example.com"},
        )
    who = {"role": "editor_reviewer"}
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER, role=who["role"], email="sme@example.com")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://am") as client:
        client.as_role = lambda role: who.update(role=role)  # type: ignore[attr-defined]
        client.seed = lambda course_id="course_1", doc=None: ArtifactService(settings=get_settings()).sync_course(  # type: ignore[attr-defined]
            course_id=course_id, course_title="Refund Handling", document=doc or chapter_doc("Refund rules", REFUNDS, tag=course_id))
        yield client
    app.dependency_overrides.pop(get_current_user, None)
    await wipe_artifact_memory()  # approved artifacts are reused by every course: leave none behind


async def _first(api, **params):
    return (await api.get("/api/artifacts", params=params)).json()["items"][0]


async def test_who_may_read_review_and_manage(api):
    matrix = {  # role -> the status each kind of action gets
        "author": {"read": 403, "review": 403, "manage": 403},
        "manager": {"read": 200, "review": 403, "manage": 403},
        "editor_reviewer": {"read": 200, "review": 200, "manage": 403},
        "admin": {"read": 200, "review": 200, "manage": 200},
    }
    for role, expected in matrix.items():
        course = f"course_{role}"
        await api.seed(course)  # a fresh course per role, so one role's approval does not use up the next one's
        api.as_role("admin")
        item = (await api.get("/api/artifacts", params={"course_id": course})).json()["items"][0]
        api.as_role(role)
        assert (await api.get("/api/artifacts")).status_code == expected["read"], role
        assert (await api.post(f"/api/artifacts/versions/{item['latest_version_id']}/approve", json={})).status_code == expected["review"], role
        assert (await api.post(f"/api/artifacts/{item['id']}/archive")).status_code == expected["manage"], role


async def test_unauthenticated_requests_are_refused():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://am") as anonymous:
        for method, path in (("get", "/api/artifacts"), ("get", "/api/artifacts/counts"), ("post", "/api/artifacts/search"), ("get", f"/api/artifacts/{uuid.uuid4()}")):
            assert (await getattr(anonymous, method)(path)).status_code in {401, 403}, path


async def test_the_review_queue_leads_with_what_is_waiting_and_shows_each_artifacts_state(api):
    await api.seed("course_a")
    queue = await api.get("/api/artifacts", params={"review_status": "pending_review"})
    assert queue.json()["total"] == 1 and queue.json()["items"][0]["review_status"] == "pending_review"
    assert (await api.get("/api/artifacts/counts")).json() == {"pending_review": 1, "approved": 0, "changes_requested": 0}
    version = queue.json()["items"][0]["latest_version_id"]

    assert (await api.post(f"/api/artifacts/versions/{version}/request-changes", json={})).status_code == 422  # a reason is required
    assert (await api.post(f"/api/artifacts/versions/{version}/request-changes", json={"comment": "Check the figure"})).json() == {"result": "changes_requested"}
    item = await _first(api)
    assert (item["review_status"], item["review_comment"], item["ever_approved"]) == ("changes_requested", "Check the figure", False)
    assert (await api.get("/api/artifacts", params={"review_status": "pending_review"})).json()["total"] == 0

    await api.seed("course_a")  # resubmitted unchanged: back in the queue
    assert (await api.get("/api/artifacts", params={"review_status": "pending_review"})).json()["total"] == 1
    assert (await api.post(f"/api/artifacts/versions/{version}/approve", json={"comment": "Good"})).json() == {"result": "approved"}
    item = await _first(api)
    assert (item["review_status"], item["active_version"], item["ever_approved"]) == ("approved", 1, True)
    assert (await api.post(f"/api/artifacts/versions/{version}/approve", json={})).status_code == 409  # already approved
    assert (await api.get("/api/artifacts/counts")).json()["approved"] == 1


async def test_an_approved_artifact_is_searchable_and_its_citation_resolves_with_its_provenance(api):
    await api.seed("course_a")
    version = (await _first(api))["latest_version_id"]
    assert (await api.post("/api/artifacts/search", json={"query": QUERY})).json() == []  # not approved: not offered
    await api.post(f"/api/artifacts/versions/{version}/approve", json={"comment": "Approved by SME"})
    hits = (await api.post("/api/artifacts/search", json={"query": QUERY, "types": ["chapter"]})).json()
    assert len(hits) == 1 and hits[0]["ref"].endswith(".1") and "fourteen days" in hits[0]["text"]
    assert (await api.post("/api/artifacts/search", json={"query": QUERY, "types": ["visual"]})).json() == []

    cited = (await api.get(f"/api/artifacts/citations/{hits[0]['ref']}")).json()
    assert cited["review_status"] == "approved" and cited["approved_by"] == str(USER) and cited["superseded"] is False
    assert cited["course_id"] == "course_a" and cited["review_comment"] == "Approved by SME" and cited["generation_run_id"] is None
    for bad, status in (("nonsense", 422), ("A9999.1", 404)):
        assert (await api.get(f"/api/artifacts/citations/{bad}")).status_code == status


async def test_editing_approved_work_makes_a_version_that_waits_and_the_detail_shows_every_version(api):
    await api.seed("course_a")
    first = await _first(api)
    await api.post(f"/api/artifacts/versions/{first['latest_version_id']}/approve", json={})
    content = {"kind": "chapter", "blocks": [{"id": "x", "type": "paragraph", "content": {"text": "Refunds are issued within thirty days."}}]}
    made = await api.post(f"/api/artifacts/{first['id']}/versions", json={"content": content, "change_note": "Longer window"})
    assert made.status_code == 201
    assert (await api.post(f"/api/artifacts/{first['id']}/versions", json={"content": content})).status_code == 409  # unchanged
    assert (await api.post(f"/api/artifacts/{first['id']}/versions", json={"content": {}})).status_code == 422

    detail = (await api.get(f"/api/artifacts/{first['id']}")).json()
    assert [(v["version_no"], v["review_status"], v["locked"], v["is_active"]) for v in detail["versions"]] == [(2, "pending_review", False, False), (1, "approved", True, True)]
    assert {a["action"] for a in detail["activity"]} >= {"submitted", "approved", "version_added"}
    # Retrieval still serves the approved one until the new version is approved.
    assert "fourteen" in (await api.post("/api/artifacts/search", json={"query": QUERY})).json()[0]["text"]
    await api.post(f"/api/artifacts/versions/{made.json()['version_id']}/approve", json={})
    assert "thirty" in (await api.post("/api/artifacts/search", json={"query": "refunds issued thirty days"})).json()[0]["text"]
    assert (await api.get(f"/api/artifacts/{uuid.uuid4()}")).status_code == 404


async def test_archive_restore_and_links(api):
    await api.seed("course_a")
    items = (await api.get("/api/artifacts")).json()["items"]
    first = items[0]
    await api.post(f"/api/artifacts/versions/{first['latest_version_id']}/approve", json={})
    api.as_role("admin")
    assert (await api.post(f"/api/artifacts/{first['id']}/archive")).json() == {"result": "archived"}
    assert (await api.post(f"/api/artifacts/{first['id']}/archive")).status_code == 409
    assert (await api.post("/api/artifacts/search", json={"query": QUERY})).json() == []
    assert (await api.get("/api/artifacts", params={"status": "archived"})).json()["total"] == 1
    assert (await api.post(f"/api/artifacts/{first['id']}/restore")).json() == {"result": "restored"}
    assert len((await api.post("/api/artifacts/search", json={"query": QUERY})).json()) == 1

    created = await api.post(f"/api/artifacts/{first['id']}/links", json={"link_type": "references_knowledge", "kb_number": 7, "kb_ordinal": 2, "note": "policy"})
    assert created.status_code == 201
    assert (await api.post(f"/api/artifacts/{first['id']}/links", json={"link_type": "related_to", "course_id": "x"})).status_code == 422  # wrong target
    assert (await api.post(f"/api/artifacts/{first['id']}/links", json={"link_type": "friends"})).status_code == 422
    detail = (await api.get(f"/api/artifacts/{first['id']}")).json()
    kb = [l for l in detail["links"] if l["link_type"] == "references_knowledge" and l["note"] == "policy"]
    assert kb and (kb[0]["target_kb_number"], kb[0]["target_kb_ordinal"], kb[0]["direction"]) == (7, 2, "out")
    assert any(l["link_type"] == "belongs_to_course" and l["target_course_id"] == "course_a" for l in detail["links"])
    await api.delete(f"/api/artifacts/links/{kb[0]['id']}")
    assert (await api.delete(f"/api/artifacts/links/{kb[0]['id']}")).status_code == 404


async def test_filters_find_artifacts_by_type_course_and_text_and_the_page_is_bounded(api):
    await api.seed("course_a")
    await api.seed("course_b", chapter_doc("Shipping rules", "Orders ship by courier within two days.", tag="b"))
    assert (await api.get("/api/artifacts")).json()["total"] == 2
    assert (await api.get("/api/artifacts", params={"course_id": "course_b"})).json()["total"] == 1
    assert (await api.get("/api/artifacts", params={"q": "shipping"})).json()["total"] == 1
    assert (await api.get("/api/artifacts", params={"artifact_type": "visual"})).json()["total"] == 0
    page = (await api.get("/api/artifacts", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["total"] == 2
    assert (await api.get("/api/artifacts", params={"limit": 1000})).status_code == 422
    assert (await api.get("/api/artifacts", params={"review_status": "bogus"})).status_code == 422


async def test_indexing_can_be_run_for_versions_approved_while_embedding_was_unavailable(api):
    await api.seed("course_a")
    version = (await _first(api))["latest_version_id"]
    await api.post(f"/api/artifacts/versions/{version}/approve", json={})
    api.as_role("admin")
    assert (await api.post("/api/artifacts/index-missing")).json() == {"indexed": 0}  # it was indexed when approved
    async with get_session_factory()() as session, session.begin():  # as if the provider had been down
        await session.execute(text("ALTER TABLE am_artifact_versions DISABLE TRIGGER am_guard_locked_version"))
        await session.execute(text("UPDATE am_artifact_versions SET embedding = NULL, embed_model = NULL"))
        await session.execute(text("ALTER TABLE am_artifact_versions ENABLE TRIGGER am_guard_locked_version"))
    assert (await api.post("/api/artifacts/index-missing")).json() == {"indexed": 1}
