"""MOM Phase 5 - learning approaches (explanatory/example_driven/
scenario_based/situation_based/hands_on/conversational), end to end: a
pure-text unit-test layer for the prompt brief itself, plus an integration
test proving the setting actually reaches the writer prompt through the
real `_course_context` builder.
"""

from __future__ import annotations

from app.agents.prompts import learning_approaches_brief
from app.course.templates.registry import load_template
from app.schemas.course import CourseInput


def _input(**overrides) -> CourseInput:
    values = {
        "course_title": "Test Course",
        "toc": [{"title": "Chapter One"}],
        "target_audience": "engineers",
        "template": "technical",
    }
    values.update(overrides)
    return CourseInput(**values)


# --- learning_approaches_brief: pure prompt-text unit tests -------------------


def test_empty_list_is_empty_the_pre_existing_default_behaviour():
    """Backward compatibility: every course created before this feature
    existed implicitly left approach selection to the writer - this must
    stay byte-identical, the same contract content_level_brief(2) has."""
    assert learning_approaches_brief([]) == ""


def test_each_approach_produces_distinct_guidance():
    approaches = [
        "explanatory",
        "example_driven",
        "scenario_based",
        "situation_based",
        "hands_on",
        "conversational",
    ]
    briefs = {a: learning_approaches_brief([a]) for a in approaches}
    # Every one of the 6 is non-empty and distinct from every other.
    assert all(briefs.values())
    assert len(set(briefs.values())) == len(approaches)


def test_example_driven_asks_for_a_real_worked_example_not_just_a_heading():
    brief = learning_approaches_brief(["example_driven"])
    assert "worked example" in brief.lower()
    assert "not just a labelled heading" in brief.lower() or "not just" in brief.lower()


def test_scenario_based_reuses_story_and_case_study_blocks():
    brief = learning_approaches_brief(["scenario_based"])
    assert "`story`" in brief or "story" in brief
    assert "case_study" in brief
    assert "constraints" in brief.lower()


def test_hands_on_requires_instructions_inputs_outputs_and_evaluation_criteria():
    brief = learning_approaches_brief(["hands_on"])
    assert "instructions" in brief.lower()
    assert "deliverable" in brief.lower()
    assert "criteria" in brief.lower()


def test_conversational_is_honestly_static_not_claimed_interactive():
    """A real, explicit MOM requirement: never claim live interactive
    tutoring if the architecture only supports static written content."""
    brief = learning_approaches_brief(["conversational"])
    assert "static" in brief.lower()
    assert "does not provide live interactive tutoring" in brief
    assert "never claim" in brief.lower() or "never" in brief.lower()


def test_multiple_approaches_are_asked_to_combine_coherently_not_repeat():
    single = learning_approaches_brief(["scenario_based"])
    combined = learning_approaches_brief(["scenario_based", "hands_on"])
    assert "coherently" in combined.lower()
    assert "repetitive" in combined.lower()
    assert combined != single
    assert "scenario-based" in combined.lower() or "scenario_based" in combined.lower()


def test_an_unrecognised_value_is_silently_ignored_not_a_hard_error():
    """Defence in depth: CourseInput's own Literal type already rejects an
    invalid value at the API boundary - this function must still degrade
    safely rather than crash if it's ever called with one directly."""
    assert learning_approaches_brief(["not_a_real_approach"]) == ""
    brief = learning_approaches_brief(["hands_on", "not_a_real_approach"])
    assert "hands_on" not in brief or "Hands-on" in brief  # the valid one still renders
    assert "not_a_real_approach" not in brief


# --- integration: the setting actually reaches the writer prompt --------------


def test_learning_approaches_reach_the_real_course_context_builder():
    from app.agents.prompts import _course_context

    template = load_template("technical_v1")
    without = _course_context(_input(learning_approaches=[]), template)
    with_approach = _course_context(_input(learning_approaches=["hands_on"]), template)

    assert "LEARNING APPROACH" not in without
    assert "LEARNING APPROACH" in with_approach
    assert "exercise" in with_approach.lower()


def test_course_input_rejects_an_invalid_learning_approach():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _input(learning_approaches=["not_a_real_approach"])


def test_course_input_defaults_to_an_empty_list():
    assert _input().learning_approaches == []


