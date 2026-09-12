"""Behaviour of the performance work.

Wall-clock speed is measured with `MOCK_LATENCY_MS`, which gives every simulated
model call a fixed cost. That turns the mock client into a way to measure the
pipeline's *concurrency* without needing an API key: if chapters really are
written in parallel, the run gets faster when the limit is raised.
"""

from __future__ import annotations

import time

import pytest

from app.agents.writer import WriterAgent
from app.core import concurrency
from app.core.config import get_settings, reset_settings_cache
from app.core.metrics import run_metrics
from app.schemas.course import GenerateRequest, TocItem
from app.services import course_service, storage_service
from app.services.openai_service import set_ai_client


def _wide_input(base, chapters: int = 6):
    return base.model_copy(
        update={"toc": [TocItem(title=f"Chapter Topic {i}") for i in range(1, chapters + 1)]}
    )


@pytest.fixture
def slow_service(monkeypatch, tmp_path):
    """A service whose mock calls each cost 60ms, so concurrency is observable."""
    monkeypatch.setenv("MOCK_LATENCY_MS", "60")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "slow"))
    reset_settings_cache()
    concurrency.reset_limiters()
    storage_service.reset_storage()
    course_service.reset_course_service()
    set_ai_client(None)
    (tmp_path / "slow" / "courses").mkdir(parents=True, exist_ok=True)
    yield course_service.get_course_service()
    reset_settings_cache()
    concurrency.reset_limiters()
    storage_service.reset_storage()
    course_service.reset_course_service()
    set_ai_client(None)


# --- the structural change -------------------------------------------------


def _track_peak_concurrent_writes(monkeypatch) -> dict[str, int]:
    """Counts how many `WriterAgent.write_chapter` calls are in flight at
    once - a direct, phase-scoped measurement of writer concurrency.

    The three tests below used to read `RunMetrics`' whole-run
    `peak_concurrent_calls` instead, but that counter has no idea which
    *phase* a call belongs to: research (`research_service.py`) and image
    generation (`image_service.py`) each run their own, unrelated
    `asyncio.gather` fan-out with their own concurrency limiter, elsewhere in
    the same `generate()` call, and both feed the same counter. That made the
    metric register concurrency from a completely different phase regardless
    of `writer_concurrency` - not flaky exactly, but measuring the wrong
    thing. Patching `write_chapter` itself (the same technique already used
    below by `test_parallel_chapters_get_topic_boundaries`) counts only what
    these tests are actually about, and needs no wall-clock assumption:
    either N writer calls were genuinely in flight at once, or they weren't.

    Call this ONCE per test, even when comparing two `generate()` runs -
    reset `state["peak"] = 0` between them instead of calling this a second
    time. A second call would capture the *already-patched* method as its
    own "original", double-wrapping it so the first run's counter keeps
    getting updated by the second run too.
    """
    state = {"current": 0, "peak": 0}
    original = WriterAgent.write_chapter

    async def wrapped(self, **kwargs):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        try:
            return await original(self, **kwargs)
        finally:
            state["current"] -= 1

    monkeypatch.setattr(WriterAgent, "write_chapter", wrapped)
    return state


async def test_parallel_writing_is_faster_than_sequential(
    slow_service, technical_input, monkeypatch
):
    """The headline change: chapters no longer wait for each other."""
    course_input = _wide_input(technical_input, 6)
    settings = slow_service.settings
    settings.writer_concurrency = 6
    peak = _track_peak_concurrent_writes(monkeypatch)

    settings.writing_mode = "sequential"
    record = await slow_service.create_course(course_input)
    started = time.perf_counter()
    sequential = await slow_service.generate(record.course_id, GenerateRequest(mode="sync"))
    sequential_wall = time.perf_counter() - started
    sequential_peak = peak["peak"]
    peak["peak"] = 0  # `current` is already back to 0 between runs

    settings.writing_mode = "parallel"
    record2 = await slow_service.create_course(course_input)
    started = time.perf_counter()
    parallel = await slow_service.generate(record2.course_id, GenerateRequest(mode="sync"))
    parallel_wall = time.perf_counter() - started
    parallel_peak = peak["peak"]

    assert len(sequential.chapters_generated) == 6
    assert len(parallel.chapters_generated) == 6

    # Sequential mode has exactly one `write_chapter` call in flight at a
    # time by construction (a plain `for` loop, no `asyncio.gather` - see
    # `_write_sequential`); parallel mode really does run all 6 chapters'
    # writer calls concurrently. This is the deterministic "parallel differs
    # from sequential" proof - not inferred from timing.
    assert sequential_peak == 1
    assert parallel_peak == 6

    # With genuinely more overlap and identical total work, parallel cannot
    # take longer than sequential - a plain ordering check, not a specific
    # ratio, so it isn't sensitive to whatever else is running on this
    # machine.
    assert parallel_wall < sequential_wall, (
        f"parallel {parallel_wall:.2f}s vs sequential {sequential_wall:.2f}s"
    )


