"""Live text streaming for chapter generation - in-memory pub/sub only.

The writer/reviser call the model with `stream=True` (see `openai_service.py`);
as real token deltas arrive they're pushed here and fanned out to any SSE
subscribers for that course (`GET /api/courses/{id}/generate/stream`).

This is deliberately NOT persisted and NOT the source of truth: the final,
schema-validated chapter (built by the existing `structured()` call exactly as
before) is what gets saved to storage/the document. This hub only carries an
ephemeral, best-effort *preview* of that same real output while it's still
in flight, extracted with a small regex heuristic (see `extract_preview_text`)
rather than a full JSON parser, since the buffer is transient invalid JSON
until the call finishes.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Literal

Phase = Literal["writing", "reviewing"]

# Matches any `"text": "..."` field - the flat prose field on DraftBlock and on
# BlockRevision's nested `block` (see app/schemas/draft.py) - handling escaped
# quotes/backslashes so a `\"` inside the string doesn't terminate the match.
_CLOSED_TEXT_RE = re.compile(r'"text"\s*:\s*"((?:\\.|[^"\\])*)"')
_OPEN_TEXT_RE = re.compile(r'"text"\s*:\s*"((?:\\.|[^"\\])*)$')
_ESCAPE_RE = re.compile(r"\\(.)")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


def _unescape(value: str) -> str:
    # Best-effort: a raw, possibly mid-escape buffer can end in a lone
    # backslash: strip it rather than let `_ESCAPE_RE` swallow the character
    # that hasn't arrived yet.
    if value.endswith("\\") and not value.endswith("\\\\"):
        value = value[:-1]
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), value)


def extract_preview_text(raw: str) -> str:
    """Best-effort human-readable text from a still-streaming JSON buffer.

    Never used for the persisted result - only for the live preview shown
    while the real `structured()` call is still in flight.
    """
    # A lone trailing backslash means an escape sequence is mid-arrival (its
    # second character hasn't streamed in yet); trim it so the open-text scan
    # below always ends on a real character, not a dangling escape.
    scan_raw = raw[:-1] if raw.endswith("\\") and not raw.endswith("\\\\") else raw

    closed = _CLOSED_TEXT_RE.findall(scan_raw)
    paragraphs = [_unescape(text) for text in closed if text.strip()]

    # A trailing, not-yet-closed "text" field is the sentence actively being
    # typed right now - only worth showing if it starts after the last closed
    # match (otherwise we'd re-show a field that's actually already closed).
    last_closed_end = 0
    for m in _CLOSED_TEXT_RE.finditer(scan_raw):
        last_closed_end = m.end()
    open_match = _OPEN_TEXT_RE.search(scan_raw)
    if open_match and open_match.start() >= last_closed_end:
        trailing = _unescape(open_match.group(1))
        if trailing.strip():
            paragraphs.append(trailing)

    return "\n\n".join(paragraphs)


@dataclass
class ChapterStreamState:
    course_id: str
    chapter_id: str
    phase: Phase
    raw: str = ""
    text: str = ""
    done: bool = False
    sequence: int = 0

    def to_event(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "phase": self.phase,
            "text": self.text,
            "done": self.done,
            "sequence": self.sequence,
        }


class GenerationStreamHub:
    """One process-wide hub. Generation runs as an in-process asyncio task
    (see CourseService._jobs), so an in-memory pub/sub is sufficient - no
    separate broker needed, and it naturally disappears when the process
    restarts (a reconnect then just sees no active stream, which is correct:
    nothing is actively streaming across a restart either)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, ChapterStreamState]] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def start(self, course_id: str, chapter_id: str, phase: Phase) -> None:
        """Begin (or restart, on a retry) a fresh stream for this chapter/phase.
        Called before every attempt, including retries, so a failed attempt's
        partial text never bleeds into the next one."""
        state = ChapterStreamState(
            course_id=course_id,
            chapter_id=chapter_id,
            phase=phase,
            sequence=self._next_sequence(),
        )
        self._states.setdefault(course_id, {})[chapter_id] = state
        self._publish(course_id, state)

    def append(self, course_id: str, chapter_id: str, delta: str) -> None:
        state = self._states.get(course_id, {}).get(chapter_id)
        if state is None or state.done:
            return
        state.raw += delta
        state.text = extract_preview_text(state.raw)
        state.sequence = self._next_sequence()
        self._publish(course_id, state)

    def finish(self, course_id: str, chapter_id: str) -> None:
        state = self._states.get(course_id, {}).get(chapter_id)
        if state is None:
            return
        state.done = True
        state.sequence = self._next_sequence()
        self._publish(course_id, state)
        # Keep the finished state around briefly for late subscribers (a
        # reconnect right as the chapter finishes) but drop it once the next
        # phase/chapter starts overwriting entries, to bound memory.

    def snapshot(self, course_id: str) -> list[ChapterStreamState]:
        """Every chapter currently (or just) streaming for this course -
        replayed to a new subscriber so a page refresh doesn't show a blank
        panel until the next delta happens to arrive."""
        return list(self._states.get(course_id, {}).values())

    def subscribe(self, course_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(course_id, set()).add(queue)
        return queue

    def unsubscribe(self, course_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(course_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(course_id, None)

    def clear_course(self, course_id: str) -> None:
        """Called once the whole run finishes - nothing is "currently
        streaming" for a finished course any more."""
        self._states.pop(course_id, None)

    def _publish(self, course_id: str, state: ChapterStreamState) -> None:
        for queue in self._subscribers.get(course_id, ()):
            queue.put_nowait(state.to_event())


_hub: GenerationStreamHub | None = None


def get_stream_hub() -> GenerationStreamHub:
    global _hub
    if _hub is None:
        _hub = GenerationStreamHub()
    return _hub


@dataclass
class _HubSink:
    """Adapter passed as `on_delta` into `AIClient.structured()` - satisfies
    the `StreamSink` protocol there without `openai_service.py` needing to
    know about the hub."""

    hub: GenerationStreamHub
    course_id: str
    chapter_id: str
    phase: Phase
    _started: bool = field(default=False, init=False)

    def reset(self) -> None:
        self.hub.start(self.course_id, self.chapter_id, self.phase)
        self._started = True

    def append(self, delta: str) -> None:
        if not self._started:
            self.reset()
        self.hub.append(self.course_id, self.chapter_id, delta)


def make_stream_sink(course_id: str, chapter_id: str, phase: Phase) -> _HubSink:
    return _HubSink(hub=get_stream_hub(), course_id=course_id, chapter_id=chapter_id, phase=phase)
