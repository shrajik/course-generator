"""Unit tests for the live-writing stream: the partial-JSON text extractor and
the in-memory pub/sub hub. No network/DB - pure logic, fast and deterministic.
"""

from __future__ import annotations

import asyncio

from app.core.generation_stream import GenerationStreamHub, extract_preview_text
from app.schemas.course import GenerateRequest


class TestExtractPreviewText:
    def test_empty_buffer_yields_empty_text(self):
        assert extract_preview_text("") == ""

    def test_single_closed_text_field(self):
        raw = '{"blocks": [{"type": "heading", "text": "Introduction"}]}'
        assert extract_preview_text(raw) == "Introduction"

    def test_multiple_closed_fields_join_with_blank_line(self):
        raw = (
            '{"blocks": ['
            '{"type": "heading", "text": "Introduction"}, '
            '{"type": "paragraph", "text": "The export market has changed."}'
            "]}"
        )
        assert extract_preview_text(raw) == "Introduction\n\nThe export market has changed."

    def test_trailing_unclosed_field_is_shown_as_in_progress(self):
        # The model is still mid-sentence - no closing quote yet.
        raw = '{"blocks": [{"type": "paragraph", "text": "The global export market has'
        assert extract_preview_text(raw) == "The global export market has"

    def test_growing_buffer_extends_the_same_sentence(self):
        first = '{"blocks": [{"type": "paragraph", "text": "The global export market has'
        second = first + " changed significantly"
        third = second + ' in recent years."}]}'

        assert extract_preview_text(first) == "The global export market has"
        assert extract_preview_text(second) == "The global export market has changed significantly"
        assert (
            extract_preview_text(third)
            == "The global export market has changed significantly in recent years."
        )

    def test_escaped_quotes_do_not_terminate_the_match_early(self):
        raw = r'{"text": "She said \"hello\" to the class"}'
        assert extract_preview_text(raw) == 'She said "hello" to the class'

    def test_dangling_backslash_is_dropped_not_corrupted(self):
        # A backslash just arrived but its escaped character hasn't yet.
        raw = '{"text": "trailing backslash \\'
        result = extract_preview_text(raw)
        assert result == "trailing backslash "

    def test_ignores_non_text_string_fields(self):
        raw = '{"chapter_title": "Chapter 1", "blocks": [{"type": "heading", "text": "Real content"}]}'
        assert extract_preview_text(raw) == "Real content"

    def test_empty_text_fields_are_skipped(self):
        raw = '{"blocks": [{"type": "divider", "text": ""}, {"type": "heading", "text": "Title"}]}'
        assert extract_preview_text(raw) == "Title"


class TestGenerationStreamHub:
    def test_start_then_append_builds_text(self):
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.append("course_1", "chapter_1", '{"text": "Hello')
        hub.append("course_1", "chapter_1", ' world"}')

        snapshot = hub.snapshot("course_1")
        assert len(snapshot) == 1
        assert snapshot[0].text == "Hello world"
        assert snapshot[0].phase == "writing"
        assert snapshot[0].done is False

    def test_finish_marks_done(self):
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.append("course_1", "chapter_1", '{"text": "done"}')
        hub.finish("course_1", "chapter_1")

        assert hub.snapshot("course_1")[0].done is True

    def test_a_retry_resets_the_buffer_not_appends_to_it(self):
        """`start()` is called again on every retry - the failed attempt's
        partial text must not leak into the new attempt's."""
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.append("course_1", "chapter_1", '{"text": "first attempt got cut off')

        hub.start("course_1", "chapter_1", "writing")  # retry
        hub.append("course_1", "chapter_1", '{"text": "second attempt succeeds"}')

        assert hub.snapshot("course_1")[0].text == "second attempt succeeds"

    def test_append_after_done_is_a_noop(self):
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.finish("course_1", "chapter_1")
        hub.append("course_1", "chapter_1", '{"text": "too late"}')

        assert hub.snapshot("course_1")[0].text == ""

    def test_clear_course_removes_all_chapter_state(self):
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.start("course_1", "chapter_2", "writing")
        hub.clear_course("course_1")

        assert hub.snapshot("course_1") == []

    def test_snapshot_is_isolated_per_course(self):
        hub = GenerationStreamHub()
        hub.start("course_1", "chapter_1", "writing")
        hub.start("course_2", "chapter_1", "writing")

        assert len(hub.snapshot("course_1")) == 1
        assert len(hub.snapshot("course_2")) == 1
        assert hub.snapshot("course_3") == []

    def test_subscribers_receive_published_events_in_order(self):
        async def run():
            hub = GenerationStreamHub()
            queue = hub.subscribe("course_1")

            hub.start("course_1", "chapter_1", "writing")
            hub.append("course_1", "chapter_1", '{"text": "hi"}')
            hub.finish("course_1", "chapter_1")

            events = [queue.get_nowait() for _ in range(3)]
            assert events[0]["phase"] == "writing"
            assert events[0]["done"] is False
            assert events[1]["text"] == "hi"
            assert events[2]["done"] is True

        asyncio.run(run())

    def test_unsubscribe_stops_further_delivery(self):
        hub = GenerationStreamHub()
        queue = hub.subscribe("course_1")
        hub.unsubscribe("course_1", queue)

        hub.start("course_1", "chapter_1", "writing")

        assert queue.empty()


async def test_writer_streams_real_content_into_the_hub(service, technical_input):
    """End-to-end through the real pipeline (offline mock client): proves
    `on_delta` is actually wired from CourseService through WriterAgent/
    ReviewerAgent through `AIClient.structured()` into the hub, and that the
    text that lands there is real (extracted from what the mock client
    actually "generated"), not a placeholder.

    A chapter's "writing" state is *replaced* (not kept) once review starts
    (see GenerationStreamHub.start) - a live subscriber sees that transition
    as it happens, so the test subscribes up front and inspects the event
    stream, rather than only the final snapshot.
    """
    from app.core.generation_stream import get_stream_hub

    hub = get_stream_hub()
    record = await service.create_course(technical_input, run_planner=True)
    queue = hub.subscribe(record.course_id)

    await service.generate(record.course_id, GenerateRequest(mode="sync", generate_images=False))

    chapters = service.storage.load_all_chapters(record.course_id)
    assert chapters  # sanity: the run actually produced chapters

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events

    writing_events = [e for e in events if e["phase"] == "writing" and e["text"]]
    assert writing_events, "expected at least one non-empty writing-phase event"

    reviewing_events = [e for e in events if e["phase"] == "reviewing"]
    assert reviewing_events, "expected the phase to actually transition to reviewing"

    # At least one streamed writing chunk overlaps with what was actually
    # persisted - the live preview isn't showing something invented.
    persisted_text = " ".join(
        block.get("content", {}).get("text", "")
        for chapter in chapters
        for block in chapter.blocks
    )

    def any_fragment_persisted(text: str) -> bool:
        return any(
            fragment.strip() and fragment.strip() in persisted_text
            for fragment in text.split("\n\n")
        )

    assert any(any_fragment_persisted(e["text"]) for e in writing_events)
