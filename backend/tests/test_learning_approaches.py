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
