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
from app.agents.reviewer import ReviewerAgent, _section_completeness_issues
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
