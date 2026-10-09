"""Content-aware page splitting - the 10 scenarios from the page-splitting
brief. Covers both the underlying layout logic (flow_blocks / reflow_document
/ layout_validator) directly, and one end-to-end pass through the real
repair pipeline (DocumentService.repair_visual_coverage) for the "insert a
visual into an already-full existing page" scenario, since that's the
literal mechanism this codebase uses for it.
"""

from __future__ import annotations

from app.course.document.builder import _page_size_for, reflow_document
from app.course.document.layout import CONTENT_HEIGHT, flow_blocks, page_visual_fraction
from app.course.document.layout_validator import (
    content_integrity_report,
    original_id,
    overlapping_pairs,
    page_content_density,
    page_overflow,
    validate_document,
    validate_page,
)
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import (
    Block,
    BlockMeta,
    BlockStyle,
    CourseDocument,
    DocumentMeta,
    Page,
)

TEMPLATE = load_template("technical_v1")


def _heading(text: str) -> Block:
    return Block(type=BlockType.HEADING, content={"text": text, "level": 2})


def _paragraph(words: int, *, tag: str = "word") -> Block:
    text = " ".join([tag] * words) + "."
    return Block(type=BlockType.PARAGRAPH, content={"text": text}, style=BlockStyle(font_size=15))


def _image(kind: str = "diagram", *, width: int = 800, height: int = 500) -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": kind, "path": "assets/x", "width": width, "height": height})


def _table(rows: int = 6) -> Block:
    return Block(
        type=BlockType.TABLE,
        content={
            "columns": ["Term", "Definition"],
            "rows": [{"cells": [f"term_{i}", f"definition text for term {i}"]} for i in range(rows)],
        },
    )


def _code(lines: int = 40) -> Block:
    return Block(
        type=BlockType.CODE,
        content={"code": "\n".join(f"x_{i} = {i}" for i in range(lines)), "language": "python"},
        style=BlockStyle(font_size=12.5),
    )


def _summary(items: int = 5) -> Block:
    return Block(type=BlockType.SUMMARY, content={"key_takeaways": [f"takeaway {i}" for i in range(items)]})


def _document_from(blocks: list[Block]) -> CourseDocument:
    """A one-page seed document, ready for reflow_document to re-paginate."""
    return CourseDocument(
        document_id="doc_split_test",
        course_id="crs_split_test",
        course_title="Splitting Test Course",
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )


# ---------------------------------------------------------------------------
# Test 1: a text-heavy page needs to split to accommodate a large diagram
# ---------------------------------------------------------------------------


def test_text_heavy_page_splits_to_make_room_for_a_large_diagram():
    heading = _heading("Recursion")
    paras = [_paragraph(90) for _ in range(3)]
    diagram = _image("diagram", width=800, height=520)
    blocks = [heading, *paras, diagram]

    pages = flow_blocks(blocks)

    assert len(pages) > 1, "a full page of text plus a large diagram must split across pages"
    diagram_page = next(page for page in pages if any(b.type is BlockType.IMAGE for b in page))
    # the diagram must not be stranded alone - some real explanatory text
    # landed on the same page as it.
    assert any(b.type is BlockType.PARAGRAPH for b in diagram_page)
    # nothing was dropped or duplicated.
    all_ids = [b.id for page in pages for b in page]
    assert sorted(all_ids) == sorted(b.id for b in blocks)


# ---------------------------------------------------------------------------
# Test 2: text and visual already fit - no unnecessary split
# ---------------------------------------------------------------------------


def test_text_and_visual_that_already_fit_stay_on_one_page():
    blocks = [_heading("Overview"), _paragraph(40), _image("illustration", width=800, height=250)]
    pages = flow_blocks(blocks)
    assert len(pages) == 1


# ---------------------------------------------------------------------------
# Test 3: multiple visuals distributed without overlap
# ---------------------------------------------------------------------------


def test_multiple_visuals_are_distributed_without_overlap():
    blocks = [
        _heading("Pipelines"),
        _paragraph(80),
        _image("diagram", width=800, height=400),
        _paragraph(80),
        _image("diagram", width=800, height=400),
        _paragraph(80),
    ]
    pages = flow_blocks(blocks)

    assert len(pages) > 1
    for page in pages:
        assert overlapping_pairs(page) == []
    all_ids = [b.id for page in pages for b in page]
    assert sorted(all_ids) == sorted(b.id for b in blocks)
    image_count = sum(1 for page in pages for b in page if b.type is BlockType.IMAGE)
    assert image_count == 2


