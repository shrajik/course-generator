"""`draft_to_content` - how a flat writer DraftBlock becomes a validated
Course Document block's content. Covers the image_kind dispatch specifically
(diagram / concept_experience / illustration fallback) - see the Phase 4 plan
at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md."""

from __future__ import annotations

from app.course.blocks.normalizer import (
    _reads_as_young_learner_audience,
    apply_young_audience_apparatus_override,
    draft_to_content,
    tag_section_intro_illustrations,
)
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.schemas.draft import DraftBlock


def _image_draft(image_kind: str, *, illustration_style: str = "") -> DraftBlock:
    return DraftBlock(
        type=BlockType.IMAGE,
        image_purpose="A purpose",
        image_prompt="A prompt",
        caption="A caption",
        image_kind=image_kind,
        illustration_style=illustration_style,
    )


def test_image_kind_diagram_passes_through():
    content = draft_to_content(_image_draft("diagram"))
    assert content["kind"] == "diagram"


def test_image_kind_concept_experience_passes_through():
    content = draft_to_content(_image_draft("concept_experience"))
    assert content["kind"] == "concept_experience"


def test_unrecognised_image_kind_falls_back_to_illustration():
    """Regression guard on the existing fallback behaviour - only "diagram"
    and "concept_experience" are real kinds; anything else (including a
    typo, or the "illustration" default itself) must still collapse to
    "illustration", never silently pass through."""
    for kind in ("illustration", "", "photo", "concept_experiencee"):
        content = draft_to_content(_image_draft(kind))
        assert content["kind"] == "illustration", kind


def test_image_kind_is_case_and_whitespace_insensitive():
    content = draft_to_content(_image_draft("  Concept_Experience  "))
    assert content["kind"] == "concept_experience"


def test_illustration_style_textbook_passes_through():
    content = draft_to_content(_image_draft("illustration", illustration_style="  Textbook  "))
    assert content["illustration_style"] == "textbook"


def test_illustration_style_blank_by_default():
    content = draft_to_content(_image_draft("illustration"))
    assert content["illustration_style"] == ""


def test_formula_expression_passes_through():
    """Regression guard: the writer is told to set `image_formula_expression`
    (app/agents/prompts.py) for any block depicting a governing equation, and
    app.services.image_service reads `content["formula_expression"]` to run
    formula_qa - this is the one field connecting the two. It was previously
    missing from DraftBlock entirely, so the model's own output for it was
    silently discarded before draft_to_content ever ran, making formula_qa a
    permanent no-op in production."""
    draft = _image_draft("illustration")
    draft.image_formula_expression = "  F = m * a  "
    content = draft_to_content(draft)
    assert content["formula_expression"] == "F = m * a"


def test_formula_expression_blank_by_default():
    content = draft_to_content(_image_draft("illustration"))
    assert content["formula_expression"] == ""


# ---------------------------------------------------------------------------
# tag_section_intro_illustrations - the structural fallback for the writer's
# own illustration_style: section_intro instruction (see WRITER_SYSTEM)
# ---------------------------------------------------------------------------


def _heading() -> Block:
    return Block(type=BlockType.HEADING, content={"text": "A Section", "level": 2})


def _paragraph() -> Block:
    return Block(type=BlockType.PARAGRAPH, content={"text": "Some text."})


def _illustration(*, illustration_style: str = "") -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": "illustration", "illustration_style": illustration_style})


def test_an_untagged_illustration_opening_a_section_gets_tagged():
    blocks = [_heading(), _illustration(), _paragraph()]
    tag_section_intro_illustrations(blocks)
    assert blocks[1].content["illustration_style"] == "section_intro"


def test_an_untagged_illustration_as_the_very_first_block_gets_tagged():
    """No heading before it at all - the chapter's own opening image."""
    blocks = [_illustration(), _paragraph()]
    tag_section_intro_illustrations(blocks)
    assert blocks[0].content["illustration_style"] == "section_intro"


def test_an_explicit_textbook_style_is_never_overwritten():
    blocks = [_heading(), _illustration(illustration_style="textbook"), _paragraph()]
    tag_section_intro_illustrations(blocks)
    assert blocks[1].content["illustration_style"] == "textbook"


# ---------------------------------------------------------------------------
# Deterministic kids-audience apparatus override (code-level backstop for the
# prompt-level "real apparatus -> textbook by default, playful_infographic
# for a young audience" instruction, which real multi-sample testing showed
# the writer follows inconsistently - roughly 1 in 6 real apparatus blocks).
# ---------------------------------------------------------------------------


def test_young_learner_audience_classifier_positive_cases():
    assert _reads_as_young_learner_audience("Children aged 6 to 9 (early primary school)")
    assert _reads_as_young_learner_audience("Kids who love science")
    assert _reads_as_young_learner_audience("Students in grades 1-5")
    assert _reads_as_young_learner_audience("Young learners, age 7")
    assert _reads_as_young_learner_audience("Elementary school students")
    assert _reads_as_young_learner_audience("", "A kindergarten class")  # second text arg matches


