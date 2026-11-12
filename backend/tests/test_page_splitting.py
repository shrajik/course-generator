"""Content-aware page splitting - the 10 scenarios from the page-splitting
brief. Covers both the underlying layout logic (flow_blocks / reflow_document
/ layout_validator) directly, and one end-to-end pass through the real
repair pipeline (DocumentService.repair_visual_coverage) for the "insert a
visual into an already-full existing page" scenario, since that's the
literal mechanism this codebase uses for it.
"""

from __future__ import annotations

from app.course.document.builder import _page_size_for, reflow_document
from app.course.document.layout import CONTENT_HEIGHT, CONTENT_WIDTH, NEAR_FIT_TOLERANCE, flow_blocks, page_visual_fraction
from app.course.document.layout_validator import (
    DOCUMENT_LEVEL,
    content_integrity_report,
    original_id,
    overlapping_pairs,
    page_content_density,
    page_overflow,
    validate_document,
    validate_document_structured,
    validate_page,
    validate_page_structured,
)
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import (
    Block,
    BlockLayout,
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
    # Tall enough that even a (deliberately bounded - see
    # shrink_image_width_to_fit's own _MIN_DIAGRAM_SHRINK_WIDTH_FRACTION)
    # narrowing of the diagram still doesn't make it fit the page's leftover
    # space - genuinely forces a split, not just a case the shrink fallback
    # now quietly absorbs.
    diagram = _image("diagram", width=800, height=700)
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
    # Needs >= MIN_WORDS_TO_ILLUSTRATE (60, app.course.document.visual_repair)
    # total words or the page never even qualifies as deficient in the
    # first place - a real trap confirmed while working on this test: a
    # page audit_and_repair_visuals finds nothing to repair for never gets
    # its own initial reflow (that function only reflows AFTER an
    # insertion), so an under-threshold fixture built with raw, un-flowed
    # BlockLayout() defaults spuriously "fails" layout validation for
    # blocks that were simply never positioned at all - a fixture bug, not
    # a real one. 20 repeats (160 words) clears that threshold safely.
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

    # A real repair must have been attempted and actually inserted
    # something - that's the invariant this test is really about. Whether
    # the repaired visual ends up fitting on page 1 itself, or needs its
    # own fresh page, isn't: a textbook-style circular diagram (see
    # app.render.diagram_renderer._layout_ring_process) has a much larger
    # minimum footprint than the old box-stack rendering did (even a
    # 2-node ring + its Key table needs ~800px, most of a page), so a page
    # already carrying this much real text can legitimately still show as
    # "still_deficient" afterward - the checks below (no missing/
    # duplicated content, no overlap, no page overflow) are what actually
    # prove the repair placed everything correctly regardless of where it
    # landed.
    assert response.repaired
    assert response.validation_findings == {}

    updated = await documents.load(record.document_id)
    content_pages = [p for p in updated.pages if p.kind == "content" and p.blocks]
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
# PDF/export fidelity: a page flow_blocks already considers a safe near-fit
# (see layout.NEAR_FIT_TOLERANCE) must never have its OWN declared size grown
# past the real print sheet height. A real, confirmed bug: `_page_size_for`
# used to require clearing the FULL margin reserve a second time before
# calling a page done, disagreeing with flow_blocks's own, already-applied
# tolerance - so a page flow_blocks placed perfectly safely still grew a few
# px past the fixed sheet size. A `.page` div taller than the print sheet
# makes Chromium's print-to-PDF engine insert its own, uncontrolled page
# break when exporting - confirmed by rendering a real export to page images
# and finding a genuinely blank extra physical page where this fired.
# ---------------------------------------------------------------------------


def test_a_near_fit_tolerance_page_is_never_grown_past_the_real_print_sheet():
    from app.schemas.template import PageGeometry

    geometry = PageGeometry()
    nominal_bottom = geometry.height - geometry.margin_bottom

    # A block whose own bottom edge lands just inside NEAR_FIT_TOLERANCE of
    # the nominal content boundary - exactly the shape flow_blocks itself
    # already considers a safe fit (see NEAR_FIT_TOLERANCE's own docstring),
    # not a genuinely oversized block.
    from app.course.document.layout import NEAR_FIT_TOLERANCE

    within_tolerance = Block(
        type=BlockType.PARAGRAPH, content={"text": "x"},
        layout=BlockLayout(
            x=geometry.margin_x, y=geometry.margin_top,
            width=geometry.content_width, height=(nominal_bottom - geometry.margin_top) + NEAR_FIT_TOLERANCE - 2.0,
        ),
    )

    size = _page_size_for([within_tolerance], geometry)

    assert size.height == geometry.height, (
        "a block flow_blocks already considers a safe near-fit must not grow the page's own "
        "declared size past the real, fixed print sheet height"
    )


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


# ---------------------------------------------------------------------------
# Structured layout findings (quality-governance follow-up, Phase 3.3/3.4) -
# validate_page_structured/validate_document_structured are the real
# detection logic; validate_page/validate_document are now thin derivations
# of them. These tests protect: (a) each category gets the right stable
# reason code, (b) the legacy string APIs stay byte-identical to the
# structured source, (c) the public dict[int, list[str]]/list[str] shapes
# and the original_block_order contract are unchanged.
# ---------------------------------------------------------------------------


def test_validate_page_structured_classifies_a_genuine_overlap():
    a = _paragraph(20)
    b = _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0  # genuinely overlaps a, no section_intro involved
    page = Page(id="page_1", page_number=1, kind="content", blocks=[a, b])

    structured = validate_page_structured(page)
    assert len(structured) == 1
    assert structured[0].reason == "overlap"
    assert structured[0].page_number == 1
    assert f"'{a.id}' and '{b.id}'" in structured[0].message


def test_validate_page_structured_classifies_a_page_overflow():
    block = _paragraph(5)
    block.layout.y = 2000.0  # far past any page's usable height
    block.layout.height = 500.0
    page = Page(id="page_1", page_number=1, kind="content", blocks=[block])

    structured = validate_page_structured(page)
    assert len(structured) == 1
    assert structured[0].reason == "page_overflow"
    assert structured[0].page_number == 1
    assert "overflows page 1" in structured[0].message


def test_validate_page_structured_classifies_an_orphaned_fragment():
    block = _paragraph(1)
    block.layout.height = 10.0  # 0 < 10 < MIN_PAGE_CONTENT_HEIGHT (40.0)
    page = Page(id="page_1", page_number=1, kind="content", blocks=[block])

    structured = validate_page_structured(page)
    assert len(structured) == 1
    assert structured[0].reason == "orphaned_fragment"
    assert "orphaned fragment" in structured[0].message


def test_validate_page_structured_classifies_a_stranded_visual():
    """Same fixture as test_page_content_density_flags_a_stranded_visual
    above - proves the structured and legacy APIs agree on this category,
    not just on the overlap/overflow ones."""
    heading = _heading("Visual Explanation")
    icon = _section_intro_image()
    pages = flow_blocks([heading, icon])
    page = Page(id="page_1", page_number=1, kind="content", blocks=pages[0])

    structured = validate_page_structured(page)
    assert len(structured) == 1
    assert structured[0].reason == "stranded_visual_density"
    assert "available page height" in structured[0].message


def test_validate_document_structured_classifies_content_reordering():
    first = _paragraph(10)
    second = _paragraph(10)
    document = _document_from([first, second])
    reflow_document(document, TEMPLATE)
    shuffled_order = [second.id, first.id]  # deliberately swapped vs rendered order

    structured = validate_document_structured(document, original_block_order=shuffled_order)
    assert len(structured) == 1
    assert structured[0].reason == "content_reordering"
    assert structured[0].page_number == DOCUMENT_LEVEL
    assert structured[0].message == (
        "original content was reordered relative to its pre-layout sequence"
    )
    # The legacy dict view keys this same finding at DOCUMENT_LEVEL, exactly
    # as before validate_document_structured existed.
    legacy = validate_document(document, original_block_order=shuffled_order)
    assert legacy == {DOCUMENT_LEVEL: [structured[0].message]}


def test_validate_page_message_parity_with_the_legacy_string_api():
    """The legacy validate_page() must return exactly the structured
    findings' messages, in the same order - a derivation, never a second
    implementation that could drift."""
    a = _paragraph(20)
    b = _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0
    page = Page(id="page_1", page_number=1, kind="content", blocks=[a, b])

    assert validate_page(page) == [f.message for f in validate_page_structured(page)]


def test_validate_document_message_parity_with_the_legacy_dict_api():
    a = _paragraph(20)
    b = _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0
    document = _document_from([a, b])

    legacy = validate_document(document)
    structured = validate_document_structured(document)
    rebuilt: dict[int, list[str]] = {}
    for finding in structured:
        rebuilt.setdefault(finding.page_number, []).append(finding.message)
    assert legacy == rebuilt


def test_validate_page_and_validate_document_return_shapes_are_unchanged():
    """Explicit type/shape regression guard, independent of content -
    protects every pre-existing caller (builder._log_layout_findings,
    visual_repair.audit_and_repair_visuals -> the public
    RepairVisualsResponse.validation_findings field) from a shape change."""
    clean_page = Page(id="page_1", page_number=1, kind="content", blocks=[_paragraph(10)])
    clean_findings = validate_page(clean_page)
    assert isinstance(clean_findings, list)
    assert clean_findings == []

    a, b = _paragraph(20), _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0
    dirty_page = Page(id="page_1", page_number=1, kind="content", blocks=[a, b])
    dirty_findings = validate_page(dirty_page)
    assert isinstance(dirty_findings, list)
    assert all(isinstance(item, str) for item in dirty_findings)

    document = _document_from([a, b])
    problems = validate_document(document)
    assert isinstance(problems, dict)
    assert all(isinstance(k, int) for k in problems)
    assert all(isinstance(v, list) and all(isinstance(s, str) for s in v) for v in problems.values())


# ---------------------------------------------------------------------------
# PDF/export fidelity follow-up: layout_validator.validate_page_structured
# and print._needs_repagination both used to measure a page's own trailing
# overflow against the strict, math-correctness-only _TOLERANCE (2px) -
# wrong once NEAR_FIT_TOLERANCE (20px) became a real, deliberate placement
# allowance flow_blocks itself relies on (see that constant's own docstring).
# These three tests prove the reconciliation is exactly scoped: a near-fit
# page is accepted as safe by BOTH callers, a genuinely too-large overflow is
# still flagged by both, and a REAL block-to-block overlap is still caught
# by the untouched, still-strict 2px check - nothing here was weakened.
# ---------------------------------------------------------------------------


def _single_trailing_block_page(overflow_px: float) -> Page:
    """One page whose only block's bottom edge lands `overflow_px` past the
    nominal content boundary (PAGE_MARGIN_TOP + CONTENT_HEIGHT) - the exact
    shape flow_blocks itself accepts up to NEAR_FIT_TOLERANCE, and the exact
    shape `test_per_image_save_during_generation_never_persists_overlapping_
    blocks` (test_course_service.py) hit for real."""
    from app.schemas.document import PAGE_MARGIN_TOP, PageSize

    nominal_bottom = PAGE_MARGIN_TOP + CONTENT_HEIGHT
    block = Block(
        type=BlockType.PARAGRAPH, content={"text": "x"},
        layout=BlockLayout(x=64, y=PAGE_MARGIN_TOP, width=CONTENT_WIDTH, height=(nominal_bottom - PAGE_MARGIN_TOP) + overflow_px),
    )
    return Page(id="page_1", page_number=1, kind="content", size=PageSize(), blocks=[block])


def test_an_overflow_within_near_fit_tolerance_is_accepted_as_safe_everywhere():
    from app.course.document.print import _needs_repagination
    from app.schemas.template import PageGeometry

    page = _single_trailing_block_page(NEAR_FIT_TOLERANCE - 2.0)

    assert page_overflow(page) <= NEAR_FIT_TOLERANCE
    findings = validate_page_structured(page)
    assert not any(f.reason == "page_overflow" for f in findings), (
        "a block within NEAR_FIT_TOLERANCE must not be reported as a structural finding"
    )
    assert _needs_repagination(page, PageGeometry()) is False, (
        "a block within NEAR_FIT_TOLERANCE must not trigger a (wasted, no-op) print-time repagination"
    )


def test_an_overflow_beyond_near_fit_tolerance_is_still_flagged_everywhere():
    from app.course.document.print import _needs_repagination
    from app.schemas.template import PageGeometry

    page = _single_trailing_block_page(NEAR_FIT_TOLERANCE + 15.0)

    assert page_overflow(page) > NEAR_FIT_TOLERANCE
    findings = validate_page_structured(page)
    assert any(f.reason == "page_overflow" for f in findings), (
        "a block genuinely past NEAR_FIT_TOLERANCE must still be reported as a structural finding"
    )
    assert _needs_repagination(page, PageGeometry()) is True, (
        "a block genuinely past NEAR_FIT_TOLERANCE must still trigger print-time repagination"
    )


def test_a_genuine_block_to_block_overlap_is_still_caught_by_the_strict_tolerance():
    """Proves the reconciliation above did NOT touch overlapping_pairs's own
    strict 2px check: two sibling blocks overlapping by 10px - well inside
    NEAR_FIT_TOLERANCE's 20px, but 5x the strict 2px _TOLERANCE - must still
    be flagged. If this ever started passing silently, the strict check
    would have been accidentally weakened by the near-fit reconciliation."""
    a = _paragraph(20)
    a.layout.y = 100.0
    a.layout.height = 60.0
    b = _paragraph(20)
    b.layout.y = 150.0  # starts 10px before `a` actually ends (100+60=160)

    pairs = overlapping_pairs([a, b])
    assert pairs == [(a.id, b.id)], "a genuine, well-within-NEAR_FIT_TOLERANCE block overlap must still be caught"

    page = Page(id="page_1", page_number=1, kind="content", blocks=[a, b])
    findings = validate_page_structured(page)
    assert any(f.reason == "overlap" for f in findings), (
        "validate_page_structured must still surface a genuine block-to-block overlap"
    )
