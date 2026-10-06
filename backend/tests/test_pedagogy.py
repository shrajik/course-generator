"""Learner profile -> pedagogy brief -> prompts -> reviewer checks.

The single most important assertion here is the prompt-invariance one: a
course with no learner profile must produce byte-identical prompts to what it
produced before the pedagogy layer existed, so adding this feature cannot
change any existing course's output.
"""

from __future__ import annotations

import re

import pytest

from app.agents import prompts
from app.agents.reviewer import ReviewerAgent, _pedagogy_issues
from app.course.pedagogy import (
    ARCHETYPES,
    analogy_profile,
    domain_match_index,
    is_cross_domain,
    learner_brief,
    pedagogy_rules_brief,
    slider_defaults,
)
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import Block, BlockMeta
from app.schemas.learner import LearnerProfile
from app.schemas.review import ChapterReview


def _profile(**overrides) -> LearnerProfile:
    return LearnerProfile(**overrides)


def _block(block_type: BlockType, content: dict) -> Block:
    return Block(
        id=f"block_{block_type.value[:6]}0001",
        type=block_type,
        content=content,
        meta=BlockMeta(),
    )


# --- domain computation ------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("STEM", "STEM", 1.0),
        ("stem", "  STEM  ", 1.0),  # case/whitespace insensitive
        ("STEM", "FINANCE", 0.0),
        ("", "FINANCE", 1.0),  # nothing to bridge from
        ("NONE", "FINANCE", 1.0),
        ("COMMERCE", "", 1.0),
    ],
)
def test_domain_match_index(source: str, target: str, expected: float) -> None:
    profile = _profile(source_domain=source, target_domain=target)
    assert domain_match_index(profile) == expected
    assert is_cross_domain(profile) is (expected == 0.0)


def test_analogy_profile_matches_science_to_finance() -> None:
    matched = analogy_profile(_profile(source_domain="STEM", target_domain="FINANCE"))
    assert matched is not None
    assert "viscosity" in " ".join(matched.pairs).lower()


def test_analogy_profile_matches_business_to_software() -> None:
    matched = analogy_profile(_profile(source_domain="COMMERCE", target_domain="SOFTWARE_ENG"))
    assert matched is not None
    assert "franchise" in " ".join(matched.pairs).lower()


def test_analogy_profile_absent_for_expert_track() -> None:
    assert analogy_profile(_profile(source_domain="STEM", target_domain="STEM")) is None


def test_unmatched_cross_domain_pair_has_no_preauthored_analogies() -> None:
    profile = _profile(source_domain="MUSIC", target_domain="HORTICULTURE")
    assert is_cross_domain(profile)
    assert analogy_profile(profile) is None
    # The generic bridge rules still apply.
    assert "COGNITIVE BRIDGE TRACK" in learner_brief(profile)


# --- brief rendering ---------------------------------------------------------


def test_brief_is_empty_without_a_profile() -> None:
    assert learner_brief(None) == ""
    assert pedagogy_rules_brief(None) == ""


@pytest.mark.parametrize("age_group", sorted(ARCHETYPES))
def test_brief_renders_for_every_archetype(age_group: str) -> None:
    brief = learner_brief(_profile(age_group=age_group))
    assert ARCHETYPES[age_group].instructional_model in brief
    assert "LEARNER PROFILE" in brief


@pytest.mark.parametrize("age_group", sorted(ARCHETYPES))
def test_brief_never_trips_the_mock_ai_prompt_parsers(age_group: str) -> None:
    """The offline mock client pulls the course/chapter title and the TOC out
    of whatever prompt it is handed (see app/services/mock_ai.py). A numbered
    line or a reserved label inside the brief would be parsed as course
    content and silently corrupt every offline test.
    """
    profile = _profile(
        age_group=age_group, source_domain="STEM", target_domain="FINANCE", manual_override=True
    )
    text = f"{learner_brief(profile)}\n{pedagogy_rules_brief(profile)}"
    for label in ("COURSE TITLE:", "CHAPTER TITLE:", "TEMPLATE KIND:", "VISUAL BRIEF:"):
        assert label not in text
    # Mirrors mock_ai._TOC_RE: any line the mock would read as a chapter title.
    assert re.findall(r"^\s*\d+\.\s*(.+)$", text, re.MULTILINE) == []


