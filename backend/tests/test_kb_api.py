"""The /api/kb admin API end to end, against a real PostgreSQL."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.api.dependencies import get_current_admin
from app.core.config import get_settings, reset_settings_cache
from app.db.session import get_session_factory
from app.kb.worker import KbWorker
from app.main import app
from tests.test_kb_write_path import policy

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")


class _Admin:
    id = None  # an actor that is not a user row: recorded as "system", never an error
    role = "admin"


@pytest.fixture
async def api(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    app.dependency_overrides[get_current_admin] = lambda: _Admin()
    worker = KbWorker(settings=get_settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as client:
        client.drain = worker.drain  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.pop(get_current_admin, None)


async def _upload(api, name="policy.txt", data=None, **form):
    response = await api.post("/api/kb/items", files=[("files", (name, data or policy(), "text/plain"))], data=form)
    assert response.status_code == 201, response.text
    await api.drain()
    return response.json()


async def test_only_an_admin_may_use_it():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as anonymous:
        for method, path in (("get", "/api/kb/items"), ("get", "/api/kb/categories"), ("post", "/api/kb/search"), ("get", f"/api/kb/items/{uuid.uuid4()}")):
            assert (await getattr(anonymous, method)(path)).status_code in {401, 403}, path


async def test_an_upload_is_proposed_filled_in_reviewed_and_published(api):
    created = await _upload(api)
    detail = (await api.get(f"/api/kb/items/{created['id']}")).json()
    assert (detail["lifecycle"], detail["processing"], detail["needs_review"], detail["unpublished_changes"]) == ("draft", "ready", True, False)
    assert detail["tags"] and detail["suggestions"]["title"] and detail["versions"][0]["files"] == ["policy.txt"] and detail["versions"][0]["passages"] >= 3
    assert not (await api.post("/api/kb/search", json={"query": "refunds eligibility unused product"})).json()  # a draft is not searchable

    # The admin reviews: picks a title, files it under a new category, keeps the proposed tags.
    category = (await api.post("/api/kb/categories", json={"name": "Finance"})).json()["id"]
    sub = (await api.post("/api/kb/categories", json={"name": "Refunds", "parent_id": category})).json()["id"]
    patched = await api.patch(f"/api/kb/items/{created['id']}", json={"title": "Refund rules", "category_id": sub, "tags": ["Refunds", "policy"]})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["title"], body["category"]["name"], sorted(body["tags"])) == ("Refund rules", "Refunds", ["Refunds", "policy"])

    assert (await api.post(f"/api/kb/items/{created['id']}/publish")).json() == {"result": "publish"}
    hits = (await api.post("/api/kb/search", json={"query": "refunds eligibility unused product"})).json()
    assert hits and hits[0]["ref"].startswith(f"G{created['number']}.")

    # Filters: by lifecycle, tag, search text, and category (a parent includes its sub-categories).
    for params, expected in (({"lifecycle": "published"}, 1), ({"lifecycle": "draft"}, 0), ({"tag": "POLICY"}, 1), ({"q": "refund"}, 1), ({"q": "nonexistentword"}, 0), ({"category_id": category}, 1), ({"needs_review": True}, 0)):
        assert (await api.get("/api/kb/items", params=params)).json()["total"] == expected, params
    tree = (await api.get("/api/kb/categories")).json()
    assert {(c["name"], c["item_count"]) for c in tree} == {("Finance", 0), ("Refunds", 1)}
    assert {(t["name"], t["item_count"]) for t in (await api.get("/api/kb/tags")).json()} == {("Refunds", 1), ("policy", 1)}


async def test_a_new_version_waits_for_publishing_and_two_admins_cannot_overwrite_each_other(api):
    created = await _upload(api)
    await api.post(f"/api/kb/items/{created['id']}/publish")
    edited = policy(eligibility="Eligible within thirty days of purchase for the customer.")
    v2 = await api.post(f"/api/kb/items/{created['id']}/versions", files=[("files", ("p2.txt", edited, "text/plain"))], data={"keep_previous_sources": "false", "change_note": "30 days"})
    assert v2.status_code == 201
    await api.drain()
    detail = (await api.get(f"/api/kb/items/{created['id']}")).json()
    assert detail["unpublished_changes"] is True and [v["is_live"] for v in detail["versions"]] == [False, True]
    v2_id, v1_id = detail["versions"][0]["id"], detail["versions"][1]["id"]

    stale = await api.post(f"/api/kb/items/{created['id']}/versions/{v2_id}/publish", json={"expected_live_version_id": str(uuid.uuid4())})
    assert stale.status_code == 409
    ok = await api.post(f"/api/kb/items/{created['id']}/versions/{v2_id}/publish", json={"expected_live_version_id": v1_id})
    assert ok.json() == {"result": "switch"}
    assert (await api.post(f"/api/kb/items/{created['id']}/versions/{v2_id}/publish", json={"expected_live_version_id": v2_id})).json() == {"result": "noop"}
    chunks = (await api.get(f"/api/kb/items/{created['id']}/chunks", params={"version_id": v2_id})).json()
    assert chunks and chunks[0]["ref"].startswith(f"G{created['number']}.") and any("thirty" in c["text"] for c in chunks)


async def test_citations_resolve_to_what_was_cited_even_after_the_passage_was_replaced(api):
    created = await _upload(api)
    await api.post(f"/api/kb/items/{created['id']}/publish")
    original = {c["ordinal"]: c["text"] for c in (await api.get(f"/api/kb/items/{created['id']}/chunks")).json()}
    detail = (await api.get(f"/api/kb/items/{created['id']}")).json()
    v1 = detail["versions"][0]["id"]
    await api.post(f"/api/kb/items/{created['id']}/versions", files=[("files", ("p2.txt", policy(eligibility="Eligible within thirty days of purchase for the customer."), "text/plain"))], data={"keep_previous_sources": "false"})
    await api.drain()
    v2 = (await api.get(f"/api/kb/items/{created['id']}")).json()["versions"][0]["id"]
    await api.post(f"/api/kb/items/{created['id']}/versions/{v2}/publish", json={"expected_live_version_id": v1})
    now = {c["ordinal"] for c in (await api.get(f"/api/kb/items/{created['id']}/chunks", params={"version_id": v2})).json()}
    replaced = (set(original) - now).pop()

    cited = await api.get(f"/api/kb/citations/G{created['number']}.{replaced}")
    body = cited.json()
    assert cited.status_code == 200 and body["text"] == original[replaced] and body["live_now"] is False and body["superseded"] is True
    assert body["generation_time"] is None and body["live_at_generation"] is None  # nothing says when the course was made

    # With a moment from before the switch, the passage was live; from after, it was not.
    before_switch = (await api.get(f"/api/kb/citations/G{created['number']}.{replaced}", params={"at": "2999-01-01T00:00:00Z"})).json()
    assert before_switch["generation_time_basis"] == "supplied" and before_switch["live_at_generation"] is False
    live_from = [v for v in (await api.get(f"/api/kb/items/{created['id']}")).json()["versions"] if v["id"] == v1][0]["live_from"]
    assert (await api.get(f"/api/kb/citations/G{created['number']}.{replaced}", params={"at": live_from})).json()["live_at_generation"] is True

    for bad, status in (("nonsense", 422), ("G999.1", 404)):
        assert (await api.get(f"/api/kb/citations/{bad}")).status_code == status


async def test_the_retrieval_log_dates_a_citation_exactly(api):
    created = await _upload(api)
    await api.post(f"/api/kb/items/{created['id']}/publish")
    from app.services.knowledge_pg import PostgresKnowledgeBackend
    from app.services.embedding_service import get_embedding_service

    backend = PostgresKnowledgeBackend(get_settings(), embeddings=get_embedding_service())
    hits = await backend.asearch("refunds eligibility unused product", trace=("course_42", "ch_1"))
    ref = hits[0].ref
    cited = (await api.get(f"/api/kb/citations/{ref}", params={"course_id": "course_42"})).json()
    assert cited["generation_time_basis"] == "retrieval_log" and cited["live_at_generation"] is True
    other = (await api.get(f"/api/kb/citations/{ref}", params={"course_id": "never_used_it"})).json()
    assert other["generation_time_basis"] is None


async def test_lifecycle_actions_and_their_errors(api):
    created = await _upload(api)
    item = created["id"]
    assert (await api.post(f"/api/kb/items/{item}/unpublish")).status_code == 409  # not published yet
    await api.post(f"/api/kb/items/{item}/publish")
    assert (await api.post(f"/api/kb/items/{item}/archive")).json()["lifecycle"] == "archived"
    assert (await api.post(f"/api/kb/items/{item}/publish")).status_code == 409
    assert (await api.post(f"/api/kb/items/{item}/restore")).json()["lifecycle"] == "unpublished"
    assert (await api.post(f"/api/kb/items/{item}/delete")).json()["deleted"] is True
    assert (await api.get("/api/kb/items")).json()["total"] == 0 and (await api.get("/api/kb/items", params={"deleted": True})).json()["total"] == 1
    assert (await api.post(f"/api/kb/items/{item}/undelete")).json()["deleted"] is False
    assert (await api.post(f"/api/kb/items/{item}/explode")).status_code == 422
    assert (await api.get(f"/api/kb/items/{uuid.uuid4()}")).status_code == 404
    actions = [a["action"] for a in (await api.get(f"/api/kb/items/{item}/activity")).json()]
    assert actions[-1] == "created" and {"published", "archive", "delete"} <= set(actions)


async def test_relationships_are_typed_two_way_visible_and_cannot_loop(api):
    a, b, c = [await _upload(api, f"d{i}.txt", f"DOC {i}\n\nA distinct document number {i} about subject {uuid.uuid4().hex}.\n".encode()) for i in range(3)]
    rel = await api.post(f"/api/kb/items/{a['id']}/relations", json={"to_item_id": b["id"], "relation_type": "supersedes", "note": "newer"})
    assert rel.status_code == 201
    assert (await api.post(f"/api/kb/items/{a['id']}/relations", json={"to_item_id": b["id"], "relation_type": "supersedes"})).status_code == 409  # repeat
    assert (await api.post(f"/api/kb/items/{a['id']}/relations", json={"to_item_id": a["id"], "relation_type": "related_to"})).status_code == 422
    await api.post(f"/api/kb/items/{b['id']}/relations", json={"to_item_id": c["id"], "relation_type": "supersedes"})
    assert (await api.post(f"/api/kb/items/{c['id']}/relations", json={"to_item_id": a["id"], "relation_type": "supersedes"})).status_code == 409  # a chain back to itself
    assert (await api.post(f"/api/kb/items/{c['id']}/relations", json={"to_item_id": a["id"], "relation_type": "related_to"})).status_code == 201  # but "related" may loop
    assert (await api.post(f"/api/kb/items/{a['id']}/relations", json={"to_item_id": b["id"], "relation_type": "friends"})).status_code == 422

    seen_from_b = (await api.get(f"/api/kb/items/{b['id']}")).json()["relations"]
    assert {(r["direction"], r["relation_type"], r["other_id"]) for r in seen_from_b} == {("in", "supersedes", a["id"]), ("out", "supersedes", c["id"])}
    assert (await api.delete(f"/api/kb/relations/{rel.json()['id']}")).status_code == 204
    assert (await api.delete(f"/api/kb/relations/{rel.json()['id']}")).status_code == 404


async def test_categories_cannot_loop_or_duplicate_and_suggested_ones_are_approved_once(api):
    root = (await api.post("/api/kb/categories", json={"name": "Company"})).json()["id"]
    child = (await api.post("/api/kb/categories", json={"name": "HR", "parent_id": root})).json()["id"]
    assert (await api.post("/api/kb/categories", json={"name": "HR", "parent_id": root})).status_code == 409
    assert (await api.post("/api/kb/categories", json={"name": "HR"})).status_code == 201  # same name, different parent
    assert (await api.patch(f"/api/kb/categories/{root}", json={"parent_id": child})).status_code == 409  # under its own child
    assert (await api.patch(f"/api/kb/categories/{child}", json={"name": "People", "parent_id": None})).status_code == 200
    assert (await api.delete(f"/api/kb/categories/{root}")).status_code == 204
    assert (await api.post("/api/kb/categories", json={"name": "x", "parent_id": str(uuid.uuid4())})).status_code == 404
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("UPDATE kb_categories SET is_suggested = true WHERE id = :c"), {"c": child})
    assert next(c for c in (await api.get("/api/kb/categories")).json() if c["id"] == child)["is_suggested"] is True
    await api.patch(f"/api/kb/categories/{child}", json={"approve": True})
    assert next(c for c in (await api.get("/api/kb/categories")).json() if c["id"] == child)["is_suggested"] is False
    assert (await api.delete(f"/api/kb/categories/{uuid.uuid4()}")).status_code == 404


async def test_bad_input_is_refused(api):
    assert (await api.post("/api/kb/items")).status_code == 422  # nothing to store
    assert (await api.post("/api/kb/items", files=[("files", ("virus.exe", b"MZ", "application/octet-stream"))])).status_code == 422
    assert (await api.post("/api/kb/search", json={"query": ""})).status_code == 422
    created = await _upload(api)
    assert (await api.patch(f"/api/kb/items/{created['id']}", json={"title": "   "})).status_code == 422
    assert (await api.patch(f"/api/kb/items/{created['id']}", json={"bogus": 1})).status_code == 422
    assert (await api.get("/api/kb/items", params={"lifecycle": "live"})).status_code == 422
