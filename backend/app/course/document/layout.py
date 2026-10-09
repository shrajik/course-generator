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

from app.course.document.code_cell import (
    CAPTION_GAP as CELL_CAPTION_GAP,
    HEAD_H as CELL_HEAD_H,
    MIN_CODE_LINES_PER_FRAGMENT,
    ROW_H as CELL_ROW_H,
    cell_body_height,
    code_box_height,
    code_lines,
    output_box_height,
    output_view,
    static_code_height,
)
from app.schemas.blocks import VISUAL_BLOCK_TYPES, BlockType
from app.schemas.template import PageGeometry
from app.schemas.document import (
    CONTENT_HEIGHT,
    CONTENT_WIDTH,
    PAGE_MARGIN_TOP,
    Block,
)

BLOCK_GAP = 18.0
SECTION_GAP = 26.0
MIN_ORPHAN_SPACE = 140.0  # don't leave a heading alone at the bottom of a page

# A page must already hold at least this much *absolute* content before this
# module will break it early to keep a visual with its lead-in text (see
# flow_blocks) - without this floor, a barely-started page (say, just a lone
# heading) would keep breaking itself almost immediately, producing a string
# of near-empty pages instead of the rare, deliberate early break this is
# meant to be. Deliberately a fixed pixel amount, not a fraction of the page:
# a percentage floor (this used to be 35% of CONTENT_HEIGHT) blocks the break
# on a page that's already substantially, genuinely full of text just because
# it hasn't crossed an arbitrary ratio - which is exactly the "text-heavy
# page can't make room for its visual" gap this module exists to close.
_MIN_CONTENT_BEFORE_EARLY_BREAK = 150.0

# How many upcoming non-visual blocks this module will bridge over to find a
# pending visual worth protecting. The writer rarely stacks more than one or
# two paragraphs before a diagram/image; bounding the scan keeps the check
# cheap and stops it from pulling in content that has nothing to do with the
# decision at hand.
_VISUAL_LOOKAHEAD_LIMIT = 3

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
    BlockType.CODE_CELL: 12.5,
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
# A deterministic SVG diagram (flow_chart, hierarchy, data_flow_diagram, ...)
# is not a fixed-aspect decorative photo - a diagram with many nodes
# genuinely needs real vertical room (a 14-node flow_chart can easily be
# 800px+ tall at its natural width). Capping it at the same MAX_IMAGE_HEIGHT
# a small photo uses forces a WIDTH-locked box (CONTENT_WIDTH stays fixed)
# into a much shorter height, and since the actual `<img>` keeps its aspect
# ratio (`max-height:100%; width:auto` - see course.html.j2), that shrinks
# the rendered WIDTH just as drastically - confirmed: a real 880x1904
# flow_chart rendered at ~199px wide under the old flat cap, unreadably
# small. This mirrors concept_experience's own ceiling (_MAX_HEIGHT in
# concept_experience_renderer.py) - "as tall as reasonably fits one page,
# no more" - rather than a size meant for a small decorative picture.
MAX_DIAGRAM_IMAGE_HEIGHT = CONTENT_HEIGHT - 100.0
DEFAULT_IMAGE_ASPECT = 0.5625  # 16:9 until the real asset is measured


def caption_height(block: Block) -> float:
    caption = block.content.get("caption")
    if not caption:
        return 0.0
    pad = _padding(block)
    width = float(block.layout.width or CONTENT_WIDTH) - 2 * pad
    return text_height(str(caption), font_size=12.5, width=width, line_height=1.45) + 8


# Kinds whose own renderer already caps its height at roughly one page and
# reports the real (not fixed-aspect) result via content.width/height - see
# app.render.concept_experience_renderer.estimate_pixel_size and
# app.render.toc_renderer.estimate_toc_pixel_size. Every block after one of
# these on the page is positioned from that estimate, so re-capping it here
# with MAX_IMAGE_HEIGHT would silently reintroduce the exact overlap those
# estimates exist to prevent.
_UNCAPPED_IMAGE_KINDS = {"concept_experience", "toc"}


# A picture whose generation failed shows a one-line "Visual unavailable"
# strip, not an empty frame the size of the picture it should have been.
MISSING_IMAGE_BOX_HEIGHT = 36.0