def test_young_learner_audience_classifier_negative_cases():
    assert not _reads_as_young_learner_audience("Senior engineers and researchers")
    assert not _reads_as_young_learner_audience("Advanced practitioners seeking expert-level depth")
    assert not _reads_as_young_learner_audience("Professionals aged 25 to 60")
    assert not _reads_as_young_learner_audience("")
    assert not _reads_as_young_learner_audience("General audience with no prior experience")


def test_young_learner_age_range_requires_both_bounds_to_read_young():
    # A range where only one bound happens to be a small number must not
    # false-positive just because a number under 13 appears somewhere.
    assert not _reads_as_young_learner_audience("Professionals with 5 to 40 years of experience")


def _apparatus_illustration(*, illustration_style: str = "textbook") -> Block:
    return Block(
        type=BlockType.IMAGE,
        content={
            "kind": "illustration",
            "illustration_style": illustration_style,
            "expected_labels": ["Wheel", "Axle", "Effort", "Load"],
        },
    )


def test_override_fires_for_kids_audience_textbook_apparatus():
    blocks = [_apparatus_illustration(illustration_style="textbook")]
    apply_young_audience_apparatus_override(blocks, "Children aged 6 to 9")
    assert blocks[0].content["illustration_style"] == "playful_infographic"
    # Labels and every other field must survive untouched - this overrides
    # the style choice only, never the content.
    assert blocks[0].content["expected_labels"] == ["Wheel", "Axle", "Effort", "Load"]


def test_override_does_not_fire_for_non_kids_audience():
    blocks = [_apparatus_illustration(illustration_style="textbook")]
    apply_young_audience_apparatus_override(blocks, "Senior engineers and researchers")
    assert blocks[0].content["illustration_style"] == "textbook"


def test_override_does_not_fire_for_non_apparatus_content_under_kids_audience():
    """A decorative/default-style or section_intro image is never touched -
    `illustration_style == "textbook"` is the one signal this override acts
    on, confirmed to be the only reliable apparatus proxy in this pipeline."""
    blocks = [_apparatus_illustration(illustration_style=""), _apparatus_illustration(illustration_style="section_intro")]
    apply_young_audience_apparatus_override(blocks, "Children aged 6 to 9")
    assert blocks[0].content["illustration_style"] == ""
    assert blocks[1].content["illustration_style"] == "section_intro"


def test_override_is_a_no_op_when_writer_already_chose_playful_infographic():
    blocks = [_apparatus_illustration(illustration_style="playful_infographic")]
    apply_young_audience_apparatus_override(blocks, "Children aged 6 to 9")
    assert blocks[0].content["illustration_style"] == "playful_infographic"


def test_override_checks_every_given_audience_text_source():
    """Both `course_input.target_audience` and a template's own stated
    `audience` are real, independent sources - either one reading as young
    must trigger the override (see normalize_draft_blocks' own call site)."""
    blocks = [_apparatus_illustration(illustration_style="textbook")]
    apply_young_audience_apparatus_override(blocks, "General learners", "Children aged 6 to 9")
    assert blocks[0].content["illustration_style"] == "playful_infographic"


def test_override_is_a_no_op_with_no_audience_signal_at_all():
    blocks = [_apparatus_illustration(illustration_style="textbook")]
    apply_young_audience_apparatus_override(blocks, "", "")
    assert blocks[0].content["illustration_style"] == "textbook"


def test_an_illustration_not_followed_by_a_paragraph_is_left_untagged():
    blocks = [_heading(), _illustration(), _heading()]
    tag_section_intro_illustrations(blocks)
    assert blocks[1].content["illustration_style"] == ""


def test_an_illustration_not_opening_a_section_is_left_untagged():
    blocks = [_paragraph(), _illustration(), _paragraph()]
    tag_section_intro_illustrations(blocks)
    assert blocks[1].content["illustration_style"] == ""


def test_a_diagram_kind_image_is_never_tagged_section_intro():
    """illustration_style only ever means something for kind == "illustration" -
    a diagram/concept_experience block opening a section must never be
    mistaken for a section_intro illustration."""
    blocks = [_heading(), Block(type=BlockType.IMAGE, content={"kind": "diagram"}), _paragraph()]
    tag_section_intro_illustrations(blocks)
    assert blocks[1].content.get("illustration_style") in (None, "")


# Real, confirmed production case: a course generated against an uploaded
# "Kids Course Template" shipped "SME VERIFICATION" callouts carrying
# fabricated citation-shaped tags like "[G2.1]" on learner-facing pages -
# nothing in this codebase ever populates a "G"-prefixed reference with real
# backing content (unlike "[S1.3]", a genuine reference to an uploaded
# source excerpt), so every such tag is the writer model inventing a
# citation shape with nothing behind it.
def test_a_fabricated_g_prefixed_citation_tag_is_stripped():
    draft = DraftBlock(
        type=BlockType.PARAGRAPH,
        text="All claims must be checked by an SME before publication [G2.1].",
    )
    content = draft_to_content(draft)
    assert "[G2.1]" not in content["text"]
    assert content["text"] == "All claims must be checked by an SME before publication ."


def test_a_real_source_citation_tag_survives_the_same_filter():
    draft = DraftBlock(
        type=BlockType.PARAGRAPH,
        text="Refunds take 30 days [S1.3].",
    )
    content = draft_to_content(draft)
    assert "[S1.3]" in content["text"]
