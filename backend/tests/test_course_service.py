"""Pipeline reliability: resume (stage-level checkpoints) and cancellation -
`CourseService._generate_chapter`/`generate`/`cancel_generation`.

All offline/mock (USE_DATABASE=false, MOCK_OPENAI=true - see conftest.py),
exercised through the real `service`/`technical_input` fixtures already used
throughout this suite.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from app.core.ids import utc_now_iso
from app.schemas.course import GenerateRequest, RunInfo
from app.services import course_service


async def test_resume_continues_a_chapter_that_was_written_but_not_reviewed(service, technical_input):
    """A real, confirmed ordering bug this fixes: `written` used to be
    marked true BEFORE the chapter was ever saved to disk, and review ran
    as part of the same atomic call with no checkpoint of its own - a crash
    (simulated here as a review failure) between writing and reviewing
    used to mean resume had no way to skip the write and continue only the
    review. Now it does."""
    record = await service.create_course(technical_input)
    course_id = record.course_id
    chapter_ids = [c.chapter_id for c in record.chapters]
    assert len(chapter_ids) >= 2
    first_chapter_id = chapter_ids[0]

    write_calls: list[str] = []
    original_write = service.writer.write_chapter

    async def counting_write(*, chapter, **kwargs):
        write_calls.append(chapter.id)
        return await original_write(chapter=chapter, **kwargs)

    review_attempts = {"n": 0}
    original_review = service.reviewer.review_chapter

    async def fails_once_for_first_chapter(*, chapter, **kwargs):
        if chapter.id == first_chapter_id and review_attempts["n"] == 0:
            review_attempts["n"] += 1
            raise RuntimeError("simulated crash during review")
        return await original_review(chapter=chapter, **kwargs)

    service.writer.write_chapter = counting_write  # type: ignore[method-assign]
    service.reviewer.review_chapter = fails_once_for_first_chapter  # type: ignore[method-assign]

    result = await service.generate(course_id, GenerateRequest(mode="sync"), job_id="job-1")
    assert result.status == "ready"

    loaded = await service.get_course_record(course_id)
    progress = {p.chapter_id: p for p in loaded.chapters}
    assert progress[first_chapter_id].written is True
    # Review crashed - never silently marked done for content that wasn't
    # actually reviewed.
    assert progress[first_chapter_id].reviewed is False
    assert write_calls.count(first_chapter_id) == 1

    # Resume: the review wrapper no longer raises (review_attempts["n"] is
    # already 1) - this must continue straight into review, not repay the
    # write call.
    result2 = await service.generate(
        course_id, GenerateRequest(mode="sync", resume=True), job_id="job-2"
    )
    assert result2.status == "ready"

    assert write_calls.count(first_chapter_id) == 1  # never repeated
    for other_id in chapter_ids[1:]:
        assert write_calls.count(other_id) == 1  # an already-done chapter isn't rewritten either

    loaded = await service.get_course_record(course_id)
    progress = {p.chapter_id: p for p in loaded.chapters}
    assert progress[first_chapter_id].written is True
    assert progress[first_chapter_id].reviewed is True


async def test_resuming_a_fully_generated_course_does_no_duplicate_work(service, technical_input):
    """Idempotent resume: calling it again (and again) after everything is
    already written+reviewed must not touch anything already on disk."""
    record = await service.create_course(technical_input)
    course_id = record.course_id

    result = await service.generate(course_id, GenerateRequest(mode="sync"), job_id="job-1")
    assert result.status == "ready"
    chapters_before = [
        c.model_dump(mode="json") for c in service.storage.load_all_chapters(course_id)
    ]

    result2 = await service.generate(
        course_id, GenerateRequest(mode="sync", resume=True), job_id="job-2"
    )
    assert result2.chapters_generated == []
    assert result2.chapters_failed == []
    chapters_after = [
        c.model_dump(mode="json") for c in service.storage.load_all_chapters(course_id)
    ]
    assert chapters_after == chapters_before

    # Resuming AGAIN changes nothing further either.
    result3 = await service.generate(
        course_id, GenerateRequest(mode="sync", resume=True), job_id="job-3"
    )
    assert result3.chapters_generated == []


async def test_images_done_reflects_the_real_final_count_not_the_stale_pre_batch_value(
    service, technical_input
):
    """A real bug caught while implementing this: `images_done` was bumped
    on a SEPARATELY-loaded copy of the record each time an image finished,
    so the terminal save at the end of the run read back the stale
    pre-batch `images_done=0` and clobbered every one of those updates."""
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"), job_id="job-1")
    assert result.status == "ready"

    loaded = await service.get_course_record(record.course_id)
    assert loaded.run.images_total > 0
    assert loaded.run.images_done == loaded.run.images_total
    assert loaded.run.images_done == result.images_generated


async def test_cancel_generation_cancels_a_live_task_and_marks_the_run_cancelled(
    service, technical_input
):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(
        job_id="job-x", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso()
    )
    await service._save_course(record)

    async def never_finishes() -> None:
        await asyncio.sleep(10)

    task = asyncio.ensure_future(never_finishes())
    service._jobs[course_id] = task

    cancelled_live = await service.cancel_generation(course_id)
    assert cancelled_live is True

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()

    reloaded = await service.get_course_record(course_id)
    assert reloaded.run.state == "cancelled"


async def test_cancel_generation_on_an_already_finished_run_is_a_safe_no_op(
    service, technical_input
):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(
        job_id="job-x", state="done", started_at=utc_now_iso(), updated_at=utc_now_iso()
    )
    await service._save_course(record)

    cancelled_live = await service.cancel_generation(course_id)
    assert cancelled_live is False

    reloaded = await service.get_course_record(course_id)
    assert reloaded.run.state == "done"  # never clobbered to "cancelled"


async def test_start_generation_refuses_a_second_call_while_a_persisted_run_is_fresh(
    service, technical_input
):
    """Cross-process guard: `self._jobs` (the in-memory registry) is empty
    here on purpose - simulating a second process, or this same process
    after a restart - but the persisted run was updated moments ago, so it's
    trusted as genuinely still in flight somewhere."""
    record = await service.create_course(technical_input)
    course_id = record.course_id
    record.run = RunInfo(
        job_id="job-fresh", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso()
    )
    await service._save_course(record)

    response = await service.start_generation(course_id, GenerateRequest())
    assert response.job_id == "job-fresh"
    assert course_id not in service._jobs


async def test_start_generation_proceeds_once_a_persisted_run_goes_stale(
    service, technical_input, settings
):
    record = await service.create_course(technical_input)
    course_id = record.course_id
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=settings.generation_heartbeat_stale_seconds + 30)
    ).isoformat()
    record.run = RunInfo(job_id="job-old", state="running", started_at=stale, updated_at=stale)
    await service._save_course(record)

    response = await service.start_generation(course_id, GenerateRequest())
    assert response.job_id != "job-old"
    assert course_id in service._jobs

    task = service._jobs.get(course_id)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_per_image_save_during_generation_never_persists_overlapping_blocks(
    service, technical_input
):
    """Real, confirmed bug: an image block's `layout.y`/`layout.height` is
    set from a pre-generation placeholder estimate (DEFAULT_IMAGE_ASPECT, a
    squat 16:9 guess - see app.course.document.layout.image_box_height)
    before the image is actually generated. `ImageService.generate_missing`'s
    `on_progress` callback saves the document the moment EACH image finishes
    (see course_service.py's `_on_image_done`, added for crash-recovery
    durability) - before this fix, that save happened BEFORE the document
    was reflowed, so a real image far taller than the placeholder guess (a
    tall flow_chart diagram in practice; an artificially tall illustration
    here) was saved with its real content dimensions while every block after
    it on the page still sat at its old, too-high position. Anyone reading
    the document in that window - the live editor/preview, confirmed via a
    real generated course - saw the next block's text rendered ON TOP of the
    image instead of below it."""
    from io import BytesIO

    from PIL import Image

    from app.course.document.layout_validator import page_overflow

    record = await service.create_course(technical_input)
    course_id = record.course_id

    # Every illustration call returns a real, deliberately TALL PNG (far off
    # the DEFAULT_IMAGE_ASPECT 16:9 placeholder guess used before
    # generation) so the pre- vs post-generation height mismatch this test
    # checks for is actually exercised, not just theoretically possible.
    buffer = BytesIO()
    Image.new("RGB", (400, 1800), color=(200, 200, 200)).save(buffer, format="PNG")
    tall_png_bytes = buffer.getvalue()

    async def tall_image(*, prompt, size=None, phase="image", provider=None):
        return tall_png_bytes

    service.ai.image = tall_image  # type: ignore[method-assign]

    # Two independent signals, both direct and both discriminating:
    # (1) a reflow happens immediately before EVERY per-image save - not
    #     just once at the very end of the whole batch - so `events` must
    #     never show two consecutive "save"s without a "reflow" between them
    #     while images are being generated; (2) whichever snapshot actually
    #     got saved is never internally overlapping. (1) is what actually
    #     distinguishes the fix: this course's mock content doesn't reliably
    #     place a trailing block right after an image on the same page for
    #     every run, so (2) alone could pass by coincidence even on the old,
    #     buggy code if no image happened to have a same-page successor.
    events: list[str] = []
    saved_snapshots = []
    capturing = {"on": False}

    original_reflow = course_service.reflow_document

    def counting_reflow(document, template):
        if capturing["on"]:
            events.append("reflow")
        return original_reflow(document, template)

    course_service.reflow_document = counting_reflow  # type: ignore[assignment]

    original_save_document = service._save_document

    async def capturing_save_document(document):
        if capturing["on"]:
            events.append("save")
            saved_snapshots.append(document.model_copy(deep=True))
        await original_save_document(document)

    service._save_document = capturing_save_document  # type: ignore[method-assign]

    # Only events strictly DURING `generate_missing` (i.e. from each
    # `on_progress` callback, one per completed image) are what this test is
    # about - the write/review phase saves a document per chapter (before
    # this window) and the post-batch repair/compaction/TOC saves (after it)
    # are each legitimate, one-off saves with no reflow required between
    # them, and would otherwise look like the same bug this test checks for.
    original_generate_missing = service.images.generate_missing

    async def generate_missing_capturing_window(*args, **kwargs):
        capturing["on"] = True
        try:
            return await original_generate_missing(*args, **kwargs)
        finally:
            capturing["on"] = False

    service.images.generate_missing = generate_missing_capturing_window  # type: ignore[method-assign]

    try:
        result = await service.generate(course_id, GenerateRequest(mode="sync"), job_id="job-1")
    finally:
        course_service.reflow_document = original_reflow  # type: ignore[assignment]

    assert result.status == "ready"
    assert result.images_generated >= 2  # otherwise this course can't exercise "every" save
    save_count = events.count("save")
    assert save_count >= result.images_generated
    # Walk `events`: between any two consecutive "save"s there must be a
    # "reflow" - i.e. no image's save was ever immediately preceded by
    # another save with no reflow in between (the old, buggy ordering).
    last_was_save = False
    for event in events:
        if event == "save":
            assert not last_was_save, (
                "a document save happened with no reflow since the previous "
                "save - an image's real dimensions could be persisted "
                "without repositioning the blocks after it"
            )
            last_was_save = True
        else:
            last_was_save = False

    for snapshot in saved_snapshots:
        for page in snapshot.pages:
            overflow = page_overflow(page)
            assert overflow <= 2.0, (
                f"page {page.page_number} had overlapping blocks "
                f"(overflow={overflow:.1f}px) in a document saved mid-generation"
            )