def image_generation_failed(block: Block) -> bool:
    """An image with no file because generating it failed. (An image that has
    simply not been generated yet has no `error`, and keeps its full slot.)"""
    content = block.content
    return (
        block.type is BlockType.IMAGE
        and content.get("kind") != "toc"
        and not content.get("path")
        and bool(content.get("error"))
    )


def image_box_height(block: Block, *, width_hint: float | None = None) -> float:
    """Height reserved for the picture itself (excluding caption and padding).

    `MAX_IMAGE_HEIGHT` caps a decorative raster picture's box - reasonable
    there, since a photo at a fixed aspect ratio never needs much vertical
    room. A `kind == "diagram"` SVG gets the taller `MAX_DIAGRAM_IMAGE_HEIGHT`
    instead - it's structured content whose real height reflects real node
    count, not a decorative aspect ratio, so the same small cap would just
    force its rendered WIDTH down too (both dimensions shrink together to
    keep the aspect ratio - see MAX_DIAGRAM_IMAGE_HEIGHT's own docstring).
    The kinds in `_UNCAPPED_IMAGE_KINDS` are not pictures at a fixed aspect
    ratio at all; they manage their own one-page ceiling internally, so any
    cap here would only double (and wrongly shrink) what they already do.

    `width_hint`, when given, is used instead of `block.layout.width` - for
    a block `flow_blocks` hasn't placed yet (a lookahead estimate, e.g.
    `_pending_visual_run`/`_is_visual_pair` peeking at an upcoming block),
    `layout.width` is still unset/default and this is the only way to get
    an accurate number. See `estimate_height`'s own IMAGE branch, the one
    caller that needs this - every other caller runs at actual placement
    time, after `layout.width` is already correct, and omits it.
    """
    if image_generation_failed(block):
        return MISSING_IMAGE_BOX_HEIGHT
    pad = _padding(block)
    raw_width = width_hint if width_hint is not None else float(block.layout.width or CONTENT_WIDTH)
    width = raw_width - 2 * pad
    intrinsic_w = block.content.get("width")
    intrinsic_h = block.content.get("height")
    if intrinsic_w and intrinsic_h:
        aspect = float(intrinsic_h) / float(intrinsic_w)
    else:
        aspect = DEFAULT_IMAGE_ASPECT
    height = width * aspect
    kind = block.content.get("kind")
    if kind in _UNCAPPED_IMAGE_KINDS:
        return height
    if kind == "diagram":
        return min(height, MAX_DIAGRAM_IMAGE_HEIGHT)
    return min(height, MAX_IMAGE_HEIGHT)


# A section_intro illustration (see app.schemas.blocks.ImageContent's own
# docstring on illustration_style) is deliberately small and float:left in
# the PDF export, with its section's first paragraph wrapping around its
# right/bottom edge - never the full CONTENT_WIDTH an ordinary image gets.
# 4:3 to match the exact size ImageService generates/crops these to.
SECTION_INTRO_IMAGE_WIDTH = 210.0
SECTION_INTRO_IMAGE_ASPECT = 0.75  # 4:3
SECTION_INTRO_FLOAT_GAP = 16.0  # matches the PDF template's own float margin-right


def _is_section_intro(block: Block) -> bool:
    return block.type is BlockType.IMAGE and (block.content.get("illustration_style") or "") == "section_intro"


