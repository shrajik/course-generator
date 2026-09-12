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
from app.core.ids import new_suffix
from app.db.engine import get_engine
from app.db.models import Course, Document
from app.db.repositories.courses import CourseRepository
from app.db.repositories.documents import DocumentRepository
from app.db.session import get_session_factory

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
# Appended to every hardcoded id this file uses, so re-running pytest against
# the same (never-dropped, see db_schema in conftest.py) test database twice
# doesn't collide with the previous run's rows.
_RUN = new_suffix()


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
def database_schema(request, monkeypatch):
    """Schema lifecycle lives in conftest.py's session-scoped db_schema
    fixture for every test below except test_alembic_initial_migration_generates_sql
    (which needs no database at all - alembic's `--sql` mode never connects).
    Scopes DATABASE_URL/USE_DATABASE to this test only (monkeypatch reverts
    them automatically), so they don't leak into unrelated offline tests."""
    if not DATABASE_URL:
        return
    url = request.getfixturevalue("db_schema")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()


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
    course_id = f"crs_testphase1_{_RUN}"
    document_id = f"doc_testphase1_{_RUN}"
    course = Course(
        course_id=course_id,
        document_id=document_id,
        title="Database Test",
        status="created",
        template_id="technical_v1",
        # A full, valid CourseInput shape - this row is never dropped between
        # tests any more (see db_schema in conftest.py), so any other
        # DB-gated test that lists every course in the table must still be
        # able to CourseInput.model_validate() this row's input_json.
        input_json={
            "course_title": "Database Test",
            "toc": [{"title": "JSONB"}],
            "target_audience": "Testers",
            "template": "technical",
        },
        metadata_json={"nested": {"enabled": True}},
        created_at=timestamp(),
        updated_at=timestamp(),
    )
    async with get_session_factory()() as session:
        async with session.begin():
            await CourseRepository(session).create(course)
            document = Document(
                document_id=document_id,
                course=course,
                version=3,
                document_json={"pages": [], "meta": {"source": "test"}},
                created_at=timestamp(),
                updated_at=timestamp(),
            )
            await DocumentRepository(session).create(document)
        loaded_course = await CourseRepository(session).get_by_course_id(course_id)
        loaded_document = await DocumentRepository(session).get_by_document_id(document_id)
    assert loaded_course is not None
    assert loaded_course.input_json["toc"][0]["title"] == "JSONB"
    assert loaded_document is not None
    assert loaded_document.document_json["meta"]["source"] == "test"
    assert loaded_document.course_pk == loaded_course.id


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_importer_is_idempotent_and_preserves_ids(tmp_path: Path) -> None:
    course_id = f"crs_existing123_{_RUN}"
    document_id = f"doc_existing123_{_RUN}"
    course_dir = tmp_path / "courses" / course_id
    course_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    course_payload = {
        "course_id": course_id,
        "document_id": document_id,
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
        "document_id": document_id,
        "course_id": course_id,
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
        document = await DocumentRepository(session).get_by_document_id(document_id)
    # Scoped to this test's own course rather than an absolute table count -
    # the shared test database is no longer wiped between tests/files (see
    # db_schema in conftest.py), so other tests' rows may also be present;
    # the guarantee that matters here (the importer didn't duplicate this
    # course on its second, idempotent run) is unaffected either way.
    matching = [c for c in courses if c.course_id == course_id]
    assert len(matching) == 1
    assert matching[0].document_id == document_id
    assert document is not None

    # Clean up: the schema is never dropped between tests (see db_schema in
    # conftest.py), so leaving this row around would keep it visible to every
    # later DB-gated test in the session for no reason. `Course`'s child
    # relationships use `passive_deletes=True` (see app/db/models.py), so this
    # relies on the database's own ON DELETE CASCADE for the document row
    # rather than the ORM nulling out its NOT NULL course_pk itself.
    async with get_session_factory()() as session:
        async with session.begin():
            row = await CourseRepository(session).get_by_course_id(course_id)
            if row is not None:
                await CourseRepository(session).delete(row)