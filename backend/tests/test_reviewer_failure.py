"""Quality-governance follow-up (Phase 3.8A): a reviewer-call failure must
never be mistaken for "reviewed and found clean".

Before this fix, CourseService._generate_chapter's own advisory
except-block (`except Exception: log.warning(...)`) left `review=None` and
`structural_findings=[]` for the whole chapter whenever ReviewerAgent.
review_chapter raised - silently skipping BOTH the LLM judgement AND every
deterministic structural check (missing blocks, visual balance, content
level 3, section completeness, code-cell execution), with nothing but a
server log line. These tests exercise review_chapter directly - the
narrowest level the fix actually lives at - mirroring
test_code_execution_verification.py's own `_review_chapter_agent` pattern
for constructing a real ReviewerAgent with a stub AI client, no network, no
live model call.
"""

from __future__ import annotations

from app.agents.prompts import ContinuityContext
from app.agents.reviewer import ReviewerAgent
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.draft import GeneratedChapter
from app.schemas.review import ChapterReview
from app.services.quality_framework import build_course_quality_report


class _RaisingAI:
    is_mock = True

    async def structured(self, **kwargs):
        raise RuntimeError("the model API timed out")


class _CleanAI:
    """The pre-existing, successful-path stub (mirrors
    test_code_execution_verification.py's `_StructuredStub`) - used to prove
    the fix changes nothing for the ordinary case."""

    is_mock = True

    async def structured(self, **kwargs):
        return ChapterReview(approved=True)


def _agent(ai) -> ReviewerAgent:
    return ReviewerAgent(ai=ai)


def _fixtures():
    template = load_template("technical_v1")
    blueprint = CourseBlueprint(course_title="T", audience="eng", template_id="technical_v1")
    chapter = BlueprintChapter(id="chapter_1", title="T", order=1)
    course_input = CourseInput(
        course_title="T", toc=[{"title": "T"}], target_audience="eng", template="technical"
    )
    return template, blueprint, chapter, course_input


def _heading(text: str) -> Block:
    return Block(type=BlockType.HEADING, content={"text": text, "level": 2})


def _paragraph(text: str = "Some real content.") -> Block:
    return Block(type=BlockType.PARAGRAPH, content={"text": text})


def _blocking_findings(structural_findings):
    """The real `approval_blocking_findings` a persisted chapter with these
    structural_findings would produce - round-tripped through
    GeneratedChapter exactly as storage does, mirroring
    test_code_execution_verification.py's own `_final_blocking_findings`."""
    chapter = GeneratedChapter(
        chapter_id="chapter_1", chapter_number=1, title="T", structural_findings=structural_findings
    )
    reloaded = GeneratedChapter.model_validate(chapter.model_dump(mode="json"))
    report = build_course_quality_report(course_id="c1", chapters=[reloaded], document=None)
    return report.approval_blocking_findings


async def test_a_reviewer_call_failure_produces_a_verification_failed_finding():
    """Point 1/2: the reviewer call raises; the failure must be represented
    as a real, typed finding - not only a server log line."""
    agent = _agent(_RaisingAI())
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("A Real Section"), _paragraph()]

    review, structural_findings = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    assert review is not None  # never None - the old bug's exact symptom
    failures = [f for f in structural_findings if f.check == "reviewer_call_failed"]
    assert len(failures) == 1
    finding = failures[0]
    assert finding.severity == "blocker"
    assert finding.verification_status == "verification_failed"
    assert finding.verifier == "reviewer_call"
    assert "model API timed out" in (finding.verification_error or "")


async def test_the_failure_survives_into_the_approval_gate_not_just_structural_findings():
    """Point 3: the chapter must not be able to pass as clean - the finding
    must actually reach `approval_blocking_findings`, the real predicate
    `approve_course` checks."""
    agent = _agent(_RaisingAI())
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("A Real Section"), _paragraph()]

    _, structural_findings = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    blocking = _blocking_findings(structural_findings)
    assert blocking  # never empty - a clean-looking course is exactly the defect this closes
    assert any(f.reason == "reviewer_call_failed" for f in blocking)
    assert all(f.verification_status != "unverified" for f in blocking)


async def test_deterministic_checks_still_run_when_the_reviewer_call_fails():
    """Point 4: a genuine structural defect (a placeholder heading) in the
    SAME chapter must still be caught, proving _apply_structural_checks runs
    against the real blocks regardless of whether the reviewer call above it
    succeeded - the old bug skipped this too, since both lived inside the
    same now-unwrapped try block."""
    agent = _agent(_RaisingAI())
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("TODO"), _paragraph()]  # a real, deterministic section-completeness defect

    review, structural_findings = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    assert any(f.check == "reviewer_call_failed" for f in structural_findings)
    assert any(f.check == "section_completeness" for f in structural_findings)
    assert any("no real title" in m for m in review.missing_required_blocks)
    assert review.approved is False  # the real structural gap still flips this, as before


async def test_normal_successful_review_is_unchanged():
    """Point 5: the ordinary, non-failing path must behave exactly as
    before this fix - no reviewer_call_failed finding at all."""
    agent = _agent(_CleanAI())
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("Introduction"), _paragraph()]

    review, structural_findings = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    assert not any(f.check == "reviewer_call_failed" for f in structural_findings)
    assert review.reviewed_at  # the real reviewer ran and stamped it, unlike the failure path


async def test_a_reviewer_failure_does_not_change_the_deterministic_outcome():
    """A call failure alone must never ADD a spurious revision trigger nor
    REMOVE a real one (rewriting cannot fix an API timeout, the same
    principle already established for code_cell_execution_skipped/
    unavailable - see reviewer.py's own _CODE_CELL_REVISION_TRIGGERING_CHECKS
    comment): the deterministic `missing_required_blocks`/`approved` outcome
    for the SAME blocks must be identical whether the reviewer call
    succeeded or failed - only the new reviewer_call_failed finding differs."""
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("A Real Section"), _paragraph()]

    clean_review, _ = await _agent(_CleanAI()).review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )
    failed_review, failed_findings = await _agent(_RaisingAI()).review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    assert failed_review.missing_required_blocks == clean_review.missing_required_blocks
    assert failed_review.approved == clean_review.approved
    assert any(f.check == "reviewer_call_failed" for f in failed_findings)


async def test_repeated_calls_on_the_same_agent_never_accumulate_the_finding():
    """Point 7: CourseService._generate_chapter REPLACES structural_findings
    on every revision-retry pass rather than accumulating it (unchanged by
    this fix) - this proves review_chapter itself holds no hidden state that
    could duplicate the finding if called more than once, which is the one
    way a duplicate could appear regardless of that caller-side replacement."""
    agent = _agent(_RaisingAI())
    template, blueprint, chapter, course_input = _fixtures()
    blocks = [_heading("A Real Section"), _paragraph()]

    _, first_pass = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )
    _, second_pass = await agent.review_chapter(
        blueprint=blueprint, chapter=chapter, template=template, course_input=course_input,
        blocks=blocks, continuity=ContinuityContext(),
    )

    assert len([f for f in first_pass if f.check == "reviewer_call_failed"]) == 1
    assert len([f for f in second_pass if f.check == "reviewer_call_failed"]) == 1