def _section_intro_pair_height(image: Block, paragraph: Block, image_h: float) -> float:
    """The ONE combined reserved height for a section_intro image and the
    paragraph it floats beside in the PDF export - an estimate, not real
    two-region text shaping (this module's whole philosophy: estimates err
    high, `min-height` in the CSS tolerates it - see this file's own
    docstring). Assumes as many of the paragraph's lines as fit within the
    image's own height wrap in the narrower space beside it; anything left
    over wraps at the full content width below the image, once its height
    is exhausted - matching how the real float actually behaves in
    Chromium."""
    font_size = _font_size(paragraph)
    lh = _line_height(paragraph)
    pad = _padding(paragraph)
    text = str(paragraph.content.get("text") or "")
    if not text.strip():
        return image_h
    narrow_width = max(CONTENT_WIDTH - SECTION_INTRO_IMAGE_WIDTH - SECTION_INTRO_FLOAT_GAP - 2 * pad, 60.0)
    full_width = max(CONTENT_WIDTH - 2 * pad, 60.0)
    narrow_cpl = max(int(narrow_width / (font_size * _SANS_RATIO)), 8)
    full_cpl = max(int(full_width / (font_size * _SANS_RATIO)), 8)
    max_lines_beside = max(int(image_h / (font_size * lh)), 1)
    lines_if_narrow_only = wrapped_line_count(text, narrow_cpl)
    if lines_if_narrow_only <= max_lines_beside:
        # The whole paragraph fits beside the image at the narrow width.
        text_h = lines_if_narrow_only * font_size * lh * _SAFETY
        return max(image_h, text_h)
    # Overflows past the image's own height - the first max_lines_beside
    # narrow-width lines sit beside it; a rough character budget estimates
    # how much text that covers, and the remainder wraps at the full width
    # below it.
    consumed_chars = max_lines_beside * narrow_cpl
    remainder = text[consumed_chars:]
    remainder_lines = wrapped_line_count(remainder, full_cpl) if remainder.strip() else 0
    text_h = (max_lines_beside + remainder_lines) * font_size * lh * _SAFETY
    return max(image_h, text_h)


def _code_cell_height(block: Block, th: Any) -> float:
    """Height of a code cell: head row + code box + output box (+ caption).

    The geometry comes from code_cell.py, which also owns the CSS numbers the
    PDF and the editor draw with - so what is reserved here is what is
    painted there. The output box is always accounted for in the state it
    will actually be drawn in: real output, or a one-line "not executed" /
    "stale" strip. (Reserving it only when output existed is what let the
    editor's own banners grow past their slot.)
    """
    content = block.content
    height = cell_body_height(content, float(block.layout.width or CONTENT_WIDTH))
    caption = content.get("caption")
    if caption and not content.get("cell_continued"):
        height += th(caption, size=12.5) + CELL_CAPTION_GAP
    return height


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
        # A section_intro picture is small and drawn without a caption.
        # It's also laid out at SECTION_INTRO_IMAGE_WIDTH, not the full
        # content width this function otherwise assumes - a real, confirmed
        # bug: `_pending_visual_run`/`_is_visual_pair` (flow_blocks) call
        # this on an UPCOMING section_intro block before flow_blocks has
        # set its real `layout.width`, so without this hint the fallback-
        # to-CONTENT_WIDTH here overestimates its height ~3x (full-width
        # aspect-ratio box vs the true narrow one), making the lookahead
        # wrongly conclude a small heading+icon pair "won't fit" the
        # current page and force an early break that strands them alone on
        # a near-empty fresh page - confirmed via a real generated course
        # whose "Common mistakes" heading+icon landed on its own 20%-full
        # page for exactly this reason.
        is_intro = _is_section_intro(block)
        caption = 0.0 if is_intro else caption_height(block)
        width_hint = SECTION_INTRO_IMAGE_WIDTH if is_intro else None
        return image_box_height(block, width_hint=width_hint) + caption + 2 * pad + 8

    if t is BlockType.QUOTE:
        return th(content.get("text")) + (th(content.get("attribution"), size=13) or 0) + 2 * pad

    if t in (BlockType.CALLOUT, BlockType.TIP, BlockType.WARNING):
        title = 22.0 if content.get("title") else 0.0
        return title + th(content.get("text")) + 2 * pad

    if t is BlockType.CODE:
        # Same box as a code cell (head row + numbered rows), so the height is
        # exactly the rendered line count - no fixed size, no padding guesses.
        caption = content.get("caption")
        height = static_code_height(content, float(block.layout.width or CONTENT_WIDTH))
        return height + (th(caption, size=12.5) + CELL_CAPTION_GAP if caption else 0)

    if t is BlockType.CODE_CELL:
        return _code_cell_height(block, th)

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
            head.meta = head.meta.model_copy(update={"continued": block.meta.continued})
            tail_content = {**block.content, key: "\n".join(lines[cut:]), "caption": ""}
            if block.type is BlockType.CODE:
                # Keep the line numbers continuous across the page break.
                tail_content["code_line_offset"] = int(block.content.get("code_line_offset") or 0) + cut
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


