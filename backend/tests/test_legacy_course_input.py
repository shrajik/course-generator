"""Regression coverage for `GET /api/courses` 500ing on a legacy/incomplete
`input_json` row.

`CourseInput` is strict on purpose (`extra="forbid"`, required `toc`/
`target_audience`/`template`) because it also validates *new* course
creation - see test_schemas.py. The bug was that `app/db/service.py`
reused that same strict validator to hydrate *every* persisted row on read
(`_to_course_record`), so one older/externally-edited row with an
incomplete `input_json` (e.g. `{"course_title": "RAG Fundamentals"}`) took
down the whole `GET /api/courses` list with a 500, not just that one course.

The fix is `_coerce_course_input`: on the read side only, keep whatever
still validates and backfill the rest with explicit placeholders instead of
raising. These tests cover:

1. A fully valid `input_json` round-trips unchanged (no behavior change for
   normal rows).
2. A legacy/incomplete `input_json` reconstructs safely instead of raising,
   preserving the one real field it has (`course_title`).
3. A mixture of valid and legacy rows can all be listed together via the
   actual `DatabaseService.list_course_records()` code path from the bug
   report.
4. Course *creation* still goes through the unmodified, strict `CourseInput`
   schema (this doesn't get any more lenient).
5. A non-validation error is not accidentally swallowed by the same code
   path (only `pydantic.ValidationError` is treated as "legacy data").
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import reset_settings_cache
from app.core.ids import new_suffix
from app.db.models import Course
from app.db.repositories.courses import CourseRepository
from app.db.service import DatabaseService, _coerce_course_input
from app.db.session import get_session_factory
from app.schemas.course import CourseInput

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
_RUN = new_suffix()


def timestamp() -> datetime:
    return datetime.now(timezone.utc)


# --- Pure unit tests for _coerce_course_input (no database needed) --------


def test_coerce_course_input_passes_through_a_valid_record():
    raw = {
        "course_title": "RAG Fundamentals",
        "toc": [{"title": "Chapter 1"}],
        "target_audience": "Backend engineers",
        "template": "technical",
    }
    result = _coerce_course_input(raw, "crs_valid")
    assert result == CourseInput.model_validate(raw)


def test_coerce_course_input_backfills_a_legacy_incomplete_record():
    """The exact shape from the bug report: only `course_title` present."""
    raw = {"course_title": "RAG Fundamentals"}

    result = _coerce_course_input(raw, "crs_legacy")

    # The one real field we had is preserved, not discarded.
    assert result.course_title == "RAG Fundamentals"
    # The rest are safe, clearly-legacy placeholders, not invented facts.
    assert result.toc
    assert result.target_audience
    assert result.template in ("technical", "non_technical")


def test_coerce_course_input_preserves_valid_fields_alongside_invalid_ones():
    """Only the genuinely broken/missing fields get placeholders - a field
    that's already valid is never overwritten just because a sibling field
    failed validation."""
    raw = {
        "course_title": "Half Legacy Course",
        "target_audience": "Data scientists",
        # toc and template are missing entirely.
    }

    result = _coerce_course_input(raw, "crs_half_legacy")

    assert result.course_title == "Half Legacy Course"
    assert result.target_audience == "Data scientists"
    assert result.toc  # backfilled
    assert result.template in ("technical", "non_technical")  # backfilled


def test_coerce_course_input_handles_unknown_extra_keys():
    """`extra="forbid"` means a since-removed key also raises, not just a
    missing one - must not crash either."""
    raw = {
        "course_title": "RAG Fundamentals",
        "toc": [{"title": "Chapter 1"}],
        "target_audience": "Backend engineers",
        "template": "technical",
        "some_removed_field": "legacy value",
    }

    result = _coerce_course_input(raw, "crs_extra_key")

    assert result.course_title == "RAG Fundamentals"
    assert result.target_audience == "Backend engineers"


def test_coerce_course_input_does_not_swallow_non_validation_errors(monkeypatch):
    """Only `pydantic.ValidationError` is treated as legacy data - any other
    exception (a real bug, a DB issue surfacing here, etc.) must still
    propagate instead of being hidden behind a placeholder record."""
    from app.db import service as db_service_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("not a validation error")

    monkeypatch.setattr(db_service_module.CourseInput, "model_validate", staticmethod(_boom))

    with pytest.raises(RuntimeError):
        _coerce_course_input({"course_title": "X"}, "crs_boom")


# --- Integration test through the real DB-backed list_course_records path -


@pytest.fixture(autouse=True)
def database_schema(request, monkeypatch):
    if not DATABASE_URL:
        return
    url = request.getfixturevalue("db_schema")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()


def _make_course_row(course_id: str, title: str, input_json: dict) -> Course:
    return Course(
        course_id=course_id,
        document_id=f"doc_{course_id}",
        title=title,
        status="ready",
        template_id="technical_v1",
        input_json=input_json,
        metadata_json={},
        created_at=timestamp(),
        updated_at=timestamp(),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_list_course_records_survives_a_mix_of_valid_and_legacy_rows():
    valid_id = f"crs_valid_{_RUN}"
    legacy_id = f"crs_legacy_{_RUN}"

    async with get_session_factory()() as session:
        async with session.begin():
            repo = CourseRepository(session)
            await repo.create(
                _make_course_row(
                    valid_id,
                    "Valid Course",
                    {
                        "course_title": "Valid Course",
                        "toc": [{"title": "Chapter 1"}],
                        "target_audience": "Everyone",
                        "template": "technical",
                    },
                )
            )
            await repo.create(
                _make_course_row(legacy_id, "RAG Fundamentals", {"course_title": "RAG Fundamentals"})
            )

        # This is the exact call from the bug report's code path
        # (app/db/service.py::list_course_records -> _to_course_record).
        records = await DatabaseService(session).list_course_records(limit=1000)

    by_id = {r.course_id: r for r in records}
    assert by_id[valid_id].input.course_title == "Valid Course"
    assert by_id[valid_id].input.target_audience == "Everyone"

    legacy = by_id[legacy_id]
    assert legacy.input.course_title == "RAG Fundamentals"
    assert legacy.input.toc  # backfilled, present, doesn't crash
    assert legacy.input.target_audience
    assert legacy.input.template in ("technical", "non_technical")


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
async def test_load_course_record_survives_a_legacy_row():
    legacy_id = f"crs_legacy_single_{_RUN}"

    async with get_session_factory()() as session:
        async with session.begin():
            await CourseRepository(session).create(
                _make_course_row(legacy_id, "RAG Fundamentals", {"course_title": "RAG Fundamentals"})
            )
        record = await DatabaseService(session).load_course_record(legacy_id)

    assert record.course_id == legacy_id
    assert record.input.course_title == "RAG Fundamentals"


# --- Creation still requires the current, strict CourseInput schema -------


def test_course_creation_still_requires_toc_target_audience_and_template():
    """Legacy-read tolerance must not leak into course creation - a new
    course with the same incomplete shape as the bug report is still
    rejected outright."""
    with pytest.raises(ValidationError):
        CourseInput.model_validate({"course_title": "RAG Fundamentals"})


# --- Full HTTP round-trip: GET /api/courses must not 500 -------------------


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_get_api_courses_survives_a_legacy_row_mixed_with_real_ones(monkeypatch):
    """End-to-end reproduction of the reported bug: hit the real
    `GET /api/courses` endpoint (not just the DB layer) with a legacy row
    alongside a normal, API-created course, as an admin (who sees every
    course) - it must return 200 with both, not 500."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("USE_DATABASE", "true")
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    admin_email = f"legacy-admin.{uuid4().hex}@example.com"
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", admin_email)
    reset_settings_cache()

    legacy_id = f"crs_legacy_api_{_RUN}"

    from app.main import app

    with TestClient(app) as client:
        register = client.post(
            "/auth/register", json={"email": admin_email, "password": "LegacyTest123!"}
        )
        assert register.status_code == 201, register.text

        create = client.post(
            "/api/courses",
            json={
                "course_title": "A Perfectly Normal Course",
                "toc": [{"title": "Foundations"}],
                "target_audience": "Backend engineers",
                "dos": [],
                "donts": [],
                "template": "technical",
                "run_planner": False,
            },
        )
        assert create.status_code == 201, create.text

        # Simulate the legacy row directly - it can't be created through the
        # (correctly strict) API, only reproduced as already-persisted data.
        # Must run on the TestClient's own anyio loop (client.portal), not a
        # separate one - asyncpg connections and the process-wide cached
        # engine are bound to whichever loop first used them (see the same
        # pattern in test_admin_authorization.py).
        async def _insert_legacy_row() -> None:
            async with get_session_factory()() as session:
                async with session.begin():
                    await CourseRepository(session).create(
                        _make_course_row(legacy_id, "RAG Fundamentals", {"course_title": "RAG Fundamentals"})
                    )

        client.portal.call(_insert_legacy_row)

        response = client.get("/api/courses")

    assert response.status_code == 200, response.text
    courses = response.json()["courses"]
    ids = {c["course_id"] for c in courses}
    assert legacy_id in ids
    assert create.json()["course_id"] in ids
    legacy_course = next(c for c in courses if c["course_id"] == legacy_id)
    assert legacy_course["course_title"] == "RAG Fundamentals"