# ---------------------------------------------------------------------------
# Test 4: a long paragraph exceeds one page - split without losing/
# duplicating content
# ---------------------------------------------------------------------------


def test_a_very_long_paragraph_splits_without_losing_or_duplicating_words():
    huge = _paragraph(3000, tag="placeholder")
    pages = flow_blocks([huge])

    assert len(pages) > 1
    reconstructed = " ".join(block.content["text"] for page in pages for block in page)
    assert reconstructed.count("placeholder") == 3000
    # every fragment id traces back to the same original block.
    base_ids = {original_id(block.id) for page in pages for block in page}
    assert base_ids == {huge.id}


# ---------------------------------------------------------------------------
# Test 5: structured content (headings, tables, code, lists) keeps its
# structure and sequence through a split
# ---------------------------------------------------------------------------


def test_structured_content_preserves_tables_and_order_through_a_split():
    heading = _heading("Reference")
    intro = _paragraph(70)
    table = _table(rows=8)
    code = _code(lines=60)
    summary = _summary(items=6)
    blocks = [heading, intro, table, code, summary]

    pages = flow_blocks(blocks)
    all_blocks = [b for page in pages for b in page]

    # the table is never split - it appears exactly once, fully intact.
    table_occurrences = [b for b in all_blocks if b.type is BlockType.TABLE]
    assert len(table_occurrences) == 1
    assert len(table_occurrences[0].content["rows"]) == 8

    # relative order is preserved (accounting for a code block that may have
    # been split into head/tail fragments sharing its base id).
    base_id_sequence = [original_id(b.id) for b in all_blocks]
    original_sequence = [b.id for b in blocks]
    # de-duplicate consecutive repeats from a split block's own fragments.
    deduped = [bid for i, bid in enumerate(base_id_sequence) if i == 0 or bid != base_id_sequence[i - 1]]
    assert deduped == original_sequence


# ---------------------------------------------------------------------------
# Test 6: inserting a visual into an already-full EXISTING page triggers
# layout recalculation and splitting (the real repair pipeline)
# ---------------------------------------------------------------------------


