"""Retrieving approved artifacts, resolving their citations, and reusing them in generation."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.artifacts.retrieval import ArtifactMemory
from app.artifacts.reuse import ArtifactReuse
from app.artifacts.service import ArtifactService
from app.core.config import get_settings, reset_settings_cache
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.session import get_session_factory
from tests.artifact_helpers import wipe_artifact_memory
from app.kb.service import KbService
from app.kb.worker import KbWorker
from app.schemas.blocks import BlockType
from app.schemas.research import ChapterResearch
from app.services.course_service import CourseService
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg import PostgresKnowledgeBackend
from tests.test_artifact_extract import block, document

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")

REVIEWER = uuid.uuid4()
QUERY = "refund eligibility for returned products"


def chapter_doc(title: str, body: str, *, tag: str, extra: list | None = None):
    """A one-chapter course. `tag` keeps block ids stable per course across calls."""
    def fixed(id_, *args, **kwargs):
        return block(*args, **kwargs).model_copy(update={"id": f"{tag}_{id_}"})

    return document(
        fixed("h", BlockType.HEADING, {"text": title, "level": 1}, chapter_number=1),
        fixed("p", BlockType.PARAGRAPH, {"text": body}),
        *(extra or []),
    )


@pytest.fixture
async def env(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    await wipe_artifact_memory()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
        await session.execute(
            text("INSERT INTO users (id, email, password_hash, role, is_active, is_verified, created_at, updated_at) VALUES (:i, :e, 'x', 'editor_reviewer', true, true, now(), now()) ON CONFLICT (id) DO NOTHING"),
            {"i": REVIEWER, "e": f"sme-{REVIEWER}@example.com"},
        )
    settings = get_settings()

    class Env:
        pass

    e = Env()
    e.settings = settings
    e.service = ArtifactService(settings=settings)
    e.memory = ArtifactMemory(settings, log_retrievals=False)
    yield e
    await wipe_artifact_memory()  # approved artifacts are reused by every course: leave none behind


async def _approve(env, course_id: str, doc, title="Refund Handling"):
    await env.service.sync_course(course_id=course_id, course_title=title, document=doc)
    await env.service.approve_course(course_id, REVIEWER)


async def _sql(sql, **params):
    async with get_session_factory()() as session, session.begin():
        result = await session.execute(text(sql), params)
        return result.all() if result.returns_rows else []


REFUNDS = "Refunds are issued within fourteen days when the product is returned unused. Eligibility requires the original packaging."
PASTA = "Boil the pasta for ten minutes in plenty of salted water, then drain it well before serving it hot."


# --- what retrieval offers -----------------------------------------------------------------------------------


async def test_only_approved_active_artifacts_are_found(env):
    await env.service.sync_course(course_id="c_pending", course_title="Pending", document=chapter_doc("Refunds", REFUNDS, tag="a"))
    assert await env.memory.search(QUERY) == [] and not await env.memory.has_artifacts()  # nothing approved yet

    await env.service.sync_course(course_id="c_back", course_title="Sent back", document=chapter_doc("Refunds again", REFUNDS, tag="b"))
    await env.service.request_changes_course("c_back", REVIEWER, "wrong")
    assert await env.memory.search(QUERY) == []  # sent back: not trusted

    await _approve(env, "c_ok", chapter_doc("Refund rules", REFUNDS, tag="c"))
    found = await env.memory.search(QUERY)
    assert [e.title for e in found] == ["Refund rules"] and found[0].ref.startswith("A") and found[0].artifact_type == "chapter"
    assert "fourteen days" in found[0].text and found[0].course_title == "Refund Handling"
    assert await env.memory.has_artifacts()


async def test_filters_by_type_course_and_by_excluding_the_course_being_written(env):
    quiz = block(BlockType.QUIZ, {"title": "Refund quiz", "questions": [{"question": "Which returned products have refund eligibility?", "answer": "unused products"}]}).model_copy(update={"id": "x_q"})
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a", extra=[quiz]))
    await _approve(env, "course_b", chapter_doc("Returns handling", "Returned products are inspected before a refund is paid within fourteen days.", tag="b"), "Returns")

    both = await env.memory.search(QUERY, limit=10)
    assert {e.artifact_type for e in both} == {"chapter", "questions"}
    assert [e.artifact_type for e in await env.memory.search(QUERY, types=("questions",))] == ["questions"]
    assert {e.title for e in await env.memory.search(QUERY, course_id="course_b")} == {"Returns handling"}
    assert "Refund rules" not in {e.title for e in await env.memory.search(QUERY, exclude_course_id="course_a", limit=10)}
    assert await env.memory.search(QUERY, course_id="no_such_course") == []
    assert await env.memory.search(PASTA) == []  # nothing about pasta: nothing offered


async def test_a_new_approved_version_replaces_the_old_in_retrieval_but_the_old_citation_still_resolves(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    (first,) = await env.memory.search(QUERY)
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS.replace("fourteen", "thirty"), tag="a"))
    (second,) = await env.memory.search(QUERY)  # one result: never two versions of the same artifact
    assert (first.ref.split(".")[0], second.ref) == (second.ref.split(".")[0], second.ref.split(".")[0] + ".2") and "thirty" in second.text

    old = await env.memory.resolve(first.ref)
    assert old["version_no"] == 1 and old["superseded"] is True and old["is_active"] is False
    assert "fourteen" in str(old["content"]) and old["approved_at"] is not None and old["approved_by"] == REVIEWER
    current = await env.memory.resolve(second.ref)
    assert current["superseded"] is False and current["is_active"] is True and current["ref"] == second.ref


async def test_a_pending_version_is_invisible_to_retrieval_and_to_citations_unless_an_admin_asks(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    await env.service.sync_course(course_id="course_a", course_title="Refund Handling", document=chapter_doc("Refund rules", REFUNDS + " Extra.", tag="a"))
    found = await env.memory.search(QUERY)
    assert len(found) == 1 and found[0].ref.endswith(".1") and "Extra" not in found[0].text
    pending_ref = found[0].ref.rsplit(".", 1)[0] + ".2"
    with pytest.raises(NotFoundError):
        await env.memory.resolve(pending_ref)
    assert (await env.memory.resolve(pending_ref, include_unapproved=True))["review_status"] == "pending_review"
    for bad, error in (("nonsense", ValidationFailedError), ("A999.1", NotFoundError)):
        with pytest.raises(error):
            await env.memory.resolve(bad)


async def test_archived_artifacts_are_not_offered(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    artifact = (await _sql("SELECT id FROM am_artifacts"))[0].id
    await env.service.set_archived(artifact, True, actor_id=REVIEWER)
    assert await env.memory.search(QUERY) == [] and not await env.memory.has_artifacts()
    await env.service.set_archived(artifact, False, actor_id=REVIEWER)
    assert len(await env.memory.search(QUERY)) == 1


async def test_near_copies_are_offered_once_and_long_artifacts_are_cut_to_what_is_relevant(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    await _approve(env, "course_b", chapter_doc("Refund rules copy", REFUNDS, tag="b"), "Copy")
    assert len(await env.memory.search(QUERY, limit=5)) == 1  # the same teaching twice is one result

    filler = " ".join(f"Filler sentence {i} says nothing useful at all." for i in range(60))
    await _approve(env, "course_c", chapter_doc("Refund essay", f"{filler} Refund eligibility depends on the original packaging. {filler}", tag="c"), "Essay")
    long = next(e for e in await env.memory.search("refund eligibility original packaging", limit=5) if e.title == "Refund essay")
    assert len(long.text) <= env.settings.artifact_excerpt_chars and "original packaging" in long.text


async def test_retrievals_are_recorded_for_provenance_and_never_fail_the_search(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    logging_memory = ArtifactMemory(env.settings)
    offered = await logging_memory.search(QUERY, trace=("course_new", "ch_1"))
    rows = await _sql("SELECT course_id, chapter_id, version_id FROM am_retrieval_log")
    assert [(r.course_id, r.chapter_id) for r in rows] == [("course_new", "ch_1")] and len(offered) == 1


# --- reuse in generation: two memories, side by side, never merged ------------------------------------------------------


async def _published_knowledge(env, body: str):
    kb = KbService(settings=env.settings)
    ref = await kb.create_item(actor_id=None, files=[("policy.txt", f"POLICY\n\n{body}\n".encode())], allow_duplicate=True)
    await KbWorker(settings=env.settings).drain()
    await kb.publish_item(ref.id, actor_id=None)
    return kb, ref


async def test_an_artifact_brings_the_knowledge_it_was_written_from_only_while_that_is_published(env):
    kb, item = await _published_knowledge(env, "Refunds are issued within fourteen days when the product is returned unused.")
    cite = f"G{item.number}.1"
    await _approve(env, "course_a", chapter_doc("Refund rules", f"Refunds take fourteen days for eligible products [{cite}].", tag="a"))
    knowledge = PostgresKnowledgeBackend(env.settings, embeddings=get_embedding_service(), log_retrievals=False)
    reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))

    offer = await reuse.offer(QUERY, course_id="course_new", chapter_id="ch_1", knowledge=knowledge, already_offered=set())
    assert [a.title for a in offer.artifacts] == ["Refund rules"] and offer.artifacts[0].source_refs == [cite]
    assert [p.ref for p in offer.linked_knowledge] == [cite]  # through Memory 1's own channel: it carries a [G..] citation

    already = await reuse.offer(QUERY, course_id="course_new", chapter_id="ch_1", knowledge=knowledge, already_offered={cite})
    assert already.linked_knowledge == []  # a search already offered it: not twice

    await kb.set_lifecycle(item.id, "unpublish", actor_id=None)  # Memory 1 withdraws it
    after = await reuse.offer(QUERY, course_id="course_new", chapter_id="ch_1", knowledge=knowledge, already_offered=set())
    assert [a.title for a in after.artifacts] == ["Refund rules"] and after.linked_knowledge == []  # the artifact's age never revives it


async def test_a_course_is_never_offered_its_own_artifacts_and_an_empty_memory_offers_nothing(env):
    reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))
    assert not await reuse.available()
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    assert await reuse.available()
    own = await reuse.offer(QUERY, course_id="course_a", chapter_id="ch_1", knowledge=None, already_offered=set())
    assert own.artifacts == [] and own.linked_knowledge == []
    off = ArtifactReuse(env.settings.model_copy(update={"enable_artifact_reuse": False}), ArtifactMemory(env.settings))
    assert not await off.available()


async def test_course_generation_gets_artifacts_within_the_knowledge_budget_and_records_where_they_came_from(env):
    kb, item = await _published_knowledge(env, "Refunds are issued within fourteen days when the product is returned unused. " + "More policy detail here. " * 30)
    cite = f"G{item.number}.1"
    await _approve(env, "course_a", chapter_doc("Refund rules", f"Refunds take fourteen days for eligible products [{cite}].", tag="a"))

    service = CourseService(settings=env.settings, use_db=True)
    record = SimpleNamespace(course_id="course_new", chapters=[SimpleNamespace(chapter_id="ch_1", sources_checked=False, sources_offered=[])])
    chapter = SimpleNamespace(id="ch_1")
    knowledge = PostgresKnowledgeBackend(env.settings, embeddings=get_embedding_service(), log_retrievals=False)
    from app.artifacts import reuse as reuse_module

    reuse_module._reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))
    try:
        artifacts, merged = await service._reusable_artifacts(record, chapter, QUERY, knowledge, [])  # noqa: SLF001
    finally:
        reuse_module.reset_artifact_reuse()
    assert [a.title for a in artifacts] == ["Refund rules"] and [p.ref for p in merged] == [cite]

    tiny = CourseService(settings=env.settings.model_copy(update={"knowledge_excerpt_chars": 500}), use_db=True)
    reuse_module._reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))
    try:
        _, none_fit = await tiny._reusable_artifacts(record, chapter, QUERY, knowledge, [])  # noqa: SLF001
    finally:
        reuse_module.reset_artifact_reuse()
    assert none_fit == []  # the linked passage does not fit the knowledge budget, so it is left out

    CourseService._record_sources(record, "ch_1", [], merged, artifacts)  # noqa: SLF001
    used = {(u.origin, u.source_id): u for u in record.chapters[0].sources_offered}
    assert used[("artifact", artifacts[0].artifact_id)].refs == [artifacts[0].ref] and used[("knowledge", item.public_id)].refs == [cite]

    research = ChapterResearch(chapter_id="ch_1")
    research.knowledge_excerpts, research.artifact_excerpts = merged, artifacts
    context = research.compact_context()
    assert "APPROVED PRIOR MATERIAL" in context and f"[{artifacts[0].ref}]" in context and "KNOWLEDGE LIBRARY EXCERPTS" in context
    assert context.index("KNOWLEDGE LIBRARY EXCERPTS") < context.index("APPROVED PRIOR MATERIAL")


# --- following the existing review workflow ------------------------------------------------------------------------------------


async def test_the_review_workflow_drives_artifact_memory_without_ever_blocking_it(env, monkeypatch):
    from app.artifacts import service as artifact_service_module
    from app.services import document_service

    doc = chapter_doc("Refund rules", REFUNDS, tag="a")

    class Documents:
        async def load(self, document_id):
            return doc

    monkeypatch.setattr(document_service, "get_document_service", lambda: Documents())
    monkeypatch.setattr(artifact_service_module, "_service", ArtifactService(settings=env.settings))
    course = CourseService(settings=env.settings, use_db=True)
    record = SimpleNamespace(
        course_id="course_flow", document_id="doc_1", template_id="t", run=SimpleNamespace(job_id="job_7"), chapters=[],
        input=SimpleNamespace(course_title="Refund Handling"),
    )

    await course._artifact_memory("submit", record, str(REVIEWER))  # noqa: SLF001
    state = await _sql("SELECT v.review_status, v.generation_run_id, v.model_info FROM am_artifact_versions v")
    assert [(s.review_status, s.generation_run_id) for s in state] == [("pending_review", "job_7")] and state[0].model_info["template_id"] == "t"

    await course._artifact_memory("request_changes", record, str(REVIEWER), "fix it")  # noqa: SLF001
    assert (await _sql("SELECT review_status, review_comment FROM am_artifact_versions"))[0].review_status == "changes_requested"

    doc.pages[0].blocks[1].content["text"] = REFUNDS + " A reviewer's edit while under review."
    await course._artifact_memory("submit", record, str(REVIEWER))  # noqa: SLF001
    await course._artifact_memory("approve", record, str(REVIEWER), "Approved")  # noqa: SLF001
    row = (await _sql("SELECT review_status, locked, is_active, search_text FROM am_artifact_versions"))[0]
    assert (row.review_status, row.locked, row.is_active) == ("approved", True, True) and "reviewer's edit" in row.search_text  # the edit is what was approved

    # A failure inside artifact memory never reaches the workflow.
    class Broken:
        async def load(self, document_id):
            raise RuntimeError("document store down")

    monkeypatch.setattr(document_service, "get_document_service", lambda: Broken())
    await course._artifact_memory("submit", record, str(REVIEWER))  # noqa: SLF001  (must not raise)
    quiet = CourseService(settings=env.settings.model_copy(update={"enable_artifact_memory": False}), use_db=True)
    await quiet._artifact_memory("approve", record, str(REVIEWER))  # noqa: SLF001  (switched off: nothing happens)
    assert (await env.service.check_invariants()) == []


# --- the real generation path ---------------------------------------------------------------------------------------------


def _record_and_chapter(course_id: str):
    from app.schemas.blueprint import BlueprintChapter
    from app.schemas.course import ChapterProgress, CourseInput, CourseRecord, TocItem

    course_input = CourseInput(course_title="Returns Training", toc=[TocItem(title="Refund eligibility")], target_audience="Support staff", template="technical")
    record = CourseRecord(
        course_id=course_id, document_id=f"doc_{course_id}", input=course_input, template_id="technical_v1", created_at="2026-11-04T00:00:00Z",
        updated_at="2026-11-04T00:00:00Z", chapters=[ChapterProgress(chapter_id="ch_1", title="Refund eligibility")],
    )
    chapter = BlueprintChapter(id="ch_1", title="Refund eligibility", objective="Explain who is eligible for a refund of returned products", key_concepts=["refund", "eligibility", "returned products"])
    return record, chapter


async def test_chapter_writing_is_given_approved_artifacts_even_with_no_knowledge_or_uploads(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    service = CourseService(settings=env.settings, use_db=True)
    record, chapter = _record_and_chapter("course_new")
    from app.artifacts import reuse as reuse_module

    reuse_module._reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))
    try:
        research, warnings = await service._attach_source_excerpts(record, [chapter], {})  # noqa: SLF001
    finally:
        reuse_module.reset_artifact_reuse()
    attached = research["ch_1"]
    assert [a.title for a in attached.artifact_excerpts] == ["Refund rules"] and attached.source_excerpts == [] and attached.knowledge_excerpts == []
    assert "APPROVED PRIOR MATERIAL" in attached.compact_context()
    offered = record.chapters[0].sources_offered
    assert [(u.origin, u.filename, u.refs) for u in offered] == [("artifact", "Refund rules", [attached.artifact_excerpts[0].ref])]
    assert record.chapters[0].sources_checked is True and warnings == []


async def test_a_course_is_not_offered_its_own_approved_work_and_with_nothing_to_offer_nothing_changes(env):
    await _approve(env, "course_same", chapter_doc("Refund rules", REFUNDS, tag="a"))
    service = CourseService(settings=env.settings, use_db=True)
    record, chapter = _record_and_chapter("course_same")
    from app.artifacts import reuse as reuse_module

    reuse_module._reuse = ArtifactReuse(env.settings, ArtifactMemory(env.settings, log_retrievals=False))
    try:
        research, _ = await service._attach_source_excerpts(record, [chapter], {})  # noqa: SLF001
    finally:
        reuse_module.reset_artifact_reuse()
    assert research == {} and record.chapters[0].sources_offered == []


async def test_reuse_switched_off_leaves_generation_exactly_as_it_was(env):
    await _approve(env, "course_a", chapter_doc("Refund rules", REFUNDS, tag="a"))
    service = CourseService(settings=env.settings.model_copy(update={"enable_artifact_reuse": False}), use_db=True)
    record, chapter = _record_and_chapter("course_new")
    from app.artifacts import reuse as reuse_module

    reuse_module._reuse = ArtifactReuse(env.settings.model_copy(update={"enable_artifact_reuse": False}), ArtifactMemory(env.settings, log_retrievals=False))
    try:
        research, _ = await service._attach_source_excerpts(record, [chapter], {})  # noqa: SLF001
    finally:
        reuse_module.reset_artifact_reuse()
    assert research == {}


async def test_an_unreachable_database_means_no_reuse_never_a_failed_chapter(env, monkeypatch):
    async def down():
        raise ConnectionError("database unreachable")

    memory = ArtifactMemory(env.settings, log_retrievals=False)
    monkeypatch.setattr(memory, "has_artifacts", down)
    assert await ArtifactReuse(env.settings, memory).available() is False


async def test_something_said_late_in_a_long_chapter_is_still_found(env):
    filler = " ".join(f"Filler sentence {i} says nothing useful at all." for i in range(400))  # ~19,000 characters
    await _approve(env, "course_long", chapter_doc("Refund essay", f"{filler} Refund eligibility depends on the original packaging. {filler}", tag="long"), "Essay")
    found = await env.memory.search("refund eligibility original packaging")
    assert [e.title for e in found] == ["Refund essay"] and "original packaging" in found[0].text
