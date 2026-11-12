"""Generation-time diagram compaction - app.course.document.diagram_compaction.
Closes a page's large trailing gap by re-rendering the flow_chart diagram
that got bridged off it (from its own already-stored spec, no AI call) more
compactly, then re-paginating."""

from __future__ import annotations

from app.course.document.diagram_compaction import compact_diagrams_for_spacing
from app.course.document.layout import CONTENT_HEIGHT, CONTENT_WIDTH, PAGE_MARGIN_TOP, image_box_height
from app.course.templates.registry import load_template
from app.render.diagram_renderer import render_diagram_svg
from app.schemas.blocks import BlockType
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec
from app.schemas.document import Block, BlockLayout, BlockMeta, CourseDocument, DocumentMeta, Page

_TECHNICAL_THEME = load_template("technical_v1").theme


def _flow_chart_spec(n: int = 7) -> DiagramSpec:
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(n)]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(n - 1)]
    return DiagramSpec(kind="flow_chart", title="A procedure", nodes=nodes, edges=edges)


def _small_block(y: float, height: float, *, chapter_id: str = "ch1") -> Block:
    return Block(
        type=BlockType.PARAGRAPH, content={"text": "A short paragraph."},
        layout=BlockLayout(x=64, y=y, width=CONTENT_WIDTH, height=height),
        meta=BlockMeta(chapter_id=chapter_id),
    )


def _diagram_block(*, spec: DiagramSpec, natural_w: int, natural_h: int, with_spec: bool = True) -> Block:
    content = {
        "kind": "diagram", "diagram_kind": "flow_chart", "path": "assets/d1.svg",
        "asset_id": "d1.svg", "width": natural_w, "height": natural_h,
    }
    if with_spec:
        content["spec"] = spec.model_dump(mode="json")
    return Block(
        type=BlockType.IMAGE, content=content,
        layout=BlockLayout(x=64, y=135, width=CONTENT_WIDTH, height=natural_h),
        meta=BlockMeta(chapter_id="ch1"),
    )


def _document(*, first_page_fill: float = 100.0, n_nodes: int = 4, with_spec: bool = True) -> CourseDocument:
    # The diagram is placed starting right under its own fresh page's
    # heading - comfortably within a full page (CONTENT_HEIGHT), which is
    # the thing that moved it off page 1 in the first place (flow_blocks's
    # own keep-with-lead-in-text rule - not re-exercised here, this module
    # picks up AFTER that decision already happened).
    spec = _flow_chart_spec(n_nodes)
    _, natural_w, natural_h = render_diagram_svg(spec, _TECHNICAL_THEME)
    thin_page = Page(
        id="p1", page_number=1, kind="content",
        blocks=[_small_block(PAGE_MARGIN_TOP, first_page_fill)],
    )
    heading = Block(
        type=BlockType.HEADING, content={"text": "A New Section", "level": 2},
        layout=BlockLayout(x=64, y=PAGE_MARGIN_TOP, width=CONTENT_WIDTH, height=45.0),
        meta=BlockMeta(chapter_id="ch1"),
    )
    diagram = _diagram_block(spec=spec, natural_w=natural_w, natural_h=natural_h, with_spec=with_spec)
    diagram.layout.y = PAGE_MARGIN_TOP + 45.0 + 18.0
    full_page = Page(id="p2", page_number=2, kind="content", blocks=[heading, diagram])
    assert diagram.layout.y + diagram.layout.height <= PAGE_MARGIN_TOP + CONTENT_HEIGHT, (
        "fixture itself invalid: the diagram must fit a full fresh page on its own"
    )
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[thin_page, full_page],
    )


async def test_a_large_trailing_gap_is_closed_by_compacting_the_next_pages_diagram(storage):
    template = load_template("technical_v1")
    # 6 nodes: the standard flowchart's own compact, single-column layout
    # (app.render.diagram_renderer._layout_standard_flowchart, now with no
    # Key table underneath it either) needs more steps than the old ring
    # design did before gap-only compaction has real room to help -
    # confirmed empirically against the current geometry, not guessed.
    document = _document(first_page_fill=100.0, n_nodes=6)

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert len(report.compacted) == 1
    assert report.compacted[0]["new_height"] < report.compacted[0]["old_height"]
    content_pages = [p for p in document.pages if p.kind == "content" and p.blocks]
    first_page = content_pages[0]
    last_bottom = max(b.layout.y + b.layout.height for b in first_page.blocks)
    blank_pct = (PAGE_MARGIN_TOP + CONTENT_HEIGHT - last_bottom) / CONTENT_HEIGHT * 100
    assert blank_pct < 100  # some real content now reaches further down the first page than before


async def test_a_page_already_within_tolerance_is_left_untouched(storage):
    template = load_template("technical_v1")
    document = _document()
    # Fill the first page almost completely - nothing to compact for.
    document.pages[0].blocks[0].layout.height = CONTENT_HEIGHT - 10.0

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert report.compacted == []


async def test_a_diagram_with_no_stored_spec_is_skipped_not_crashed(storage):
    template = load_template("technical_v1")
    document = _document(with_spec=False)
    original_width = document.pages[1].blocks[1].content.get("width")

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert report.compacted == []
    assert document.pages[1].blocks[1].content.get("width") == original_width  # untouched


async def test_the_documents_own_last_page_is_never_treated_as_needing_a_fix(storage):
    template = load_template("technical_v1")
    document = _document()
    document.pages = document.pages[:1]  # the thin page is now the only (= last) page

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert report.compacted == []


