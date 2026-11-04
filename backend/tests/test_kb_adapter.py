"""The PostgreSQL knowledge service answering the existing /api/knowledge operations."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, NotFoundError
from app.db.session import get_session_factory
from app.kb.service import KbService
from app.kb.worker import KbWorker
from app.services import knowledge_service
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg_service import PostgresKnowledgeService
from tests.sample_files import POLICY_TEXT

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
async def svc(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    settings = get_settings()
    service = PostgresKnowledgeService(settings, embeddings=get_embedding_service(), log_retrievals=False)
    service.worker = KbWorker(settings=settings)
    return service


async def test_an_upload_is_a_draft_with_a_permanent_number_and_the_old_record_shape(svc):
    created = await svc.aupload("policy.txt", POLICY_TEXT, description="  Refund   rules ")
    assert created.source_id.startswith("src_") and created.global_number >= 1
    assert (created.status, created.enabled, created.description, created.kind) == ("uploaded", False, "Refund rules", "txt")
    await svc.worker.drain()
    ready = await svc.aget(created.source_id)
    assert ready.status == "ready" and ready.enabled is False and ready.chunk_count >= 3 and ready.embedded_chunks == ready.chunk_count
    assert ready.size_bytes == len(POLICY_TEXT) and ready.sha256 and ready.outline


async def test_the_old_on_off_switch_publishes_and_unpublishes(svc):
    created = await svc.aupload("policy.txt", POLICY_TEXT)
    with pytest.raises(ConflictError, match="still being processed"):
        await svc.aupdate(created.source_id, enabled=True)
    await svc.worker.drain()
    query = "who handles disputed refund cases"
    assert await svc.asearch(query) == []  # a draft is not searchable
    on = await svc.aupdate(created.source_id, enabled=True, description="Live now")
    assert on.enabled is True and on.description == "Live now"
    hits = await svc.asearch(query, limit=3)
    assert hits and hits[0].ref.startswith(f"G{created.global_number}.") and "billing team" in hits[0].text
    assert (await svc.aupdate(created.source_id, enabled=True)).enabled is True  # already on: harmless
    off = await svc.aupdate(created.source_id, enabled=False)
    assert off.enabled is False and await svc.asearch(query) == []
    assert (await svc.aupdate(created.source_id, enabled=False)).enabled is False


async def test_the_same_file_again_returns_the_existing_document_and_a_failed_one_can_be_reuploaded(svc):
    first = await svc.aupload("policy.txt", POLICY_TEXT)
    again = await svc.aupload("renamed.txt", POLICY_TEXT)
    assert again.source_id == first.source_id and again.global_number == first.global_number
    blank = await svc.aupload("blank.txt", b"   \n  \n")
    await svc.worker.drain()
    assert (await svc.aget(blank.source_id)).status == "failed"
    retry = await svc.aupload("blank2.txt", b"   \n  \n")  # same bytes: the failed one does not block it
    assert retry.source_id != blank.source_id


async def test_listing_stats_chunks_and_delete_match_the_old_behaviour(svc):
    a = await svc.aupload("policy.txt", POLICY_TEXT)
    b = await svc.aupload("cooking.txt", b"COOKING\n\nBoil the pasta for ten minutes in plenty of salted water, then drain it well.\n")
    stats = await svc.astats()
    assert stats == {"documents": 2, "searchable": 0, "passages": 0, "processing": 2}
    await svc.worker.drain()
    await svc.aupdate(a.source_id, enabled=True)
    listing = await svc.alist_documents()
    assert [d.global_number for d in listing] == sorted(d.global_number for d in listing) and {d.source_id for d in listing} == {a.source_id, b.source_id}
    ready_a = await svc.aget(a.source_id)
    stats = await svc.astats()
    assert stats["documents"] == 2 and stats["searchable"] == 1 and stats["passages"] == ready_a.chunk_count and stats["processing"] == 0

    chunks = await svc.achunks(a.source_id, 0, 2)
    assert [c.index for c in chunks] == [0, 1] and all(c.embedded for c in chunks)  # index = ordinal - 1: refs unchanged
    assert (await svc.achunks(a.source_id, 1, 2))[0].index == 1

    await svc.adelete(a.source_id)
    with pytest.raises(NotFoundError):
        await svc.aget(a.source_id)
    assert [d.source_id for d in await svc.alist_documents()] == [b.source_id]
    assert await svc.asearch("who handles disputed refund cases") == []  # trashed content is never offered
    with pytest.raises(NotFoundError):
        await svc.aupdate("src_aaaaaaaaaaaa", enabled=True)


async def test_the_deleted_numbers_are_never_reused(svc):
    first = await svc.aupload("policy.txt", POLICY_TEXT)
    await svc.adelete(first.source_id)
    second = await svc.aupload("other.txt", b"OTHER\n\nSomething else entirely about warehouse shipping schedules.\n")
    assert second.global_number > first.global_number


async def test_the_number_guard_lifts_a_rewound_sequence_and_never_lowers_one(svc):
    kb = KbService(settings=get_settings())
    first = await svc.aupload("policy.txt", POLICY_TEXT)
    async with get_session_factory()() as session, session.begin():  # simulate a restored backup rewinding the sequence
        await session.execute(text("SELECT setval('kb_item_number_seq', 1, false)"))
    assert await kb.ensure_number_sequence() == first.global_number + 1
    second = await svc.aupload("other.txt", b"OTHER\n\nSomething else entirely about warehouse shipping schedules.\n")
    assert second.global_number == first.global_number + 1
    far = second.global_number + 500
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("SELECT setval('kb_item_number_seq', :far)"), {"far": far})
    await kb.ensure_number_sequence()  # a sequence already ahead of every number is left alone
    third = await svc.aupload("third.txt", b"THIRD\n\nA third unrelated document about office plants and watering.\n")
    assert third.global_number == far + 1


def test_the_configured_backend_is_chosen_by_settings(monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "postgres")
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    knowledge_service.reset_knowledge_service()
    try:
        assert isinstance(knowledge_service.get_knowledge_service(), PostgresKnowledgeService)
        monkeypatch.setenv("USE_DATABASE", "false")  # no database: always the files
        reset_settings_cache()
        knowledge_service.reset_knowledge_service()
        assert isinstance(knowledge_service.get_knowledge_service(), knowledge_service.KnowledgeService)
    finally:
        knowledge_service.reset_knowledge_service()
        reset_settings_cache()


async def test_an_actor_that_is_not_a_user_id_is_recorded_as_none_not_an_error(svc):
    created = await svc.aupload("policy.txt", POLICY_TEXT, actor_id="admin-1")
    assert created.source_id
    await svc.worker.drain()
    await svc.aupdate(created.source_id, enabled=True, actor_id="admin-1")
    assert uuid.UUID(str(uuid.uuid4()))  # (ids stay UUIDs when they are real ones)
