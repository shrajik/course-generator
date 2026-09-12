"""Phase 2: Course/Blueprint/Document persistence is PostgreSQL-primary.

These tests opt back into a live database (TEST_DATABASE_URL) and exercise
CourseService/DocumentService with `use_db=True` directly against it, proving:

* course/blueprint/document read+write goes through Postgres, not course.json/
  blueprint.json/document.json
* research/chapters/assets stay on the filesystem regardless (untouched by the
  migration)
* the existing API contracts (list/get/blueprint/document) still work when
  backed by the database

The rest of the suite (conftest.py sets USE_DATABASE=false) never touches a
database at all, so it stays fast and hermetic; this file is the DB-backed
counterpart, skipped unless TEST_DATABASE_URL is configured - see
test_database_foundation.py for the equivalent repository-level tests.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import reset_settings_cache
from app.db.repositories.courses import CourseRepository
from app.db.service import get_database_service
from app.db.session import get_session_factory
from app.schemas.course import GenerateRequest
from app.schemas.patch import AiEditRequest
from app.services.course_service import CourseService
from app.services.document_service import DocumentService
from app.services.openai_service import get_ai_client
from app.services.storage_service import get_storage

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture(autouse=True)
def database_schema(db_schema, monkeypatch):
    """Schema lifecycle lives in conftest.py's session-scoped db_schema
    fixture - see its docstring for why this no longer drops tables. Scopes
    DATABASE_URL/USE_DATABASE to this test only (monkeypatch reverts them
    automatically), so they don't leak into unrelated offline tests."""
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()


@pytest.fixture
def db_service():
    return CourseService(
        ai=get_ai_client(), storage=get_storage(), use_db=True
    )


@pytest.fixture
def db_documents():
    return DocumentService(
        ai=get_ai_client(), storage=get_storage(), use_db=True
    )


async def test_create_course_writes_to_postgres_not_filesystem(db_service, technical_input):
    record = await db_service.create_course(technical_input, run_planner=False)

    async with get_session_factory()() as session:
        stored = await CourseRepository(session).get_by_course_id(record.course_id)
    assert stored is not None
    assert stored.title == technical_input.course_title

    assert not db_service.storage.course_exists(record.course_id)


async def test_blueprint_persists_to_postgres(db_service, technical_input):
    record = await db_service.create_course(technical_input, run_planner=True)

    assert await db_service.has_blueprint(record.course_id)
    blueprint = await db_service.get_blueprint(record.course_id)
    assert blueprint.chapters

    assert not db_service.storage.has_blueprint(record.course_id)


async def test_generate_persists_document_to_postgres_and_chapters_to_filesystem(
    db_service, technical_input
):
    record = await db_service.create_course(technical_input, run_planner=True)
    await db_service.generate(record.course_id, GenerateRequest(mode="sync", generate_images=False))

    assert await db_service.has_document(record.course_id)
    document = await db_service.load_document(record.course_id)
    assert document.pages
    assert not db_service.storage.has_document(record.course_id)

    # Chapters/research are not part of the migration - they stay on disk.
    chapters = db_service.storage.load_all_chapters(record.course_id)
    assert chapters
    assert db_service.storage.chapters_dir(record.course_id).exists()


async def test_list_courses_reads_from_postgres(db_service, technical_input, non_technical_input):
    first = await db_service.create_course(technical_input, run_planner=False)
    second = await db_service.create_course(non_technical_input, run_planner=False)

    records = await db_service.list_courses()
    ids = {r.course_id for r in records}
    assert {first.course_id, second.course_id} <= ids


async def test_document_service_ai_edit_updates_postgres(
    db_service, db_documents, technical_input
):
    record = await db_service.create_course(technical_input, run_planner=True)
    await db_service.generate(record.course_id, GenerateRequest(mode="sync", generate_images=False))

    document = await db_documents.load(record.document_id)
    _, paragraph = next(
        (page, block)
        for page, block in document.iter_blocks()
        if block.type.value == "paragraph" and block.meta.origin == "generated"
    )

    result = await db_documents.ai_edit(
        record.document_id,
        AiEditRequest(
            selected_block_ids=[paragraph.id],
            instruction="Make this more concise.",
            apply=True,
        ),
    )
    assert result.applied

    async with get_database_service() as db:
        reloaded = await db.load_document(record.document_id)
    assert reloaded.version == document.version + 1


async def test_course_service_helpers_fall_back_to_filesystem_when_db_disabled(
    technical_input,
):
    """Sanity check: the same service class still works filesystem-only."""
    fs_service = CourseService(ai=get_ai_client(), storage=get_storage(), use_db=False)
    record = await fs_service.create_course(technical_input, run_planner=False)
    assert fs_service.storage.course_exists(record.course_id)