def split_code_cell(
    block: Block, available: float, capacity: float
) -> tuple[Block, Block] | None:
    """Split a code cell that is too tall for ANY page, for printing only.

    A cell is one logical block, so the rule is the opposite of paragraphs and
    plain code: it is never split just because it does not fit the space left
    on this page. Returning None sends the whole cell to the top of a fresh
    page, which is almost always what is wanted. Only a cell taller than a full
    page (`capacity`) - which no amount of moving can fix - is split.

    Two safe cuts, in order of preference:

    * if all the code fits here but the output does not: code on this page,
      output alone on the next. The output is atomic and capped
      (MAX_OUTPUT_LINES), so it always fits a page on its own.
    * otherwise cut the code between two lines. The last fragment carries the
      output, and a fragment never leaves a one- or two-line stub behind.

    Fragments exist only in the copy made for printing (see
    app.course.document.print); a stored document always holds whole cells.
    """
    if estimate_height(block) <= capacity:
        return None

    content = block.content
    width = float(block.layout.width or CONTENT_WIDTH)
    lines = code_lines(str(content.get("code") or ""))
    offset = int(content.get("cell_line_offset") or 0)
    shows_output = not content.get("cell_hide_output")
    # Staleness is judged against the whole cell's code, not a fragment's.
    whole = content.get("cell_code", content.get("code"))
    base = {**content, "cell_code": whole}

    def fragment(start: int, stop: int, *, output: bool, continued: bool) -> Block:
        piece = {
            **base,
            "code": "\n".join(lines[start:stop]),
            "cell_line_offset": offset + start,
            "cell_hide_output": not output,
            "cell_continued": continued or bool(content.get("cell_continued")),
        }
        if continued or content.get("cell_continued"):
            piece["caption"] = ""
        return _clone(block, piece, continued=continued)

    # Cut 1: the whole code fits here, only the output does not.
    if shows_output:
        code_only = fragment(0, len(lines), output=False, continued=False)
        if estimate_height(code_only) <= available:
            tail_content = {
                **base,
                "code": "",
                "cell_hide_code": True,
                "cell_hide_output": False,
                "cell_continued": True,
                "caption": "",
            }
            return code_only, _clone(block, tail_content, continued=True)

    # Cut 2: between two lines of code.
    floor = MIN_CODE_LINES_PER_FRAGMENT
    if len(lines) < 2 * floor:
        return None
    for cut in range(len(lines) - 1, floor - 1, -1):
        if len(lines) - cut < 2 and not shows_output:
            continue  # would strand a single line on its own page
        head = fragment(0, cut, output=False, continued=False)
        if estimate_height(head) <= available:
            return head, fragment(cut, len(lines), output=shows_output, continued=True)
    return None


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------


def _is_visual_pair(current_block: Block, queue: list[Block]) -> list[Block] | None:
    """Two visuals placed directly back-to-back, nothing between them - e.g.
    a diagram-only template slot (technical_v1.json's `visual_explanation`)
    where the writer adds a small section_intro icon immediately followed by
    the section's real diagram image, with no paragraph allowed in between.
    A real, confirmed bug: when the second image didn't fit the remaining
    page space, it alone got bumped to a fresh page (images aren't
    splittable), stranding the first - often just a heading + a small icon -
    alone on an otherwise near-empty page. Returns `[current_block, next]`
    only when both are visuals with nothing between them; None otherwise -
    deliberately narrower than `_pending_visual_run`'s bridging lookahead
    below, which already handles the (more common) text-then-visual case."""
    if current_block.type not in VISUAL_BLOCK_TYPES or not queue:
        return None
    nxt = queue[0]
    return [current_block, nxt] if nxt.type in VISUAL_BLOCK_TYPES else None


