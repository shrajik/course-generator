"""AI memory layer: templates, generation history and MemoryService retrieval,
against a real Postgres.

Same convention as test_database_foundation.py: gated on TEST_DATABASE_URL so
the default test run (no test database configured) never touches a real
database. Run with TEST_DATABASE_URL set to a disposable Postgres to execute
these for real.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import new_suffix
from app.course.templates.registry import clear_cache, load_template, refresh_db_templates
from app.db.models import Course
from app.db.repositories.courses import CourseRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    MAX_HISTORY_NOTES,
    MemoryContext,
    TemplateCreateRequest,
    TemplateUpdateRequest,
)
from app.services.memory_service import MemoryService
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
    tests, and keeps the per-test template-registry cache reset."""
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    clear_cache()
    yield
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


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


async def test_template_editing_never_mutates_a_version_a_course_references():
    base = load_template("technical")
    service = TemplateService()
    created = await service.create(
        TemplateCreateRequest(name=f"Custom Tech {_RUN}", kind="technical", description="v1", config=base)
    )
    assert created.template_id == f"{created.slug}_v1"

    old_id = created.template_id
    updated = await service.update(created.slug, TemplateUpdateRequest(description="v2"))
    assert updated.template_id == f"{created.slug}_v2"
    assert updated.version == 2

    # A course created against v1 keeps resolving v1's exact content forever.
    reloaded_old = load_template(old_id)
    assert reloaded_old.description == "v1"
    reloaded_new = load_template(updated.template_id)
    assert reloaded_new.description == "v2"

    # Archive so this test-only template stops showing up in the picker for
    # the rest of the session (the schema is never dropped between tests
    # any more - see db_schema in conftest.py - unlike course/sample rows,
    # non-archived templates are visible via available_templates()).
    await service.archive(created.slug)


async def test_template_create_rejects_duplicate_slug():
    base = load_template("technical")
    service = TemplateService()
    name = f"Dup {_RUN}"
    created = await service.create(
        TemplateCreateRequest(name=name, kind="technical", description="", config=base)
    )
    with pytest.raises(ConflictError):
        await service.create(
            TemplateCreateRequest(name=name, kind="technical", description="", config=base)
        )
    await service.archive(created.slug)


# ---------------------------------------------------------------------------
# MERGE NOTE (feature/visuals x origin/visual-and-flowchart): this section
# used to test VisualKnowledgeService (asset-exists validation, approval-
# filtered search) and CourseSampleService (course-must-exist and no-
# duplicate-registration guards), both removed by origin/visual-and-
# flowchart with no drop-in replacement class. Investigated against the
# actual new architecture rather than guessed at - every concern already has
# an equivalent test:
#   - asset-exists / bad-input validation on registration -> test_source_
#     service.py::test_bad_uploads_are_refused_with_a_clear_reason (+ the
#     oversized/hostile-filename tests beside it)
#   - approval-equivalent filtering (only enabled/ready material is used) ->
#     test_knowledge_library.py::test_switching_a_document_off_removes_it_
#     from_search_and_back_on_restores_it, test_a_failed_document_is_
#     listed_with_its_reason_and_never_searched
#   - duplicate registration -> test_source_service.py::test_the_same_
#     file_uploaded_twice_is_processed_once, test_knowledge_library.py::
#     test_the_same_file_is_not_added_twice_and_keeps_its_number (the new
#     behaviour dedupes and returns the existing record rather than raising
#     ConflictError - a confirmed, intentional difference, not a gap)
# "A course must exist before registering a sample for it" has no
# equivalent, because it no longer applies: an uploaded source is owner-
# scoped, never course-scoped, so nothing needs a course to exist first -
# a confirmed architecture change, not a missing test.
# ---------------------------------------------------------------------------


async def test_template_update_requires_existing_slug():
    service = TemplateService()
    with pytest.raises(NotFoundError):
        await service.update("does-not-exist", TemplateUpdateRequest(description="x"))