async def test_compaction_never_runs_for_a_diagram_that_already_fits(storage):
    """page 1 has a real gap (>15%, so the page itself is a candidate), and
    a small, 3-node diagram whose NATURAL (uncompacted) height already fits
    comfortably even in the resulting budget - nothing to compact for."""
    template = load_template("technical_v1")
    document = _document(first_page_fill=10.0, n_nodes=3)

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert report.compacted == []


# ---------------------------------------------------------------------------
# Lever 2 (last-resort display-width narrowing) - the actual fix for a real,
# confirmed root cause: `render_diagram_svg_fitting`'s `gap_scale` (lever 1,
# exercised by every test above) only ever changes anything for flow_chart/
# process/cycle/circuit_schematic (see that function's own docstring) - a
# concept_map or periodic_table diagram bridged away from a page used to get
# NO second chance at all, because `_find_compactable_diagram` didn't even
# recognise it as a candidate. These tests use concept_map specifically
# because `_layout_concept_wheel` has no gap_scale support whatsoever, so a
# pass here can only be explained by lever 2, never lever 1 - and assert on
# relative page-fill percentages / PASS-vs-FAIL QA outcomes, never exact
# pixel coordinates or wall-clock timing.
# ---------------------------------------------------------------------------


def _concept_map_spec(n_satellites: int = 3) -> DiagramSpec:
    nodes = [DiagramNode(id="focal", label="Central concept", level=0)]
    edges = []
    for i in range(n_satellites):
        nodes.append(DiagramNode(id=f"s{i}", label=f"Related idea {i}"))
        edges.append(DiagramEdge(source="focal", target=f"s{i}", label="relates to"))
    return DiagramSpec(kind="concept_map", title="A concept map", nodes=nodes, edges=edges)


def _concept_map_document(*, first_page_fill: float, n_satellites: int = 3) -> CourseDocument:
    spec = _concept_map_spec(n_satellites)
    _, natural_w, natural_h = render_diagram_svg(spec, _TECHNICAL_THEME)
    thin_page = Page(
        id="p1", page_number=1, kind="content",
        blocks=[_small_block(PAGE_MARGIN_TOP, first_page_fill)],
    )
    heading = Block(
        type=BlockType.HEADING, content={"text": "A New Section", "level": 2},
        layout=BlockLayout(x=64, y=PAGE_MARGIN_TOP, width=CONTENT_WIDTH, height=45.0),
        meta=BlockMeta(chapter_id="ch1"),
    )
    diagram = Block(
        type=BlockType.IMAGE,
        content={
            "kind": "diagram", "diagram_kind": "concept_map", "path": "assets/d1.svg",
            "asset_id": "d1.svg", "width": natural_w, "height": natural_h, "spec": spec.model_dump(mode="json"),
        },
        layout=BlockLayout(x=64, y=PAGE_MARGIN_TOP + 45.0 + 18.0, width=CONTENT_WIDTH, height=0.0),
        meta=BlockMeta(chapter_id="ch1"),
    )
    # The real DISPLAY box height (image_box_height, what flow_blocks itself
    # would compute) - NOT the raw SVG canvas height (natural_h, at
    # CANVAS_WIDTH=880): a concept_map's own focal/satellite circle geometry
    # makes its raw canvas height alone exceed CONTENT_HEIGHT even for a
    # single satellite, which would wrongly fail the "fits a full fresh
    # page" sanity check below despite the diagram fitting comfortably once
    # actually displayed at CONTENT_WIDTH=666.
    diagram.layout.height = round(image_box_height(diagram), 2)
    full_page = Page(id="p2", page_number=2, kind="content", blocks=[heading, diagram])
    assert diagram.layout.y + diagram.layout.height <= PAGE_MARGIN_TOP + CONTENT_HEIGHT, (
        "fixture itself invalid: the diagram must fit a full fresh page on its own"
    )
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[thin_page, full_page],
    )


async def test_a_concept_map_bridged_off_a_page_is_recovered_via_width_narrowing(storage):
    """A concept_map (gap_scale-ineligible) left just a little too tall for
    page 1's own remainder gets a real second chance now - proof it's lever
    2, not lever 1: the reported new_height exactly equals the diagram's own
    original, UNCOMPACTED height (lever 1 never touched it), yet the page's
    own trailing gap still closes."""
    template = load_template("technical_v1")
    document = _concept_map_document(first_page_fill=0.0, n_satellites=6)
    original_height = document.pages[1].blocks[1].content.get("height")

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert len(report.compacted) == 1
    compacted = report.compacted[0]
    assert compacted["new_height"] == original_height  # lever 1 is a no-op for concept_map
    assert compacted["compacted_display_width"] < CONTENT_WIDTH  # lever 2 is what actually fired
    content_pages = [p for p in document.pages if p.kind == "content" and p.blocks]
    first_page = content_pages[0]
    last_bottom = max(b.layout.y + b.layout.height for b in first_page.blocks)
    blank_pct = (PAGE_MARGIN_TOP + CONTENT_HEIGHT - last_bottom) / CONTENT_HEIGHT * 100
    assert blank_pct <= 15.0  # the page the diagram was bridged off now passes the QA gate


async def test_a_concept_map_whose_gap_is_too_large_for_either_lever_is_reported_not_silenced(storage):
    """When the gap genuinely can't be recovered without shrinking the
    diagram past a legible floor, compaction must say so - never silently
    drop the candidate the way the old flow_chart-only kind filter did."""
    template = load_template("technical_v1")
    document = _concept_map_document(first_page_fill=560.0, n_satellites=3)

    report = await compact_diagrams_for_spacing(document, template, storage=storage)

    assert report.compacted == []
    assert len(report.skipped) == 1
    assert "concept_map" in report.skipped[0]["reason"]