async def test_writer_calls_actually_overlap(slow_service, technical_input, monkeypatch):
    slow_service.settings.writing_mode = "parallel"
    slow_service.settings.writer_concurrency = 4
    peak = _track_peak_concurrent_writes(monkeypatch)
    record = await slow_service.create_course(_wide_input(technical_input, 4))
    await slow_service.generate(record.course_id, GenerateRequest(mode="sync"))
    # All 4 chapters are independent and the limiter permits all 4 at once -
    # a genuinely parallel writer must let all 4 write_chapter calls be in
    # flight together at some point during the run.
    assert peak["peak"] == 4


async def test_raising_the_writer_limit_shortens_the_write_phase(
    slow_service, technical_input, monkeypatch
):
    """Raising the limit is the actual mechanism that lets more chapters
    overlap - proved directly via the live concurrent-call counter rather
    than inferred from a wall-clock ratio (see `_track_peak_concurrent_writes`
    above for why that counter isn't RunMetrics' whole-run one)."""
    course_input = _wide_input(technical_input, 6)
    slow_service.settings.writing_mode = "parallel"
    peak = _track_peak_concurrent_writes(monkeypatch)

    slow_service.settings.writer_concurrency = 1
    record = await slow_service.create_course(course_input)
    narrow = await slow_service.generate(record.course_id, GenerateRequest(mode="sync"))
    narrow_peak = peak["peak"]
    assert narrow_peak == 1, "concurrency=1 must never overlap"
    peak["peak"] = 0  # `current` is already back to 0 between runs

    concurrency.reset_limiters()
    slow_service.settings.writer_concurrency = 6
    record2 = await slow_service.create_course(course_input)
    wide = await slow_service.generate(record2.course_id, GenerateRequest(mode="sync"))
    wide_peak = peak["peak"]
    assert wide_peak == 6, (
        "concurrency=6 with 6 independent chapters should let all 6 overlap"
    )

    # With genuinely more overlap and the same total work, the wide run
    # cannot take longer than the narrow one - a plain ordering check
    # instead of a specific ratio.
    narrow_phase = narrow.timings["phases"]["write+review"]["seconds"]
    wide_phase = wide.timings["phases"]["write+review"]["seconds"]
    assert wide_phase < narrow_phase, f"{wide_phase} vs {narrow_phase}"


# --- continuity safeguards --------------------------------------------------


async def test_parallel_chapters_get_topic_boundaries(service, technical_input, monkeypatch):
    """Each chapter is told what its neighbours cover, so they don't overlap."""
    seen = []
    original = WriterAgent.write_chapter

    async def spy(self, **kwargs):
        seen.append(kwargs["continuity"])
        return await original(self, **kwargs)

    monkeypatch.setattr(WriterAgent, "write_chapter", spy)
    service.settings.writing_mode = "parallel"
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert len(seen) == 2
    # The first chapter knows what is coming later, which is what stops it
    # teaching chapter two's material.
    first = next(c for c in seen if not c.previous)
    assert first.upcoming, "chapter 1 was not told about later chapters"
    rendered = first.render()
    assert "RESERVED FOR LATER CHAPTERS" in rendered
    assert any(c.planned for c in seen), "planned summaries were not used"


async def test_continuity_pass_runs_once_after_a_parallel_run(service, technical_input):
    service.settings.writing_mode = "parallel"
    service.settings.continuity_pass = True
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    continuity_calls = [c for c in service.ai.calls if c.get("purpose") == "continuity"]
    assert len(continuity_calls) == 1


async def test_planned_summaries_exist_for_every_chapter(service, technical_input):
    record = await service.create_course(technical_input)
    blueprint = service.storage.load_blueprint(record.course_id)
    assert all(chapter.summary.strip() for chapter in blueprint.chapters)


# --- fewer calls -----------------------------------------------------------


async def test_research_takes_one_call_per_chapter(service, technical_input):
    """Search + structuring in a single call instead of two."""
    assert get_settings().single_call_research is True
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    research_calls = [c for c in service.ai.calls if c["kind"] == "research"]
    structuring = [
        c for c in service.ai.calls if str(c.get("purpose", "")).startswith("research_structure")
    ]
    assert len(research_calls) == 2  # one per chapter
    assert structuring == []  # no second shaping call