async def test_archiving_a_template_hides_it_from_new_course_picker_only():
    base = load_template("technical")
    service = TemplateService()
    created = await service.create(
        TemplateCreateRequest(name=f"Archive Me {_RUN}", kind="technical", description="", config=base)
    )
    await service.archive(created.slug)
    await refresh_db_templates()

    from app.course.templates.registry import available_templates

    assert created.template_id not in {t.template_id for t in available_templates()}
    # But it still resolves directly - an existing course is unaffected.
    assert load_template(created.template_id).template_id == created.template_id


# ---------------------------------------------------------------------------
# generation history
# ---------------------------------------------------------------------------


async def test_generation_run_history_keeps_every_attempt():
    course = await _make_course("crs_hist1")
    # job_id is globally unique (not scoped per course) - suffix it like
    # everything else, so a stale row from a previous run against this same
    # persistent test database can't get silently reattached to it instead
    # of a fresh row being created for this run's course.
    job_1, job_2 = f"job_1_{_RUN}", f"job_2_{_RUN}"
    from app.db.service import get_database_service as _gds

    async with _gds() as db:
        await db.record_generation_run(
            course.course_id, job_id=job_1, state="failed", payload={}, error="boom"
        )
        await db.record_generation_run(
            course.course_id,
            job_id=job_2,
            state="done",
            payload={"chapters_generated": ["chapter_1"], "chapters_failed": []},
        )
        runs = await db.list_generation_runs(course.course_id)
    assert {r.job_id for r in runs} == {job_1, job_2}
    done = next(r for r in runs if r.job_id == job_2)
    assert done.chapters_generated == ["chapter_1"]


# ---------------------------------------------------------------------------
# MemoryService: bounded, relevance-filtered, fails soft
# ---------------------------------------------------------------------------


async def test_memory_service_returns_empty_context_with_nothing_registered():
    service = MemoryService()
    context = await service.build_context(stage="writer", course_title="Anything at all")
    assert context.is_empty()
    assert context.render() == ""


async def test_memory_service_bounds_history_notes_to_the_configured_maximum():
    """Replacement for the removed sample-notes bound test (CourseSampleService
    is gone - see this file's own docstring): MemoryService's own context-size
    guarantee now lives in `history_notes` instead, capped by MAX_HISTORY_NOTES
    (currently 2) regardless of how many matching past courses exist. Enforced
    twice over - HybridRetriever.rank() itself returns at most `limit` items,
    and MemoryContext.render() re-slices to MAX_HISTORY_NOTES again - so this
    also catches a regression in either one alone."""
    topic = f"Negotiation Basics {_RUN}"
    from app.db.service import get_database_service as _gds

    async with _gds() as db:
        for index in range(MAX_HISTORY_NOTES + 2):
            course = await _make_course(f"crs_hbound{index}", title=f"{topic} {index}")
            await db.record_generation_run(
                course.course_id,
                job_id=f"job_hbound{index}_{_RUN}",
                state="done",
                payload={"chapters_generated": ["chapter_1"], "chapters_failed": []},
            )

    service = MemoryService()
    context = await service.build_context(stage="reviewer", course_title=topic)
    assert len(context.history_notes) <= MAX_HISTORY_NOTES
    assert context.history_notes  # the bound is real, not a false-empty pass


async def test_memory_service_fails_soft_when_retrieval_raises(monkeypatch):
    service = MemoryService()

    async def boom(*args, **kwargs):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(service, "_add_history_notes", boom)
    context = await service.build_context(stage="reviewer", course_title="Anything")
    assert isinstance(context, MemoryContext)
    assert context.is_empty()


async def test_memory_service_is_a_noop_when_disabled():
    settings = get_settings().model_copy(update={"enable_memory_retrieval": False})
    service = MemoryService(settings=settings)
    context = await service.build_context(stage="reviewer", course_title="Should Not Be Found")
    assert context.is_empty()
