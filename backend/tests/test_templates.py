"""Template configuration validation."""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError
from app.course.templates.registry import available_templates, load_template
from app.schemas.blocks import BlockType
from app.schemas.course import TEMPLATE_IDS


def test_exactly_two_templates_exist():
    templates = available_templates()
    assert {t.template_id for t in templates} == {"technical_v1", "non_technical_v1"}
    assert {t.kind for t in templates} == {"technical", "non_technical"}


@pytest.mark.parametrize("key", list(TEMPLATE_IDS) + list(TEMPLATE_IDS.values()))
def test_templates_load_by_kind_and_by_id(key):
    assert load_template(key).template_id in TEMPLATE_IDS.values()


def test_unknown_template_raises():
    with pytest.raises(NotFoundError):
        load_template("gamified_v9")


@pytest.mark.parametrize("template_id", ["technical_v1", "non_technical_v1"])
def test_template_structure_is_coherent(template_id):
    template = load_template(template_id)
    assert template.sections, "a template needs a chapter structure"
    assert template.required_sections(), "at least one section must be required"
    assert template.writer_guidance and template.review_focus and template.image_guidance

    # Section block types and required types must be allowed by the template.
    allowed = set(template.allowed_block_types)
    for section in template.sections:
        assert set(section.block_types) <= allowed, section.key
    assert set(template.required_block_types) <= allowed

    # Section keys are unique and every one is usable in a prompt.
    keys = [s.key for s in template.sections]
    assert len(keys) == len(set(keys))
    assert template.outline_for_prompt().count("\n") == len(keys) - 1


@pytest.mark.parametrize("template_id", ["technical_v1", "non_technical_v1"])
def test_every_allowed_block_type_has_a_style(template_id):
    template = load_template(template_id)
    for block_type in template.allowed_block_types:
        if block_type is BlockType.QUOTE:
            continue
        assert template.style_for(block_type), f"{block_type} has no default style"


def test_technical_template_expects_code_and_exercises():
    template = load_template("technical")
    section_keys = {s.key for s in template.sections}
    assert {"code_example", "implementation", "exercise", "quiz"} <= section_keys
    assert BlockType.EXERCISE in template.required_block_types


def test_non_technical_template_expects_story_and_case_study():
    template = load_template("non_technical")
    section_keys = {s.key for s in template.sections}
    assert {"story", "case_study", "framework", "pro_tips"} <= section_keys
    assert BlockType.STORY in template.required_block_types


def test_templates_share_the_pipeline_not_the_structure():
    technical = load_template("technical")
    non_technical = load_template("non_technical")
    assert technical.section_labels() != non_technical.section_labels()
    assert technical.theme.accent_color != non_technical.theme.accent_color
