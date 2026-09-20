"""`draft_to_content` - how a flat writer DraftBlock becomes a validated
Course Document block's content. Covers the image_kind dispatch specifically
(diagram / concept_experience / illustration fallback) - see the Phase 4 plan
at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md."""

from __future__ import annotations

from app.course.blocks.normalizer import draft_to_content
from app.schemas.blocks import BlockType
from app.schemas.draft import DraftBlock


def _image_draft(image_kind: str) -> DraftBlock:
    return DraftBlock(
        type=BlockType.IMAGE,
        image_purpose="A purpose",
        image_prompt="A prompt",
        caption="A caption",
        image_kind=image_kind,
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