async def test_surgical_revision_rewrites_only_the_flagged_blocks(service, technical_input):
    from app.agents.prompts import ContinuityContext
    from app.course.templates.registry import load_template
    from app.schemas.review import ChapterReview, ReviewIssue

    service.settings.surgical_revision = True
    record = await service.create_course(technical_input)
    blueprint = service.storage.load_blueprint(record.course_id)
    chapter = blueprint.chapters[0]
    template = load_template(blueprint.template_id)

    blocks, summary = await service.writer.write_chapter(
        blueprint=blueprint,
        chapter=chapter,
        template=template,
        course_input=record.input,
        research=None,
        continuity=ContinuityContext(),
    )
    before = [block.content.get("text") for block in blocks]

    review = ChapterReview(
        approved=False,
        issues=[
            ReviewIssue(
                severity="blocker",
                category="clarity",
                block_index=1,
                description="Too wordy",
                suggestion="Shorten it",
            )
        ],
    )
    revised, _ = await service.writer.revise_chapter(
        blueprint=blueprint,
        chapter=chapter,
        template=template,
        course_input=record.input,
        research=None,
        continuity=ContinuityContext(),
        review=review,
        blocks=blocks,
        summary=summary,
    )

    assert len(revised) == len(blocks)
    assert revised[1].content.get("text") != before[1]  # the flagged block changed
    untouched = [i for i in range(len(blocks)) if i != 1]
    for index in untouched:
        assert revised[index].content == blocks[index].content


# --- background runs, resume, early document -------------------------------


async def test_background_generation_returns_immediately(service, technical_input):
    record = await service.create_course(technical_input)
    response = await service.start_generation(
        record.course_id, GenerateRequest(mode="background")
    )
    assert response.accepted is True
    assert response.job_id.startswith("job_")

    task = service._jobs.get(record.course_id)
    assert task is not None
    await task

    reloaded = service.storage.load_course(record.course_id)
    assert reloaded.status == "ready"
    assert reloaded.run is not None
    assert reloaded.run.state == "done"
    assert reloaded.run.chapters_done == 2
    assert reloaded.run.timings


async def test_document_exists_before_the_run_finishes(service, technical_input):
    """The editor must be openable on chapter 1 while later chapters are written."""
    course_input = _wide_input(technical_input, 4)
    record = await service.create_course(course_input)
    seen_early = False
    original = WriterAgent.write_chapter
    service.settings.writing_mode = "sequential"  # deterministic ordering

    async def spy(self, **kwargs):
        nonlocal seen_early
        if kwargs["chapter"].order == 4 and service.storage.has_document(record.course_id):
            seen_early = True
        return await original(self, **kwargs)

    import pytest as _pytest  # local import keeps the monkeypatch scoped

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(WriterAgent, "write_chapter", spy)
        await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert seen_early, "document was not saved until the whole run finished"


async def test_resume_skips_chapters_already_on_disk(service, technical_input):
    record = await service.create_course(_wide_input(technical_input, 4))
    await service.generate(
        record.course_id,
        GenerateRequest(chapter_ids=["chapter_1", "chapter_2"], mode="sync"),
    )
    result = await service.generate(record.course_id, GenerateRequest(resume=True, mode="sync"))
    assert result.chapters_generated == ["chapter_3", "chapter_4"]


async def test_resume_with_nothing_left_is_not_an_error(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    result = await service.generate(record.course_id, GenerateRequest(resume=True, mode="sync"))
    assert result.chapters_generated == []
    assert result.status == "ready"


async def test_run_state_carries_progress_and_timings(service, technical_input, client):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    body = client.get(f"/api/courses/{record.course_id}/run").json()
    assert body["run"]["state"] == "done"
    assert body["run"]["chapters_done"] == 2
    assert body["run"]["timings"]["calls"] > 0


# --- instrumentation --------------------------------------------------------


async def test_metrics_capture_phases_and_calls(service, technical_input):
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))
    timings = result.timings
    assert timings["calls"] > 0
    assert "write+review" in timings["phases"]
    assert "research" in timings["phases"]
    assert timings["wall_seconds"] >= 0


async def test_metrics_are_a_noop_without_a_run_context():
    from app.core.metrics import current_metrics, phase

    assert current_metrics() is None
    with phase("nothing"):  # must not raise outside a run
        pass
    with run_metrics() as metrics:
        with phase("something"):
            pass
        assert "something" in metrics.phases


# --- adaptive concurrency --------------------------------------------------


async def test_limiter_shrinks_on_rate_limit_and_still_serves():
    limiter = concurrency.AdaptiveLimiter(4, name="test")
    assert limiter.effective_limit == 4
    await limiter.throttled()
    assert limiter.effective_limit == 2
    await limiter.throttled()
    assert limiter.effective_limit == 1
    await limiter.throttled()  # never below the minimum
    assert limiter.effective_limit == 1
    async with limiter.slot():
        pass  # work still gets through at the reduced limit


async def test_per_phase_concurrency_falls_back_to_the_global_limit():
    settings = get_settings()
    settings.max_concurrency = 5
    settings.writer_concurrency = 0
    settings.research_concurrency = 2
    assert settings.concurrency_for("writer") == 5
    assert settings.concurrency_for("research") == 2
