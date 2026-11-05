"""Content Studio Phase 1 (backend data-model foundation): the brief's own
task list / business goal / performance gap / companion deliverables, and
packaging & tracking standards - see app/schemas/course.py's own comment
block and app/schemas/standards.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents import prompts
from app.course.templates.registry import load_template
from app.schemas.course import CourseInput
from app.schemas.standards import AccessibilitySettings, MetadataSettings, PackagingSettings


def _input(**overrides) -> CourseInput:
    return CourseInput(
        course_title="T", toc=[{"title": "One"}], target_audience="Engineers", template="technical",
        **overrides,
    )


# --- schema: CourseInput additions -------------------------------------------


def test_the_new_fields_default_to_empty_byte_identical_to_before():
    ci = _input()
    assert ci.tasks == [] and ci.business_goal == "" and ci.performance_gap == ""
    assert ci.companions == [] and ci.packaging is None


def test_a_course_input_dict_missing_the_new_keys_entirely_still_validates():
    """Backward compatibility: a course stored before these fields existed
    has none of these keys in its JSON at all, not even blank ones."""
    payload = {
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "Engineers",
        "template": "technical",
    }
    ci = CourseInput.model_validate(payload)
    assert ci.tasks == [] and ci.packaging is None


def test_tasks_business_goal_and_performance_gap_round_trip():
    ci = _input(
        tasks=["Recognise an incident", "Classify severity"],
        business_goal="Fewer late reports",
        performance_gap="Agents under-report low-severity incidents",
    )
    assert ci.tasks == ["Recognise an incident", "Classify severity"]
    assert ci.business_goal == "Fewer late reports"
    assert ci.performance_gap == "Agents under-report low-severity incidents"


def test_companions_accepts_only_the_known_values():
    ci = _input(companions=["job_aid", "quick_ref"])
    assert ci.companions == ["job_aid", "quick_ref"]
    with pytest.raises(ValidationError):
        _input(companions=["poster"])


def test_too_many_tasks_is_rejected():
    with pytest.raises(ValidationError):
        _input(tasks=[f"Task {i}" for i in range(21)])


def test_an_overlong_business_goal_is_rejected():
    with pytest.raises(ValidationError):
        _input(business_goal="x" * 501)


# --- schema: PackagingSettings -----------------------------------------------


def test_packaging_defaults_to_nothing_selected():
    settings = PackagingSettings()
    assert settings.standard is None and settings.authoring_tool is None
    assert settings.track_beyond_lms is False and settings.lrs_confirmed is False


def test_packaging_round_trips_a_real_selection():
    settings = PackagingSettings(
        standard="xapi", target_lms="Cornerstone", authoring_tool="articulate_storyline",
        track_beyond_lms=True, lrs_confirmed=True,
    )
    assert settings.standard == "xapi" and settings.authoring_tool == "articulate_storyline"
    assert settings.track_beyond_lms and settings.lrs_confirmed


def test_an_unknown_standard_or_tool_is_rejected():
    with pytest.raises(ValidationError):
        PackagingSettings(standard="moodle_native")
    with pytest.raises(ValidationError):
        PackagingSettings(authoring_tool="lectora")


def test_packaging_is_optional_on_course_input():
    ci = _input(packaging=PackagingSettings(standard="scorm_2004"))
    assert ci.packaging is not None and ci.packaging.standard == "scorm_2004"


# --- prompt brief: _brief_details_brief / _course_context --------------------


def test_no_brief_details_adds_nothing():
    assert prompts._brief_details_brief(_input()) == ""


def test_prompts_are_byte_identical_without_brief_details():
    template = load_template("technical_v1")
    before = prompts._course_context(_input(), template)
    assert "BRIEF DETAILS:" not in before


def test_tasks_become_bulleted_objective_seeds():
    brief = prompts._brief_details_brief(_input(tasks=["Do A", "Do B"]))
    assert "BRIEF DETAILS:" in brief
    assert "- Do A" in brief and "- Do B" in brief
    assert "learning objective" in brief


def test_business_goal_and_stated_performance_gap_are_both_included():
    brief = prompts._brief_details_brief(
        _input(business_goal="Fewer errors", performance_gap="Training gap on step 3")
    )
    assert "Business/performance goal: Fewer errors" in brief
    assert "Performance gap: Training gap on step 3" in brief
    assert "drafted/inferred" not in brief  # a stated gap is never relabelled as drafted


def test_a_blank_performance_gap_asks_the_model_to_draft_one_honestly():
    brief = prompts._brief_details_brief(_input(business_goal="Fewer errors"))
    assert "not stated by the creator" in brief
    assert "drafted/inferred" in brief


def test_course_context_includes_brief_details_when_set():
    template = load_template("technical_v1")
    context = prompts._course_context(_input(tasks=["Do A"]), template)
    assert "BRIEF DETAILS:" in context


# --- API: the new fields are stored with the course --------------------------


def test_the_new_fields_are_stored_with_the_course(client):
    response = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "Engineers",
        "template": "technical", "run_planner": False,
        "tasks": ["Recognise an incident"],
        "business_goal": "Fewer late reports",
        "companions": ["job_aid", "quick_ref"],
        "packaging": {"standard": "xapi", "authoring_tool": "articulate_storyline"},
    })
    assert response.status_code == 201, response.text
    stored = response.json()["input"]
    assert stored["tasks"] == ["Recognise an incident"]
    assert stored["business_goal"] == "Fewer late reports"
    assert stored["companions"] == ["job_aid", "quick_ref"]
    assert stored["packaging"]["standard"] == "xapi"

    bad = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "E",
        "template": "technical", "run_planner": False, "companions": ["poster"],
    })
    assert bad.status_code == 422


# --- schema: AccessibilitySettings / MetadataSettings (Content Studio Phase 2)


def test_accessibility_defaults_to_wcag22_aa_with_no_extras():
    settings = AccessibilitySettings()
    assert settings.compliance_target == "wcag22_aa"
    assert settings.extra_requirements == [] and settings.brand_colors == []


def test_accessibility_round_trips_a_real_selection():
    settings = AccessibilitySettings(
        compliance_target="wcag21_aa", extra_requirements=["section_508", "pdf_ua"],
        brand_colors=["#0E6E66", "#F5A623"],
    )
    assert settings.compliance_target == "wcag21_aa"
    assert settings.extra_requirements == ["section_508", "pdf_ua"]
    assert settings.brand_colors == ["#0E6E66", "#F5A623"]


def test_an_unknown_target_or_requirement_is_rejected():
    with pytest.raises(ValidationError):
        AccessibilitySettings(compliance_target="wcag30_aaa")
    with pytest.raises(ValidationError):
        AccessibilitySettings(extra_requirements=["hipaa"])


def test_metadata_defaults_to_no_preference_and_blank_overrides():
    settings = MetadataSettings()
    assert settings.schema_preference is None
    assert settings.tagging_conventions == "" and settings.owner_override == ""


def test_metadata_round_trips_a_real_selection():
    settings = MetadataSettings(
        schema_preference="dublin_core", tagging_conventions="internal-taxonomy-v2", owner_override="Jane Doe"
    )
    assert settings.schema_preference == "dublin_core"
    assert settings.tagging_conventions == "internal-taxonomy-v2"
    assert settings.owner_override == "Jane Doe"


def test_an_unknown_metadata_schema_preference_is_rejected():
    with pytest.raises(ValidationError):
        MetadataSettings(schema_preference="marc21")


def test_accessibility_and_metadata_are_optional_on_course_input():
    ci = _input()
    assert ci.accessibility is None and ci.metadata is None
    ci2 = _input(
        accessibility=AccessibilitySettings(compliance_target="wcag21_aa"),
        metadata=MetadataSettings(owner_override="Jane Doe"),
    )
    assert ci2.accessibility.compliance_target == "wcag21_aa"
    assert ci2.metadata.owner_override == "Jane Doe"


def test_accessibility_and_metadata_are_stored_with_the_course(client):
    response = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "Engineers",
        "template": "technical", "run_planner": False,
        "accessibility": {"compliance_target": "wcag21_aa", "extra_requirements": ["section_508"]},
        "metadata": {"schema_preference": "dublin_core", "owner_override": "Jane Doe"},
    })
    assert response.status_code == 201, response.text
    stored = response.json()["input"]
    assert stored["accessibility"]["compliance_target"] == "wcag21_aa"
    assert stored["metadata"]["owner_override"] == "Jane Doe"