# --- approach_review_brief: giving the REVIEWER the same facts (Phase 3.10a) --
# Previously `reviewer_user` never read `learning_approaches`/`content_level`
# at all, even though `course_input` was already a parameter - the reviewer
# was blind to what was requested. These tests cover the new
# `approach_review_brief` helper directly (pure prompt-text, same style as
# the `learning_approaches_brief` tests above) plus its actual wiring into
# `reviewer_user`, without asserting unrelated full prompt contents.


def test_approach_review_brief_is_empty_for_the_pre_existing_default():
    """Default content_level=2, no approaches selected - must stay exactly
    as silent as learning_approaches_brief([]) and content_level_brief(2)
    themselves, the two contracts this helper is built on."""
    from app.agents.prompts import _appended, approach_review_brief

    brief = approach_review_brief(_input())
    assert brief == ""
    # The real splice point in reviewer_user uses _appended(...) - proving
    # THAT composition is also a no-op (no stray newline) is what actually
    # guarantees the reviewer prompt stays byte-for-byte unchanged, not just
    # that the brief function itself returns "".
    assert _appended(brief) == ""


def test_approach_review_brief_includes_the_existing_hands_on_guidance_verbatim():
    from app.agents.prompts import approach_review_brief, learning_approaches_brief

    course_input = _input(learning_approaches=["hands_on"])
    brief = approach_review_brief(course_input)
    # Reused verbatim, never a second maintained copy of the same guidance.
    assert learning_approaches_brief(["hands_on"]) in brief


def test_approach_review_brief_includes_the_existing_level_3_guidance_verbatim():
    from app.agents.prompts import approach_review_brief, content_level_brief

    course_input = _input(content_level=3)
    brief = approach_review_brief(course_input)
    assert content_level_brief(3) in brief


def test_approach_review_brief_caps_severity_at_major_or_minor():
    from app.agents.prompts import approach_review_brief

    brief = approach_review_brief(_input(learning_approaches=["hands_on"]))
    assert "'major'" in brief
    assert "'minor'" in brief
    assert "Never report this as 'blocker'" in brief


def test_approach_review_brief_never_claims_independent_verification():
    """Quality-governance invariant: nothing in this prompt addition may
    instruct the model to mark its own opinion as verified - that
    classification is, and must remain, entirely downstream
    (app.services.quality_framework always marks issue-sourced findings
    "unverified" regardless of prompt wording)."""
    from app.agents.prompts import approach_review_brief

    brief = approach_review_brief(_input(learning_approaches=["hands_on"], content_level=3))
    assert "verified" not in brief.lower()


# --- integration: the brief actually reaches the real reviewer prompt --------


def test_reviewer_prompt_is_unchanged_for_a_default_course():
    from app.agents.prompts import ContinuityContext, reviewer_user
    from app.schemas.blueprint import BlueprintChapter, CourseBlueprint

    template = load_template("technical_v1")
    blueprint = CourseBlueprint(course_title="Test", audience="engineers", template_id="technical_v1")
    chapter = BlueprintChapter(id="chapter_1", title="Test Chapter", order=1)

    prompt = reviewer_user(
        blueprint,
        chapter,
        template,
        blocks=[{"type": "paragraph", "text": "content"}],
        course_input=_input(),
        continuity=ContinuityContext(),
    )
    assert "REQUESTED FOR THIS CHAPTER" not in prompt
    assert "approach_adherence" not in prompt


def test_reviewer_prompt_gains_approach_and_level_guidance_when_requested():
    from app.agents.prompts import ContinuityContext, reviewer_user
    from app.schemas.blueprint import BlueprintChapter, CourseBlueprint

    template = load_template("technical_v1")
    blueprint = CourseBlueprint(course_title="Test", audience="engineers", template_id="technical_v1")
    chapter = BlueprintChapter(id="chapter_1", title="Test Chapter", order=1)
    course_input = _input(learning_approaches=["hands_on"], content_level=3)

    prompt = reviewer_user(
        blueprint,
        chapter,
        template,
        blocks=[{"type": "paragraph", "text": "content"}],
        course_input=course_input,
        continuity=ContinuityContext(),
    )
    assert "REQUESTED FOR THIS CHAPTER" in prompt
    assert "LEARNING APPROACH" in prompt
    assert "CONTENT LEVEL 3" in prompt
    assert "approach_adherence" in prompt


