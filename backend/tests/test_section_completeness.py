"""MOM Phase 7 - section completeness contracts.

`BlueprintChapter` already carries a real, per-chapter contract (objective,
planned sections with key points, key concepts, prerequisites) - the gap was
that nothing ever checked a written chapter against it. This covers the two
halves of that fix:

1. `_section_completeness_issues` - the deterministic, objective half (empty
   headings, abrupt endings, broken Python) in app.agents.reviewer.
2. `chapter_contract_brief` - the meaning-dependent half, which hands the
   planner's own contract to the reviewer model so coverage becomes something
   it actually judges instead of something nobody checks.
"""

from __future__ import annotations

from app.agents.prompts import chapter_contract_brief, reviewer_user
from app.agents.reviewer import ReviewerAgent, _content_level_3_issues, _section_completeness_issues
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, ChapterSection, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.review import ChapterReview


def _block(block_type: BlockType, content: dict) -> Block:
    return Block(type=block_type, content=content)


# --- _section_completeness_issues: deterministic/objective checks -------------


def test_a_well_formed_chapter_has_no_completeness_issues():
    blocks = [
        _block(BlockType.HEADING, {"text": "Core Concept"}),
        _block(BlockType.PARAGRAPH, {"text": "Real content here."}),
        _block(BlockType.SUMMARY, {"title": "Summary", "key_takeaways": ["Point one."]}),
    ]
    assert _section_completeness_issues(blocks) == []


def test_an_empty_heading_is_flagged():
    blocks = [
        _block(BlockType.HEADING, {"text": "   "}),
        _block(BlockType.PARAGRAPH, {"text": "Some text."}),
    ]
    issues = _section_completeness_issues(blocks)
    assert any("heading with no real title" in i for i in issues)


def test_a_placeholder_heading_is_flagged():
    blocks = [
        _block(BlockType.HEADING, {"text": "TODO"}),
        _block(BlockType.PARAGRAPH, {"text": "Some text."}),
    ]
    issues = _section_completeness_issues(blocks)
    assert any("heading with no real title" in i for i in issues)


def test_a_real_heading_is_not_flagged_even_if_short():
    blocks = [_block(BlockType.HEADING, {"text": "Idempotency"})]
    issues = _section_completeness_issues(blocks)
    assert not any("heading with no real title" in i for i in issues)


def test_a_chapter_that_ends_on_a_bare_heading_is_flagged():
    blocks = [
        _block(BlockType.PARAGRAPH, {"text": "Intro."}),
        _block(BlockType.HEADING, {"text": "Advanced Topics"}),
    ]
    issues = _section_completeness_issues(blocks)
    assert any("ends immediately after a heading" in i for i in issues)


def test_a_chapter_that_ends_on_content_is_not_flagged():
    blocks = [
        _block(BlockType.HEADING, {"text": "Advanced Topics"}),
        _block(BlockType.PARAGRAPH, {"text": "The actual content."}),
    ]
    issues = _section_completeness_issues(blocks)
    assert not any("ends immediately after a heading" in i for i in issues)


def test_trailing_dividers_after_a_bare_heading_still_count_as_abrupt():
    blocks = [
        _block(BlockType.HEADING, {"text": "Advanced Topics"}),
        _block(BlockType.DIVIDER, {}),
    ]
    issues = _section_completeness_issues(blocks)
    assert any("ends immediately after a heading" in i for i in issues)


def test_empty_code_block_is_flagged():
    blocks = [_block(BlockType.CODE, {"language": "python", "code": ""})]
    issues = _section_completeness_issues(blocks)
    assert any("no code in it" in i for i in issues)


def test_syntactically_broken_python_is_flagged():
    blocks = [_block(BlockType.CODE, {"language": "python", "code": "def broken(:\n    pass"})]
    issues = _section_completeness_issues(blocks)
    assert any("does not parse" in i for i in issues)


def test_valid_python_is_not_flagged():
    blocks = [_block(BlockType.CODE, {"language": "python", "code": "def ok():\n    return 1\n"})]
    assert _section_completeness_issues(blocks) == []


def test_non_python_code_is_never_syntax_checked():
    """A deliberately "broken" (by Python's grammar) snippet in another
    language must not be judged by the wrong language's rules."""
    blocks = [_block(BlockType.CODE, {"language": "javascript", "code": "function f() {"})]
    assert _section_completeness_issues(blocks) == []


def test_integration_wires_into_missing_required_blocks():
    review = ChapterReview(approved=True, issues=[])
    blocks = [
        _block(BlockType.PARAGRAPH, {"text": "Intro."}),
        _block(BlockType.HEADING, {"text": "TODO"}),
    ]
    ReviewerAgent._apply_structural_checks(review, load_template("technical_v1"), blocks)
    assert any("heading with no real title" in m for m in review.missing_required_blocks)
    assert review.needs_revision() is True


