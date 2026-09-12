"""Test configuration.

Every test runs against the offline mock AI client and an isolated temp data
directory, so the suite needs no API key and never touches real course data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Must be set before app modules read settings.
_TMP_DATA = tempfile.mkdtemp(prefix="course_creator_tests_")
os.environ["MOCK_OPENAI"] = "true"
os.environ["OPENAI_API_KEY"] = ""
os.environ["DATA_DIR"] = _TMP_DATA
os.environ["ENABLE_IMAGE_GENERATION"] = "true"
os.environ["MAX_REVIEW_REVISIONS"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"
# This suite exercises the pipeline end-to-end against the filesystem, isolated
# per test via DATA_DIR above; it never touches a real database. Postgres-backed
# persistence has its own tests in test_database_foundation.py, which opt back
# in with TEST_DATABASE_URL.
os.environ["USE_DATABASE"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.db.engine import dispose_engine  # noqa: E402
from app.schemas.course import CourseInput  # noqa: E402
from app.services import course_service, document_service, storage_service  # noqa: E402
from app.services.embedding_service import reset_embedding_service  # noqa: E402
from app.services.openai_service import set_ai_client  # noqa: E402

# The DB-gated slice of the suite (test_*.py files with their own
# `pytestmark = pytest.mark.skipif(not DATABASE_URL, ...)`) opts back into a
# real Postgres via this same env var - see the `db_schema` fixture below.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_dir():
    yield
    shutil.rmtree(_TMP_DATA, ignore_errors=True)


@pytest.fixture(scope="session")
def db_schema() -> str:
    """Shared schema lifecycle for every DB-gated test file.

    The real Alembic migrations are applied exactly once for the whole
    pytest session - the same path production deployments use - and never
    dropped mid-session. Every DB-gated test file requests this fixture
    (directly or via a thin per-file wrapper) instead of maintaining its own
    subprocess-alembic or Base.metadata.create_all/drop_all copy.

    Session scope (not per-file/per-test) is the fix for a real bug: when
    some files reset the schema with Base.metadata.drop_all() after each of
    their own tests while others assumed a module-scoped `alembic upgrade
    head` would keep tables around for their whole file, running the full
    suite together let one file's teardown silently wipe tables another file
    needed - and because `alembic_version` still said "head" afterwards, the
    next `alembic upgrade head` became a no-op and never recreated them,
    producing `relation "..." does not exist` failures. With one shared,
    non-destructive schema setup, that interleaving can't happen.

    This fixture ONLY runs the migration - it deliberately does not leave
    DATABASE_URL/USE_DATABASE set in os.environ afterward (restoring
    DATABASE_URL's prior value once the migration subprocess, which needs it
    in its inherited environment, has finished). Each DB-gated test file
    scopes those two env vars to its own tests via `monkeypatch` instead
    (same as it already did for JWT_SECRET etc.) - a session-scoped fixture
    can't use `monkeypatch` itself (function-scoped only), and leaving
    USE_DATABASE=true set process-wide previously leaked into unrelated
    offline tests that ran later in the same session (they'd unexpectedly
    require Postgres-backed auth instead of running filesystem-only).

    Tests isolate their own data instead of relying on a wiped-clean table
    (unique IDs/names per test run, assertions scoped to what the test
    itself created) - see individual test files.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not configured")
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
    assert result.returncode == 0, result.stderr
    return TEST_DATABASE_URL


@pytest.fixture(autouse=True)
async def _dispose_db_engine_after_test():
    """`app.db.engine.get_engine()` caches one AsyncEngine per process, but
    each DB-gated test's own TestClient opens a fresh anyio portal - its own
    event loop - and asyncpg connections are bound to the loop they were
    opened on. Reusing a cached engine whose pool holds connections from a
    now-closed prior test's loop crashes with "Event loop is closed".

    Disposing after every test (not just DB-gated ones - this is a no-op
    when the engine was never created) forces the next test that touches the
    database to build a fresh engine bound to its own loop. The formerly
    Base.metadata.create_all/drop_all-based test files used to do this only
    for themselves each test; centralising it here covers every DB-gated
    file uniformly, including the module-scoped-fixture ones that never
    disposed at all.
    """
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Give every test its own data directory."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    storage_service.reset_storage()
    course_service.reset_course_service()
    document_service.reset_document_service()
    set_ai_client(None)
    reset_embedding_service()
    (tmp_path / "data" / "courses").mkdir(parents=True, exist_ok=True)
    yield
    reset_settings_cache()
    storage_service.reset_storage()
    course_service.reset_course_service()
    document_service.reset_document_service()
    set_ai_client(None)
    reset_embedding_service()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def storage():
    return storage_service.get_storage()


@pytest.fixture
def service():
    return course_service.get_course_service()


@pytest.fixture
def documents():
    return document_service.get_document_service()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def technical_input() -> CourseInput:
    return CourseInput(
        course_title="Introduction to RAG",
        toc=[
            {"title": "What is RAG", "sections": ["Motivation", "Architecture"]},
            {"title": "Chunking and Embeddings"},
        ],
        target_audience="Backend engineers new to LLMs",
        dos=["Use concrete, runnable examples", "Define jargon on first use"],
        donts=["No marketing language", "Do not cite unverified benchmarks"],
        template="technical",
    )


@pytest.fixture
def non_technical_input() -> CourseInput:
    return CourseInput(
        course_title="Practical Negotiation for Managers",
        toc=[{"title": "Preparing to Negotiate"}, {"title": "Anchoring and Framing"}],
        target_audience="First-time people managers",
        dos=["Use realistic workplace scenarios"],
        donts=["Do not give legal advice"],
        template="non_technical",
    )
