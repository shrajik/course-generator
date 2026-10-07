"""`draft_to_content` - how a flat writer DraftBlock becomes a validated
Course Document block's content. Covers the image_kind dispatch specifically
(diagram / concept_experience / illustration fallback) - see the Phase 4 plan
at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md."""

from __future__ import annotations

from app.course.blocks.normalizer import draft_to_content, tag_section_intro_illustrations
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