def test_a_blank_required_block_finding_reaches_the_approval_gate():
    """These new findings must get the SAME verification treatment as the
    pre-existing section_completeness checks (placeholder headings, empty
    code) - independently_verified, blocker severity, reaching
    approval_blocking_findings - via the existing structural-override
    fallback, never a new approval mechanism."""
    from app.schemas.draft import GeneratedChapter
    from app.services.quality_framework import build_course_quality_report

    review = ChapterReview(approved=True, issues=[])
    blocks = [_block(BlockType.QUIZ, {"title": "Check", "questions": []})]
    structural_findings = ReviewerAgent._apply_structural_checks(
        review, load_template("technical_v1"), blocks
    )
    quiz_finding = next(f for f in structural_findings if "quiz with no questions" in f.description)
    assert quiz_finding.check == "section_completeness"
    assert quiz_finding.dimension == "completeness"

    chapter = GeneratedChapter(
        chapter_id="chapter_1", chapter_number=1, title="T", structural_findings=structural_findings
    )
    reloaded = GeneratedChapter.model_validate(chapter.model_dump(mode="json"))
    report = build_course_quality_report(course_id="c1", chapters=[reloaded], document=None)

    blocking = [f for f in report.approval_blocking_findings if "quiz with no questions" in f.description]
    assert len(blocking) == 1
    assert blocking[0].severity == "blocker"
    assert blocking[0].verification_status == "independently_verified"


# --- required-content-block emptiness (quality-governance follow-up, -----
# Phase 3.9a): a block of the right TYPE is present, but its own core
# content is genuinely blank - the same class of defect as the placeholder-
# heading/empty-code checks above, just for four more block types.


def test_an_empty_learning_objectives_list_is_flagged():
    blocks = [_block(BlockType.LEARNING_OBJECTIVES, {"title": "Objectives", "items": []})]
    issues = _section_completeness_issues(blocks)
    assert any("no real objectives listed" in i for i in issues)


def test_a_learning_objectives_list_of_blank_strings_is_also_flagged():
    """Non-empty list, but every entry is whitespace - still genuinely
    empty, not merely a zero-length list."""
    blocks = [_block(BlockType.LEARNING_OBJECTIVES, {"title": "Objectives", "items": ["   ", ""]})]
    issues = _section_completeness_issues(blocks)
    assert any("no real objectives listed" in i for i in issues)


def test_a_populated_learning_objectives_list_is_not_flagged():
    blocks = [
        _block(BlockType.LEARNING_OBJECTIVES, {"title": "Objectives", "items": ["Explain X."]})
    ]
    assert _section_completeness_issues(blocks) == []


def test_an_empty_quiz_is_flagged():
    blocks = [_block(BlockType.QUIZ, {"title": "Check", "questions": []})]
    issues = _section_completeness_issues(blocks)
    assert any("quiz with no questions" in i for i in issues)


def test_a_populated_quiz_is_not_flagged():
    blocks = [
        _block(
            BlockType.QUIZ,
            {"title": "Check", "questions": [{"question": "What is X?", "answer": "X is..."}]},
        )
    ]
    assert _section_completeness_issues(blocks) == []


def test_a_summary_with_no_key_takeaways_is_flagged():
    blocks = [_block(BlockType.SUMMARY, {"title": "Summary", "key_takeaways": []})]
    issues = _section_completeness_issues(blocks)
    assert any("no key takeaways listed" in i for i in issues)


def test_a_summary_with_blank_next_steps_only_is_not_flagged():
    """`next_steps` is deliberately not required - no evidence it must
    always be non-empty, unlike `key_takeaways` - a summary with real
    takeaways and no next steps is valid, not a defect."""
    blocks = [
        _block(
            BlockType.SUMMARY,
            {"title": "Summary", "key_takeaways": ["Point one."], "next_steps": []},
        )
    ]
    assert _section_completeness_issues(blocks) == []


def test_a_populated_summary_is_not_flagged():
    blocks = [
        _block(
            BlockType.SUMMARY,
            {"title": "Summary", "key_takeaways": ["Point one."], "next_steps": ["Try it."]},
        )
    ]
    assert _section_completeness_issues(blocks) == []


def test_an_exercise_with_blank_instructions_is_flagged():
    blocks = [_block(BlockType.EXERCISE, {"title": "Try It", "instructions": "   "})]
    issues = _section_completeness_issues(blocks)
    assert any("exercise with no instructions" in i for i in issues)


