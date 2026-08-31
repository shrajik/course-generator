"""Layout engine: height estimation, pagination and block splitting."""

from __future__ import annotations

from app.course.document.layout import (
    estimate_height,
    flow_blocks,
    split_block,
    text_height,
    wrapped_line_count,
)
from app.schemas.blocks import BlockType
from app.schemas.document import (
    CONTENT_HEIGHT,
    CONTENT_WIDTH,
    PAGE_MARGIN_TOP,
    Block,
    BlockStyle,
)


def _paragraph(words: int) -> Block:
    text = " ".join(["placeholder"] * words) + "."
    return Block(type=BlockType.PARAGRAPH, content={"text": text}, style=BlockStyle(font_size=15))


def test_wrapped_line_count_breaks_on_words():
    assert wrapped_line_count("one two three", 20) == 1
    assert wrapped_line_count("one two three four five six", 12) == 3
    assert wrapped_line_count("a\n\nb", 40) == 3


def test_text_height_grows_with_length():
    short = text_height("hello", font_size=15, width=600)
    long = text_height("hello " * 200, font_size=15, width=600)
    assert 0 < short < long


def test_bold_text_is_estimated_wider():
    plain = text_height("Practical Negotiation for Managers", font_size=42, width=666)
    bold = text_height("Practical Negotiation for Managers", font_size=42, width=666, bold=True)
    assert bold >= plain


def test_every_block_type_estimates_a_positive_height():
    samples = {
        BlockType.HEADING: {"text": "Title"},
        BlockType.PARAGRAPH: {"text": "Body copy."},
        BlockType.IMAGE: {"prompt": "p", "caption": "c"},
        BlockType.QUOTE: {"text": "q", "attribution": "a"},
        BlockType.CALLOUT: {"title": "t", "text": "x"},
        BlockType.CODE: {"code": "print(1)\nprint(2)", "language": "python"},
        BlockType.TABLE: {"columns": ["a", "b"], "rows": [{"cells": ["1", "2"]}]},
        BlockType.QUIZ: {"questions": [{"question": "q?", "options": ["a", "b"], "answer": "a"}]},
        BlockType.EXERCISE: {"instructions": "do it", "steps": ["one"]},
        BlockType.CHALLENGE: {"instructions": "harder"},
        BlockType.CASE_STUDY: {"context": "c", "challenge": "ch", "lessons": ["l"]},
        BlockType.STORY: {"text": "once", "takeaway": "t"},
        BlockType.TIP: {"text": "tip"},
        BlockType.WARNING: {"text": "careful"},
        BlockType.SUMMARY: {"key_takeaways": ["a", "b"], "next_steps": ["c"]},
        BlockType.DIVIDER: {},
        BlockType.LEARNING_OBJECTIVES: {"items": ["a", "b"]},
        BlockType.REFLECTION: {"items": ["why?"]},
    }
    for block_type, content in samples.items():
        block = Block(type=block_type, content=content)
        assert estimate_height(block) > 0, block_type


def test_blocks_flow_onto_a_single_page_when_they_fit():
    pages = flow_blocks([_paragraph(20), _paragraph(20)])
    assert len(pages) == 1
    first, second = pages[0]
    assert first.layout.y >= PAGE_MARGIN_TOP
    assert second.layout.y > first.layout.y + first.layout.height - 1


def test_long_content_paginates():
    pages = flow_blocks([_paragraph(60) for _ in range(20)])
    assert len(pages) > 1
    for page in pages:
        last = page[-1]
        assert last.layout.y + last.layout.height <= PAGE_MARGIN_TOP + CONTENT_HEIGHT + 1


def test_no_blocks_overlap_within_a_page():
    pages = flow_blocks([_paragraph(40) for _ in range(12)])
    for page in pages:
        for previous, current in zip(page, page[1:]):
            assert current.layout.y >= previous.layout.y + previous.layout.height - 0.01


def test_oversized_paragraph_is_split_across_pages():
    huge = _paragraph(4000)
    pages = flow_blocks([huge])
    assert len(pages) > 1
    text = " ".join(
        block.content["text"] for page in pages for block in page
    )
    assert text.count("placeholder") == 4000


def test_split_block_returns_none_for_atomic_blocks():
    block = Block(type=BlockType.IMAGE, content={"prompt": "x"})
    assert split_block(block, 300) is None


def test_split_block_refuses_a_tiny_window():
    assert split_block(_paragraph(500), 10) is None


def test_code_blocks_split_on_line_boundaries():
    code = "\n".join(f"line_{i} = {i}" for i in range(400))
    block = Block(type=BlockType.CODE, content={"code": code}, style=BlockStyle(font_size=12.5))
    result = split_block(block, 400)
    assert result is not None
    head, tail = result
    head_lines = head.content["code"].split("\n")
    tail_lines = tail.content["code"].split("\n")
    assert len(head_lines) + len(tail_lines) == 400
    assert head_lines[0] == "line_0 = 0"
    assert tail_lines[-1] == "line_399 = 399"
    assert estimate_height(head) <= 400


def test_summary_lists_split_and_keep_every_item():
    block = Block(
        type=BlockType.SUMMARY,
        content={"key_takeaways": [f"takeaway number {i}" for i in range(60)]},
        style=BlockStyle(font_size=15, padding=18),
    )
    result = split_block(block, 300)
    assert result is not None
    head, tail = result
    assert len(head.content["key_takeaways"]) + len(tail.content["key_takeaways"]) == 60


def test_headings_are_not_orphaned_at_the_page_bottom():
    blocks = [_paragraph(45) for _ in range(9)]
    blocks.append(Block(type=BlockType.HEADING, content={"text": "New Section"}))
    blocks.append(_paragraph(60))
    pages = flow_blocks(blocks)
    for page in pages:
        if page[-1].type is BlockType.HEADING:
            raise AssertionError("a heading was left alone at the bottom of a page")


def test_layout_width_is_normalised_to_the_content_width():
    block = _paragraph(10)
    block.layout.width = 12345
    flow_blocks([block])
    assert block.layout.width == CONTENT_WIDTH