def test_cross_domain_brief_requires_jargon_tags() -> None:
    brief = learner_brief(_profile(source_domain="STEM", target_domain="FINANCE"))
    assert "<jargon term=" in brief
    assert "EXPERT TRACK" not in brief


def test_expert_track_brief_skips_analogies() -> None:
    brief = learner_brief(_profile(source_domain="FINANCE", target_domain="FINANCE"))
    assert "EXPERT TRACK" in brief
    assert "COGNITIVE BRIDGE TRACK" not in brief
    assert "Skip baseline summaries" in brief


def test_brief_states_the_chunk_cap() -> None:
    assert "no single text block may exceed 275 words" in learner_brief(
        _profile(chunk_word_cap=275)
    )


def test_rules_brief_lists_banned_verbs_and_information_types() -> None:
    brief = pedagogy_rules_brief(_profile())
    assert "appreciate" in brief
    assert "Classification" in brief
    assert "click on" in brief


def test_slider_defaults_match_the_archetype_table() -> None:
    assert slider_defaults("KG_PRIMARY")["gamification_index"] == 0.9
    assert slider_defaults("HIGHER_ED")["gamification_index"] == 0.0
    assert slider_defaults("KG_PRIMARY")["chunk_word_cap"] == 200


# --- prompt integration ------------------------------------------------------


def _course_input(profile: LearnerProfile | None) -> CourseInput:
    return CourseInput(
        course_title="Intro to Risk",
        toc=[{"title": "Chapter One"}],
        target_audience="Analysts",
        template="non_technical",
        learner_profile=profile,
    )


def test_prompts_are_byte_identical_without_a_profile() -> None:
    """Guards every pre-existing course: no profile, no prompt change."""
    template = load_template("non_technical_v1")
    without = prompts._course_context(_course_input(None), template)
    # What the function produced before the pedagogy layer was added.
    expected = """\
COURSE TITLE: Intro to Risk
TARGET AUDIENCE: Analysts
TEMPLATE KIND: non_technical
TEMPLATE: {name} ({tid}) - {desc}
LANGUAGE: en
REQUESTED TONE: (not specified)

TABLE OF CONTENTS (as provided by the user):
1. Chapter One

DO'S:
(none provided)

DON'TS:
(none provided)
""".format(name=template.name, tid=template.template_id, desc=template.description)
    assert without == expected


def test_profile_reaches_the_planner_prompt() -> None:
    template = load_template("non_technical_v1")
    course_input = _course_input(_profile(age_group="KG_PRIMARY"))
    rendered = prompts.planner_user(course_input, template)
    assert "Story-Based Pedagogy" in rendered


def test_profile_reaches_the_writer_prompt_inside_the_cached_prefix() -> None:
    from app.schemas.blueprint import CourseBlueprint

    template = load_template("non_technical_v1")
    blueprint = CourseBlueprint(course_title="Intro to Risk")
    profile = _profile(age_group="PROFESSIONAL", source_domain="STEM", target_domain="FINANCE")
    prefix = prompts.writer_shared_prefix(blueprint, _course_input(profile), template)
    assert "Knowles' Andragogy" in prefix
    assert "COGNITIVE BRIDGE TRACK" in prefix
    assert "INFORMATION MAPPING" in prefix


# --- reviewer checks ---------------------------------------------------------


def test_no_pedagogy_issues_without_a_profile() -> None:
    blocks = [_block(BlockType.PARAGRAPH, {"text": "word " * 900})]
    assert _pedagogy_issues(blocks, None) == []


def test_banned_objective_verb_is_flagged() -> None:
    blocks = [
        _block(
            BlockType.LEARNING_OBJECTIVES,
            {"items": ["Learners will be able to understand portfolio risk."]},
        )
    ]
    issues = _pedagogy_issues(blocks, _profile())
    assert [issue.category for issue in issues] == ["pedagogy"]
    assert issues[0].severity == "major"
    assert issues[0].block_index == 0