async def test_repair_splits_a_nearly_full_existing_page_to_fit_its_new_visual(
    service, documents, technical_input
):
    record = await service.create_course(technical_input, run_planner=False)
    blocks = [
        Block(id="heading", type=BlockType.HEADING, content={"text": "Iterators", "level": 1}),
        Block(
            id="para1", type=BlockType.PARAGRAPH,
            content={"text": " ".join(["iterator protocol next stopiteration"] * 20)},
            meta=BlockMeta(chapter_id="ch1", chapter_number=1),
        ),
        Block(
            id="para2", type=BlockType.PARAGRAPH,
            content={"text": " ".join(["generators yield lazily produce values"] * 20)},
            meta=BlockMeta(chapter_id="ch1", chapter_number=1),
        ),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    await documents.save(record.document_id, document)

    response = await documents.repair_visual_coverage(record.document_id)

    assert response.still_deficient == []
    assert response.validation_findings == {}

    updated = await documents.load(record.document_id)
    content_pages = [p for p in updated.pages if p.kind == "content" and p.blocks]
    assert len(content_pages) >= 2, "the original page's text no longer fit once a visual joined it"

    original_ids = ["heading", "para1", "para2"]
    report = content_integrity_report(original_ids, updated)
    assert report == {"missing": [], "duplicated": []}
    for page in content_pages:
        assert page_overflow(page) <= 0


# ---------------------------------------------------------------------------
# Test 7: one split cascades correctly across several already-full pages
# ---------------------------------------------------------------------------


def test_redistribution_cascades_correctly_across_multiple_consecutive_pages():
    blocks: list[Block] = []
    for section in range(4):
        blocks.append(_heading(f"Section {section}"))
        for _ in range(3):
            blocks.append(_paragraph(90))
        blocks.append(_image("diagram", width=800, height=350))

    document = _document_from(blocks)
    reflow_document(document, TEMPLATE)

    content_pages = [p for p in document.pages if p.kind == "content" and p.blocks]
    assert len(content_pages) > 2, "four dense sections must span more than 2 pages"

    for page in content_pages:
        assert page_overflow(page) <= 0
        assert overlapping_pairs(page.blocks) == []

    original_ids = [b.id for b in blocks]
    report = content_integrity_report(original_ids, document)
    assert report == {"missing": [], "duplicated": []}
    assert validate_document(document, original_block_order=original_ids) == {}


# ---------------------------------------------------------------------------
# Test 8: a visual too large for even a fresh page gets a dedicated,
# correctly-sized page instead of being silently clipped
# ---------------------------------------------------------------------------


def test_an_oversized_visual_gets_a_page_grown_to_actually_contain_it():
    # A concept_experience block whose own renderer somehow reported a
    # height taller than a full content page (its normal `_MAX_HEIGHT` cap
    # leaves this at CONTENT_HEIGHT - 100 in practice, but nothing should
    # depend on that holding forever - see image_box_height's contract for
    # _UNCAPPED_IMAGE_KINDS).
    oversized = _image("concept_experience", width=666, height=int(CONTENT_HEIGHT) + 300)
    blocks = [_heading("A Big Diagram"), oversized]

    pages = flow_blocks(blocks)
    page = pages[-1]
    size = _page_size_for(page)

    # the page was grown to actually fit its content...
    assert page_overflow(Page(blocks=page, size=size)) <= 0
    # ...never shrunk, cropped, or dropped.
    assert any(b.type is BlockType.IMAGE and b.layout.height > CONTENT_HEIGHT for b in page)


# ---------------------------------------------------------------------------
# Test 10: content integrity - every original block present exactly once,
# in order, after a heavy multi-split reflow
# ---------------------------------------------------------------------------


def test_content_integrity_after_a_heavy_multi_split_reflow():
    blocks: list[Block] = [_heading("Deep Dive")]
    for _ in range(6):
        blocks.append(_paragraph(120))
    blocks.append(_table(rows=5))
    blocks.append(_code(lines=50))
    blocks.append(_image("diagram", width=800, height=400))
    for _ in range(4):
        blocks.append(_paragraph(100))

    document = _document_from(blocks)
    reflow_document(document, TEMPLATE)

    original_ids = [b.id for b in blocks]
    report = content_integrity_report(original_ids, document)
    assert report == {"missing": [], "duplicated": []}

    findings = validate_document(document, original_block_order=original_ids)
    assert findings == {}


# ---------------------------------------------------------------------------
# section_intro float pairing doesn't false-positive as an overlap
# ---------------------------------------------------------------------------


def _section_intro_image() -> Block:
    return Block(
        type=BlockType.IMAGE,
        content={"kind": "illustration", "illustration_style": "section_intro", "path": "assets/x", "width": 400, "height": 300},
    )


def test_a_section_intro_pair_is_not_flagged_as_an_overlap():
    """flow_blocks deliberately gives a section_intro image and its paired
    paragraph the same y (see layout.py) - overlapping_pairs must not
    mistake that intentional sharing for the real regression it otherwise
    exists to catch."""
    heading = _heading("Delegation")
    image, paragraph = _section_intro_image(), _paragraph(60)
    document = _document_from([heading, image, paragraph])
    reflow_document(document, TEMPLATE)

    page = document.pages[0]
    assert overlapping_pairs(page.blocks) == []
    assert validate_document(document) == {}


def test_an_unrelated_genuine_overlap_is_still_flagged():
    """The section_intro exception is narrow - two blocks sharing a y for
    any OTHER reason (a real regression, not the deliberate float pairing)
    must still be caught."""
    a = _paragraph(20)
    b = _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0  # genuinely overlaps a, no section_intro involved
    assert overlapping_pairs([a, b]) == [(a.id, b.id)]


# ---------------------------------------------------------------------------
# page content density: a visual stranded with no accompanying content
# ---------------------------------------------------------------------------


def test_page_content_density_flags_a_stranded_visual():
    """A heading + small section_intro icon alone on a page clears
    MIN_PAGE_CONTENT_HEIGHT's bare-fragment floor but still leaves most of
    the page empty - a real, confirmed bug chain (see
    app.course.document.layout._is_visual_pair's own docstring)."""
    heading = _heading("Visual Explanation")
    icon = _section_intro_image()
    pages = flow_blocks([heading, icon])
    assert len(pages) == 1
    page = Page(id="page_1", page_number=1, kind="content", blocks=pages[0])

    assert page_content_density(page) < 0.35
    findings = validate_page(page)
    assert any("available page height" in f for f in findings)


def test_page_content_density_does_not_flag_a_normal_short_text_only_page():
    """Regression guard: a plain short page of pure text (e.g. a document's
    own trailing paragraph, naturally shorter than a full page since the
    content just ran out) is completely normal and must never be flagged -
    a confirmed false positive while adding this check, caught against a
    real multi-page reflow scenario."""
    pages = flow_blocks([_paragraph(40)])
    page = Page(id="page_1", page_number=1, kind="content", blocks=pages[0])
    findings = validate_page(page)
    assert not any("available page height" in f for f in findings)
