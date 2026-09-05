"""Integration tests for the Phase 1 PostgreSQL foundation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.commands.import_filesystem_data import import_courses
from app.core.config import reset_settings_cache
from app.db.engine import dispose_engine, get_engine
from app.db.models import Course, Document
from app.db.repositories.courses import CourseRepository
from app.db.repositories.documents import DocumentRepository
from app.db.session import get_session_factory

DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def test_alembic_initial_migration_generates_sql() -> None:
    backend_dir = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "CREATE TABLE courses" in result.stdout
    assert "CREATE TABLE documents" in result.stdout
    assert "CREATE TABLE blueprints" in result.stdout


@pytest.fixture(autouse=True)
async def database_schema():
    if not DATABASE_URL:
        yield
        return
    os.environ["DATABASE_URL"] = DATABASE_URL or ""
    reset_settings_cache()
    async with get_engine().begin() as connection:
        await connection.run_sync(lambda sync_connection: Course.metadata.create_all(sync_connection))
        await connection.run_sync(lambda sync_connection: Document.metadata.create_all(sync_connection))
    yield
    async with get_engine().begin() as connection:
        await connection.run_sync(lambda sync_connection: Document.metadata.drop_all(sync_connection))
        await connection.run_sync(lambda sync_connection: Course.metadata.drop_all(sync_connection))
    await dispose_engine()


def timestamp() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_database_connection() -> None:
    async with get_engine().connect() as connection:
        result = await connection.scalar(text("SELECT 1"))
    assert result == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_course_document_retrieval_and_jsonb() -> None:
    course = Course(
        course_id="crs_testphase1",
        document_id="doc_testphase1",
        title="Database Test",
        status="created",
        template_id="technical_v1",
        input_json={"toc": [{"title": "JSONB"}]},
        metadata_json={"nested": {"enabled": True}},
        created_at=timestamp(),
        updated_at=timestamp(),
    )
    async with get_session_factory()() as session:
        async with session.begin():
            await CourseRepository(session).create(course)
            document = Document(
                document_id="doc_testphase1",
                course=course,
                version=3,
                document_json={"pages": [], "meta": {"source": "test"}},
                created_at=timestamp(),
                updated_at=timestamp(),
            )
            await DocumentRepository(session).create(document)
        loaded_course = await CourseRepository(session).get_by_course_id("crs_testphase1")
        loaded_document = await DocumentRepository(session).get_by_document_id("doc_testphase1")
    assert loaded_course is not None
    assert loaded_course.input_json["toc"][0]["title"] == "JSONB"
    assert loaded_document is not None
    assert loaded_document.document_json["meta"]["source"] == "test"
    assert loaded_document.course_pk == loaded_course.id


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_importer_is_idempotent_and_preserves_ids(tmp_path: Path) -> None:
    course_dir = tmp_path / "courses" / "crs_existing123"
    course_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    course_payload = {
        "course_id": "crs_existing123",
        "document_id": "doc_existing123",
        "status": "planned",
        "input": {
            "course_title": "Imported Course",
            "toc": [{"title": "One"}],
            "target_audience": "Testers",
            "template": "technical",
        },
        "template_id": "technical_v1",
        "created_at": now,
        "updated_at": now,
    }
    document_payload = {
        "document_id": "doc_existing123",
        "course_id": "crs_existing123",
        "course_title": "Imported Course",
        "template_id": "technical_v1",
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "pages": [],
    }
    (course_dir / "course.json").write_text(json.dumps(course_payload), encoding="utf-8")
    (course_dir / "document.json").write_text(json.dumps(document_payload), encoding="utf-8")

    first = await import_courses(tmp_path)
    second = await import_courses(tmp_path)
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["updated"] == 1

    async with get_session_factory()() as session:
        courses = await CourseRepository(session).list()
        document = await DocumentRepository(session).get_by_document_id("doc_existing123")
    assert len(courses) == 1
    assert courses[0].course_id == "crs_existing123"
    assert courses[0].document_id == "doc_existing123"
    assert document is not None