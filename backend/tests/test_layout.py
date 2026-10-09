"""Layout engine: height estimation, pagination and block splitting."""

from __future__ import annotations

import pytest

from app.course.document.layout import (
    MAX_DIAGRAM_IMAGE_HEIGHT,
    MAX_IMAGE_HEIGHT,
    estimate_height,
    flow_blocks,
    image_box_height,
    page_visual_fraction,
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


def _image_block(kind: str, *, width: int | None = None, height: int | None = None) -> Block:
    content: dict = {"kind": kind, "path": "assets/x"}
    if width is not None:
        content["width"] = width
    if height is not None:
        content["height"] = height
    return Block(type=BlockType.IMAGE, content=content)


def test_illustration_image_height_stays_capped():
    """A decorative raster illustration never needs a box taller than
    MAX_IMAGE_HEIGHT - its content is a fixed aspect ratio, not structured
    content whose real height matters."""
    block = _image_block("illustration", width=800, height=4000)
    assert image_box_height(block) == MAX_IMAGE_HEIGHT


def test_diagram_image_height_gets_a_taller_cap_than_a_decorative_picture():
    """A real, previously-confirmed bug: a tall, many-node flow_chart SVG
    (e.g. 880x1904 for a 14-step pipeline) was capped at the same
    MAX_IMAGE_HEIGHT a small decorative photo uses. Since the box's width
    stays fixed at the content width, that flat height cap forced the
    rendered image down to a fraction of its real size (confirmed: ~199px
    wide out of 666px available) - unreadably small, exactly the "flowcharts
    are too small" complaint. A diagram is structured content, not a
    decorative aspect ratio, so it earns the taller, concept_experience-like
    ceiling instead (MAX_DIAGRAM_IMAGE_HEIGHT)."""
    block = _image_block("diagram", width=800, height=4000)
    assert image_box_height(block) == MAX_DIAGRAM_IMAGE_HEIGHT
    assert MAX_DIAGRAM_IMAGE_HEIGHT > MAX_IMAGE_HEIGHT


def test_concept_experience_image_height_is_not_capped():
    """A user-reported real bug: a concept_experience block's actual
    rendered height was capped the same way a picture's is, so the
    following block on the page was positioned too high and visually
    overlapped this one. Its intrinsic height (from
    concept_experience_renderer.estimate_pixel_size) must be respected in
    full, however tall the real content is."""
    block = _image_block("concept_experience", width=666, height=4000)
    assert image_box_height(block) > MAX_IMAGE_HEIGHT
    assert image_box_height(block) == pytest.approx(4000.0)


def test_concept_experience_without_intrinsic_size_falls_back_uncapped():
    """A block generated before this fix (no width/height yet) still uses
    the generic aspect ratio - but even that fallback must not be capped
    for this kind, since a future re-render could still exceed 430px."""
    block = _image_block("concept_experience")
    assert image_box_height(block) > 0


# ---------------------------------------------------------------------------
# keeping a visual with the text that leads into it (60/40 page balance)
# ---------------------------------------------------------------------------


def test_a_visual_is_never_stranded_alone_on_a_page_with_no_lead_in_text():
    """The reported gap: plain sequential packing only asks "does *this*
    block fit", so a paragraph can fill a page right up to the edge and
    leave the image it was building up to stranded alone at the top of the
    next page - a genuinely text-only page followed by an image with no
    lead-in. Calibrated so page 0 fills past the early-break floor
    (_MIN_FILL_BEFORE_EARLY_BREAK) before the last paragraph, and that last
    paragraph would fit on page 0 by itself but would leave less room than
    the image needs (confirmed against the un-patched behaviour while
    writing this test: without the fix, this exact scenario put the image
    alone on page 1 with the paragraph left behind on page 0)."""
    lead_in = _paragraph(100)  # the paragraph immediately explaining the image
    blocks = [_paragraph(110), lead_in, _image_block("illustration", width=800, height=300)]
    pages = flow_blocks(blocks)

    image_page = next(page for page in pages if page[-1].type is BlockType.IMAGE)
    assert lead_in in image_page, "the image's lead-in paragraph was left behind on the previous page"


def test_keep_together_rule_does_not_fire_on_a_nearly_empty_page():
    """Regression guard: the early-break rule must not trigger before the
    page holds a reasonable amount of content, or it would produce a string
    of near-empty pages instead of a rare, deliberate early break."""
    tiny_lead_in = _paragraph(15)
    blocks = [tiny_lead_in, _image_block("illustration", width=800, height=300)]
    pages = flow_blocks(blocks)
    assert len(pages) == 1
    assert pages[0][0] is tiny_lead_in
    assert pages[0][1].type is BlockType.IMAGE


def test_keep_together_rule_never_fires_when_the_visual_already_fits():
    """Regression guard: plenty of room on the page - no early break needed,
    natural packing already keeps them together."""
    blocks = [_paragraph(20), _image_block("illustration", width=800, height=300)]
    pages = flow_blocks(blocks)
    assert len(pages) == 1


def _heading(text: str, *, level: int = 2) -> Block:
    return Block(type=BlockType.HEADING, content={"text": text, "level": level})


def test_two_visuals_placed_back_to_back_move_together_not_split_across_pages():
    """Reproduces the reported bug exactly: a diagram-only template slot
    (technical_v1.json's visual_explanation section) pairs a small
    section_intro icon with the section's real diagram image, back-to-back,
    with no paragraph between them. Images aren't splittable, so if the
    second one alone didn't fit the remaining page space, it used to get
    bumped to a fresh page by itself, stranding the first - often just a
    heading + a tiny icon - behind on an otherwise near-empty page. The two
    images must always land on the same page as each other.

    Sized (confirmed empirically) so heading+icon+diagram genuinely don't
    all fit one page - without the fix, this exact scenario put the heading
    and icon on page 1 and the diagram alone on page 2."""
    heading = _heading("Visual Explanation")
    small_icon = _section_intro_image()
    large_diagram = _image_block("diagram", width=666, height=750)

    pages = flow_blocks([heading, small_icon, large_diagram])
    assert len(pages) > 1, "scenario didn't actually force a page split - test isn't exercising anything"

    icon_page = next(page for page in pages if small_icon in page)
    diagram_page = next(page for page in pages if large_diagram in page)
    assert icon_page is diagram_page, "the two back-to-back images were split across pages"


def test_page_visual_fraction_of_an_all_text_page_is_zero():
    pages = flow_blocks([_paragraph(30), _paragraph(30)])
    assert page_visual_fraction(pages[0]) == 0.0


def test_page_visual_fraction_reflects_real_rendered_heights():
    pages = flow_blocks([_paragraph(20), _image_block("illustration", width=800, height=300)])
    page = pages[0]
    text_block, image_block_ = page
    fraction = page_visual_fraction(page)
    expected = image_block_.layout.height / (
        text_block.layout.height + image_block_.layout.height + 18.0
    )
    assert fraction == pytest.approx(expected)
    assert 0.0 < fraction < 1.0


def test_page_visual_fraction_of_an_empty_page_is_zero():
    assert page_visual_fraction([]) == 0.0


# ---------------------------------------------------------------------------
# section_intro float pairing (small illustration + its paragraph)
# ---------------------------------------------------------------------------


def _section_intro_image(*, width: int = 400, height: int = 300) -> Block:
    return Block(
        type=BlockType.IMAGE,
        content={"kind": "illustration", "illustration_style": "section_intro", "path": "assets/x", "width": width, "height": height},
    )


def test_a_section_intro_image_and_its_paragraph_share_one_combined_box():
    from app.course.document.layout import SECTION_INTRO_IMAGE_WIDTH

    image, paragraph = _section_intro_image(), _paragraph(40)
    pages = flow_blocks([image, paragraph])
    assert len(pages) == 1
    assert pages[0] == [image, paragraph]
    # Narrow, not the full content width - it floats beside the text, never
    # spans the page the way an ordinary illustration would.
    assert image.layout.width == SECTION_INTRO_IMAGE_WIDTH
    assert image.layout.width < CONTENT_WIDTH
    # Same starting y - the PDF export puts both in one flowed container at
    # this shared position (see html_renderer.py's "section_intro_pair").
    assert image.layout.y == paragraph.layout.y
    # The image keeps its OWN small height (so the non-floating editor
    # canvas still shows a sane box) while the FULL combined reserved
    # height sits on the paragraph.
    assert image.layout.height < paragraph.layout.height
    assert image.layout.z_index == 1  # paints over the paragraph in the editor


def test_a_section_intro_pair_height_is_at_least_the_images_own_height():
    image, paragraph = _section_intro_image(), _paragraph(3)  # a very short paragraph
    flow_blocks([image, paragraph])
    assert paragraph.layout.height >= image.layout.height


def test_a_section_intro_pair_grows_taller_for_a_longer_paragraph():
    """Short enough that the paragraph fits entirely within the image's own
    height (no page split); long enough to overflow past it into the
    "wraps at full width below the image" branch of
    _section_intro_pair_height."""
    short_image, short_para = _section_intro_image(), _paragraph(10)
    long_image, long_para = _section_intro_image(), _paragraph(60)
    flow_blocks([short_image, short_para])
    flow_blocks([long_image, long_para])
    assert long_para.layout.height > short_para.layout.height


def test_a_section_intro_pair_moves_together_to_a_fresh_page_when_it_doesnt_fit():
    """Never split across the page boundary - the whole pair retries
    together at the top of a fresh page."""
    filler = _paragraph(200)  # fills most of a page by itself
    image, paragraph = _section_intro_image(), _paragraph(60)
    pages = flow_blocks([filler, image, paragraph])
    assert len(pages) == 2
    assert pages[0] == [filler]
    assert pages[1] == [image, paragraph]
    assert image.layout.y == PAGE_MARGIN_TOP
    assert image.layout.y == paragraph.layout.y


def test_an_ordinary_illustration_followed_by_a_paragraph_is_not_paired():
    """Only illustration_style == "section_intro" triggers pairing - the
    existing plain/decorative and textbook illustration behaviour (its own
    full-width block, stacked above the text) is completely unaffected."""
    image = _image_block("illustration", width=800, height=300)
    paragraph = _paragraph(20)
    pages = flow_blocks([image, paragraph])
    assert image.layout.width == CONTENT_WIDTH
    assert paragraph.layout.y > image.layout.y  # stacked, not sharing a y
    assert pages == [[image, paragraph]]


def test_a_section_intro_image_with_no_following_paragraph_stays_small():
    """No paragraph immediately after it (it is followed by a heading, a list,
    a callout...) - it is still laid out as the small thumbnail it was
    generated as, never silently dropped, and never blown up to full width."""
    from app.course.document.layout import SECTION_INTRO_IMAGE_WIDTH

    image = _section_intro_image()
    heading = _heading_block("Next section")
    flow_blocks([image, heading])
    assert image.layout.width == SECTION_INTRO_IMAGE_WIDTH
    assert heading.layout.width == CONTENT_WIDTH


def _heading_block(text: str) -> Block:
    return Block(type=BlockType.HEADING, content={"text": text, "level": 2})


def test_estimate_height_of_an_unplaced_section_intro_image_uses_its_narrow_width():
    """A real, confirmed bug: `estimate_height` on a section_intro image
    whose `layout.width` hasn't been narrowed yet by `flow_blocks` (i.e. a
    lookahead peek at an upcoming block, before it's actually placed) used
    to fall back to the full CONTENT_WIDTH - nearly 3x wider than the
    SECTION_INTRO_IMAGE_WIDTH it actually renders at - wildly overestimating
    its height. `_pending_visual_run`/`_is_visual_pair` (flow_blocks) rely on
    exactly this lookahead to decide whether an upcoming heading+icon pair
    fits the current page; the inflated estimate made them wrongly conclude
    it wouldn't, forcing an unnecessary early page break that stranded the
    heading alone on an otherwise near-empty fresh page. A freshly
    constructed Block's `layout.width` already defaults to CONTENT_WIDTH
    (see BlockLayout), so this is the exact unplaced state a lookahead
    actually sees."""
    from app.course.document.layout import SECTION_INTRO_IMAGE_WIDTH

    intro = _section_intro_image()
    assert intro.layout.width == CONTENT_WIDTH  # still unplaced/default

    unplaced_estimate = estimate_height(intro)

    placed = _section_intro_image()
    placed.layout.width = SECTION_INTRO_IMAGE_WIDTH
    placed_estimate = estimate_height(placed)

    assert unplaced_estimate == pytest.approx(placed_estimate)


def test_a_heading_and_its_icon_stay_with_preceding_content_when_they_genuinely_fit():
    """Reproduces the real bug end-to-end, tuned (empirically, against this
    page geometry) so the preceding paragraph leaves just enough remaining
    room for the true, narrow-width heading+icon height but NOT enough for
    the old inflated full-width estimate - confirmed by running this exact
    scenario against the pre-fix code, which splits it across 2 pages
    (heading+icon stranded alone on the second, near-empty one); this is
    the same shape of bug a real generated course hit (a "Common mistakes"
    heading+icon landing alone on a 20%-full page, see
    page_content_density's own docstring). With the fix, all three
    genuinely fit together in the space available and must land on one
    page."""
    filler = _paragraph(180)
    heading = _heading_block("Common mistakes - and the early signs")
    icon = _section_intro_image()

    pages = flow_blocks([filler, heading, icon])

    assert len(pages) == 1, "the heading+icon pair was unnecessarily bumped to a fresh page"
    assert pages[0] == [filler, heading, icon]


# --- failed images and section_intro layout -----------------------------------


def _image(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": "illustration", **content})


def test_a_failed_image_reserves_only_a_strip():
    failed = estimate_height(_image(error="Azure 429", generated=False))
    pending = estimate_height(_image())
    done = estimate_height(_image(path="a.png", width=1024, height=1024))
    assert failed < 80 < pending < done


def test_a_failed_image_does_not_leave_a_blank_box_between_blocks():
    pages = flow_blocks([_paragraph(10), _image(error="x"), _paragraph(10)])
    blocks = pages[0]
    assert blocks[2].layout.y - (blocks[1].layout.y + blocks[1].layout.height) < 30


def test_a_section_intro_picture_with_no_paragraph_beside_it_stays_small():
    intro = _image(path="a.png", width=400, height=300, illustration_style="section_intro")
    callout = Block(type=BlockType.TIP, content={"text": "A tip."})
    placed = flow_blocks([intro, callout])[0]
    assert placed[0].layout.width == 210.0
    assert placed[0].layout.height < 200
    assert placed[1].layout.width == CONTENT_WIDTH


def test_a_section_intro_picture_beside_a_paragraph_is_still_paired():
    intro = _image(path="a.png", width=400, height=300, illustration_style="section_intro")
    placed = flow_blocks([intro, _paragraph(40)])[0]
    assert placed[0].layout.width == 210.0
    assert placed[0].layout.y == placed[1].layout.y
