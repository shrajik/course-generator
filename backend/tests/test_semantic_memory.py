"""Hybrid (keyword + semantic/pgvector) memory retrieval, against a real
Postgres with the `vector` extension.

Same convention as test_memory_layer.py: gated on TEST_DATABASE_URL, so the
default test run (no test database configured) never touches a real
database. Run against a disposable Postgres with pgvector - see
docker-compose.yml's `postgres-test` service - never the shared dev database.

The offline mock embedding (see mock_ai._fake_embedding) is a lexical
"hashing trick", not a real model: it captures word overlap, not synonymy.
Tests below that need "differently worded, same meaning" use pairs that
share vocabulary but not the exact query substring (so the keyword/ILIKE
path provably can't find them) and use a lowered similarity threshold
appropriate for this weaker offline signal, documented at each call site -
the production default (0.75) is tuned for a real embedding model, not this
mock.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.core.config import get_settings, reset_settings_cache
from app.core.ids import new_suffix
from app.course.templates.registry import clear_cache, load_template
from app.db.models import Course
from app.db.repositories.course_samples import CourseSampleRepository
from app.db.repositories.courses import CourseRepository
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.repositories.visual_knowledge import VisualKnowledgeRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    MAX_SAMPLE_NOTES,
    CourseSampleCreateRequest,
    CourseSampleUpdateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
    VisualKnowledgeCreateRequest,
    VisualKnowledgeUpdateRequest,
)
from app.services.course_sample_service import CourseSampleService
from app.services.course_service import CourseService
from app.services.embedding_service import EmbeddingService, reset_embedding_service
from app.services.hybrid_retriever import HybridRetriever
from app.services.memory_service import MemoryService
from app.services.storage_service import get_storage
from app.services.template_service import TemplateService
from app.services.visual_knowledge_service import VisualKnowledgeService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
# Appended to every hardcoded id/name this file uses, so re-running pytest
# against the same (never-dropped, see db_schema in conftest.py) test
# database twice doesn't collide with the previous run's rows.
_RUN = new_suffix()

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture(autouse=True)
def database_schema(db_schema, monkeypatch):
    """Schema lifecycle lives in conftest.py's session-scoped db_schema
    fixture. Scopes DATABASE_URL/USE_DATABASE to this test only (monkeypatch
    reverts them automatically), so they don't leak into unrelated offline
    tests, and keeps the per-test embedding-service/template-registry cache
    resets."""
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    reset_embedding_service()
    clear_cache()
    yield
    reset_embedding_service()
    clear_cache()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_course(course_id: str, title: str = "Sample Course") -> Course:
    course_id = f"{course_id}_{_RUN}"
    course = Course(
        course_id=course_id,
        document_id=f"doc_{course_id}",
        title=title,
        status="ready",
        template_id="technical_v1",
        # A full, valid CourseInput shape - this row is never dropped between
        # tests any more (see db_schema in conftest.py), so any other DB-gated
        # test that lists every course in the table (e.g. an admin "list all
        # courses" endpoint) must still be able to CourseInput.model_validate()
        # this row's input_json.
        input_json={
            "course_title": title,
            "toc": [{"title": "Chapter 1"}],
            "target_audience": "Testers",
            "template": "technical",
        },
        metadata_json={},
        created_at=_now(),
        updated_at=_now(),
    )
    async with get_session_factory()() as session:
        async with session.begin():
            await CourseRepository(session).create(course)
    return course


async def _get_embedding(source_type: str, source_id):
    async with get_session_factory()() as session:
        return await MemoryEmbeddingRepository(session).get(source_type=source_type, source_id=source_id)


# ---------------------------------------------------------------------------
# embedding generation + persistence
# ---------------------------------------------------------------------------


async def test_embedding_is_generated_and_persisted_on_course_sample_register():
    settings = get_settings()
    course = await _make_course("crs_emb1", title="Intro to Vector Databases")
    entry = await CourseSampleService().register(
        CourseSampleCreateRequest(course_id=course.course_id, topic="vector databases")
    )

    row = await _get_embedding("course_sample", entry.id)

    assert row is not None
    assert row.model == settings.embedding_model
    assert len(row.embedding) == settings.embedding_dimensions
    assert row.content_hash


async def test_disabling_embeddings_creates_no_row_and_keyword_path_still_works(monkeypatch):
    # The lifecycle hooks (sync_embedding) read the *global* settings
    # singleton via get_embedding_service(), so disabling the flag for real
    # (env var + cache reset) rather than a local model_copy is what actually
    # stops the embedding call.
    monkeypatch.setenv("ENABLE_EMBEDDINGS", "false")
    reset_settings_cache()
    reset_embedding_service()
    course = await _make_course("crs_emb2", title="Negotiation Basics")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(course_id=course.course_id, topic="Negotiation")
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    assert await _get_embedding("course_sample", entry.id) is None

    memory = MemoryService(samples=sample_service, settings=get_settings())
    context = await memory.build_context(stage="writer", course_title="Negotiation Basics")
    assert any("Negotiation" in note for note in context.sample_notes)


async def test_semantic_retrieval_surfaces_a_differently_worded_match():
    """A sample that shares vocabulary with the query but not its exact
    phrasing: ILIKE can't find it (proven below), but the semantic path
    does. Threshold lowered for the mock's weaker lexical-overlap signal -
    see module docstring."""
    settings = get_settings().model_copy(update={"semantic_similarity_threshold": 0.15})
    course = await _make_course("crs_emb3", title="Faraday coil course")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(
            course_id=course.course_id,
            title="Faraday's Law and Electromagnetic Induction Basics",
            description="covers electromagnetic induction concepts using coils and magnets",
        )
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    query = "Understanding Electromagnetic Induction in Coils"

    # Keyword path alone genuinely finds nothing for this query - the ILIKE
    # filter matches the *whole* query string as one substring.
    keyword_only, _ = await sample_service.list(search=query, approved_only=True)
    assert keyword_only == []

    memory = MemoryService(samples=sample_service, settings=settings)
    context = await memory.build_context(stage="writer", course_title=query)
    assert any("Faraday" in note for note in context.sample_notes)


async def test_semantic_similarity_threshold_rejects_weak_matches():
    settings = get_settings().model_copy(update={"semantic_similarity_threshold": 0.95})
    course = await _make_course("crs_emb4", title="Faraday coil course 2")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(
            course_id=course.course_id,
            title="Faraday's Law and Electromagnetic Induction Basics",
            description="covers electromagnetic induction concepts using coils and magnets",
        )
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    async with get_session_factory()() as session:
        hits = await MemoryEmbeddingRepository(session).search(
            source_type="course_sample",
            query_vector=await EmbeddingService(settings=settings).embed_one(
                "Understanding Electromagnetic Induction in Coils"
            ),
            limit=10,
            threshold=settings.semantic_similarity_threshold,
        )
    assert hits == []


async def test_hybrid_retrieval_does_not_let_one_method_hide_the_others_results():
    """One sample only a keyword search would find, one only semantic search
    would find - both must appear; neither method is allowed to crowd the
    other out just by being fetched first.

    Uses a `_RUN`-unique token (not just `_RUN`-suffixed ids) in the shared
    vocabulary: with the schema no longer wiped between tests (see db_schema
    in conftest.py), the course_sample table accumulates rows across every
    past run against this database, and MAX_SAMPLE_NOTES=2 leaves no slack -
    a reused, common word like "xylophone" could let older runs' rows
    outrank this run's semantic-only entry for the top-2 slots. A token
    unique to this run can only ever match this run's own two rows.
    """
    settings = get_settings().model_copy(update={"semantic_similarity_threshold": 0.15})
    keyword_course = await _make_course("crs_hybrid_kw", title="Keyword Match Course")
    semantic_course = await _make_course("crs_hybrid_sem", title="Semantic Match Course")
    sample_service = CourseSampleService()
    tag = f"zyx{_RUN}"

    keyword_entry = await sample_service.register(
        CourseSampleCreateRequest(
            course_id=keyword_course.course_id,
            title=f"Rare {tag} Tuning Query Phrase",
            description="literally contains the query phrase",
        )
    )
    await sample_service.update(keyword_entry.id, CourseSampleUpdateRequest(approved=True))

    semantic_entry = await sample_service.register(
        CourseSampleCreateRequest(
            course_id=semantic_course.course_id,
            title=f"{tag} tuning for percussion instrument technicians",
            description="woodwork and mallet percussion tuning guidance",
        )
    )
    await sample_service.update(semantic_entry.id, CourseSampleUpdateRequest(approved=True))

    query = f"Rare {tag} Tuning Query Phrase"
    memory = MemoryService(samples=sample_service, settings=settings)
    context = await memory.build_context(stage="writer", course_title=query)

    sources = set(context.sources)
    assert f"course_sample:{keyword_entry.id}" in sources
    assert f"course_sample:{semantic_entry.id}" in sources


async def test_records_without_embeddings_remain_retrievable_via_keyword(monkeypatch):
    """Simulates a row created before this feature (or while embeddings were
    failing): no memory_embeddings row, but keyword retrieval must not care."""
    monkeypatch.setenv("ENABLE_EMBEDDINGS", "false")
    reset_settings_cache()
    reset_embedding_service()
    course = await _make_course("crs_emb5", title="Legacy Sample No Embedding")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(course_id=course.course_id, topic="legacy")
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))
    assert await _get_embedding("course_sample", entry.id) is None

    # Embeddings re-enabled for retrieval, but this record still has none.
    monkeypatch.setenv("ENABLE_EMBEDDINGS", "true")
    reset_settings_cache()
    reset_embedding_service()
    memory = MemoryService(samples=sample_service, settings=get_settings())
    context = await memory.build_context(stage="writer", course_title="Legacy Sample No Embedding")
    assert any("Legacy Sample" in note for note in context.sample_notes)


async def test_embedding_failure_falls_back_to_keyword_without_breaking_build_context(monkeypatch):
    course = await _make_course("crs_emb6", title="Resilience Test Course")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(course_id=course.course_id, topic="resilience")
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    async def boom(self, query):
        raise RuntimeError("embedding provider on fire")

    monkeypatch.setattr(HybridRetriever, "_embed_query", boom)

    memory = MemoryService(samples=sample_service, settings=get_settings())
    context = await memory.build_context(stage="writer", course_title="Resilience Test Course")
    assert any("Resilience" in note for note in context.sample_notes)


async def test_memory_context_bounds_still_respected_with_hybrid_retrieval():
    for i in range(MAX_SAMPLE_NOTES + 3):
        course = await _make_course(f"crs_emb_bound{i}", title=f"Bounded Topic {i}")
        sample_service = CourseSampleService()
        entry = await sample_service.register(
            CourseSampleCreateRequest(course_id=course.course_id, topic="Bounded Topic")
        )
        await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    memory = MemoryService()
    context = await memory.build_context(stage="writer", course_title="Bounded Topic")
    assert len(context.sample_notes) <= MAX_SAMPLE_NOTES


# ---------------------------------------------------------------------------
# templates: versioned, independent embeddings
# ---------------------------------------------------------------------------


async def test_template_versions_get_independent_embeddings():
    base = load_template("technical")
    service = TemplateService()
    v1 = await service.create(
        TemplateCreateRequest(name=f"Embed Versioned {_RUN}", kind="technical", description="v1", config=base)
    )
    v1_embedding = await _get_embedding("template", v1.id)
    assert v1_embedding is not None
    v1_hash = v1_embedding.content_hash

    v2 = await service.update(v1.slug, TemplateUpdateRequest(description="v2 completely different text"))
    v2_embedding = await _get_embedding("template", v2.id)
    assert v2_embedding is not None
    assert v2.id != v1.id

    # v1's row (and its embedding) is untouched by the v2 update.
    v1_embedding_after = await _get_embedding("template", v1.id)
    assert v1_embedding_after is not None
    assert v1_embedding_after.content_hash == v1_hash
    assert load_template(v1.template_id).description == "v1"

    # Archive so this test-only template stops showing up in the picker for
    # the rest of the session (the schema is never dropped between tests
    # any more - see db_schema in conftest.py - unlike course/sample rows,
    # non-archived templates are visible via available_templates()).
    await service.archive(v1.slug)


async def test_archiving_a_template_preserves_its_embedding():
    base = load_template("technical")
    service = TemplateService()
    created = await service.create(
        TemplateCreateRequest(name=f"Archive Embed {_RUN}", kind="technical", description="", config=base)
    )
    assert await _get_embedding("template", created.id) is not None

    await service.archive(created.slug)

    assert await _get_embedding("template", created.id) is not None


# ---------------------------------------------------------------------------
# generation runs: compact, bounded embedded text
# ---------------------------------------------------------------------------


async def test_generation_run_embedding_is_compact_not_the_raw_payload():
    """The embedding lifecycle hook lives in CourseService._record_generation_run
    (it needs the course title alongside the run), not in the lower-level
    DatabaseService.record_generation_run used directly by
    test_generation_run_history_keeps_every_attempt in test_memory_layer.py -
    so this goes through the real production call path."""
    course = await _make_course("crs_emb_run", title="Long Course For Embedding")
    long_chapter_list = [f"Chapter {i}: {'x' * 50}" for i in range(50)]
    # job_id is globally unique (not scoped per course) - suffix it like
    # everything else so a stale row from a previous run can't get reused.
    job_id = f"job_embed_1_{_RUN}"

    service = CourseService()
    await service._record_generation_run(
        course.course_id, job_id, "done", generated=long_chapter_list, failed=[]
    )

    async with get_session_factory()() as session:
        from app.db.repositories.generation_runs import GenerationRunRepository

        run = await GenerationRunRepository(session).get_by_job_id(job_id)
    row = await _get_embedding("generation_run", run.id)

    assert row is not None
    # Not the raw 50-chapter payload - only a handful of chapter titles at most.
    assert len(row.embedded_text) < len("".join(long_chapter_list))
    assert row.embedded_text.count("Chapter") <= 5


# ---------------------------------------------------------------------------
# visual knowledge: delete removes the embedding too
# ---------------------------------------------------------------------------


async def test_deleting_visual_knowledge_deletes_its_embedding():
    course = await _make_course("crs_emb_vk", title="Visual Embedding Course")
    storage = get_storage()
    storage.save_asset(course.course_id, b"<svg></svg>", extension="svg")
    service = VisualKnowledgeService(storage)
    entry = await service.register(
        VisualKnowledgeCreateRequest(
            course_id=course.course_id,
            asset_path="assets/image_001.svg",
            topic="embedding lifecycle",
            description="a diagram",
        )
    )
    assert await _get_embedding("visual_knowledge", entry.id) is not None

    await service.delete(entry.id)

    assert await _get_embedding("visual_knowledge", entry.id) is None
