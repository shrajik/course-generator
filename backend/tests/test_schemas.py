"""Pydantic schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.blocks import CONTENT_MODELS, BlockType, merge_content, validate_content
from app.schemas.course import CourseInput, TocItem
from app.schemas.document import Block, CourseDocument, Page
from app.schemas.review import ReviewScores


def test_every_block_type_has_a_content_model():
    missing = [bt for bt in BlockType if bt not in CONTENT_MODELS]
    assert missing == []


def test_spec_block_types_are_all_present():
    required = {
        "heading",
        "paragraph",
        "image",
        "quote",
        "callout",
        "code",
        "table",
        "quiz",
        "exercise",
        "case_study",
        "story",
        "tip",
        "warning",
        "summary",
        "divider",
    }
    assert required <= set(BlockType.values())


def test_block_validates_and_normalises_content():
    block = Block(type=BlockType.HEADING, content={"text": "Understanding RAG"})
    assert block.content["level"] == 2  # default filled in
    assert block.id.startswith("block_")
    assert block.layout.width > 0


def test_block_rejects_content_missing_required_field():
    with pytest.raises(ValidationError):
        Block(type=BlockType.PARAGRAPH, content={})


def test_content_style_and_layout_stay_separate():
    block = Block(
        type=BlockType.HEADING,
        content={"text": "Title"},
        style={"font_size": 32, "font_weight": 700},
        layout={"x": 80, "y": 100, "width": 600, "height": 60},
    )
    dumped = block.model_dump(mode="json")
    assert set(dumped) >= {"content", "style", "layout"}
    assert dumped["style"]["font_size"] == 32
    assert dumped["layout"] == {"x": 80, "y": 100, "width": 600, "height": 60, "z_index": 0}
    assert "font_size" not in dumped["content"]


def test_merge_content_is_shallow_and_revalidated():
    original = validate_content(BlockType.QUIZ, {"title": "Check", "questions": []})
    merged = merge_content(BlockType.QUIZ, original, {"title": "Harder check"})
    assert merged["title"] == "Harder check"
    assert merged["questions"] == []


def test_extra_content_keys_are_preserved():
    content = validate_content(BlockType.PARAGRAPH, {"text": "x", "editor_note": "keep me"})
    assert content["editor_note"] == "keep me"


def test_course_input_accepts_plain_string_toc():
    course_input = CourseInput(
        course_title="X",
        toc=["One", "Two"],
        target_audience="Y",
        template="technical",
    )
    assert course_input.toc == [TocItem(title="One"), TocItem(title="Two")]
    assert course_input.template_id == "technical_v1"


def test_course_input_requires_a_known_template():
    with pytest.raises(ValidationError):
        CourseInput(
            course_title="X", toc=["One"], target_audience="Y", template="hybrid"
        )


def test_course_input_rejects_empty_toc():
    with pytest.raises(ValidationError):
        CourseInput(course_title="X", toc=[], target_audience="Y", template="technical")


def test_document_lookup_helpers():
    block = Block(type=BlockType.PARAGRAPH, content={"text": "hello"})
    document = CourseDocument(
        document_id="doc_1",
        course_id="crs_1",
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[block])],
    )
    assert document.find_block(block.id)[1] is block
    assert document.find_block("nope") is None
    assert document.block_ids() == [block.id]
    version = document.version
    document.touch()
    assert document.version == version + 1


def test_review_scores_ignore_none_dimensions():
    scores = ReviewScores(accuracy=8, clarity=6, technical_correctness=None)
    assert 0 < scores.overall() <= 10
