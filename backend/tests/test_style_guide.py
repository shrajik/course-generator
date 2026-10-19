"""Writing style guide: MSTP or CMOS, plus client overrides."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.agents import prompts
from app.agents.reviewer import _pedagogy_issues
from app.course.pedagogy import mstp_rule_lines, pedagogy_rules_brief
from app.course.style_guide import style_guide_brief, style_issues
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.learner import LearnerProfile
from app.schemas.style import StyleGuide

CMOS = StyleGuide(base="cmos")
MSTP = StyleGuide(base="mstp")


def _para(text: str) -> Block:
    return Block(type=BlockType.PARAGRAPH, content={"text": text})


def _descriptions(issues) -> str:
    return " | ".join(i.description for i in issues)


# --- schema -------------------------------------------------------------------


def test_the_default_is_mstp_with_author_date_citations_and_no_overrides():
    style = StyleGuide()
    assert style.base == "mstp" and style.citation_system == "author_date" and not style.has_overrides


def test_terms_are_trimmed_deduplicated_and_blank_ones_dropped():
    style = StyleGuide(brand_terms=[" Acme  Cloud ", "acme cloud", "", "  "], banned_words=["synergy", "Synergy"])
    assert style.brand_terms == ["Acme Cloud"] and style.banned_words == ["synergy"]


def test_an_unknown_guide_or_an_overlong_term_is_rejected():
    with pytest.raises(ValidationError):
        StyleGuide(base="apa")
    with pytest.raises(ValidationError):
        StyleGuide(brand_terms=["x" * 81])


# --- prompt brief -------------------------------------------------------------


def test_no_style_guide_adds_nothing():
    assert style_guide_brief(None) == ""


def test_mstp_brief_carries_the_microsoft_rules_and_no_chicago_rules():
    brief = style_guide_brief(MSTP)
    assert "Microsoft Writing Style Guide" in brief and "serial comma" in brief
    assert "Chicago" not in brief.replace("STYLE GUIDE FOR THIS COURSE", "")


def test_cmos_brief_is_formal_and_carries_exactly_the_chosen_citation_system():
    author_date = style_guide_brief(StyleGuide(base="cmos"))
    notes = style_guide_brief(StyleGuide(base="cmos", citation_system="notes_bibliography"))
    assert "formal editorial register" in author_date and "serial comma" not in author_date
    assert "author-date" in author_date and "notes-bibliography" not in author_date
    assert "notes-bibliography" in notes and "author-date" not in notes


def test_overrides_are_listed_and_marked_as_taking_precedence():
    brief = style_guide_brief(StyleGuide(brand_terms=["AcmeCloud"], banned_words=["synergy", "leverage"]))
    assert "take precedence" in brief and "AcmeCloud" in brief and "synergy; leverage" in brief
    assert "conflicts" in brief


def test_the_brief_never_contains_a_line_the_mock_ai_would_misread():
    brief = style_guide_brief(StyleGuide(base="cmos", brand_terms=["A"], banned_words=["b"]))
    assert not re.search(r"^\s*\d+\. ", brief, re.MULTILINE)
    for label in ("COURSE TITLE:", "CHAPTER TITLE:", "TEMPLATE KIND:"):
        assert label not in brief


def _input(style: StyleGuide | None, profile: LearnerProfile | None = None) -> CourseInput:
    return CourseInput(
        course_title="T", toc=[{"title": "One"}], target_audience="Engineers", template="technical",
        learner_profile=profile, style_guide=style,
    )


def test_prompts_are_unchanged_without_a_style_guide():
    template = load_template("technical_v1")
    assert prompts._course_context(_input(None), template) == prompts._course_context(_input(None), template)
    assert "STYLE GUIDE FOR THIS COURSE" not in prompts._course_context(_input(None), template)
    assert "STYLE GUIDE FOR THIS COURSE" in prompts._course_context(_input(CMOS), template)


def test_an_explicit_style_guide_replaces_the_default_writing_rules_instead_of_adding_to_them():
    template = load_template("technical_v1")
    profile = LearnerProfile()

    def writer_prompt(style):
        from app.schemas.blueprint import CourseBlueprint

        return prompts.writer_shared_prefix(CourseBlueprint(course_title="T"), _input(style, profile), template)

    rules = "Microsoft Writing Style Guide house rules"
    assert writer_prompt(None).count(rules) == 1  # today's behaviour, unchanged
    assert writer_prompt(MSTP).count(rules) == 1  # the explicit guide, not twice
    assert writer_prompt(MSTP).count("STYLE GUIDE FOR THIS COURSE") == 1  # not repeated by the prefix
    assert writer_prompt(CMOS).count(rules) == 0 and "Chicago Manual of Style house rules" in writer_prompt(CMOS)


def test_the_learner_profile_brief_is_unchanged_by_the_refactor():
    assert mstp_rule_lines()[0] in pedagogy_rules_brief(LearnerProfile())
    assert mstp_rule_lines()[0] not in pedagogy_rules_brief(LearnerProfile(), include_writing_style=False)


# --- reviewer checks ----------------------------------------------------------


def test_no_style_guide_means_no_style_issues():
    assert style_issues([_para("Leverage synergy.")], None) == []


def test_a_banned_word_is_a_major_issue_at_the_offending_block():
    issues = style_issues([_para("Fine."), _para("We leverage synergy here.")], StyleGuide(banned_words=["synergy"]))
    assert issues[0].severity == "major" and issues[0].block_index == 1 and "synergy" in issues[0].description


def test_banned_words_match_whole_words_only():
    assert style_issues([_para("The apple pie.")], StyleGuide(banned_words=["app"])) == []


def test_a_banned_phrase_with_symbols_still_matches():
    assert style_issues([_para("Use C++ daily.")], StyleGuide(banned_words=["C++"]))


def test_a_brand_term_in_the_wrong_case_is_reported_with_the_right_form():
    issues = style_issues([_para("We use acmecloud daily.")], StyleGuide(brand_terms=["AcmeCloud"]))
    assert issues[0].severity == "minor" and '"AcmeCloud"' in issues[0].suggestion


def test_a_correctly_written_brand_term_is_fine():
    assert style_issues([_para("We use AcmeCloud daily.")], StyleGuide(brand_terms=["AcmeCloud"])) == []


def test_cmos_flags_second_person_and_small_numbers_but_mstp_does_not():
    text = "You will complete 12 steps."
    cmos = _descriptions(style_issues([_para(text)], CMOS))
    assert "second person" in cmos and "number" in cmos
    assert style_issues([_para(text)], MSTP) == []


@pytest.mark.parametrize("text", ["Version 2.1 shipped.", "Gains were 50% higher.", "Meet at 12:30.", "It happened in 2024.", "Twelve steps."])
def test_numbers_that_are_not_bare_whole_numbers_are_left_alone(text):
    assert not any("number" in i.description for i in style_issues([_para(text)], CMOS))


def test_mstp_slips_are_checked_for_mstp_and_by_default_but_not_for_cmos():
    blocks = [_para("Click on the button, e.g. the blue one.")]
    profile = LearnerProfile()
    assert _pedagogy_issues(blocks, profile)  # today's behaviour
    assert _pedagogy_issues(blocks, profile, MSTP)
    assert not any(i.category == "style" for i in _pedagogy_issues(blocks, profile, CMOS))


def test_the_style_guide_is_stored_with_the_course(client):
    response = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "Engineers",
        "template": "technical", "run_planner": False,
        "style_guide": {"base": "cmos", "brand_terms": ["AcmeCloud"], "banned_words": ["synergy"]},
    })
    assert response.status_code == 201, response.text
    stored = response.json()["input"]["style_guide"]
    assert stored["base"] == "cmos" and stored["brand_terms"] == ["AcmeCloud"]
    bad = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "E",
        "template": "technical", "run_planner": False, "style_guide": {"base": "apa"},
    })
    assert bad.status_code == 422
