"""Crash recovery: `CourseService.recover_interrupted_courses` - the startup
scan wired into `app/main.py`'s lifespan.

A course whose persisted run was left "queued"/"running" when the PREVIOUS
process stopped is orphaned, not actually in flight - a fresh `CourseService`
(simulating a restart) has an empty `self._jobs`. All offline/mock
(USE_DATABASE=false - see conftest.py).
"""

from __future__ import annotations

import asyncio
import contextlib

from app.core.ids import utc_now_iso
from app.schemas.course import GenerateRequest, RunInfo
from app.services.course_service import CourseService


async def _await_jobs(svc: CourseService) -> None:
    """Let whatever background task(s) recovery just started actually run
    to completion before asserting on the final state."""
    for task in list(svc._jobs.values()):
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_running_course_found_at_startup_is_marked_interrupted_and_auto_resumed(
    service, technical_input
):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(
        job_id="job-dead", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso(),
        retry_count=0,
    )
    await service._save_course(record)

    # A fresh instance - same storage/settings, empty `_jobs` - is the
    # actual simulation of "the process restarted."
    fresh = CourseService(storage=service.storage, settings=service.settings, use_db=False)

    recovered = await fresh.recover_interrupted_courses()
    assert course_id in recovered

    await _await_jobs(fresh)

    final = await fresh.get_course_record(course_id)
    # Auto-resumed successfully under mock AI - finishes "ready", not left
    # dangling as "interrupted".
    assert final.status == "ready"
    assert final.run.retry_count == 1


async def test_a_queued_course_found_at_startup_is_also_recovered(service, technical_input):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(job_id="job-dead", state="queued", started_at=utc_now_iso(), updated_at=utc_now_iso())
    await service._save_course(record)

    fresh = CourseService(storage=service.storage, settings=service.settings, use_db=False)
    recovered = await fresh.recover_interrupted_courses()
    assert course_id in recovered
    await _await_jobs(fresh)

    final = await fresh.get_course_record(course_id)
    assert final.status == "ready"


async def test_a_done_or_failed_course_is_left_alone_by_the_startup_scan(service, technical_input):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(job_id="job-ok", state="done", started_at=utc_now_iso(), updated_at=utc_now_iso())
    await service._save_course(record)

    fresh = CourseService(storage=service.storage, settings=service.settings, use_db=False)
    recovered = await fresh.recover_interrupted_courses()
    assert course_id not in recovered

    final = await fresh.get_course_record(course_id)
    assert final.run.state == "done"  # untouched


async def test_recovery_stops_auto_resuming_past_the_configured_retry_limit(
    service, technical_input, settings
):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(
        job_id="job-dead", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso(),
        retry_count=settings.generation_max_auto_resume_retries,
    )
    await service._save_course(record)

    fresh = CourseService(storage=service.storage, settings=service.settings, use_db=False)
    recovered = await fresh.recover_interrupted_courses()
    assert course_id in recovered
    # No auto-resume this time - nothing was started.
    assert course_id not in fresh._jobs

    final = await fresh.get_course_record(course_id)
    assert final.run.state == "interrupted"
    assert final.run.retry_count == settings.generation_max_auto_resume_retries  # unchanged


async def test_recovery_is_best_effort_and_never_raises_on_a_bad_course_record(
    service, technical_input
):
    """One corrupt/unreadable course must not block the rest of the scan."""
    good = await service.create_course(technical_input)
    good.run = RunInfo(job_id="job-good", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso())
    await service._save_course(good)

    # A second course whose course.json is present but unparseable.
    bad = await service.create_course(technical_input)
    bad_path = service.storage.course_path(bad.course_id)
    bad_path.write_text("not valid json", encoding="utf-8")

    fresh = CourseService(storage=service.storage, settings=service.settings, use_db=False)
    recovered = await fresh.recover_interrupted_courses()

    assert good.course_id in recovered
    assert bad.course_id not in recovered
    await _await_jobs(fresh)