def _pending_visual_run(current_block: Block, queue: list[Block]) -> list[Block] | None:
    """If a visual is coming up within a short, bridgeable run of non-visual
    blocks starting right after `current_block`, return
    `[current_block, ...bridged blocks..., visual]` - the whole group that
    needs to land on the same page together. None when `current_block` is
    itself a visual (nothing to "keep it with"), or no visual appears within
    `_VISUAL_LOOKAHEAD_LIMIT` blocks."""
    if current_block.type in VISUAL_BLOCK_TYPES:
        return None
    run = [current_block]
    for candidate in queue[:_VISUAL_LOOKAHEAD_LIMIT]:
        run.append(candidate)
        if candidate.type in VISUAL_BLOCK_TYPES:
            return run
    return None


def _make_ids_unique(pages: list[list[Block]]) -> None:
    """Give every block on every page its own id.

    Splitting names a tail `{id}_c1`. Re-flowing a document whose blocks were
    already split splits a fragment again and mints an id an existing fragment
    already has, so two blocks shared one id. The editor keys its blocks by id:
    with duplicates React left a ghost copy of a code fragment drawn over the
    following pages. The first holder keeps its id; later ones are chained with
    another `_c1` - the form the layout validator already recognises.
    """
    seen: set[str] = set()
    for page in pages:
        for block in page:
            while block.id in seen:
                block.id = f"{block.id}_c1"
            seen.add(block.id)


