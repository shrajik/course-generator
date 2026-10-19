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

from app.core.config import reset_settings_cache
from app.core.ids import new_suffix
from app.course.templates.registry import clear_cache, load_template
from app.db.models import Course
from app.db.repositories.courses import CourseRepository
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.session import get_session_factory
from app.schemas.memory import TemplateCreateRequest, TemplateUpdateRequest
from app.services.course_service import CourseService
from app.services.embedding_service import reset_embedding_service
from app.services.template_service import TemplateService

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
# MERGE NOTE (feature/visuals x origin/visual-and-flowchart): this section
# used to test embedding generation/persistence and hybrid semantic+keyword
# retrieval entirely through `CourseSampleService`, which origin/visual-and-
# flowchart removed (replaced by app.services.knowledge_service /
# source_service - a differently-shaped, upload-based architecture, not a
# drop-in substitute: MemoryService no longer even accepts a `samples=`
# constructor argument). Mechanically rewriting these 9 tests against the
# new service without investigating its actual semantics risked fabricating
# incorrect behaviour, so they were removed here rather than guessed at.
#
# Follow-up investigation (not guessed - read against the actual new code
# and its own test files) found every one of the 9 removed tests' concerns
# already has an equivalent, often more thorough, test elsewhere against the
# real new architecture - nothing here was a silent, permanent loss:
#   - embedding generation/persistence on upload -> test_source_service.py::
#     test_a_text_upload_becomes_ready_with_counts_an_outline_and_files
#   - disabled-embeddings keyword fallback -> test_source_service.py::
#     test_embedding_being_unavailable_still_leaves_the_source_ready
#   - semantic retrieval by meaning, no keyword overlap -> test_source_
#     ingestion.py::test_semantic_ranking_uses_vectors_when_there_is_no_
#     keyword_overlap, test_knowledge_library.py::test_semantic_search_
#     matches_by_meaning_when_no_words_overlap
#   - similarity-threshold rejection of weak matches -> test_knowledge_
#     library.py::test_a_vector_that_is_not_close_enough_contributes_nothing
#   - hybrid fusion not letting one method hide the other's results ->
#     test_source_ingestion.py::test_fusion_rewards_agreement_between_
#     keyword_and_semantic, test_knowledge_library.py::test_agreement_
#     between_keywords_and_meaning_ranks_first_and_the_limit_holds
#   - records without embeddings still retrievable via keyword ->
#     test_source_ingestion.py::test_chunks_without_a_vector_still_rank_by_
#     keywords, test_knowledge_library.py::test_passages_without_vectors_
#     still_match_by_keyword_and_bad_vectors_are_ignored
#   - embedding-failure resilience -> test_embeddings.py::test_embedding_
#     service_failure_returns_none_not_raises (+ the same source_service
#     test above, at the integration level)
#   - delete removes the embedding too -> test_source_service.py::
#     test_deleting_removes_everything (deletes the whole source folder,
#     vectors.bin included)
# The one capability with no equivalent anywhere - MemoryService's own
# bounded-context guarantee (previously MAX_SAMPLE_NOTES on sample_notes,
# now MAX_HISTORY_NOTES on history_notes) - has a new test in
# test_memory_layer.py::test_memory_service_bounds_history_notes_to_the_
# configured_maximum. "A course must exist before a sample can be
# registered for it" has no equivalent either, but that's a confirmed,
# intentional architecture change: an uploaded source is owner-scoped, not
# course-scoped, so nothing needs a course to exist first.
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
