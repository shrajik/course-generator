"""AI memory layer: prompt-level backward compatibility and agent wiring.

These never touch the database - they verify the two guarantees that matter
most for safety: (1) a prompt built with no/empty memory is byte-identical to
one built before this feature existed, and (2) each agent actually receives
memory at the right stage and nowhere else, without requiring a real DB.
"""

from __future__ import annotations

from app.agents import prompts
from app.agents.planner import PlannerAgent
from app.agents.prompts import ContinuityContext
from app.agents.reviewer import ReviewerAgent
from app.agents.writer import WriterAgent
from app.course.templates.registry import load_template
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.memory import MemoryContext
from app.services.memory_service import MemoryService


class _FakeMemoryService(MemoryService):
    """Returns a canned, non-empty context for one stage only, so a test can
    prove an agent both receives memory when available and doesn't leak it
    into stages that shouldn't see it."""

    def __init__(self, *, only_stage: str, note: str) -> None:
        super().__init__()
        self.only_stage = only_stage
        self.note = note
        self.calls: list[str] = []

    async def build_context(self, *, stage, **kwargs) -> MemoryContext:  # type: ignore[override]
        self.calls.append(stage)
        if stage != self.only_stage:
            return MemoryContext()
        return MemoryContext(sample_notes=[self.note])


def _course_input() -> CourseInput:
    return CourseInput(
        course_title="Intro to Testing",
        toc=[{"title": "Chapter One"}],
        target_audience="engineers",
        template="technical",
    )


def _blueprint() -> CourseBlueprint:
    return CourseBlueprint(
        course_title="Intro to Testing",
        audience="engineers",
        template_id="technical_v1",
        course_summary="summary",
        chapters=[BlueprintChapter(id="chapter_1", title="Chapter One", order=1)],
    )


# ---------------------------------------------------------------------------
# memory_section() / backward compatibility
# ---------------------------------------------------------------------------


def test_memory_section_is_empty_string_for_blank_input():
    assert prompts.memory_section("") == ""
    assert prompts.memory_section("   ") == ""
    assert prompts.memory_section(None) == ""  # type: ignore[arg-type]


def test_memory_section_wraps_non_empty_memory():
    rendered = prompts.memory_section("past courses used a quadrant framework")
    assert "RELEVANT MEMORY" in rendered
    assert "past courses used a quadrant framework" in rendered


def test_planner_user_with_empty_memory_matches_no_memory_arg():
    template = load_template("technical")
    course_input = _course_input()
    with_default = prompts.planner_user(course_input, template)
    with_empty = prompts.planner_user(course_input, template, memory="")
    assert with_default == with_empty


def test_writer_user_with_empty_memory_matches_no_memory_arg():
    template = load_template("technical")
    blueprint = _blueprint()
    chapter = blueprint.chapters[0]
    continuity = ContinuityContext()
    with_default = prompts.writer_user(
        blueprint, chapter, template, None, continuity, course_input=_course_input()
    )
    with_empty = prompts.writer_user(
        blueprint, chapter, template, None, continuity, course_input=_course_input(), memory=""
    )
    assert with_default == with_empty


def test_reviewer_user_with_empty_memory_matches_no_memory_arg():
    template = load_template("technical")
    blueprint = _blueprint()
    chapter = blueprint.chapters[0]
    continuity = ContinuityContext()
    blocks = [{"type": "heading", "content": {"text": "Chapter One"}}]
    with_default = prompts.reviewer_user(
        blueprint, chapter, template, blocks, course_input=_course_input(), continuity=continuity
    )
    with_empty = prompts.reviewer_user(
        blueprint, chapter, template, blocks, course_input=_course_input(),
        continuity=continuity, memory="",
    )
    assert with_default == with_empty


def test_writer_user_includes_memory_when_present():
    template = load_template("technical")
    blueprint = _blueprint()
    chapter = blueprint.chapters[0]
    rendered = prompts.writer_user(
        blueprint, chapter, template, None, ContinuityContext(),
        course_input=_course_input(), memory="a relevant past example",
    )
    assert "a relevant past example" in rendered
    assert "RELEVANT MEMORY" in rendered


# ---------------------------------------------------------------------------
# agent wiring: each stage gets its own memory, nothing bleeds elsewhere
# ---------------------------------------------------------------------------


async def test_planner_receives_planner_stage_memory(service, technical_input):
    fake = _FakeMemoryService(only_stage="planner", note="reuse a proven 6-chapter shape")
    agent = PlannerAgent(service.ai, service.settings, memory=fake)
    blueprint = await agent.plan(technical_input)
    assert blueprint.chapters
    assert fake.calls == ["planner"]


async def test_writer_receives_writer_stage_memory_not_reviewer(service, technical_input):
    from app.course.templates.registry import load_template as _lt

    fake = _FakeMemoryService(only_stage="writer", note="prefer worked examples over theory")
    agent = WriterAgent(service.ai, service.settings, memory=fake)
    template = _lt("technical")
    bp = _blueprint()
    blocks, summary = await agent.write_chapter(
        blueprint=bp,
        chapter=bp.chapters[0],
        template=template,
        course_input=technical_input,
        research=None,
        continuity=ContinuityContext(),
    )
    assert blocks
    assert fake.calls == ["writer"]


async def test_reviewer_receives_reviewer_stage_memory(service, technical_input):
    from app.course.templates.registry import load_template as _lt

    fake = _FakeMemoryService(only_stage="reviewer", note="past courses lost points on missing examples")
    agent = ReviewerAgent(service.ai, service.settings, memory=fake)
    template = _lt("technical")
    bp = _blueprint()
    blocks = [
        Block(type=BlockType.HEADING, content={"text": "Chapter One", "level": 1}),
        Block(type=BlockType.PARAGRAPH, content={"text": "Some content about the chapter."}),
    ]
    review = await agent.review_chapter(
        blueprint=bp,
        chapter=bp.chapters[0],
        template=template,
        course_input=technical_input,
        blocks=blocks,
        continuity=ContinuityContext(),
    )
    assert review is not None
    assert fake.calls == ["reviewer"]
