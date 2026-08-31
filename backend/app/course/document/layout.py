"""Deterministic layout engine.

Estimates each block's height, then flows blocks into fixed-size pages, splitting
long paragraphs / code / lists across page boundaries. The engine writes absolute
`layout` coordinates into the Course Document so the PDF renderer (and later the
visual editor) can position blocks without re-deriving anything.

Heights are estimates, not a full text-shaping engine. The CSS deliberately uses
`min-height`, so a slightly under-estimated block grows rather than clipping.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.schemas.blocks import BlockType
from app.schemas.document import (
    CONTENT_HEIGHT,
    CONTENT_WIDTH,
    PAGE_MARGIN_TOP,
    PAGE_MARGIN_X,
    Block,
)

BLOCK_GAP = 18.0
SECTION_GAP = 26.0
MIN_ORPHAN_SPACE = 140.0  # don't leave a heading alone at the bottom of a page

# Average glyph advance as a fraction of the font size, calibrated against the
# metrics of the fonts Chromium actually falls back to (DejaVu / Liberation /
# Helvetica class). Bold faces are noticeably wider.
_SANS_RATIO = 0.53
_MONO_RATIO = 0.61
_BOLD_FACTOR = 1.12
# Estimates should err high: extra whitespace is harmless, overlap is not.
_SAFETY = 1.06

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_DEFAULT_FONT_SIZE: dict[BlockType, float] = {
    BlockType.HEADING: 26.0,
    BlockType.CODE: 12.5,
    BlockType.TABLE: 13.5,
    BlockType.QUOTE: 16.0,
}


def _font_size(block: Block) -> float:
    return float(
        block.style.font_size or _DEFAULT_FONT_SIZE.get(block.type, 15.0)
    )


def _line_height(block: Block) -> float:
    if block.style.line_height:
        return float(block.style.line_height)
    return 1.28 if block.type is BlockType.HEADING else 1.6


def _padding(block: Block) -> float:
    return float(block.style.padding or 0.0)


def wrapped_line_count(text: str, chars_per_line: int, *, wrap_words: bool = True) -> int:
    """Greedy word wrap - matches how a browser breaks lines far better than
    dividing the character count."""
    lines = 0
    for raw_line in str(text).split("\n"):
        if not raw_line.strip():
            lines += 1
            continue
        if not wrap_words:
            lines += max(1, math.ceil(len(raw_line) / chars_per_line))
            continue
        used = 0
        started = False
        for word in raw_line.split():
            needed = len(word) if not started else len(word) + 1
            if started and used + needed > chars_per_line:
                lines += 1
                used = len(word)
            else:
                used += needed
                started = True
            while used > chars_per_line:  # a single word longer than the line
                lines += 1
                used -= chars_per_line
        lines += 1
    return max(lines, 1)


def text_height(
    text: str,
    *,
    font_size: float,
    width: float,
    line_height: float = 1.6,
    mono: bool = False,
    bold: bool = False,
) -> float:
    """Estimate the rendered height of a text run."""
    if not text:
        return 0.0
    ratio = _MONO_RATIO if mono else _SANS_RATIO
    if bold:
        ratio *= _BOLD_FACTOR
    chars_per_line = max(int(width / (font_size * ratio)), 8)
    lines = wrapped_line_count(text, chars_per_line, wrap_words=not mono)
    return lines * font_size * line_height * _SAFETY


def _list_height(items: list[Any], *, font_size: float, width: float, lh: float) -> float:
    total = 0.0
    for item in items:
        total += text_height(
            f"• {item}", font_size=font_size, width=width - 20, line_height=lh
        ) + 4
    return total


MAX_IMAGE_HEIGHT = 430.0
DEFAULT_IMAGE_ASPECT = 0.5625  # 16:9 until the real asset is measured


def caption_height(block: Block) -> float:
    caption = block.content.get("caption")
    if not caption:
        return 0.0
    pad = _padding(block)
    width = float(block.layout.width or CONTENT_WIDTH) - 2 * pad
    return text_height(str(caption), font_size=12.5, width=width, line_height=1.45) + 8


def image_box_height(block: Block) -> float:
    """Height reserved for the picture itself (excluding caption and padding)."""
    pad = _padding(block)
    width = float(block.layout.width or CONTENT_WIDTH) - 2 * pad
    intrinsic_w = block.content.get("width")
    intrinsic_h = block.content.get("height")
    if intrinsic_w and intrinsic_h:
        aspect = float(intrinsic_h) / float(intrinsic_w)
    else:
        aspect = DEFAULT_IMAGE_ASPECT
    return min(width * aspect, MAX_IMAGE_HEIGHT)


def estimate_height(block: Block) -> float:
    """Height in CSS px for one block at the document content width."""
    content = block.content
    font_size = _font_size(block)
    lh = _line_height(block)
    pad = _padding(block)
    width = float(block.layout.width or CONTENT_WIDTH) - 2 * pad
    t = block.type

    is_bold = t is BlockType.HEADING or (block.style.font_weight or 0) >= 600

    def th(
        value: Any, *, size: float | None = None, mono: bool = False, bold: bool | None = None
    ) -> float:
        return text_height(
            str(value or ""),
            font_size=size or font_size,
            width=width,
            line_height=lh,
            mono=mono,
            bold=is_bold if bold is None else bold,
        )

    if t is BlockType.HEADING:
        return th(content.get("text")) + 10

    if t is BlockType.PARAGRAPH:
        return th(content.get("text"))

    if t is BlockType.IMAGE:
        return image_box_height(block) + caption_height(block) + 2 * pad + 8

    if t is BlockType.QUOTE:
        return th(content.get("text")) + (th(content.get("attribution"), size=13) or 0) + 2 * pad

    if t in (BlockType.CALLOUT, BlockType.TIP, BlockType.WARNING):
        title = 22.0 if content.get("title") else 0.0
        return title + th(content.get("text")) + 2 * pad

    if t is BlockType.CODE:
        caption = content.get("caption")
        body = th(content.get("code"), mono=True)
        return body + (th(caption, size=12.5) + 6 if caption else 0) + 2 * pad + 8

    if t is BlockType.TABLE:
        rows = content.get("rows") or []
        columns = content.get("columns") or []
        col_width = max(width / max(len(columns), 1), 60)
        header = 20 + font_size * lh
        body = 0.0
        for row in rows:
            cells = row.get("cells", []) if isinstance(row, dict) else []
            tallest = max(
                [
                    text_height(str(cell), font_size=font_size, width=col_width - 16, line_height=lh)
                    for cell in cells
                ]
                or [font_size * lh]
            )
            body += tallest + 16
        caption = content.get("caption")
        return header + body + (th(caption, size=12.5) + 8 if caption else 0) + 2 * pad + 4

    if t is BlockType.QUIZ:
        total = 26.0 + 2 * pad
        for question in content.get("questions") or []:
            total += th(question.get("question")) + 6
            total += len(question.get("options") or []) * (font_size * lh + 6)
            total += th(f"Answer: {question.get('answer','')}", size=font_size - 1)
            total += th(question.get("explanation"), size=font_size - 1) + 12
        return total

    if t in (BlockType.EXERCISE, BlockType.CHALLENGE):
        total = 26.0 + 2 * pad + th(content.get("instructions"))
        total += _list_height(content.get("steps") or [], font_size=font_size, width=width, lh=lh)
        total += _list_height(content.get("hints") or [], font_size=font_size, width=width, lh=lh)
        if content.get("expected_outcome"):
            total += th(f"Expected outcome: {content['expected_outcome']}") + 8
        return total + 12

    if t is BlockType.CASE_STUDY:
        total = 26.0 + 2 * pad
        for key in ("context", "challenge", "outcome"):
            if content.get(key):
                total += 20 + th(content[key])
        total += _list_height(
            content.get("actions") or [], font_size=font_size, width=width, lh=lh
        )
        total += _list_height(
            content.get("lessons") or [], font_size=font_size, width=width, lh=lh
        )
        return total + 20

    if t is BlockType.STORY:
        total = (22.0 if content.get("title") else 0.0) + th(content.get("text")) + 2 * pad
        if content.get("takeaway"):
            total += th(f"Takeaway: {content['takeaway']}") + 10
        return total

    if t is BlockType.SUMMARY:
        total = 26.0 + 2 * pad
        total += _list_height(
            content.get("key_takeaways") or [], font_size=font_size, width=width, lh=lh
        )
        if content.get("next_steps"):
            total += 22 + _list_height(
                content["next_steps"], font_size=font_size, width=width, lh=lh
            )
        return total + 8

    if t is BlockType.DIVIDER:
        return 24.0

    if t in (BlockType.LEARNING_OBJECTIVES, BlockType.REFLECTION):
        total = 26.0 + 2 * pad
        if content.get("intro"):
            total += th(content["intro"]) + 6
        total += _list_height(
            content.get("items") or [], font_size=font_size, width=width, lh=lh
        )
        return total + 8

    # Unknown type - be generous.
    return th(content.get("text")) + 40


# ---------------------------------------------------------------------------
# splitting
# ---------------------------------------------------------------------------

_SPLITTABLE_LIST_KEYS: dict[BlockType, str] = {
    BlockType.LEARNING_OBJECTIVES: "items",
    BlockType.REFLECTION: "items",
    BlockType.SUMMARY: "key_takeaways",
}

MIN_FRAGMENT_HEIGHT = 70.0


def _clone(block: Block, content: dict[str, Any], *, continued: bool) -> Block:
    meta = block.meta.model_copy(update={"continued": continued})
    clone = Block(
        id=block.id if not continued else f"{block.id}_c{int(continued)}",
        type=block.type,
        content=content,
        style=block.style.model_copy(deep=True),
        layout=block.layout.model_copy(deep=True),
        meta=meta,
    )
    return clone


def split_block(block: Block, available: float) -> tuple[Block, Block] | None:
    """Split `block` so the first part fits in `available` px. None if impossible."""
    if available < MIN_FRAGMENT_HEIGHT:
        return None

    content = block.content
    t = block.type

    if t is BlockType.PARAGRAPH:
        return _split_text(block, content.get("text", ""), available, key="text")

    if t is BlockType.CODE:
        return _split_lines(block, content.get("code", ""), available, key="code")

    list_key = _SPLITTABLE_LIST_KEYS.get(t)
    if list_key:
        return _split_list(block, list_key, available)

    return None


def _fits(block: Block, available: float) -> bool:
    return estimate_height(block) <= available


def _split_text(
    block: Block, text: str, available: float, *, key: str
) -> tuple[Block, Block] | None:
    parts = _SENTENCE_RE.split(text or "")
    if len(parts) < 2:
        # A single very long sentence: fall back to splitting on words.
        parts = (text or "").split()
        if len(parts) < 8:
            return None
    for cut in range(len(parts) - 1, 0, -1):
        head_text = " ".join(parts[:cut]).strip()
        head = _clone(block, {**block.content, key: head_text}, continued=False)
        if _fits(head, available):
            tail_text = " ".join(parts[cut:]).strip()
            if not tail_text:
                return None
            tail = _clone(block, {**block.content, key: tail_text}, continued=True)
            return head, tail
    return None


def _split_lines(
    block: Block, text: str, available: float, *, key: str
) -> tuple[Block, Block] | None:
    lines = (text or "").split("\n")
    if len(lines) < 4:
        return None
    for cut in range(len(lines) - 1, 0, -1):
        head = _clone(block, {**block.content, key: "\n".join(lines[:cut])}, continued=False)
        if _fits(head, available):
            tail_content = {**block.content, key: "\n".join(lines[cut:]), "caption": ""}
            return head, _clone(block, tail_content, continued=True)
    return None


def _split_list(block: Block, key: str, available: float) -> tuple[Block, Block] | None:
    items = list(block.content.get(key) or [])
    if len(items) < 2:
        return None
    for cut in range(len(items) - 1, 0, -1):
        head = _clone(block, {**block.content, key: items[:cut]}, continued=False)
        if _fits(head, available):
            tail_content = {**block.content, key: items[cut:], "title": ""}
            if key == "key_takeaways":
                tail_content["next_steps"] = block.content.get("next_steps") or []
                head.content["next_steps"] = []
            return head, _clone(block, tail_content, continued=True)
    return None


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------


def flow_blocks(blocks: list[Block]) -> list[list[Block]]:
    """Position blocks and group them into pages. Mutates layout coordinates."""
    pages: list[list[Block]] = []
    current: list[Block] = []
    y = PAGE_MARGIN_TOP
    bottom = PAGE_MARGIN_TOP + CONTENT_HEIGHT

    def start_new_page() -> None:
        nonlocal current, y
        if current:
            pages.append(current)
        current = []
        y = PAGE_MARGIN_TOP

    queue = list(blocks)
    while queue:
        block = queue.pop(0)
        block.layout.x = PAGE_MARGIN_X
        block.layout.width = CONTENT_WIDTH
        height = estimate_height(block)

        gap = 0.0
        if current:
            gap = SECTION_GAP if block.type is BlockType.HEADING else BLOCK_GAP

        # Never leave a heading stranded at the bottom of a page.
        if (
            block.type is BlockType.HEADING
            and current
            and y + gap + height + MIN_ORPHAN_SPACE > bottom
        ):
            start_new_page()
            gap = 0.0

        if y + gap + height <= bottom:
            block.layout.y = y + gap
            block.layout.height = round(height, 2)
            current.append(block)
            y = block.layout.y + height
            continue

        # Doesn't fit: try to split it across the page boundary.
        available = bottom - (y + gap)
        pieces = split_block(block, available)
        if pieces is not None:
            head, tail = pieces
            head.layout.x = PAGE_MARGIN_X
            head.layout.width = CONTENT_WIDTH
            head.layout.y = y + gap
            head.layout.height = round(estimate_height(head), 2)
            current.append(head)
            start_new_page()
            queue.insert(0, tail)
            continue

        if current:
            # Retry the whole block at the top of a fresh page.
            start_new_page()
            queue.insert(0, block)
            continue

        # Fresh page, unsplittable and taller than a page: place it and let the
        # CSS grow the box rather than clipping the content.
        block.layout.y = y
        block.layout.height = round(height, 2)
        current.append(block)
        y = block.layout.y + height

    if current:
        pages.append(current)
    return pages
