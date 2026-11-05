"""MOM Phase 4 - content levels (1 = text-only, 2 = text + visuals, 3 =
provisional experiential framing). Level 2 is the pre-existing default
behaviour - every assertion about it is really a backward-compatibility
guard, not a new feature test.
"""

from __future__ import annotations

from app.agents.prompts import content_level_brief
from app.agents.reviewer import ReviewerAgent, _content_level_3_issues
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest
from app.schemas.document import Block
from app.schemas.review import ChapterReview


# --- content_level_brief: pure prompt-text unit tests -------------------------


def test_level_2_brief_is_empty_the_pre_existing_default_behaviour():
    """Backward compatibility: every course created before this feature
    existed implicitly ran at "level 2" (text + visuals where useful) - this
    must stay byte-identical, the same contract learner_brief(None) has."""
    assert content_level_brief(2) == ""


def test_level_1_brief_forbids_image_blocks():
    brief = content_level_brief(1)
    assert "TEXT ONLY" in brief
    assert "not plan or request any" in brief
    assert "image" in brief.lower()


def test_level_3_brief_asks_for_experiential_content():
    brief = content_level_brief(3)
    assert "EXPERIENTIAL" in brief
    assert "scenario" in brief.lower() or "hands-on" in brief.lower()


def test_each_level_produces_distinct_text():
    briefs = {level: content_level_brief(level) for level in (1, 2, 3)}
    assert briefs[1] != briefs[2] != briefs[3]
    assert briefs[1] != briefs[3]


# --- approach_review_brief: the reviewer also learns the content level --------
# (Phase 3.10a) - complements test_learning_approaches.py's coverage of the
# same helper; this file adds the content_level=1 case specifically.


def test_approach_review_brief_includes_the_level_1_guidance_verbatim():
    from app.agents.prompts import approach_review_brief, content_level_brief
    from app.schemas.course import CourseInput

    course_input = CourseInput(
        course_title="Test",
        toc=[{"title": "Chapter One"}],
        target_audience="engineers",
        template="technical",
        content_level=1,
    )
    brief = approach_review_brief(course_input)
    assert content_level_brief(1) in brief


def test_approach_review_brief_is_empty_at_level_2_with_no_approaches():
    from app.agents.prompts import approach_review_brief
    from app.schemas.course import CourseInput

    course_input = CourseInput(
        course_title="Test",
        toc=[{"title": "Chapter One"}],
        target_audience="engineers",
        template="technical",
    )
    assert course_input.content_level == 2
    assert approach_review_brief(course_input) == ""


# --- end-to-end: the structural backstop (not just the prompt) ----------------


async def test_level_1_course_generates_zero_images_even_though_the_mock_writer_still_plans_some(
    service, technical_input
):
    """The real, confirmed reason this needs a STRUCTURAL backstop, not just
    a prompt instruction: the offline mock writer (app.services.mock_ai)
    deterministically includes an image block in its output regardless of
    what the prompt says (it doesn't "read" prompts) - a real model would
    honour content_level_brief's instruction and simply never plan one, but
    this test proves the backstop holds even when a writer (mock or a real
    one that ignores the instruction) asks for an image anyway."""
    level_1_input = technical_input.model_copy(update={"content_level": 1})
    record = await service.create_course(level_1_input)

    response = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert response.images_generated == 0
    document = await service.load_document(record.course_id)
    image_blocks = [block for _, block in document.iter_blocks() if block.type.value == "image"]
    # Any image block the mock writer planned anyway must never have been
    # actually generated (no asset path) - generate_images=False means
    # ImageService.generate_missing is never even called (see
    # CourseService._finalise), not that it ran and silently produced nothing.
    for block in image_blocks:
        if block.content.get("kind") in ("toc",):
            continue  # the deterministic, AI-free TOC page is unaffected by content_level
        assert not block.content.get("path")
        assert not block.content.get("generated")


async def test_level_2_course_is_unaffected_the_default_still_generates_images(
    service, technical_input
):
    """Regression guard: content_level defaulting to 2 (and this course not
    setting it explicitly) must behave exactly as before this feature
    existed - images still get attempted normally."""
    assert technical_input.content_level == 2  # the schema's own default
    record = await service.create_course(technical_input)

    response = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert response.images_generated > 0


# --- content level 3: a real validation path, not only a prompt line ----------
# The MOM explicitly requires Level 3 to have an actual generation/validation
# path, not just a prompt instruction a model can ignore - these tests exercise
# `_content_level_3_issues`, the deterministic, post-hoc structural check that
# feeds the EXISTING missing_required_blocks -> full-rewrite retry loop (see
# CourseService._generate_chapter's `max_review_revisions`), not a new system.


def _block(block_type: BlockType, content: dict) -> Block:
    return Block(type=block_type, content=content)