def flow_blocks(
    blocks: list[Block],
    *,
    geometry: PageGeometry | None = None,
    split_oversized_cells: bool = False,
) -> list[list[Block]]:
    """Position blocks and group them into pages. Mutates layout coordinates.

    `geometry` is the page box to flow into - an uploaded template can carry
    its own (see app.course.templates.docx_parser.style). Omitting it uses the
    built-in box, so every existing caller and both built-in templates
    paginate exactly as they always have.

    `split_oversized_cells` is for printing only. A stored document keeps every
    code cell whole (an editor edits one cell, not fragments of it); when the
    PDF is made, a cell taller than a page is split so nothing is drawn past
    the sheet - see split_code_cell and app.course.document.print.
    """
    box = geometry or PageGeometry()
    margin_top = box.margin_top
    margin_x = box.margin_x
    content_width = box.content_width
    pages: list[list[Block]] = []
    current: list[Block] = []
    y = margin_top
    bottom = margin_top + box.content_height

    def start_new_page() -> None:
        nonlocal current, y
        if current:
            pages.append(current)
        current = []
        y = margin_top

    queue = list(blocks)
    while queue:
        block = queue.pop(0)

        # A section_intro image and the paragraph it floats beside (see
        # SECTION_INTRO_IMAGE_WIDTH's own docstring) get ONE combined
        # reserved box instead of two separate ones - the image sits INSIDE
        # the paragraph's own vertical span via CSS float in the PDF export,
        # so it must never also add its own separate slot above/below it.
        # Scoped deliberately narrow (retry on a fresh page, or give up and
        # fall back to an ordinary standalone image; never attempt to split
        # the paragraph across a page boundary) - see this module's own
        # "estimates err high, min-height tolerates it" philosophy; a rare
        # edge case landing a line or two off is harmless, a broken pair
        # isn't.
        if (
            block.type is BlockType.IMAGE
            and (block.content.get("illustration_style") or "") == "section_intro"
            and queue
            and queue[0].type is BlockType.PARAGRAPH
        ):
            paragraph = queue[0]
            block.layout.x = margin_x
            block.layout.width = SECTION_INTRO_IMAGE_WIDTH
            image_h = image_box_height(block) + 2 * _padding(block)
            paragraph.layout.x = margin_x
            paragraph.layout.width = content_width
            combined_h = _section_intro_pair_height(block, paragraph, image_h)
            gap = (BLOCK_GAP if current else 0.0)

            if y + gap + combined_h <= bottom:
                block.layout.y = y + gap
                block.layout.height = round(image_h, 2)
                block.layout.z_index = 1  # paints over the paragraph in the (non-floating) editor canvas
                paragraph.layout.y = y + gap
                paragraph.layout.height = round(combined_h, 2)
                current.append(block)
                current.append(paragraph)
                y = paragraph.layout.y + combined_h
                queue.pop(0)  # consume the paragraph - it's part of this pair
                continue
            if current:
                start_new_page()
                queue.insert(0, block)  # retry the whole pair together, at the top of a fresh page
                continue
            # Doesn't fit even alone on a fresh page - give up on pairing and
            # fall through to the default handling below, which will lay
            # this image out as an ordinary full-width standalone picture
            # (block.layout.width gets overwritten to content_width just
            # below); the paragraph stays queued and is processed normally
            # on the next iteration.

        block.layout.x = margin_x
        # A section_intro picture with no paragraph to sit beside (its section
        # opens with a list, a callout, a quiz...) keeps its small size. It
        # used to fall through to full width, which blew a 4:3 thumbnail up to
        # a page-wide 665x465 picture.
        block.layout.width = SECTION_INTRO_IMAGE_WIDTH if _is_section_intro(block) else content_width
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

        # Never let two visuals placed directly back-to-back get split
        # across a page boundary (see _is_visual_pair) - deliberately NOT
        # gated behind _MIN_CONTENT_BEFORE_EARLY_BREAK like the lookahead
        # below: the bug this fixes is exactly a SMALL amount of preceding
        # content (a bare heading + a small icon) getting stranded alone,
        # so requiring "enough content already" first would silence the
        # protection in precisely the case that needs it.
        if current:
            visual_pair = _is_visual_pair(block, queue)
            if visual_pair is not None:
                pair_height = sum(estimate_height(b) for b in visual_pair) + BLOCK_GAP
                space_here = bottom - (y + gap)
                would_strand_pair = pair_height > space_here
                pair_fits_a_fresh_page = pair_height <= box.content_height
                if would_strand_pair and pair_fits_a_fresh_page:
                    start_new_page()
                    gap = 0.0

        # Never let a visual (image/table/code) get separated from the text
        # immediately before it - the writer places that text right next to
        # the visual specifically because it explains it (see WRITER_SYSTEM's
        # pacing rule), but plain sequential packing has no notion of that
        # relationship: it only checks whether *this* block fits, so a
        # paragraph (or two) can fill a page right up to the edge and leave
        # the very visual it was building up to stranded alone at the top of
        # the next page - a genuinely text-only page followed by a visual
        # with no lead-in, not the 60/40 mix the writer intended. Looking
        # ahead to find that pending visual (bridging over a short run of
        # non-visual blocks, not just a single one - see
        # _pending_visual_run) and breaking early keeps the whole group
        # together on the same page instead. Bounded so it can't cascade:
        # the lookahead only bridges a few blocks, and only fires once this
        # page already holds a reasonable amount of content
        # (_MIN_CONTENT_BEFORE_EARLY_BREAK) so it can't produce a string of
        # near-empty pages.
        if current and (y - margin_top) >= _MIN_CONTENT_BEFORE_EARLY_BREAK:
            run = _pending_visual_run(block, queue)
            if run is not None:
                run_height = sum(estimate_height(b) for b in run) + BLOCK_GAP * (len(run) - 1)
                space_here = bottom - (y + gap)
                would_strand_visual = run_height > space_here
                fits_a_fresh_page = run_height <= box.content_height
                if would_strand_visual and fits_a_fresh_page:
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
        if block.type is BlockType.CODE_CELL:
            pieces = (
                split_code_cell(block, available, box.content_height)
                if split_oversized_cells
                else None
            )
        else:
            pieces = split_block(block, available)
        if pieces is not None:
            head, tail = pieces
            head.layout.x = margin_x
            head.layout.width = content_width
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
    _make_ids_unique(pages)
    return pages


def page_visual_fraction(page: list[Block]) -> float:
    """The fraction of `page`'s own rendered height (each block's already-
    computed `layout.height`, plus the gaps between them) taken up by
    VISUAL_BLOCK_TYPES blocks - a real, measured 60/40 number computed from
    the exact same heights the layout engine itself placed, not a proxy like
    word count. Call only on a page `flow_blocks` has already laid out (every
    block needs `layout.height` set). Returns 0.0 for an empty page."""
    if not page:
        return 0.0
    visual_height = sum(b.layout.height for b in page if b.type in VISUAL_BLOCK_TYPES)
    total_height = sum(b.layout.height for b in page) + BLOCK_GAP * (len(page) - 1)
    return visual_height / total_height if total_height else 0.0
