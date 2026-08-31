"""Test configuration.

Every test runs against the offline mock AI client and an isolated temp data
directory, so the suite needs no API key and never touches real course data.
"""

from __future__ import annotations

import os
import shutil
import tempfile

# Must be set before app modules read settings.
_TMP_DATA = tempfile.mkdtemp(prefix="course_creator_tests_")
os.environ["MOCK_OPENAI"] = "true"
os.environ["OPENAI_API_KEY"] = ""
os.environ["DATA_DIR"] = _TMP_DATA
os.environ["ENABLE_IMAGE_GENERATION"] = "true"
os.environ["MAX_REVIEW_REVISIONS"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.schemas.course import CourseInput  # noqa: E402
from app.services import course_service, document_service, storage_service  # noqa: E402
from app.services.openai_service import set_ai_client  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_dir():
    yield
    shutil.rmtree(_TMP_DATA, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Give every test its own data directory."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    storage_service.reset_storage()
    course_service.reset_course_service()
    document_service.reset_document_service()
    set_ai_client(None)
    (tmp_path / "data" / "courses").mkdir(parents=True, exist_ok=True)
    yield
    reset_settings_cache()
    storage_service.reset_storage()
    course_service.reset_course_service()
    document_service.reset_document_service()
    set_ai_client(None)


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