def test_level_3_issues_empty_at_other_levels():
    blocks = [_block(BlockType.PARAGRAPH, {"text": "just prose"})]
    assert _content_level_3_issues(blocks, 1) == []
    assert _content_level_3_issues(blocks, 2) == []


def test_level_3_flags_a_chapter_with_no_worked_exercise_or_scenario():
    blocks = [_block(BlockType.PARAGRAPH, {"text": "abstract explanation only"})]
    issues = _content_level_3_issues(blocks, 3)
    assert any("exercise` or `challenge`" in issue for issue in issues)
    assert any("case_study` block" in issue for issue in issues)


def test_level_3_rejects_an_exercise_that_is_just_a_labelled_heading():
    """A real, explicit MOM requirement: a block with no expected_outcome and
    no steps/hints is a heading with no real content, not a worked activity."""
    blocks = [
        _block(
            BlockType.EXERCISE,
            {"title": "Exercise", "instructions": "Try it yourself."},
        )
    ]
    issues = _content_level_3_issues(blocks, 3)
    assert any("exercise` or `challenge`" in issue for issue in issues)


def test_level_3_accepts_a_real_worked_exercise_and_a_case_study():
    blocks = [
        _block(
            BlockType.EXERCISE,
            {
                "title": "Exercise",
                "instructions": "Apply the approach to a real problem.",
                "steps": ["Pick a problem", "Apply the technique"],
                "expected_outcome": "A short write-up of what changed.",
            },
        ),
        _block(
            BlockType.CASE_STUDY,
            {
                "title": "A team turns it around",
                "context": "A team missing its targets.",
                "challenge": "No shared definition of done.",
                "outcome": "Forecasting became predictable.",
            },
        ),
    ]
    assert _content_level_3_issues(blocks, 3) == []


def test_level_3_accepts_a_challenge_block_and_a_story_block():
    """CHALLENGE reuses ExerciseContent and STORY is the lighter-weight
    scenario shape - both must count, not just EXERCISE/CASE_STUDY."""
    blocks = [
        _block(
            BlockType.CHALLENGE,
            {
                "title": "Challenge",
                "instructions": "Ship a fix under a tight deadline.",
                "hints": ["Scope ruthlessly"],
                "expected_outcome": "A merged, working fix.",
            },
        ),
        _block(
            BlockType.STORY,
            {"text": "A Monday morning problem.", "takeaway": "Structure beats improvisation."},
        ),
    ]
    assert _content_level_3_issues(blocks, 3) == []


def test_apply_structural_checks_wires_level_3_into_missing_required_blocks():
    """Integration: the check actually runs inside the reviewer's real
    structural-checks entry point, not just as a standalone function."""
    from app.schemas.course import CourseInput

    template_free_course = CourseInput(
        course_title="Test",
        toc=[{"title": "Chapter One"}],
        target_audience="engineers",
        template="technical",
        content_level=3,
    )
    review = ChapterReview(approved=True, issues=[])
    blocks = [_block(BlockType.PARAGRAPH, {"text": "abstract only"})]
    from app.course.templates.registry import load_template

    ReviewerAgent._apply_structural_checks(
        review,
        load_template("technical_v1"),
        blocks,
        template_free_course.learner_profile,
        template_free_course,
    )
    assert any("content level 3" in m for m in review.missing_required_blocks)
    assert review.needs_revision() is True


async def test_level_3_non_technical_course_satisfies_the_check_from_the_mock_writer(
    service, non_technical_input
):
    """The non-technical mock output already includes a worked exercise and a
    case study (see app.services.mock_ai), so a level-3 course built from it
    needs no extra revision - a genuine, non-circular pass of the real check
    against real (mock) generation output, not a hand-built fixture."""
    level_3_input = non_technical_input.model_copy(update={"content_level": 3})
    record = await service.create_course(level_3_input)

    response = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert response.chapters_generated
    chapters = service.storage.load_all_chapters(record.course_id)
    assert chapters
    for chapter in chapters:
        review = chapter.review or {}
        assert not any(
            "content level 3" in m for m in review.get("missing_required_blocks", [])
        )


async def test_level_3_technical_course_is_flagged_when_the_writer_never_adds_a_scenario(
    service, technical_input
):
    """Honest-failure regression guard: the mock technical-template output has
    no case_study/story block at all, so the structural check must keep
    flagging it through every revision attempt - proving the backstop is a
    real, active check during generation, not a check that silently no-ops."""
    level_3_input = technical_input.model_copy(update={"content_level": 3})
    record = await service.create_course(level_3_input)

    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    chapters = service.storage.load_all_chapters(record.course_id)
    assert chapters
    chapter = chapters[0]
    assert chapter.review is not None
    assert any(
        "content level 3" in m for m in chapter.review.get("missing_required_blocks", [])
    )
    assert chapter.review.get("approved") is False
