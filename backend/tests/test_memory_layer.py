"""AI memory layer: templates, visual knowledge, course samples, generation
history and MemoryService retrieval, against a real Postgres.

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
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.ids import new_suffix
from app.course.templates.registry import clear_cache, load_template, refresh_db_templates
from app.db.models import Course
from app.db.repositories.courses import CourseRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    MAX_SAMPLE_NOTES,
    CourseSampleCreateRequest,
    CourseSampleUpdateRequest,
    MemoryContext,
    TemplateCreateRequest,
    TemplateUpdateRequest,
    VisualKnowledgeCreateRequest,
    VisualKnowledgeUpdateRequest,
)
from app.services.course_sample_service import CourseSampleService
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
# visual knowledge
# ---------------------------------------------------------------------------


async def test_visual_knowledge_register_validates_the_asset_exists():
    course = await _make_course("crs_vk1")
    storage = get_storage()
    storage.save_asset(course.course_id, b"<svg></svg>", extension="svg")

    service = VisualKnowledgeService(storage)
    with pytest.raises(ValidationFailedError):
        await service.register(
            VisualKnowledgeCreateRequest(
                course_id=course.course_id, asset_path="assets/does_not_exist.svg"
            )
        )

    # The real one succeeds.
    entry = await service.register(
        VisualKnowledgeCreateRequest(
            course_id=course.course_id,
            asset_path="assets/image_001.svg",
            kind="diagram",
            diagram_kind="flow_chart",
            topic="eye anatomy",
            description="labelled diagram of the eye",
            tags=["biology", "eye"],
        )
    )
    assert entry.approved is False
    assert entry.asset_url is not None


async def test_visual_knowledge_search_only_returns_approved_by_default():
    course = await _make_course("crs_vk2")
    storage = get_storage()
    storage.save_asset(course.course_id, b"<svg></svg>", extension="svg")
    service = VisualKnowledgeService(storage)
    entry = await service.register(
        VisualKnowledgeCreateRequest(
            course_id=course.course_id,
            asset_path="assets/image_001.svg",
            topic="eye anatomy",
            description="a diagram",
        )
    )
    items, total = await service.list(search="eye", approved_only=True)
    assert total == 0 and items == []

    await service.update(entry.id, VisualKnowledgeUpdateRequest(approved=True))
    items, total = await service.list(search="eye", approved_only=True)
    assert total == 1
    assert items[0].id == entry.id


# ---------------------------------------------------------------------------
# course samples
# ---------------------------------------------------------------------------


async def test_course_sample_requires_an_existing_course():
    service = CourseSampleService()
    with pytest.raises(NotFoundError):
        await service.register(CourseSampleCreateRequest(course_id="crs_missing"))


async def test_course_sample_cannot_be_registered_twice():
    course = await _make_course("crs_sample1", title="RAG Fundamentals")
    service = CourseSampleService()
    await service.register(CourseSampleCreateRequest(course_id=course.course_id, topic="RAG"))
    with pytest.raises(ConflictError):
        await service.register(CourseSampleCreateRequest(course_id=course.course_id, topic="RAG"))


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


async def test_memory_service_bounds_sample_notes_to_the_configured_maximum():
    for i in range(MAX_SAMPLE_NOTES + 3):
        course = await _make_course(f"crs_bound{i}", title=f"Negotiation Basics {i}")
        sample_service = CourseSampleService()
        entry = await sample_service.register(
            CourseSampleCreateRequest(course_id=course.course_id, topic="Negotiation")
        )
        await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))

    service = MemoryService()
    context = await service.build_context(stage="writer", course_title="Negotiation Basics")
    assert len(context.sample_notes) <= MAX_SAMPLE_NOTES


async def test_memory_service_fails_soft_when_retrieval_raises(monkeypatch):
    service = MemoryService()

    async def boom(*args, **kwargs):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(service.samples, "list", boom)
    context = await service.build_context(stage="writer", course_title="Anything")
    assert isinstance(context, MemoryContext)
    assert context.is_empty()


async def test_memory_service_is_a_noop_when_disabled():
    settings = get_settings().model_copy(update={"enable_memory_retrieval": False})
    service = MemoryService(settings=settings)
    course = await _make_course("crs_disabled1", title="Should Not Be Found")
    sample_service = CourseSampleService()
    entry = await sample_service.register(
        CourseSampleCreateRequest(course_id=course.course_id, topic="Should Not Be Found")
    )
    await sample_service.update(entry.id, CourseSampleUpdateRequest(approved=True))
    context = await service.build_context(stage="writer", course_title="Should Not Be Found")
    assert context.is_empty()
