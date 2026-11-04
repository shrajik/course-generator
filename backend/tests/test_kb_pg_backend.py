"""The PostgreSQL knowledge backend against a real database.

Skipped unless TEST_DATABASE_URL points at a PostgreSQL with pgvector (the whole
module needs it). It imports the golden corpus with the real import command and then
asks the backend the golden questions: whatever the file library answers, the database
must answer, and nothing that is not live may ever be returned.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.core.config import get_settings, reset_settings_cache
from app.db.session import get_session_factory
from app.scripts import kb_import
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg import PostgresKnowledgeBackend
from app.services.knowledge_service import get_knowledge_service
from app.services.source_service import get_source_service
from tests import golden_knowledge as golden

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
async def backend(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    factory = get_session_factory()
    async with factory() as session, session.begin():
        # Retrieval is corpus-wide, so these tests need the knowledge tables to themselves.
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    files = get_knowledge_service()
    for name, data in golden.CORPUS:
        record = files.upload(name, data)
        await get_source_service().wait(record.source_id)
    docs, _ = kb_import.load_legacy(get_source_service())
    results = await kb_import.import_documents(factory, docs, kb_import.legacy_counter(get_source_service()), get_settings().embedding_model)
    assert [r.outcome for r in results] == ["imported"] * 3
    return PostgresKnowledgeBackend(settings=get_settings(), embeddings=get_embedding_service())


async def _unpublish(item_number: int) -> None:
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("UPDATE kb_chunks SET is_live = false WHERE item_id = (SELECT id FROM kb_items WHERE number = :n)"), {"n": item_number})
        await session.execute(text("UPDATE kb_items SET lifecycle_status = 'unpublished' WHERE number = :n"), {"n": item_number})
        await session.execute(text("UPDATE kb_epoch SET epoch = epoch + 1"))


async def test_the_golden_questions_get_the_pinned_passage(backend):
    for query, expected in golden.GOLDEN.items():
        refs = [e.ref for e in await backend.asearch(query)]
        assert (refs[0] if refs else None) == expected, query


async def test_every_answer_equals_the_file_librarys(backend):
    files = get_knowledge_service()
    for query in golden.GOLDEN:
        assert [e.ref for e in await backend.asearch(query)] == [e.ref for e in await files.search(query)], query
        assert [(e.source_id, e.filename, e.heading, e.page, e.text) for e in await backend.asearch(query)] == [
            (e.source_id, e.filename, e.heading, e.page, e.text) for e in await files.search(query)
        ]


async def test_an_unpublished_item_is_never_returned_but_its_citations_still_resolve(backend):
    assert [e.ref for e in await backend.asearch("boil pasta and drain it")] == ["G2.1"]
    await _unpublish(2)
    assert await backend.asearch("boil pasta and drain it") == []
    assert "G2.1" not in (await backend.alibrary()).passages
    # An old citation still reaches the text it cited: asked for explicitly, it is included.
    assert "G2.1" in (await backend.alibrary(extra_refs=["G2.1"])).passages
    resolved = await backend.apassages_by_refs(["G2.1", "G9.9", "S1.1"])
    assert [e.ref for e in resolved] == ["G2.1"] and "pasta" in resolved[0].text


async def test_a_purged_item_no_longer_resolves(backend):
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("UPDATE kb_items SET purged_at = now() WHERE number = 3"))
    assert await backend.apassages_by_refs(["G3.1"]) == []


async def test_nothing_is_offered_when_nothing_is_live(backend):
    for number in (1, 2, 3):
        await _unpublish(number)
    assert await backend.ahas_documents() is False
    assert await backend.asearch("refund eligibility for returned products") == []


async def test_retrievals_are_logged_with_who_asked_and_never_fail_the_search(backend):
    hits = await backend.asearch("boil pasta and drain it", trace=("course_a", "ch_1"))
    async with get_session_factory()() as session:
        rows = (await session.execute(text("SELECT item_number, ordinal, course_id, chapter_id, version_id FROM kb_retrieval_log"))).all()
    assert [(r.item_number, r.ordinal, r.course_id, r.chapter_id) for r in rows] == [(2, 1, "course_a", "ch_1")] and rows[0].version_id
    assert len(hits) == 1

    quiet = PostgresKnowledgeBackend(settings=get_settings(), embeddings=get_embedding_service(), log_retrievals=False)
    await quiet.asearch("boil pasta and drain it", trace=("course_b", "ch_1"))
    async with get_session_factory()() as session:
        assert (await session.execute(text("SELECT count(*) FROM kb_retrieval_log"))).scalar() == 1


async def test_the_audit_library_matches_the_file_librarys_live_passages(backend):
    assert set((await backend.alibrary()).passages) == set(get_knowledge_service().library().passages)