def test_approved_objective_verb_passes() -> None:
    blocks = [
        _block(
            BlockType.LEARNING_OBJECTIVES,
            {"items": ["Learners will be able to compare two hedging strategies."]},
        )
    ]
    assert _pedagogy_issues(blocks, _profile()) == []


def test_banned_verb_in_ordinary_prose_is_not_flagged() -> None:
    """"understand" is only banned as an objective's verb, not as English."""
    blocks = [_block(BlockType.PARAGRAPH, {"text": "This helps you understand the market."})]
    assert _pedagogy_issues(blocks, _profile()) == []


def test_chunk_word_cap_is_enforced_with_a_block_index() -> None:
    blocks = [
        _block(BlockType.PARAGRAPH, {"text": "word " * 50}),
        _block(BlockType.PARAGRAPH, {"text": "word " * 300}),
    ]
    issues = _pedagogy_issues(blocks, _profile(chunk_word_cap=200))
    assert len(issues) == 1
    assert issues[0].category == "chunk_size"
    # A block index is what lets the writer fix just this block.
    assert issues[0].block_index == 1


def test_style_slips_are_minor_only() -> None:
    blocks = [_block(BlockType.PARAGRAPH, {"text": "Please click on Save, e.g. after editing."})]
    issues = _pedagogy_issues(blocks, _profile())
    assert len(issues) == 1
    assert issues[0].severity == "minor"
    # Minor issues alone must not force a revision round.
    review = ChapterReview(approved=True, summary="ok")
    review.issues.extend(issues)
    assert review.needs_revision() is False


def test_profile_round_trips_through_the_api(client) -> None:
    """End-to-end wiring: the profile posted on create is the one later reads
    return, without a migration - it rides inside the existing input JSON."""
    payload = {
        "course_title": "Introduction to RAG",
        "toc": [{"title": "What is RAG"}],
        "target_audience": "Backend engineers",
        "template": "technical",
        "learner_profile": {
            "age_group": "MIDDLE_SECONDARY",
            "source_domain": "COMMERCE",
            "target_domain": "SOFTWARE_ENG",
            "chunk_word_cap": 320,
            "manual_override": True,
        },
    }
    created = client.post("/api/courses", json=payload)
    assert created.status_code == 201, created.text

    fetched = client.get(f"/api/courses/{created.json()['course_id']}")
    assert fetched.status_code == 200, fetched.text
    profile = fetched.json()["course"]["input"]["learner_profile"]
    assert profile["age_group"] == "MIDDLE_SECONDARY"
    assert profile["chunk_word_cap"] == 320
    # Untouched fields come back as the documented defaults.
    assert profile["edu_level"] == "BACHELORS"


def test_courses_without_a_profile_still_create(client) -> None:
    created = client.post(
        "/api/courses",
        json={
            "course_title": "Introduction to RAG",
            "toc": [{"title": "What is RAG"}],
            "target_audience": "Backend engineers",
            "template": "technical",
        },
    )
    assert created.status_code == 201, created.text
    fetched = client.get(f"/api/courses/{created.json()['course_id']}")
    assert fetched.json()["course"]["input"]["learner_profile"] is None


def test_pedagogy_defaults_endpoint_serves_every_archetype(client) -> None:
    response = client.get("/api/pedagogy/defaults")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["age_group"] for item in body["age_groups"]} == set(ARCHETYPES)
    kg = next(item for item in body["age_groups"] if item["age_group"] == "KG_PRIMARY")
    assert kg["chunk_word_cap"] == 200
    assert kg["gamification_index"] == 0.9


def test_chunk_overrun_triggers_a_surgical_revision_not_a_rewrite() -> None:
    review = ChapterReview(approved=True, summary="ok")
    template = load_template("non_technical_v1")
    blocks = [_block(BlockType.PARAGRAPH, {"text": "word " * 300})]
    ReviewerAgent._apply_structural_checks(review, template, blocks, _profile(chunk_word_cap=100))
    assert review.needs_revision() is True
    # Surgical revision requires an offending index and no missing-block entry.
    assert review.offending_indices() == [0]
