"""Permanent removal: who is warned, what goes, and what is kept."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.session import get_session_factory
from app.kb.purge import Purger
from app.kb.queries import KbQueries
from app.kb.service import KbService
from app.kb.taxonomy import Taxonomy
from app.kb.worker import KbWorker
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg import PostgresKnowledgeBackend
from tests.test_kb_write_path import policy

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
async def env(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    monkeypatch.setenv("KB_PURGE_ENABLED", "true")
    reset_settings_cache()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    settings = get_settings()

    class Env:
        pass

    e = Env()
    e.settings, e.kb, e.tax, e.purger = settings, KbService(settings=settings), Taxonomy(), Purger(settings)
    e.worker = KbWorker(settings=settings)
    e.backend = PostgresKnowledgeBackend(settings, embeddings=get_embedding_service())
    return e


async def _published(env, name="policy.txt", data=None):
    ref = await env.kb.create_item(actor_id=None, files=[(name, data or policy())], allow_duplicate=True)
    await env.worker.drain()
    await env.kb.publish_item(ref.id, actor_id=None)
    return ref


async def _sql(sql, **params):
    async with get_session_factory()() as session, session.begin():
        result = await session.execute(text(sql), params)
        return result.all() if result.returns_rows else []


async def _course_citing(number: int) -> str:
    """A course whose stored document cites G<number>, as an older course would (no retrieval log)."""
    course_id = f"course_{uuid.uuid4().hex[:8]}"
    pk = uuid.uuid4()
    await _sql(
        "INSERT INTO courses (id, course_id, document_id, title, status, template_id, review_status, input_json, metadata_json, created_at, updated_at) "
        "VALUES (:id, :c, :d, 'Old course', 'ready', 't', 'draft', '{}'::jsonb, '{}'::jsonb, now(), now())",
        id=pk, c=course_id, d=f"doc_{course_id}",
    )
    await _sql(
        "INSERT INTO documents (id, document_id, course_pk, version, document_json, created_at, updated_at) VALUES (:id, :d, :pk, 1, CAST(:j AS jsonb), now(), now())",
        id=uuid.uuid4(), d=f"doc_{course_id}", pk=pk, j=json.dumps({"pages": [{"blocks": [{"text": f"Refunds take fourteen days [G{number}.2]."}]}]}),
    )
    return course_id


async def test_the_impact_names_every_course_that_cites_the_item_from_every_source(env):
    ref = await _published(env)
    from_text = await _course_citing(ref.number)
    await env.backend.asearch("refunds eligibility unused product", trace=("course_logged", "ch_1"))
    impact = await env.purger.impact(ref.id)
    assert impact["courses"] == sorted({from_text, "course_logged"})
    assert impact["from_retrieval_log"] == ["course_logged"] and impact["from_document_text"] == [from_text]
    assert f"2 course(s) cite G{ref.number}" in impact["warning"]
    other = await _published(env, "other.txt", b"OTHER\n\nSomething unrelated about warehouse shipping schedules.\n")
    assert (await env.purger.impact(other.id))["courses"] == [] and "No course is known" in (await env.purger.impact(other.id))["warning"]
    with pytest.raises(NotFoundError):
        await env.purger.impact(uuid.uuid4())


async def test_purge_needs_the_flag_the_trash_and_the_typed_number(env, monkeypatch):
    ref = await _published(env)
    off = Purger(env.settings.model_copy(update={"kb_purge_enabled": False}))
    await env.kb.set_lifecycle(ref.id, "delete", actor_id=None)
    with pytest.raises(ConflictError, match="not enabled"):
        await off.purge(ref.id, confirm_number=ref.number)
    with pytest.raises(ValidationFailedError, match="confirm"):
        await env.purger.purge(ref.id, confirm_number=ref.number + 1)
    live = await _published(env, "live.txt", b"LIVE\n\nAn item that is still published and so cannot be purged.\n")
    with pytest.raises(ConflictError, match="trash"):
        await env.purger.purge(live.id, confirm_number=live.number)
    with pytest.raises(NotFoundError):
        await env.purger.purge(uuid.uuid4(), confirm_number=1)


async def test_purge_removes_content_but_keeps_the_number_and_the_history(env):
    ref = await _published(env)
    other = await _published(env, "other.txt", b"OTHER\n\nSomething unrelated about warehouse shipping schedules.\n")
    await env.tax.add_relation(ref.id, other.id, "related_to")
    await env.tax.set_tags(ref.id, ["policy"])
    stored = [r.storage_key for r in await _sql("SELECT s.storage_key FROM kb_sources s JOIN kb_version_sources vs ON vs.source_id = s.id WHERE vs.version_id = :v", v=ref.version_id)]
    assert stored and (env.kb.data_dir / stored[0]).is_file()
    cite = f"G{ref.number}.2"
    assert await env.backend.apassages_by_refs([cite])

    await env.kb.set_lifecycle(ref.id, "delete", actor_id=None)
    result = await env.purger.purge(ref.id, confirm_number=ref.number, actor_id=None)
    assert result == {"purged": ref.number, "files_removed": 1} and not (env.kb.data_dir / stored[0]).exists()

    assert (await _sql("SELECT count(*) AS n FROM kb_chunks WHERE item_id = :i", i=ref.id))[0].n == 0
    assert (await _sql("SELECT count(*) AS n FROM kb_relations"))[0].n == 0 and (await _sql("SELECT count(*) AS n FROM kb_item_tags"))[0].n == 0
    stub = (await _sql("SELECT number, purged_at, deleted_at, description, body_md IS NULL AS no_body FROM kb_items i JOIN kb_item_versions v ON v.item_id = i.id WHERE i.id = :i", i=ref.id))[0]
    assert stub.number == ref.number and stub.purged_at is not None and stub.deleted_at is not None and stub.no_body
    actions = [r.action for r in await _sql("SELECT action FROM kb_activity WHERE item_id = :i", i=ref.id)]
    assert "created" in actions and "purged" in actions  # the history survives the purge

    # The citation can no longer be shown or checked - the same as a document deleted from the old library.
    assert await env.backend.apassages_by_refs([cite]) == []
    with pytest.raises(NotFoundError):
        await KbQueries().resolve_citation(cite)
    assert (await env.backend.alibrary(extra_refs=[cite])).passages.keys().isdisjoint({cite})
    # The number is never handed out again, and the neighbour is untouched.
    fresh = await env.kb.create_item(actor_id=None, body_md="A later item written after the purge happened.")
    assert fresh.number > ref.number
    assert await env.backend.apassages_by_refs([f"G{other.number}.1"])
    with pytest.raises(ConflictError, match="permanently removed"):
        await env.purger.purge(ref.id, confirm_number=ref.number)
    with pytest.raises(NotFoundError):
        await env.purger.impact(ref.id)


async def test_a_file_another_item_still_uses_is_not_removed(env):
    first = await env.kb.create_item(actor_id=None, files=[("a.txt", policy())])
    second = await env.kb.create_item(actor_id=None, files=[("b.txt", policy())], allow_duplicate=True)  # the same bytes: one stored file
    await env.worker.drain()
    key = (await _sql("SELECT storage_key FROM kb_sources"))[0].storage_key
    await env.kb.set_lifecycle(first.id, "delete", actor_id=None)
    assert (await env.purger.purge(first.id, confirm_number=first.number))["files_removed"] == 0
    assert (env.kb.data_dir / key).is_file()
    await env.kb.set_lifecycle(second.id, "delete", actor_id=None)
    assert (await env.purger.purge(second.id, confirm_number=second.number))["files_removed"] == 1
    assert not (env.kb.data_dir / key).exists()


async def test_a_legacy_original_is_never_deleted_by_a_purge(env, tmp_path):
    """Imported documents' originals are used in place; deleting one would end the rollback option."""
    ref = await env.kb.create_item(actor_id=None, body_md="Typed text that will stand in for an imported document.")
    await env.worker.drain()
    legacy = env.kb.data_dir / "sources" / "global" / "src_aaaaaaaaaaaa" / "original.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"%PDF legacy")
    source = uuid.uuid4()
    await _sql("INSERT INTO kb_sources (id, sha256, filename, kind, size_bytes, storage_key) VALUES (:i, 'abc', 'old.pdf', 'pdf', 11, 'sources/global/src_aaaaaaaaaaaa/original.pdf')", i=source)
    await _sql("INSERT INTO kb_version_sources (version_id, source_id, position) VALUES (:v, :s, 0)", v=ref.version_id, s=source)
    await env.kb.set_lifecycle(ref.id, "delete", actor_id=None)
    assert (await env.purger.purge(ref.id, confirm_number=ref.number))["files_removed"] == 0
    assert legacy.is_file()
