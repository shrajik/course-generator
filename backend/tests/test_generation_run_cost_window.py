"""GenerationRunRepository.count_model_calls_in_window - the real,
cross-course call count behind the Azure Cost Management reconciliation
feature's measured per-image rate (see app.services.azure_cost_reconciliation
and app.api.admin's MAI-Image-2.6 cost reconciliation endpoint).

Same convention as test_memory_layer.py: gated on TEST_DATABASE_URL so the
default test run (no test database configured) never touches a real
database. Run with TEST_DATABASE_URL set to a disposable Postgres to execute
these for real.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import reset_settings_cache
from app.core.ids import new_suffix
from app.db.models import Course
from app.db.repositories.courses import CourseRepository
from app.db.service import get_database_service
from app.db.session import get_session_factory

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
_RUN = new_suffix()

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture(autouse=True)
def database_schema(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_course(course_id: str) -> Course:
    course_id = f"{course_id}_{_RUN}"
    course = Course(
        course_id=course_id,
        document_id=f"doc_{course_id}",
        title="Cost Window Test Course",
        status="ready",
        template_id="technical_v1",
        input_json={
            "course_title": "Cost Window Test Course",
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


def _call(model: str, *, timestamp: datetime) -> dict:
    return {
        "kind": "image",
        "purpose": "illustration",
        "model": model,
        "seconds": 1.0,
        "timestamp": timestamp.isoformat(),
    }


async def test_only_calls_inside_the_window_are_counted():
    course = await _make_course("crs_cost_window1")
    anchor = _now()
    payload = {
        "model_usage": [
            _call("MAI-Image-2.6", timestamp=anchor - timedelta(days=5)),  # inside
            _call("MAI-Image-2.6", timestamp=anchor - timedelta(days=10)),  # inside
            _call("MAI-Image-2.6", timestamp=anchor - timedelta(days=40)),  # before window
            _call("FLUX.2-flex", timestamp=anchor - timedelta(days=5)),  # wrong model
        ]
    }
    async with get_database_service() as db:
        await db.record_generation_run(
            course.course_id, job_id=f"job_cost_window1_{_RUN}", state="done", payload=payload
        )
        count = await db.count_model_calls_in_window(
            model="MAI-Image-2.6", start=anchor - timedelta(days=30), end=anchor
        )
    assert count == 2


async def test_calls_across_multiple_courses_are_all_counted():
    anchor = _now()
    course_a = await _make_course("crs_cost_window2a")
    course_b = await _make_course("crs_cost_window2b")
    async with get_database_service() as db:
        await db.record_generation_run(
            course_a.course_id,
            job_id=f"job_cost_window2a_{_RUN}",
            state="done",
            payload={"model_usage": [_call("MAI-Image-2.6", timestamp=anchor - timedelta(days=1))]},
        )
        await db.record_generation_run(
            course_b.course_id,
            job_id=f"job_cost_window2b_{_RUN}",
            state="done",
            payload={"model_usage": [_call("MAI-Image-2.6", timestamp=anchor - timedelta(days=1))]},
        )
        count = await db.count_model_calls_in_window(
            model="MAI-Image-2.6", start=anchor - timedelta(days=2), end=anchor
        )
    assert count >= 2  # >= since the persistent test DB may carry rows from other test runs


async def test_an_empty_window_counts_as_zero_not_an_error():
    course = await _make_course("crs_cost_window3")
    anchor = _now()
    async with get_database_service() as db:
        await db.record_generation_run(
            course.course_id,
            job_id=f"job_cost_window3_{_RUN}",
            state="done",
            payload={"model_usage": [_call("MAI-Image-2.6", timestamp=anchor - timedelta(days=100))]},
        )
        count = await db.count_model_calls_in_window(
            model="MAI-Image-2.6", start=anchor - timedelta(days=5), end=anchor
        )
    assert count == 0