def test_a_challenge_with_blank_instructions_is_also_flagged():
    """CHALLENGE shares ExerciseContent with EXERCISE and gets the same
    check, matching how _content_level_3_issues already treats them
    identically."""
    blocks = [_block(BlockType.CHALLENGE, {"title": "Challenge", "instructions": ""})]
    issues = _section_completeness_issues(blocks)
    assert any("challenge with no instructions" in i for i in issues)


def test_an_exercise_with_real_instructions_but_no_steps_or_hints_is_not_flagged_here():
    """`steps`/`hints`/`expected_outcome` are schema-optional-by-default and
    not this check's concern - only `_content_level_3_issues` (and only at
    content_level 3) cares whether they're populated. This proves the two
    checks are genuinely orthogonal, not redundant."""
    blocks = [
        _block(
            BlockType.EXERCISE,
            {"title": "Try It", "instructions": "Implement a stack.", "steps": [], "hints": []},
        )
    ]
    assert _section_completeness_issues(blocks) == []


def test_a_populated_exercise_is_not_flagged():
    blocks = [
        _block(BlockType.EXERCISE, {"title": "Try It", "instructions": "Implement a stack."})
    ]
    assert _section_completeness_issues(blocks) == []


def test_a_blank_exercise_produces_two_distinct_non_duplicate_findings_at_level_3():
    """The two checks can both legitimately fire for the SAME block without
    being duplicates: _section_completeness_issues flags THIS block's own
    blank instructions; _content_level_3_issues separately flags that the
    CHAPTER as a whole lacks a qualifying worked exercise. Different
    granularity, different wording, both true - not a duplicate finding."""
    blocks = [_block(BlockType.EXERCISE, {"title": "Try It", "instructions": ""})]

    completeness_issues = _section_completeness_issues(blocks)
    level_3_issues = _content_level_3_issues(blocks, content_level=3)

    assert any("exercise with no instructions" in i for i in completeness_issues)
    assert any("needs at least one `exercise` or `challenge` block" in i for i in level_3_issues)
    assert completeness_issues[0] != level_3_issues[0]


def test_a_quiz_is_flagged_even_at_content_level_1():
    """Unlike _content_level_3_issues, these four checks have no content-
    level parameter at all - a blank required block is always a defect,
    never gated on the level-3-only experiential requirement. content_level
    1/2 courses still have quizzes checked the same way."""
    blocks = [_block(BlockType.QUIZ, {"title": "Check", "questions": []})]
    issues = _section_completeness_issues(blocks)
    assert any("quiz with no questions" in i for i in issues)


# --- chapter_contract_brief: the meaning-dependent half ------------------------


def _chapter(**overrides) -> BlueprintChapter:
    values = dict(
        id="chapter_1",
        title="Idempotency",
        order=1,
        objective="Explain idempotency and when it matters.",
        sections=[
            ChapterSection(title="Definition", summary="", key_points=["Exactly-once vs retried"]),
        ],
        key_concepts=["idempotency key", "deduplication window"],
        prerequisites=["Messaging basics"],
    )
    values.update(overrides)
    return BlueprintChapter(**values)


def test_contract_brief_is_empty_for_a_chapter_with_no_contract_fields():
    """Backward compatibility: a hand-built or legacy chapter with none of
    these fields set must leave the reviewer prompt unchanged."""
    bare = BlueprintChapter(id="c", title="C", order=1)
    assert chapter_contract_brief(bare) == ""


def test_contract_brief_includes_objective_sections_and_key_concepts():
    brief = chapter_contract_brief(_chapter())
    assert "Explain idempotency" in brief
    assert "Definition" in brief
    assert "Exactly-once vs retried" in brief
    assert "idempotency key" in brief
    assert "Messaging basics" in brief
    assert "completeness" in brief.lower()


def test_contract_reaches_the_real_reviewer_prompt():
    template = load_template("technical_v1")
    blueprint = CourseBlueprint(course_title="Test", audience="engineers", template_id="technical_v1")
    course_input = CourseInput(
        course_title="Test",
        toc=[{"title": "Idempotency"}],
        target_audience="engineers",
        template="technical",
    )
    from app.agents.prompts import ContinuityContext

    prompt = reviewer_user(
        blueprint,
        _chapter(),
        template,
        blocks=[{"type": "paragraph", "text": "content"}],
        course_input=course_input,
        continuity=ContinuityContext(),
    )
    assert "CHAPTER CONTRACT" in prompt
    assert "idempotency key" in prompt


def test_reviewer_system_tells_the_model_to_use_the_contract():
    from app.agents.prompts import REVIEWER_SYSTEM

    assert "CHAPTER CONTRACT" in REVIEWER_SYSTEM
    assert "completeness" in REVIEWER_SYSTEM