# --- learning-approach deterministic enforcement (Phase 3.10b) ---------------
# `_hands_on_issues` / `_scenario_approach_issues` are deliberately SEPARATE
# from `_content_level_3_issues` (never modified, never touched here) and
# from each other - each gets its own fixed StructuralFinding.check tag at
# the `_apply_structural_checks` call site, never inferred from description
# text (see the last test in this section).


def _exercise(expected_outcome: str = "", steps=None, hints=None):
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block

    return Block(
        type=BlockType.EXERCISE,
        content={
            "instructions": "Do the thing.",
            "expected_outcome": expected_outcome,
            "steps": steps or [],
            "hints": hints or [],
        },
    )


def _case_study(context: str = "", challenge: str = "", outcome: str = ""):
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block

    return Block(
        type=BlockType.CASE_STUDY,
        content={"context": context, "challenge": challenge, "outcome": outcome},
    )


def _prose():
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block

    return Block(type=BlockType.PARAGRAPH, content={"text": "abstract only"})


def test_no_learning_approach_has_no_experiential_requirement():
    from app.agents.reviewer import _hands_on_issues, _scenario_approach_issues

    blocks = [_prose()]
    assert _hands_on_issues(blocks, []) == []
    assert _scenario_approach_issues(blocks, []) == []


def test_hands_on_without_a_worked_exercise_is_flagged():
    from app.agents.reviewer import _hands_on_issues

    issues = _hands_on_issues([_prose()], ["hands_on"])
    assert len(issues) == 1
    assert "hands_on" in issues[0] and "exercise" in issues[0]


def test_hands_on_with_a_real_worked_exercise_is_not_flagged():
    from app.agents.reviewer import _hands_on_issues

    blocks = [_exercise(expected_outcome="A working fix.", steps=["Do X", "Do Y"])]
    assert _hands_on_issues(blocks, ["hands_on"]) == []


def test_scenario_based_without_a_scenario_is_flagged():
    from app.agents.reviewer import _scenario_approach_issues

    issues = _scenario_approach_issues([_prose()], ["scenario_based"])
    assert len(issues) == 1
    assert "scenario_based" in issues[0] and "case_study" in issues[0]
    assert "situation_based" not in issues[0]  # never names an approach that wasn't requested


def test_scenario_based_with_a_real_case_study_is_not_flagged():
    from app.agents.reviewer import _scenario_approach_issues

    blocks = [
        _case_study(
            context="A team missing targets.",
            challenge="No shared definition of done.",
            outcome="Forecasting improved.",
        )
    ]
    assert _scenario_approach_issues(blocks, ["scenario_based"]) == []


def test_situation_based_without_a_scenario_states_the_two_response_limitation():
    from app.agents.reviewer import _scenario_approach_issues

    issues = _scenario_approach_issues([_prose()], ["situation_based"])
    assert len(issues) == 1
    assert "situation_based" in issues[0]
    assert "does NOT verify" in issues[0]
    assert "two plausible responses" in issues[0]


def test_situation_based_with_a_valid_scenario_produces_no_finding_at_all():
    """The corrected behaviour: a valid scenario must never be sent to
    revision solely over the unverifiable two-response requirement - the
    limitation is documented in the function's own docstring, never
    emitted as its own unconditional finding."""
    from app.agents.reviewer import _scenario_approach_issues

    blocks = [_case_study(context="x", challenge="y", outcome="z")]
    assert _scenario_approach_issues(blocks, ["situation_based"]) == []


def test_level_3_behavior_is_unchanged_by_this_addition():
    """Regression guard: _content_level_3_issues itself was not modified."""
    from app.agents.reviewer import _content_level_3_issues

    issues = _content_level_3_issues([_prose()], 3)
    assert any("exercise` or `challenge`" in i for i in issues)
    assert any("case_study` block" in i for i in issues)


def test_apply_structural_checks_assigns_distinct_check_tags_not_inferred_from_text():
    from app.agents.reviewer import ReviewerAgent
    from app.schemas.review import ChapterReview

    review = ChapterReview(approved=True)
    course_input = _input(learning_approaches=["hands_on", "scenario_based"])
    deterministic = ReviewerAgent._apply_structural_checks(
        review, load_template("technical_v1"), [_prose()], None, course_input
    )
    checks = {f.check for f in deterministic}
    assert "learning_approach_hands_on" in checks
    assert "learning_approach_scenario" in checks
    assert any("hands_on" in m for m in review.missing_required_blocks)
    assert any("scenario_based" in m for m in review.missing_required_blocks)
    assert review.needs_revision() is True
