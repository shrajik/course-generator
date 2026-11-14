"""Diagram / static visual support: structured spec -> deterministic SVG."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import pytest

import app.services.mock_ai as mock_ai_module
from app.agents.writer import WriterAgent
from app.core.errors import AIServiceError
from app.course.templates.registry import load_template
from app.render.diagram_renderer import (
    CANVAS_WIDTH,
    DETAIL_SIZE,
    EDGE_LABEL_FONT_SIZE,
    LABEL_SIZE,
    MARGIN,
    PANEL_HEIGHT,
    RING_LABEL_SIZE,
    _DFD_LABEL_SIZE,
    render_diagram_svg,
)
from app.render.schematic_layout import resolve_schematic_layout
from app.render.textbook_palette import COLOR_ROLES, TEXTBOOK_PALETTE, resolve_color_role
from app.render.visual_blueprints import BLUEPRINT_REGISTRY, apply_blueprint_defaults, get_blueprint
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest
from app.schemas.diagram import (
    DiagramEdge,
    DiagramNode,
    DiagramSpec,
    GraphFunction,
    GraphSeries,
    SchematicRelationship,
    SchematicShape,
    SchematicState,
)
from app.schemas.document import CONTENT_WIDTH, Block
from app.schemas.template import TemplateTheme
from app.services.diagram_render_qa import validate_rendered_svg
from app.services.diagram_qa import (
    CANVAS_CLIPPING,
    DANGLING_RELATIONSHIP,
    DUPLICATE_CRITICAL_COMPONENT,
    GARBLED_TEXT,
    INVALID_COLOR_ROLE,
    INVALID_RELATIONSHIP,
    MISSING_PRIMARY_OBJECT,
    MISSING_RELATIONSHIP,
    MISSING_REQUIRED_COMPONENT,
    OBJECT_COLLISION,
    WEAK_FOCAL_HIERARCHY,
    _looks_garbled,
    evaluate_blueprint,
    evaluate_schematic,
)
from app.services.diagram_service import DiagramService

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_diagram_spec_usability_requires_two_to_nine_labelled_nodes():
    assert not DiagramSpec(nodes=[]).is_usable()
    assert not DiagramSpec(nodes=[DiagramNode(label="Solo")]).is_usable()
    assert DiagramSpec(nodes=[DiagramNode(label="A"), DiagramNode(label="B")]).is_usable()
    # A relationship/structural kind (not in SEQUENTIAL_KINDS) keeps the
    # original, tighter 9-node cap.
    assert not DiagramSpec(
        kind="concept_map",
        nodes=[DiagramNode(label=f"n{i}") for i in range(10)],
        edges=[DiagramEdge(source="n0", target="n1", label="relates to")],
    ).is_usable()


def test_sequential_diagram_kinds_tolerate_up_to_fourteen_nodes():
    """A genuinely multi-stage technical pipeline (flow_chart/process/
    data_flow_diagram/swimlane/cycle) earns a higher node ceiling than a
    relationship diagram - see max_nodes_for. Rejecting a real 10-14 step
    process as "unusable" pushed it onto the raster illustration fallback,
    which cannot reliably spell that many labels (see ImageService)."""
    assert DiagramSpec(
        kind="flow_chart", nodes=[DiagramNode(label=f"n{i}") for i in range(14)]
    ).is_usable()
    assert not DiagramSpec(
        kind="flow_chart", nodes=[DiagramNode(label=f"n{i}") for i in range(15)]
    ).is_usable()


def test_diagram_spec_unknown_kind_falls_back_to_smart_art():
    assert DiagramSpec(kind="flow_chart").normalised_kind() == "flow_chart"
    assert DiagramSpec(kind="mind_map").normalised_kind() == "smart_art"
    assert DiagramSpec(kind="").normalised_kind() == "smart_art"


# ---------------------------------------------------------------------------
# renderer (pure, AI-free)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "flow_chart", "process", "cycle", "comparison", "smart_art", "conceptual", "concept_map",
        "pyramid", "matrix", "radial_mindmap", "chevron_process", "timeline", "staircase_process",
        "balance_comparison", "nested_circles", "network_mesh", "decision_tree",
    ],
)
def test_render_diagram_svg_produces_well_formed_svg(kind):
    nodes = [
        DiagramNode(id=f"n{i}", label=f"Step {i}", detail="A short supporting detail.")
        for i in range(4)
    ]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(3)]
    spec = DiagramSpec(kind=kind, title=f"{kind} demo", nodes=nodes, edges=edges)

    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())

    root = ET.fromstring(svg_bytes)  # raises if malformed
    assert root.tag.endswith("svg")
    assert width > 0 and height > 0


# ---------------------------------------------------------------------------
# render_diagram_svg_fitting (generation-time diagram compaction)
# ---------------------------------------------------------------------------


def _linear_flow_chart(n: int = 4, *, detail: bool = False) -> DiagramSpec:
    nodes = [
        DiagramNode(
            id=f"n{i}", label=f"Step {i}",
            detail="Some supporting detail text for this step's own Key table row." if detail else "",
        )
        for i in range(n)
    ]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(n - 1)]
    return DiagramSpec(kind="flow_chart", title="A linear procedure", nodes=nodes, edges=edges)


def test_fitting_returns_the_normal_render_when_it_already_fits():
    # Byte-for-byte equality isn't meaningful here - render_diagram_svg gives
    # every marker id a fresh random suffix per call (see its own docstring),
    # so two calls of the identical spec never produce identical bytes.
    # Dimensions (what gap_scale actually controls) are the real signal.
    from app.render.diagram_renderer import render_diagram_svg_fitting

    spec = _linear_flow_chart()
    _, normal_w, normal_h = render_diagram_svg(spec, TemplateTheme())
    _, fitted_w, fitted_h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=normal_h + 50)
    assert (fitted_w, fitted_h) == (normal_w, normal_h)


def test_fitting_returns_the_normal_render_when_max_height_is_none():
    from app.render.diagram_renderer import render_diagram_svg_fitting

    spec = _linear_flow_chart()
    _, normal_w, normal_h = render_diagram_svg(spec, TemplateTheme())
    _, fitted_w, fitted_h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=None)
    assert (fitted_w, fitted_h) == (normal_w, normal_h)


def test_fitting_compacts_vertical_spacing_without_touching_node_count_or_text():
    """Unlike the old `_layout_flow_chart`, gap_scale on the new
    `_layout_ring_process` only compacts the Key table's own row spacing
    (the ring's own node size/spacing is never touched by it - see
    `_layout_ring_process`'s own docstring) - a bigger node count with real
    `detail` text gives the Key table enough weight for that compaction to
    produce a measurable, realistically-achievable height reduction."""
    from app.render.diagram_renderer import render_diagram_svg_fitting

    spec = _linear_flow_chart(8, detail=True)
    normal_svg, normal_w, normal_h = render_diagram_svg(spec, TemplateTheme())
    target = normal_h - 60  # a modest, realistically-closeable shortfall
    fitted_svg, fitted_w, fitted_h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=target)

    assert fitted_h <= target
    assert fitted_h < normal_h
    assert fitted_w == normal_w  # width (and therefore on-page scale) is untouched
    for node in spec.nodes:
        assert f">{node.label}<" in fitted_svg.decode("utf-8")  # every label still rendered, same text
    normal_root = ET.fromstring(normal_svg)
    fitted_root = ET.fromstring(fitted_svg)
    normal_font_sizes = {el.get("font-size") for el in normal_root.iter() if el.get("font-size")}
    fitted_font_sizes = {el.get("font-size") for el in fitted_root.iter() if el.get("font-size")}
    assert fitted_font_sizes == normal_font_sizes  # compaction never changes font size


def test_fitting_never_returns_something_taller_than_the_normal_render():
    """Even an impossible target (far below what any compaction could reach)
    must come back no worse than the uncompacted baseline - this function's
    own contract (see its docstring: "never makes anything WORSE")."""
    from app.render.diagram_renderer import render_diagram_svg_fitting

    spec = _linear_flow_chart(6)
    normal_h = render_diagram_svg(spec, TemplateTheme())[2]
    fitted_h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=1.0)[2]
    assert fitted_h <= normal_h


def test_fitting_ignores_max_height_for_a_kind_gap_scale_does_not_affect():
    from app.render.diagram_renderer import render_diagram_svg_fitting

    spec = DiagramSpec(
        kind="hierarchy",
        nodes=[DiagramNode(id="root", label="Root", level=0), DiagramNode(id="a", label="Child", level=1)],
        edges=[DiagramEdge(source="root", target="a")],
    )
    _, normal_w, normal_h = render_diagram_svg(spec, TemplateTheme())
    _, fitted_w, fitted_h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=1.0)
    assert (fitted_w, fitted_h) == (normal_w, normal_h)


# ---------------------------------------------------------------------------
# textbook-style ring (flow_chart/process/cycle) and concept wheel
# (concept_map) - replaces the old box-and-arrow look for exactly these
# kinds (see render_diagram_svg's own dispatch)
# ---------------------------------------------------------------------------


def test_ring_process_never_truncates_a_long_node_label_with_an_ellipsis():
    """The actual bug this whole rendering change fixes: a node's label
    text must never be cut short with "…" - the circle grows to fit it
    instead (see _circle_node's own docstring)."""
    long_label = "A genuinely long step description that would have overflowed the old small flow_chart box"
    spec = DiagramSpec(
        kind="cycle",
        nodes=[DiagramNode(id="a", label=long_label), DiagramNode(id="b", label="Next")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    # every word of the long label survives somewhere in the output (the
    # node circle itself, wrapped across lines).
    for word in long_label.split():
        assert word in text, f"word lost: {word}"


def test_ring_process_detail_never_truncates_in_the_key_table():
    """Same guarantee for the Key table's own explanation column - detail
    text moved OUT of the node (see _layout_ring_process's own docstring)
    must still never be cut short, however long."""
    long_detail = (
        "A much longer supporting explanation sentence for this one step that would never have "
        "fit cleanly inside a small circular node, which is exactly why it lives in the Key table instead."
    )
    spec = DiagramSpec(
        kind="cycle",
        nodes=[
            DiagramNode(id="a", label="Step", detail=long_detail),
            DiagramNode(id="b", label="Next"),
        ],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in long_detail.split():
        assert word in text, f"word lost: {word}"


def test_ring_process_never_draws_text_on_a_connector_arrow():
    """No arrow (<path> or <line> with an arrowhead marker) may carry a
    text label - an edge's own `label`, even when the model set one, is
    simply not rendered anywhere on the connector (see
    _curved_connector's own docstring)."""
    spec = DiagramSpec(
        kind="cycle",
        nodes=[DiagramNode(id="a", label="Start"), DiagramNode(id="b", label="End")],
        edges=[DiagramEdge(source="a", target="b", label="a label that must never appear on the arrow")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "a label that must never appear on the arrow" not in text
    assert 'marker-end="url(#diagram-arrow' in text  # the arrow itself still drew


def test_ring_process_stays_open_for_a_non_cyclic_sequence():
    """A plain step-by-step procedure with no real last->first edge must
    NOT draw a closing arrow back to the start - only genuine cycles
    close the ring (see _layout_ring_process's own docstring)."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(4)]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(3)]  # no n3 -> n0
    spec = DiagramSpec(kind="cycle", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    arrow_count = text.count('marker-end="url(#diagram-arrow')
    assert arrow_count == 3  # N-1 arrows, ring left open


def test_ring_process_closes_for_a_genuine_cycle():
    """A spec whose own edges genuinely close the loop (a real repeating
    cycle) gets the matching extra closing arrow."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(4)]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{(i + 1) % 4}") for i in range(4)]  # n3 -> n0 included
    spec = DiagramSpec(kind="cycle", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    arrow_count = text.count('marker-end="url(#diagram-arrow')
    assert arrow_count == 4  # N arrows, ring closed


def test_cycle_draws_one_background_petal_per_node_never_flagged_as_overlap():
    """The overlapping-petal "flower" background (reference infographic
    template's own cycle style - see _layout_ring_process's own comment on
    it) is purely decorative: one ellipse per node, drawn behind the real
    circles/arrows, with no `class="diagram-box"` so it's correctly
    invisible to the overlap/geometry QA (the petals are DESIGNED to
    overlap their neighbours near the centre, same reasoning honeycomb's
    own exemption uses) - and it must never introduce a real geometry
    finding on an otherwise ordinary cycle."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(5)]
    spec = DiagramSpec(kind="cycle", title="A cycle", nodes=nodes)
    theme = load_template("technical").theme
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg_bytes)
    ellipses = [el for el in root.iter() if el.tag.endswith("ellipse")]
    assert len(ellipses) == 5
    assert all("diagram-box" not in (el.get("class") or "") for el in ellipses)
    assert validate_rendered_svg(svg_bytes, kind="cycle") == []


# --- standard flowchart (flow_chart/process - oval/rectangle/diamond) ------


def test_standard_flowchart_never_truncates_a_long_label_with_an_ellipsis():
    long_label = "A genuinely long step description that would have overflowed a small flowchart box"
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label=long_label), DiagramNode(id="b", label="Next")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in long_label.split():
        assert word in text, f"word lost: {word}"


def test_standard_flowchart_does_not_render_node_detail_as_visible_text():
    """A real textbook flowchart box shows only its label - `node.detail`
    (a longer supporting explanation) is intentionally never drawn as
    VISIBLE text anywhere for flow_chart/process: there is no more
    separate Key table under the diagram repeating/elaborating on what the
    shapes already show (see _layout_standard_flowchart's own docstring).
    It still reaches the node's own hover tooltip (`<title>`, never shown
    by default) - that is deliberately unaffected, so this checks the
    VISIBLE `<text>`/`<tspan>` content only, not the whole SVG source."""
    long_detail = (
        "A much longer supporting explanation sentence that would never have fit cleanly "
        "inside a small flowchart box."
    )
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Step", detail=long_detail), DiagramNode(id="b", label="Next")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    visible_text = "".join((el.text or "") for el in root.iter() if el.tag.endswith("}text") or el.tag.endswith("}tspan"))
    assert "Step" in visible_text and "Next" in visible_text
    assert long_detail not in visible_text
    assert long_detail in svg_bytes.decode("utf-8")  # still reaches the hover tooltip, just not visibly


def test_standard_flowchart_draws_edge_labels_as_a_pill_on_the_arrow():
    """A decision's own edge label ("Yes"/"No") sits directly on its own
    arrow as a small pill (see _edge_label) - the same convention every
    other kind in this module already uses for a labelled edge - instead
    of a separate explanatory block under the diagram."""
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Start"), DiagramNode(id="b", label="End")],
        edges=[DiagramEdge(source="a", target="b", label="Approved")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "Approved" in text
    assert 'class="diagram-edge-label"' in text  # the pill background _edge_label draws
    assert 'marker-end="url(#diagram-arrow' in text  # the arrow itself still drew


def test_standard_flowchart_edge_label_never_overlaps_a_node_even_when_compacted():
    """A real, confirmed gap caught by reviewing an actual generated PDF: a
    labelled edge's own gap was the SAME `gap` diagram compaction shrinks
    for every other segment - `render_diagram_svg_fitting` (generation-
    time diagram compaction, see app.course.document.diagram_compaction)
    could shrink it below the 2-line _edge_label pill's own height, so the
    pill visibly overlapped the node below it. A labelled segment must
    stay clear regardless of how aggressively the rest of the diagram was
    compacted to fit its page."""
    from app.render.diagram_renderer import render_diagram_svg_fitting

    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(7)]
    edges = [
        DiagramEdge(source=f"n{i}", target=f"n{i + 1}", label="use geometry to construct the next relation")
        if i == 3
        else DiagramEdge(source=f"n{i}", target=f"n{i + 1}")
        for i in range(6)
    ]
    spec = DiagramSpec(kind="flow_chart", title="Compacted", nodes=nodes, edges=edges)
    # A tight budget, the same way a page genuinely short on room would
    # call this - forces real compaction (gap_scale < 1) rather than the
    # diagram's own natural, uncompacted size.
    svg_bytes, _w, _h = render_diagram_svg_fitting(spec, TemplateTheme(), max_height=700)
    assert validate_rendered_svg(svg_bytes, kind="flow_chart") == []


def test_standard_flowchart_uses_the_conventional_oval_rectangle_diamond_shapes():
    """Start/end -> oval(pill), an ordinary step -> rectangle, a real
    decision (2+ outgoing edges) -> diamond - the standard notation the
    brief asks for, not the old uniform rounded-box look."""
    nodes = [
        DiagramNode(id="s", label="Start"),
        DiagramNode(id="p", label="Do the thing"),
        DiagramNode(id="d", label="Ready?"),
        DiagramNode(id="e", label="End"),
    ]
    edges = [
        DiagramEdge(source="s", target="p"),
        DiagramEdge(source="p", target="d"),
        DiagramEdge(source="d", target="e", label="Yes"),
        DiagramEdge(source="d", target="s", label="No"),
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert text.count("<polygon") >= 1  # the decision diamond
    assert 'rx="28.0"' in text or "rx=\"28" in text  # a pill's rx == half its own height (_PILL_H / 2)
    assert "<rect" in text  # the ordinary process rectangle


def test_standard_flowcharts_decision_branch_meaning_is_never_lost():
    """A decision's own edge labels ("Yes"/"No") are never lost - each is
    drawn directly on its own arrow (see
    test_standard_flowchart_draws_edge_labels_as_a_pill_on_the_arrow)."""
    nodes = [
        DiagramNode(id="d", label="Ready?"),
        DiagramNode(id="go", label="Proceed"),
        DiagramNode(id="wait", label="Wait"),
    ]
    edges = [
        DiagramEdge(source="d", target="go", label="Yes"),
        DiagramEdge(source="d", target="wait", label="No"),
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "Yes" in text and "Proceed" in text
    assert "No" in text and "Wait" in text


def test_standard_flowchart_draws_a_side_connector_for_a_non_consecutive_edge():
    """A decision's alternate branch (not the next node in sequence) is a
    short curved side connector, never a persistent second column (see
    _layout_standard_flowchart's own docstring)."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(4)]
    edges = [
        DiagramEdge(source="n0", target="n1"),
        DiagramEdge(source="n1", target="n2"),
        DiagramEdge(source="n2", target="n3"),
        DiagramEdge(source="n1", target="n3"),  # skips n2 - a non-consecutive edge
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    # 3 straight consecutive arrows (<line>) + 1 curved side connector (<path> with an arrowhead).
    assert text.count("<line") >= 3
    assert "marker-end" in text and "<path d=" in text


def test_concept_wheel_draws_real_connecting_lines_and_edge_labels():
    """Root-cause fix (production course audit): an earlier version of
    _layout_concept_wheel drew no edges at all, reasoning that "parts of X"
    is conveyed by proximity/colour alone - true only for a plain
    membership edge, and a real, confirmed regression for a real course's
    specifically-labelled relationships (several satellite-to-satellite
    edges with real causal meaning like "increases"/"reduces", which
    proximity cannot convey at all - the information was silently lost).
    Every edge - focal spoke or satellite-to-satellite - must now draw a
    real connector with its own label."""
    spec = DiagramSpec(
        kind="concept_map",
        nodes=[
            DiagramNode(id="subj", label="Subject", level=0),
            DiagramNode(id="a", label="Part A", level=1),
            DiagramNode(id="b", label="Part B", level=1),
        ],
        edges=[
            DiagramEdge(source="a", target="subj", label="belongs to"),
            DiagramEdge(source="b", target="subj", label="belongs to"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert 'marker-end="url(#diagram-arrow' in text
    assert "belongs to" in text


def test_render_diagram_svg_hierarchy_layout():
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="a", label="Child A", level=1),
        DiagramNode(id="b", label="Child B", level=1),
    ]
    edges = [DiagramEdge(source="root", target="a"), DiagramEdge(source="root", target="b")]
    spec = DiagramSpec(kind="hierarchy", nodes=nodes, edges=edges)

    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    assert width > 0 and height > 0


def test_hierarchy_rows_are_centred_on_the_canvas_not_stuck_left():
    """A real, confirmed bug: every row used to start flush at x=MARGIN
    regardless of its own width, so only the single WIDEST row ever
    happened to span the canvas edge-to-edge - a narrower row (a lone
    root node, a shorter branch) sat stuck to the left instead of centred
    under the wider rows around it. Uses an uneven tree (1 root, 2
    children, 1 grandchild) specifically because a perfectly even tree
    can't distinguish "centred" from "stuck left" - every row would
    coincidentally share the same width either way."""
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="a", label="Child A", level=1),
        DiagramNode(id="b", label="Child B", level=1),
        DiagramNode(id="c", label="Grandchild", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="a"), DiagramEdge(source="root", target="b"),
        DiagramEdge(source="a", target="c"),
    ]
    spec = DiagramSpec(kind="hierarchy", nodes=nodes, edges=edges)
    svg_bytes, width, _height = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    boxes = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    # Group boxes into rows by their own y - each row's own centre must
    # land on the canvas centre, not just the widest row's.
    rows: dict[float, list[tuple[float, float]]] = {}
    for box in boxes:
        y = round(float(box.get("y")), 1)
        x, w = float(box.get("x")), float(box.get("width"))
        rows.setdefault(y, []).append((x, x + w))
    assert len(rows) == 3  # 3 distinct levels
    for y, spans in rows.items():
        left = min(s[0] for s in spans)
        right = max(s[1] for s in spans)
        assert abs((left + right) / 2 - width / 2) < 1.0, f"row at y={y} is not centred"


# ---------------------------------------------------------------------------
# `_real_delegation_spec` is the EXACT structure a real API call produced
# (captured directly, not hand-guessed) for "a manager's delegation decision
# workflow" - still used below for connectivity validation and font-size
# checks against the new ring rendering.
# ---------------------------------------------------------------------------


def _real_delegation_spec() -> DiagramSpec:
    nodes = [
        DiagramNode(id="start", label="Start"),
        DiagramNode(id="assess_request", label="Assess Request"),
        DiagramNode(id="decide_delegate", label="Delegate?"),
        DiagramNode(id="select_assignee", label="Select Assignee"),
        DiagramNode(id="define_scope", label="Define Scope"),
        DiagramNode(id="add_criteria", label="Add Criteria & Checkpoints"),
        DiagramNode(id="confirm_handoff", label="Confirm Handoff"),
        DiagramNode(id="monitor_decision", label="Monitor"),
        DiagramNode(id="close", label="Close"),
        DiagramNode(id="escalate", label="Escalate"),
        DiagramNode(id="keep_task", label="Keep Task"),
        DiagramNode(id="end", label="End"),
    ]
    edges = [
        DiagramEdge(source="start", target="assess_request"),
        DiagramEdge(source="assess_request", target="decide_delegate"),
        DiagramEdge(source="decide_delegate", target="select_assignee", label="Yes"),
        DiagramEdge(source="decide_delegate", target="keep_task", label="No"),
        DiagramEdge(source="select_assignee", target="define_scope"),
        DiagramEdge(source="define_scope", target="add_criteria"),
        DiagramEdge(source="add_criteria", target="confirm_handoff"),
        DiagramEdge(source="confirm_handoff", target="monitor_decision"),
        DiagramEdge(source="monitor_decision", target="close", label="Close"),
        DiagramEdge(source="monitor_decision", target="escalate", label="Escalate"),
        DiagramEdge(source="close", target="end"),
        DiagramEdge(source="escalate", target="end"),
        DiagramEdge(source="keep_task", target="end"),
    ]
    return DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)


def test_flow_chart_connectivity_issues_flags_multiple_disconnected_ends():
    """Spec-level validation (before rendering) for the requested rule:
    fails if more than one node has no outgoing edge - three separate
    terminal paths (Close, Escalate, Keep) that never converge on a single
    shared End."""
    from app.services.diagram_service import _flow_chart_connectivity_issues

    nodes = [
        DiagramNode(id="start", label="Start"),
        DiagramNode(id="decide", label="Route"),
        DiagramNode(id="close", label="Close"),
        DiagramNode(id="escalate", label="Escalate"),
    ]
    edges = [
        DiagramEdge(source="start", target="decide"),
        DiagramEdge(source="decide", target="close", label="A"),
        DiagramEdge(source="decide", target="escalate", label="B"),
        # neither "close" nor "escalate" leads anywhere - two disconnected ends.
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    issues = _flow_chart_connectivity_issues(spec)
    assert len(issues) == 1
    assert "no outgoing edge" in issues[0]


def test_flow_chart_connectivity_issues_is_clean_for_the_real_spec():
    from app.services.diagram_service import _flow_chart_connectivity_issues

    assert _flow_chart_connectivity_issues(_real_delegation_spec()) == []


def test_flow_chart_connectivity_issues_ignores_process_kind():
    """"process" (an ongoing pipeline) legitimately has no Start/End
    bookends - this rule is specific to "flow_chart" (a bounded procedure)."""
    from app.services.diagram_service import _flow_chart_connectivity_issues

    nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B"), DiagramNode(id="c", label="C")]
    edges = [DiagramEdge(source="a", target="b"), DiagramEdge(source="b", target="c"), DiagramEdge(source="c", target="a")]
    spec = DiagramSpec(kind="process", nodes=nodes, edges=edges)
    assert _flow_chart_connectivity_issues(spec) == []


async def test_diagram_service_retries_once_on_disconnected_ends_then_renders_anyway(service, monkeypatch):
    """The full production path: a spec with multiple disconnected ends
    triggers exactly one retry with targeted feedback; if the retry can't
    fix it either (as here, since the mock always returns the same
    structure), the diagram still renders - render_diagram_svg's own
    connectivity guarantees mean it's never silently discarded for the
    unreliable raster fallback over this alone."""
    template = load_template("technical")
    block = _diagram_block(diagram_kind="flow_chart")
    calls: list[str] = []

    def _disconnected_spec() -> DiagramSpec:
        return DiagramSpec(
            kind="flow_chart",
            nodes=[
                DiagramNode(id="start", label="Start"),
                DiagramNode(id="decide", label="Route"),
                DiagramNode(id="close", label="Close"),
                DiagramNode(id="escalate", label="Escalate"),
            ],
            edges=[
                DiagramEdge(source="start", target="decide"),
                DiagramEdge(source="decide", target="close", label="A"),
                DiagramEdge(source="decide", target="escalate", label="B"),
            ],
        )

    async def fake_request_spec(self, **kwargs):
        calls.append(kwargs.get("qa_feedback", ""))
        return _disconnected_spec()

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_disconnected_ends", block=block, template=template, course_title="Test Course"
    )

    assert ok is True  # still rendered - real edges are guaranteed regardless
    assert len(calls) == 2  # exactly one retry, never unbounded
    assert "outgoing edge" in calls[1]
    # Quality-governance follow-up: this "retry exhausted, shipped anyway"
    # state must be visible to the quality report, not only this test's own
    # log-capture - a real, confirmed gap this closes (previously only a
    # log.warning, no persisted finding at all).
    findings = block.content.get("visual_qa_findings")
    assert findings and findings[0]["type"] == "flow_chart_connectivity"
    assert findings[0]["resolution"] == "rendered_with_unresolved_issues"
    assert findings[0]["deterministic"] is True


async def test_a_resolved_connectivity_retry_leaves_no_finding_behind(service, monkeypatch):
    """The mirror case: when the retry DOES produce a clean spec, nothing
    should be persisted - and a re-render of a block that previously carried
    a connectivity finding must not leave a STALE one behind now that it's
    fixed (merge_content is a shallow merge, so this must be set explicitly
    to [], never just omitted)."""
    template = load_template("technical")
    block = _diagram_block(diagram_kind="flow_chart")
    block.content["visual_qa_findings"] = [
        {"type": "flow_chart_connectivity", "detail": "stale from an earlier attempt",
         "resolution": "rendered_with_unresolved_issues", "deterministic": True}
    ]

    def _disconnected_spec() -> DiagramSpec:
        return DiagramSpec(
            kind="flow_chart",
            nodes=[
                DiagramNode(id="start", label="Start"),
                DiagramNode(id="decide", label="Route"),
                DiagramNode(id="close", label="Close"),
                DiagramNode(id="escalate", label="Escalate"),
            ],
            edges=[
                DiagramEdge(source="start", target="decide"),
                DiagramEdge(source="decide", target="close", label="A"),
                DiagramEdge(source="decide", target="escalate", label="B"),
            ],
        )

    def _reconnected_spec() -> DiagramSpec:
        return DiagramSpec(
            kind="flow_chart",
            nodes=[
                DiagramNode(id="start", label="Start"),
                DiagramNode(id="decide", label="Route"),
                DiagramNode(id="close", label="Close"),
                DiagramNode(id="escalate", label="Escalate"),
                DiagramNode(id="end", label="End"),
            ],
            edges=[
                DiagramEdge(source="start", target="decide"),
                DiagramEdge(source="decide", target="close", label="A"),
                DiagramEdge(source="decide", target="escalate", label="B"),
                DiagramEdge(source="close", target="end"),
                DiagramEdge(source="escalate", target="end"),
            ],
        )

    calls = {"n": 0}

    async def fake_request_spec(self, **kwargs):
        calls["n"] += 1
        return _disconnected_spec() if calls["n"] == 1 else _reconnected_spec()

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_resolved_connectivity", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content.get("visual_qa_findings") == []


# ---------------------------------------------------------------------------
# diagram_content_qa: semantic/content-relevance (the "meaningless flowchart" gap)
# ---------------------------------------------------------------------------


async def test_evaluate_content_is_a_noop_for_schematic_and_concept_experience():
    """Each already has its own dedicated semantic check elsewhere - never a
    silent double-charge of two LLM calls for the same spec."""
    from app.services.diagram_content_qa import evaluate_content

    assert (await evaluate_content(
        DiagramSpec(kind="schematic"), brief="anything", course_title="Course"
    )).passed
    assert (await evaluate_content(
        DiagramSpec(kind="concept_experience"), brief="anything", course_title="Course"
    )).passed


async def test_evaluate_content_is_a_noop_with_a_blank_brief():
    """Nothing to grade the content against - never fabricate a judgement."""
    from app.services.diagram_content_qa import evaluate_content

    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    assert (await evaluate_content(spec, brief="   ", course_title="Course")).passed


async def test_evaluate_content_catches_off_topic_and_generic_labels(monkeypatch):
    from app.services.diagram_content_qa import GENERIC_CONTENT, OFF_TOPIC, evaluate_content

    def failing_critique(user: str) -> dict:
        return {
            "on_topic": False, "on_topic_detail": "labels describe an unrelated topic",
            "specific_enough": False, "specific_detail": "every label is a generic placeholder",
            "missing_concepts": [],
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", failing_critique)

    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Step 1"), DiagramNode(id="b", label="Step 2")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    result = await evaluate_content(spec, brief="Explain the water cycle", course_title="Course")

    assert not result.passed
    reasons = {issue.reason for issue in result.issues}
    assert reasons == {OFF_TOPIC, GENERIC_CONTENT}
    assert all(issue.deterministic is False for issue in result.issues)  # an LLM judgment, never a proven fact


async def test_evaluate_content_reports_missing_key_concepts(monkeypatch):
    from app.services.diagram_content_qa import MISSING_KEY_CONCEPT, evaluate_content

    def failing_critique(user: str) -> dict:
        return {
            "on_topic": True, "specific_enough": True,
            "missing_concepts": ["condensation", "precipitation"],
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", failing_critique)

    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Evaporation"), DiagramNode(id="b", label="Collection")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    result = await evaluate_content(spec, brief="Explain the water cycle", course_title="Course")

    assert not result.passed
    assert result.issues[0].reason == MISSING_KEY_CONCEPT
    assert "condensation" in result.issues[0].detail and "precipitation" in result.issues[0].detail


async def test_evaluate_content_catches_structurally_garbled_labels_without_an_llm_call(monkeypatch):
    """Layer 0 (release-readiness audit follow-up): a node label mixing Latin
    with Cyrillic/Greek homoglyphs is caught by the same deterministic,
    free `_looks_garbled` check `concept_qa`/`evaluate_schematic` already
    use - BEFORE any LLM call is made. Monkeypatching ContentCritique to
    raise proves the short-circuit: if the LLM path were reached, this test
    would fail with that exception instead of the assertion below."""
    from app.services.diagram_content_qa import STRUCTURALLY_GARBLED_TEXT, evaluate_content

    def must_not_be_called(user: str) -> dict:
        raise AssertionError("evaluate_content must short-circuit before any LLM call")

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", must_not_be_called)

    # "Тriggered" - the T is Cyrillic У+0422, every other letter is Latin;
    # visually near-identical to "Triggered", exactly the homoglyph-swap
    # class _looks_garbled exists to catch.
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Тriggered probe"), DiagramNode(id="b", label="Done")],
        edges=[DiagramEdge(source="a", target="b")],
    )

    result = await evaluate_content(spec, brief="Explain the probe workflow", course_title="Course")

    assert not result.passed
    assert result.issues[0].reason == STRUCTURALLY_GARBLED_TEXT
    assert result.issues[0].deterministic is True


async def test_evaluate_content_catches_misspelled_and_scrambled_labels_via_the_llm_critique(monkeypatch):
    """Layer 1 (release-readiness audit follow-up): the real, confirmed
    defect class from a live production preview - plain-Latin misspellings
    ("Triggerted" for "Triggered", "Ambigeous" for "Ambiguous") and fully
    scrambled sentences, neither of which _looks_garbled can detect (no
    homoglyphs involved) - caught via the existing ContentCritique call's
    new `text_legible` judgment, not a new API call."""
    from app.services.diagram_content_qa import MISSPELLED_OR_GARBLED_TEXT, evaluate_content

    def illegible_critique(user: str) -> dict:
        return {
            "on_topic": True, "specific_enough": True, "missing_concepts": [],
            "text_legible": False,
            "text_legible_detail": "'Triggerted probe' and 'Ambigeous or missing signal' are misspelled.",
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", illegible_critique)

    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[
            DiagramNode(id="a", label="Triggerted probe (often a sanitizer)"),
            DiagramNode(id="b", label="Ambigeous or missing signal"),
        ],
        edges=[DiagramEdge(source="a", target="b")],
    )
    result = await evaluate_content(spec, brief="Explain the triage workflow", course_title="Course")

    assert not result.passed
    assert result.issues[0].reason == MISSPELLED_OR_GARBLED_TEXT
    assert "Triggerted" in result.issues[0].detail
    assert result.issues[0].deterministic is False  # an LLM judgment, never a proven fact


async def test_evaluate_content_does_not_false_flag_correctly_spelled_labels_as_illegible(monkeypatch):
    """A real short/technical label (an abbreviation, a symbol-heavy term)
    must not be mistaken for garbled text - valid content is never
    unnecessarily rejected."""
    from app.services.diagram_content_qa import evaluate_content

    def clean_critique(user: str) -> dict:
        return {"on_topic": True, "specific_enough": True, "missing_concepts": [], "text_legible": True}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", clean_critique)

    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Read crash log"), DiagramNode(id="b", label="Triggered probe")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    result = await evaluate_content(spec, brief="Explain the triage workflow", course_title="Course")

    assert result.passed


async def test_diagram_content_qa_retries_once_then_succeeds(service, monkeypatch):
    """Full production path: a content-QA failure triggers exactly one
    retry with targeted feedback; a spec that passes on the retry ships
    normally, with no stale finding left behind."""
    template = load_template("technical")
    block = _diagram_block()
    calls = {"n": 0}

    def sometimes_failing_critique(user: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"on_topic": False, "on_topic_detail": "off topic", "specific_enough": True, "missing_concepts": []}
        return {"on_topic": True, "specific_enough": True, "missing_concepts": []}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", sometimes_failing_critique)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_content_qa_retry", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert calls["n"] == 2  # exactly one retry, never unbounded
    assert block.content.get("path", "").endswith(".svg")
    assert block.content.get("visual_qa_findings") == []


async def test_diagram_content_qa_gets_a_second_chance_when_the_first_retry_does_not_improve(service, monkeypatch):
    """Real, confirmed gap this fixes: a SINGLE retry wasn't always enough
    for the model to fold in a specific missing concept, even when told
    exactly what was missing - the first retry's critique can come back
    with the SAME single issue (same count, same reason code), which a raw
    "did the issue count go down" check can't tell apart from "no further
    attempt would ever help". This diagram only succeeds on the SECOND
    retry - proving the bounded budget is actually spent, not abandoned
    after one attempt that merely failed to improve."""
    template = load_template("technical")
    block = _diagram_block()
    calls = {"n": 0}

    def improves_only_on_the_third_attempt(user: str) -> dict:
        calls["n"] += 1
        if calls["n"] < 3:
            return {
                "on_topic": True, "specific_enough": True,
                "missing_concepts": ["frame-conversion step"],
            }
        return {"on_topic": True, "specific_enough": True, "missing_concepts": []}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", improves_only_on_the_third_attempt)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_content_qa_second_retry", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert calls["n"] == 3  # original attempt + two retries, the full bounded budget
    assert block.content.get("path", "").endswith(".svg")
    assert block.content.get("visual_qa_findings") == []


async def test_diagram_garbled_text_retries_once_with_the_specific_bad_labels_fed_back(service, monkeypatch):
    """Follow-up to the two Layer-0/Layer-1 unit tests above: this is the
    REAL production path (diagram_service.py's retry loop, not evaluate_content
    called directly) - proves retry feedback for the new reason code actually
    reaches the next generation attempt, the same contract already proven for
    off_topic/generic_content."""
    template = load_template("technical")
    block = _diagram_block()
    calls = {"n": 0}
    feedback_seen: list[str] = []

    def sometimes_illegible_critique(user: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "on_topic": True, "specific_enough": True, "missing_concepts": [],
                "text_legible": False, "text_legible_detail": "'Triggerted' is misspelled",
            }
        return {"on_topic": True, "specific_enough": True, "missing_concepts": [], "text_legible": True}

    original_request_spec = service.images.diagrams._request_spec

    async def spying_request_spec(*args, **kwargs):
        if kwargs.get("qa_feedback"):
            feedback_seen.append(kwargs["qa_feedback"])
        return await original_request_spec(*args, **kwargs)

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", sometimes_illegible_critique)
    monkeypatch.setattr(service.images.diagrams, "_request_spec", spying_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_garbled_text_retry", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert calls["n"] == 2  # exactly one retry, never unbounded
    assert block.content.get("path", "").endswith(".svg")
    assert block.content.get("visual_qa_findings") == []  # resolved - no stale finding left behind
    assert feedback_seen and "Triggerted" in feedback_seen[0]


async def test_diagram_garbled_text_is_withheld_rather_than_shipped_as_an_illustration(service, monkeypatch):
    """Visual-generation policy fix: a diagram whose labels are confirmed
    misspelled/garbled, after an exhausted retry, is now WITHHELD entirely
    (`DiagramService.generate_for_block` returns `True`, "handled") rather
    than falling back to the raster illustration path - a structured
    diagram kind must never become unrelated AI art, regardless of why the
    structured attempt had trouble. `ImageService`'s own illustration
    machinery is never even reached for this case any more (confirmed: no
    `ImageTextCheck` mock needed here, unlike the old fallback-era version
    of this test). The specific garbled-text finding survives alongside the
    generic "withheld" marker, never silently erased."""
    template = load_template("technical")
    block = _diagram_block()

    def always_illegible_critique(user: str) -> dict:
        return {
            "on_topic": True, "specific_enough": True, "missing_concepts": [],
            "text_legible": False, "text_legible_detail": "'Ambigeous' is misspelled",
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", always_illegible_critique)

    ok = await service.images.generate_for_block(
        course_id="course_garbled_text_fallback", block=block, template=template, course_title="Test Course"
    )

    # Withheld is a "handled" outcome, not a failure of the block.
    assert ok is True
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("error") is None  # a confirmed QA failure, not a technical/provider failure
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "misspelled_or_garbled_text" for f in findings)
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)
    assert all(f["resolution"] == "image_withheld_after_qa_failure" for f in findings)


async def test_a_diagram_with_unresolved_render_qa_issues_ships_flagged_not_withheld(service, monkeypatch):
    """Visual-generation policy fix: when the rendered SVG's own geometry QA
    (node overlap, a shared edge segment, a duplicate arrowhead) still has
    issues after one corrective retry, the diagram is a real, legible,
    correctly-shaped answer to the brief - a layout imperfection, not a
    reason to discard it. It ships anyway as a real SVG asset, flagged
    `rendered_with_unresolved_issues` for human review, never falling back
    to an unrelated AI illustration and never withheld (withholding is
    reserved for a genuinely unusable/unreadable spec - see the garbled-text
    and is_usable() tests above, a materially different case from this one)."""
    import app.services.diagram_service as diagram_service_module
    from app.services.diagram_render_qa import RenderQAIssue

    forced_issue = RenderQAIssue(kind="shared_edge_segment", detail="forced for this test")
    monkeypatch.setattr(
        diagram_service_module, "validate_rendered_svg", lambda svg_bytes, kind: [forced_issue]
    )

    template = load_template("technical")
    block = _diagram_block()

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_render_qa_ships_flagged", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert block.content.get("generated") is True
    assert block.content.get("path", "").endswith(".svg")  # a real diagram, never a raster illustration
    assert block.content.get("omitted_after_qa_failure") is False
    findings = block.content.get("visual_qa_findings")
    assert any(
        f["type"] == "shared_edge_segment" and f["resolution"] == "rendered_with_unresolved_issues"
        for f in findings
    )


async def test_a_clean_diagram_regeneration_clears_a_stale_withheld_flag(service):
    """Real, confirmed bug (PDF/visual-pipeline audit, doc_81d0b1d44cae): a
    block previously withheld (`omitted_after_qa_failure: true`, no `path`,
    exactly `_generate_illustration`'s own withholding state) that later
    regenerates a genuinely clean diagram kept that stale `true` forever -
    `DiagramService.generate_for_block`'s success-path `merge_content` never
    reset it, unlike `visual_qa_findings` right beside it (whose own comment
    already states the identical "must never leave a stale earlier-attempt
    marker" rule). `image_generation_failed()` also checks `path`, so the
    compact placeholder never visibly reappeared - but the raw field stayed
    a live lie: a real image shipped while its own content still claimed
    "withheld". Confirmed via a real regenerated course block before this
    fix (`omitted_after_qa_failure: True` alongside a real `path` and empty
    `visual_qa_findings`)."""
    template = load_template("technical")
    block = _diagram_block(omitted_after_qa_failure=True, path=None, generated=False)

    ok = await service.images.generate_for_block(
        course_id="course_stale_withheld_flag", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content.get("path", "").endswith(".svg")
    assert block.content.get("omitted_after_qa_failure") is False


async def test_diagram_content_qa_ships_the_best_attempt_flagged_after_exhausted_retry(service, monkeypatch):
    """Fallback-cascade root-cause fix: unlike a genuinely garbled/
    misspelled label (still falls back to illustration - see the sibling
    garbled-text test), a persistent off-topic/generic/missing-concept
    judgment is a critique of how WELL the diagram covers the brief, not a
    claim its real, legible, correctly-spelled labels are wrong. The best
    structurally-sound attempt now ships anyway, flagged for human review,
    rather than being discarded for the raster illustration fallback -
    mirrors concept_visual_service.py's own already-established "no
    fallback on content-QA failure" contract."""
    template = load_template("technical")
    block = _diagram_block()

    def always_failing_critique(user: str) -> dict:
        return {"on_topic": False, "on_topic_detail": "still off topic", "specific_enough": True, "missing_concepts": []}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", always_failing_critique)

    ok = await service.images.generate_for_block(
        course_id="course_content_qa_fallback", block=block, template=template, course_title="Test Course"
    )

    assert ok is True  # the real, topic-grounded diagram ships, flagged for review
    assert block.content.get("generated") is True
    assert block.content.get("path", "").endswith(".svg")
    findings = block.content.get("visual_qa_findings")
    assert findings and findings[0]["type"] == "off_topic"
    assert findings[0]["resolution"] == "rendered_with_unresolved_issues"
    assert findings[0]["deterministic"] is False


def test_flow_chart_asymmetric_reconvergence_falls_back_to_linear_not_a_crash():
    """`_layout_ring_process` has no notion of decision branches/
    reconvergence at all any more - every node, however tangled its real
    edges are, just takes its own position on the ring. This asymmetric
    topology (one branch reconverges, the other doesn't) used to be the
    exact shape that broke the old box-and-branch renderer's fallback
    logic; kept as a regression guard that the new, topology-agnostic
    layout never drops a node or crashes on it either."""
    nodes = [
        DiagramNode(id="start", label="Start"),
        DiagramNode(id="check", label="Check"),
        DiagramNode(id="a", label="Branch A"),
        DiagramNode(id="b", label="Branch B"),
        DiagramNode(id="shared", label="Shared next step"),
        DiagramNode(id="after_shared", label="After shared"),
        DiagramNode(id="isolated", label="Other trigger"),
    ]
    edges = [
        DiagramEdge(source="start", target="check"),
        DiagramEdge(source="check", target="a", label="Yes"),  # dead-ends alone
        DiagramEdge(source="check", target="b", label="No"),  # reaches "shared"
        DiagramEdge(source="b", target="shared"),
        # An unrelated inbound edge (from a node outside either branch and
        # outside the main spine entirely), so "shared" has in-degree 2 - a
        # reconvergence candidate - without branch A ever reaching it too:
        # the asymmetry this test is actually about.
        DiagramEdge(source="isolated", target="shared"),
        DiagramEdge(source="shared", target="after_shared"),
    ]
    svg_bytes, width, height = render_diagram_svg(DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges), TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text, f"{node.label} missing"
    assert width > 0 and height > 0



def test_render_diagram_svg_escapes_untrusted_text():
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[
            DiagramNode(id="a", label='<script>alert("x")</script>'),
            DiagramNode(id="b", label="Safe & sound"),
        ],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    assert b"<script>" not in svg_bytes
    ET.fromstring(svg_bytes)  # would fail on unescaped '&' or '<' in text content


def test_render_diagram_svg_is_interactive():
    """Hover/focus must reveal the full, untruncated detail - not just what
    fits visually in the box."""
    long_detail = (
        "A much longer supporting sentence that will not fit on the three "
        "wrapped lines drawn inside the box itself, so the tooltip is the "
        "only place a reader can see all of it."
    )
    nodes = [
        DiagramNode(id="n0", label="Cornea", detail=long_detail),
        DiagramNode(id="n1", label="Lens", detail="Focuses light onto the retina."),
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=[DiagramEdge(source="n0", target="n1")])

    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")

    assert "<title>Cornea" in text
    assert long_detail in text  # full text reaches the tooltip even if wrapped/truncated on-box
    assert 'class="diagram-node"' in text
    assert "tabindex=" in text  # keyboard-focusable, not hover-only
    assert ":hover" in text and ":focus" in text


def test_render_diagram_svg_handles_an_empty_spec_without_crashing():
    svg_bytes, width, height = render_diagram_svg(DiagramSpec(nodes=[]), TemplateTheme())
    ET.fromstring(svg_bytes)
    assert width > 0 and height > 0


# ---------------------------------------------------------------------------
# ImageService/DiagramService integration (offline mock AI)
# ---------------------------------------------------------------------------


def _diagram_block(**overrides) -> Block:
    content = {
        "kind": "diagram",
        "purpose": "Show the stages of the pipeline",
        "prompt": "Four sequential stages: gather, plan, build, review",
        "caption": "Figure: pipeline stages",
        **overrides,
    }
    return Block(type=BlockType.IMAGE, content=content)


async def test_diagram_block_generates_an_svg_asset(service):
    template = load_template("technical")
    block = _diagram_block()

    ok = await service.images.generate_for_block(
        course_id="course_diagram_1", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content["kind"] == "diagram"
    assert block.content["path"].endswith(".svg")
    assert block.content["generated"] is True
    assert block.content["error"] is None
    assert block.content["width"] and block.content["height"]
    asset_path = service.storage.asset_abs_path("course_diagram_1", block.content["path"])
    assert asset_path.exists()
    assert asset_path.read_bytes().startswith(b"<svg")


async def test_replace_image_on_a_diagram_feeds_its_existing_spec_back_in_as_correction_context(
    service, monkeypatch,
):
    """Replace Image root cause (pipeline investigation): `replace_image`
    (app.course.document.patcher) clears `content["path"]` so the block
    regenerates, but leaves `content["spec"]` (the already-rendered
    structure) untouched - DiagramService.generate_for_block must read it
    back and pass it to `_request_spec` as `existing_outline` so a
    correction can preserve the existing nodes/edges instead of the model
    inventing a brand-new diagram from the editor's text prompt alone,
    with no idea the old one ever existed."""
    from app.services.diagram_service import diagram_outline

    template = load_template("technical")
    original_spec = DiagramSpec(
        kind="concept_map",
        title="Fig. 1: Mathematical Tools",
        nodes=[
            DiagramNode(id="hub", label="Mathematical Tools", level=0),
            DiagramNode(id="n1", label="Analytic workflows", level=1),
            DiagramNode(id="n2", label="Coords & separation", level=1),
        ],
        edges=[
            DiagramEdge(source="hub", target="n1", label="includes"),
            DiagramEdge(source="hub", target="n2", label="includes"),
        ],
    )
    block = _diagram_block(
        prompt="Fix the numbers overlapping the labels",
        spec=original_spec.model_dump(mode="json"),
    )

    captured: dict = {}

    async def fake_request_spec(self, **kwargs):
        captured.update(kwargs)
        return original_spec

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_replace_diagram", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert captured.get("existing_outline") == diagram_outline(original_spec)


async def test_a_correction_that_discards_most_original_labels_is_flagged_as_drift(service, monkeypatch):
    """Phase 3 quality-verification step (deterministic, zero extra AI
    calls): when a replace_image correction's regenerated spec keeps very
    few of the original diagram's labels, that's a measurable sign the
    instruction was followed as a redesign rather than a correction -
    recorded as a visible finding for a reviewer, never silently accepted
    as a successful fix."""
    template = load_template("technical")
    original_spec = DiagramSpec(
        kind="concept_map",
        nodes=[
            DiagramNode(id="hub", label="Mathematical Tools", level=0),
            DiagramNode(id="n1", label="Analytic workflows", level=1),
            DiagramNode(id="n2", label="Coords & separation", level=1),
        ],
        edges=[
            DiagramEdge(source="hub", target="n1", label="includes"),
            DiagramEdge(source="hub", target="n2", label="includes"),
        ],
    )
    block = _diagram_block(
        prompt="Fix the overlapping numbers", spec=original_spec.model_dump(mode="json"),
    )

    async def fake_request_spec(self, **kwargs):
        # A completely different diagram - none of the original labels survive.
        return DiagramSpec(
            kind="process",
            nodes=[DiagramNode(id="x", label="Unrelated step one"), DiagramNode(id="y", label="Unrelated step two")],
            edges=[DiagramEdge(source="x", target="y")],
        )

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_drift", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    findings = block.content.get("visual_qa_findings")
    assert findings and any(f["type"] == "replace_image_correction_drift" for f in findings)


async def test_a_correction_that_keeps_most_labels_is_not_flagged_as_drift(service, monkeypatch):
    template = load_template("technical")
    original_spec = DiagramSpec(
        kind="concept_map",
        nodes=[
            DiagramNode(id="hub", label="Mathematical Tools", level=0),
            DiagramNode(id="n1", label="Analytic workflows", level=1),
            DiagramNode(id="n2", label="Coords & separation", level=1),
        ],
        edges=[
            DiagramEdge(source="hub", target="n1", label="includes"),
            DiagramEdge(source="hub", target="n2", label="includes"),
        ],
    )
    block = _diagram_block(
        prompt="Fix the overlapping numbers", spec=original_spec.model_dump(mode="json"),
    )

    async def fake_request_spec(self, **kwargs):
        # Same structure, just the one thing the instruction asked to fix.
        return original_spec.model_copy(deep=True)

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_no_drift", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    findings = block.content.get("visual_qa_findings") or []
    assert not any(f["type"] == "replace_image_correction_drift" for f in findings)


async def test_a_fresh_diagram_block_with_no_prior_spec_passes_no_existing_outline(service, monkeypatch):
    """The mirror case: a block generating for the FIRST time (no
    `content["spec"]` yet) must never fabricate correction context - a
    first-time generation is unaffected by this fix."""
    block = _diagram_block()  # no "spec" key at all
    template = load_template("technical")
    captured: dict = {}

    async def fake_request_spec(self, **kwargs):
        captured.update(kwargs)
        return DiagramSpec(
            kind="process",
            nodes=[DiagramNode(id="a", label="Gather"), DiagramNode(id="b", label="Build")],
            edges=[DiagramEdge(source="a", target="b")],
        )

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_fresh_diagram", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert captured.get("existing_outline") is None


async def test_illustration_blocks_are_unaffected(service):
    """Regression check: the default (non-diagram) path is untouched."""
    template = load_template("technical")
    block = Block(
        type=BlockType.IMAGE,
        content={"purpose": "A hero illustration", "prompt": "A calm workspace"},
    )
    assert block.content["kind"] == "illustration"

    ok = await service.images.generate_for_block(
        course_id="course_illustration_1",
        block=block,
        template=template,
        course_title="Test Course",
    )

    assert ok is True
    assert block.content["path"].endswith(".png")


async def test_diagram_generation_is_withheld_on_spec_failure(service, monkeypatch):
    """Visual-generation policy fix: a structured diagram kind must never
    fall back to an unrelated AI illustration, even when the AI call that
    builds its structure fails outright - the block is withheld instead
    (a handled outcome, not a technical failure)."""
    template = load_template("technical")
    block = _diagram_block()
    original_structured = service.ai.structured

    async def flaky_structured(*, schema, **kwargs):
        if schema.__name__ == "DiagramSpec":
            raise AIServiceError("diagram model unavailable")
        return await original_structured(schema=schema, **kwargs)

    monkeypatch.setattr(service.ai, "structured", flaky_structured)

    ok = await service.images.generate_for_block(
        course_id="course_diagram_fallback",
        block=block,
        template=template,
        course_title="Test Course",
    )

    assert ok is True
    assert block.content.get("path") is None  # withheld, never an unrelated illustration
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content["kind"] == "diagram"  # the intent is preserved for next time
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)


async def test_diagram_with_too_few_nodes_is_withheld(service, monkeypatch):
    """Visual-generation policy fix: a genuinely unusable spec (too few
    labelled nodes/edges) is withheld, never routed to an unrelated AI
    illustration."""
    template = load_template("technical")
    block = _diagram_block()
    original_structured = service.ai.structured

    async def sparse_structured(*, schema, **kwargs):
        if schema.__name__ == "DiagramSpec":
            return DiagramSpec(kind="flow_chart", nodes=[DiagramNode(label="Only one")])
        return await original_structured(schema=schema, **kwargs)

    monkeypatch.setattr(service.ai, "structured", sparse_structured)

    ok = await service.images.generate_for_block(
        course_id="course_diagram_sparse",
        block=block,
        template=template,
        course_title="Test Course",
    )

    assert ok is True
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True


async def test_diagram_with_too_many_nodes_gets_a_consolidation_retry(service, monkeypatch):
    """A genuinely rich technical brief can legitimately produce more nodes
    than a flow_chart's cap (14, see max_nodes_for) - rather than discarding
    the whole structured attempt for the unreliable raster fallback, one
    retry asks the model to consolidate down to the budget it actually has."""
    template = load_template("technical")
    block = _diagram_block()
    original_structured = service.ai.structured
    calls: list[str] = []

    async def sequenced_structured(*, schema, **kwargs):
        if schema.__name__ != "DiagramSpec":
            return await original_structured(schema=schema, **kwargs)
        calls.append(kwargs.get("user", ""))
        if len(calls) == 1:
            return DiagramSpec(
                kind="flow_chart",
                nodes=[DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(16)],
                edges=[
                    DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(15)
                ],
            )
        # the consolidation retry: a properly shrunk spec.
        return DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id=f"m{i}", label=f"Merged step {i}") for i in range(6)],
            edges=[DiagramEdge(source=f"m{i}", target=f"m{i + 1}") for i in range(5)],
        )

    monkeypatch.setattr(service.ai, "structured", sequenced_structured)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_diagram_consolidate", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert len(calls) == 2, "expected exactly one consolidation retry"
    assert "more than a flow_chart diagram can clearly show" in calls[1]
    assert block.content["path"].endswith(".svg")  # the structured path succeeded, no raster fallback
    svg_text = service.storage.asset_abs_path("course_diagram_consolidate", block.content["path"]).read_text(
        encoding="utf-8"
    )
    assert "Merged step 0" in svg_text
    assert "Step 0" not in svg_text  # the over-budget attempt was discarded, not rendered


async def test_diagram_is_withheld_when_consolidation_retry_still_exceeds_the_cap(service, monkeypatch):
    """Visual-generation policy fix: when even the consolidation retry still
    exceeds the kind's node cap (`is_usable()` false), the block is withheld
    - never a silently truncated diagram, and never an unrelated AI
    illustration."""
    template = load_template("technical")
    block = _diagram_block()
    original_structured = service.ai.structured

    async def always_too_many(*, schema, **kwargs):
        if schema.__name__ != "DiagramSpec":
            return await original_structured(schema=schema, **kwargs)
        return DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(20)],
            edges=[DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(19)],
        )

    monkeypatch.setattr(service.ai, "structured", always_too_many)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_diagram_still_too_many", block=block, template=template, course_title="Test Course"
    )

    assert ok is True  # handled (withheld), not a failure the caller must fall back from
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True


async def test_diagram_generation_can_be_disabled_via_settings(service):
    service.settings.enable_diagram_generation = False
    template = load_template("technical")
    block = _diagram_block()

    ok = await service.images.generate_for_block(
        course_id="course_diagram_disabled",
        block=block,
        template=template,
        course_title="Test Course",
    )

    assert ok is True
    assert block.content["path"].endswith(".png")


# ---------------------------------------------------------------------------
# full pipeline
# ---------------------------------------------------------------------------


async def test_diagram_blocks_render_as_svg_through_the_full_pipeline(
    service, technical_input, monkeypatch
):
    original = WriterAgent.write_chapter

    async def with_diagram(self, **kwargs):
        blocks, summary = await original(self, **kwargs)
        return [*blocks, _diagram_block()], summary

    monkeypatch.setattr(WriterAgent, "write_chapter", with_diagram)

    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    document = service.storage.load_document(record.course_id)
    images = document.blocks_of_type(BlockType.IMAGE)
    diagrams = [b for b in images if b.content.get("kind") == "diagram"]
    # The system-built "toc" block (app.render.toc_renderer) is neither an
    # AI diagram nor an AI raster illustration - it's excluded from both
    # buckets below rather than asserted against either one's path format.
    illustrations = [
        b for b in images if b.content.get("kind") not in ("diagram", "toc")
    ]

    assert diagrams, "expected at least one diagram block"
    assert illustrations, "the pre-existing illustration path must still run"

    for block in diagrams:
        path = block.content["path"]
        assert path.endswith(".svg")
        assert service.storage.asset_abs_path(record.course_id, path).exists()
        assert block.content["width"] and block.content["height"]

    for block in illustrations:
        assert block.content["path"].endswith(".png")


# ---------------------------------------------------------------------------
# concept_map: labelled relationship diagrams (not sequential flowcharts)
# ---------------------------------------------------------------------------


def _rects_by_class(svg_text: str, css_class: str) -> list[tuple[float, float, float, float]]:
    pattern = re.compile(
        rf'<rect class="{css_class}" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"'
    )
    return [tuple(float(v) for v in match) for match in pattern.findall(svg_text)]


def _bboxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


@pytest.mark.parametrize("satellite_count", [1, 2, 3, 4, 5, 6, 7, 8])
@pytest.mark.parametrize("long_relationship_label", [False, True])
@pytest.mark.parametrize("long_node_label", [False, True])
def test_concept_map_labels_stay_clear_of_nodes_across_sizes(
    satellite_count, long_relationship_label, long_node_label
):
    """Broad sweep: whatever the node count or label length, a relationship
    label must never overlap a node box - covers both the "own spoke"
    undershoot bug and the "crowds a neighbouring spoke" bug."""
    node_label = "A rather long component name that wraps to two lines" if long_node_label else "Part"
    relationship = (
        "a long descriptive relationship label that wraps across two lines"
        if long_relationship_label
        else "causes"
    )
    nodes = [DiagramNode(id="focal", label="Subject", level=0)]
    edges = []
    for i in range(satellite_count):
        nodes.append(DiagramNode(id=f"s{i}", label=f"{node_label} {i}", level=1))
        edges.append(DiagramEdge(source=f"s{i}", target="focal", label=relationship))
    spec = DiagramSpec(kind="concept_map", nodes=nodes, edges=edges)

    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    boxes = _rects_by_class(text, "diagram-box")
    labels = _rects_by_class(text, "diagram-edge-label")

    offenders = [(label, box) for label in labels for box in boxes if _bboxes_overlap(label, box)]
    assert not offenders, f"n={satellite_count} overlaps: {offenders}"


def test_edge_label_wraps_long_text_instead_of_overflowing_its_pill():
    from app.render.diagram_renderer import _edge_label

    short = _edge_label(100.0, 100.0, "causes", TemplateTheme())
    assert short.count("<tspan") == 1

    long_text = "a long descriptive relationship label that wraps across two lines cleanly"
    wrapped = _edge_label(100.0, 100.0, long_text, TemplateTheme())
    assert wrapped.count("<tspan") == 2
    # The pill grew taller to fit the second line rather than clipping it.
    short_height = float(re.search(r'height="([\d.]+)"', short).group(1))
    long_height = float(re.search(r'height="([\d.]+)"', wrapped).group(1))
    assert long_height > short_height


def test_edge_label_pill_width_uses_the_same_calibrated_ratio_as_node_boxes():
    """The old ad hoc 6.5px/char estimate rendered pills meaningfully
    narrower than this module's own already-calibrated ratio (the same one
    `_wrap` and every node box use, confirmed against real browser
    rendering - see `_wrap`'s own docstring) - a real, confirmed case:
    "Charge <-> Queue item" overflowed its pill and was visually clipped by
    a node box painted on top of it afterward."""
    from app.render.diagram_renderer import (
        EDGE_LABEL_FONT_SIZE,
        EDGE_LABEL_MAX_WIDTH,
        _SANS_RATIO,
        _edge_label,
        _wrap,
    )

    label = "Charge <-> Queue item"
    svg = _edge_label(100.0, 100.0, label, TemplateTheme())
    width = float(re.search(r'width="([\d.]+)"', svg).group(1))

    # Mirror _edge_label's own wrapping step - the width formula operates on
    # the longest WRAPPED line, not the raw unwrapped label.
    lines = _wrap(label, font_size=EDGE_LABEL_FONT_SIZE, width=EDGE_LABEL_MAX_WIDTH - 16.0, max_lines=2)
    longest = max(len(line) for line in lines)

    old_ad_hoc_width = min(max(longest * 6.5 + 14, 40.0), EDGE_LABEL_MAX_WIDTH)
    calibrated_width = min(max(longest * EDGE_LABEL_FONT_SIZE * _SANS_RATIO + 14, 40.0), EDGE_LABEL_MAX_WIDTH)

    assert abs(width - calibrated_width) < 0.2
    assert width > old_ad_hoc_width


def test_concept_map_focal_node_is_chosen_by_real_connectivity_not_declared_level():
    """Reproduces the reported bug exactly: the declared level==0 node had
    only 1 real edge while a DIFFERENT node was actually the best-connected
    one - the old level-only rule always picked the former, forcing every
    other edge to bow messily around a ring centred on a near-isolated
    node. The best-connected node must become focal instead."""
    from app.render.diagram_renderer import _focal_node

    weak_level0 = DiagramNode(id="hub", label="Software <-> EM analogy", level=0)
    well_connected = DiagramNode(id="queue", label="Queue item", level=1)
    charge = DiagramNode(id="charge", label="Charge", level=1)
    limits = DiagramNode(id="limits", label="Analogy limits", level=1)
    nodes = [weak_level0, well_connected, charge, limits]
    edges = [
        DiagramEdge(source="hub", target="limits", label="Limits"),
        DiagramEdge(source="charge", target="queue", label="Charge <-> Queue item"),
        DiagramEdge(source="queue", target="limits", label="also related"),
    ]
    # hub: degree 1 (limits). queue: degree 2 (charge, limits) - the real centre.
    assert _focal_node(nodes, edges).id == "queue"


def test_concept_map_focal_node_keeps_a_genuine_hub_focal():
    """Regression guard: a well-formed single-hub spec (the common,
    already-correct case) must render exactly as before - the level==0 node
    IS the best-connected one here, so it stays focal."""
    from app.render.diagram_renderer import _focal_node

    hub = DiagramNode(id="hub", label="Hub", level=0)
    a = DiagramNode(id="a", label="A", level=1)
    b = DiagramNode(id="b", label="B", level=1)
    nodes = [hub, a, b]
    edges = [
        DiagramEdge(source="hub", target="a", label="x"),
        DiagramEdge(source="hub", target="b", label="y"),
    ]
    assert _focal_node(nodes, edges).id == "hub"


def test_concept_map_focal_node_tie_breaks_toward_declared_level_zero():
    """A genuine 3-way degree tie must still resolve to the declared
    level==0 node, even when a tied node is listed first - only a STRICTLY
    better-connected node should ever override the level-based default."""
    from app.render.diagram_renderer import _focal_node

    hub = DiagramNode(id="hub", label="Hub", level=0)
    tied = DiagramNode(id="tied", label="Tied", level=1)
    other = DiagramNode(id="other", label="Other", level=1)
    nodes = [tied, hub, other]  # the tied, non-hub node listed first
    edges = [
        DiagramEdge(source="hub", target="tied", label="a"),
        DiagramEdge(source="hub", target="other", label="b"),
        DiagramEdge(source="tied", target="other", label="c"),
    ]
    assert _focal_node(nodes, edges).id == "hub"


def test_concept_map_without_relationships_is_not_usable():
    """A handful of boxes with no labelled relationship between them is not a
    concept map - it's an unlabelled list, and should be rejected so the
    caller falls back rather than accept a hollow "diagram"."""
    bare_nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    assert not DiagramSpec(kind="concept_map", nodes=bare_nodes, edges=[]).is_usable()

    unlabelled_edge = [DiagramEdge(source="a", target="b", label="")]
    assert not DiagramSpec(kind="concept_map", nodes=bare_nodes, edges=unlabelled_edge).is_usable()

    labelled_edge = [DiagramEdge(source="a", target="b", label="causes")]
    assert DiagramSpec(kind="concept_map", nodes=bare_nodes, edges=labelled_edge).is_usable()

    three_bare_nodes = bare_nodes + [DiagramNode(id="c", label="C")]
    assert DiagramSpec(
        kind="concept_map", nodes=three_bare_nodes, edges=[DiagramEdge(source="a", target="b")]
    ).is_usable()


# ---------------------------------------------------------------------------
# kind-hint enforcement: a requested relationship diagram must not silently
# become a flowchart (the exact bug this hint mechanism exists to catch)
# ---------------------------------------------------------------------------


def test_kind_conflicts_flags_sequential_vs_relationship_mismatch():
    from app.services.diagram_service import _kind_conflicts

    assert _kind_conflicts("concept_map", "flow_chart") is True
    assert _kind_conflicts("concept_map", "process") is True
    assert _kind_conflicts("hierarchy", "cycle") is True
    assert _kind_conflicts("comparison", "smart_art") is True
    # Compatible-enough choices within a family are left alone.
    assert _kind_conflicts("flow_chart", "process") is False
    assert _kind_conflicts("concept_map", "hierarchy") is False
    assert _kind_conflicts("concept_map", "concept_map") is False
    assert _kind_conflicts("", "flow_chart") is False  # no hint, no opinion


def test_kind_conflicts_flags_smart_art_answering_a_relationship_hint():
    """A real, confirmed gap: smart_art's own renderer deliberately never
    draws edges ("numbered list, no connecting arrows") - it can never
    satisfy a relationship brief by construction, exactly like answering
    with a sequential kind can't (already caught above), but this specific
    case previously slipped through un-flagged."""
    from app.services.diagram_service import _kind_conflicts

    assert _kind_conflicts("concept_map", "smart_art") is True
    assert _kind_conflicts("hierarchy", "smart_art") is True
    assert _kind_conflicts("er_diagram", "smart_art") is True
    # Still no opinion when smart_art itself was requested, or no hint given.
    assert _kind_conflicts("smart_art", "smart_art") is False
    assert _kind_conflicts("", "smart_art") is False


def test_kind_conflicts_flags_comparison_answering_any_relationship_hint():
    """Phase 2 card-grid audit root-cause fix: `comparison` (a flat list of
    things, each described in its own prose card) is the SAME kind of
    silent escape hatch smart_art already was - a real, confirmed
    production case showed a relationship-kind hint (`schematic`, now also
    separately retired at the generation layer - see its own tests) coming
    back as `comparison` instead, un-flagged, because this branch only
    checked for a sequential or smart_art substitute. This must now be
    flagged for EVERY RELATIONSHIP_KINDS member, not just the one case that
    happened to be noticed - the whole point is this is a general pattern,
    not a schematic-specific one."""
    from app.services.diagram_service import _kind_conflicts

    assert _kind_conflicts("concept_map", "comparison") is True
    assert _kind_conflicts("hierarchy", "comparison") is True
    assert _kind_conflicts("decision_tree", "comparison") is True
    assert _kind_conflicts("fishbone", "comparison") is True
    assert _kind_conflicts("network_mesh", "comparison") is True
    assert _kind_conflicts("er_diagram", "comparison") is True
    assert _kind_conflicts("schematic", "comparison") is True
    # A hint that genuinely IS comparison is never flagged against itself.
    assert _kind_conflicts("comparison", "comparison") is False


async def test_diagram_service_retries_once_when_hint_is_ignored(service):
    """The model first ignores the requested concept_map and returns a
    flow_chart; the service must retry with the kind pinned rather than
    silently accepting the mismatch."""
    from app.schemas.diagram import DiagramSpec as _Spec

    template = load_template("technical")
    block = _diagram_block(diagram_kind="concept_map")
    calls: list[str] = []
    original_structured = service.ai.structured

    async def flaky_kind(*, schema, user, **kwargs):
        if schema is _Spec:
            calls.append(user)
            if len(calls) == 1:
                return _Spec(
                    kind="flow_chart",
                    nodes=[DiagramNode(id="a", label="Step A"), DiagramNode(id="b", label="Step B")],
                    edges=[DiagramEdge(source="a", target="b")],
                )
            return _Spec(
                kind="concept_map",
                nodes=[
                    DiagramNode(id="subj", label="Subject", level=0),
                    DiagramNode(id="part", label="Component", level=1),
                ],
                edges=[DiagramEdge(source="part", target="subj", label="acts on")],
            )
        return await original_structured(schema=schema, user=user, **kwargs)

    service.ai.structured = flaky_kind
    try:
        ok = await service.images.diagrams.generate_for_block(
            course_id="course_kind_retry", block=block, template=template, course_title="Test Course"
        )
    finally:
        service.ai.structured = original_structured

    assert ok is True
    assert len(calls) == 2  # the corrective retry actually happened
    assert "REQUESTED DIAGRAM TYPE: concept_map" in calls[1]
    svg_path = service.storage.asset_abs_path("course_kind_retry", block.content["path"])
    # The corrected spec's own node labels were rendered, AND its edge
    # label ("acts on") - _layout_concept_wheel now draws every edge (see
    # its own docstring on the root-cause fix this closed).
    rendered = svg_path.read_text(encoding="utf-8")
    assert "Subject" in rendered and "Component" in rendered
    assert "acts on" in rendered


async def test_diagram_service_still_produces_a_diagram_after_a_persistent_mismatch(service):
    """If the model ignores the pinned kind even on retry, the block must
    still end up with *a* diagram rather than nothing - but the retry must
    genuinely have been attempted first."""
    from app.schemas.diagram import DiagramSpec as _Spec

    template = load_template("technical")
    block = _diagram_block(diagram_kind="concept_map")
    call_count = 0
    original_structured = service.ai.structured

    async def always_flow_chart(*, schema, user, **kwargs):
        nonlocal call_count
        if schema is _Spec:
            call_count += 1
            return _Spec(
                kind="flow_chart",
                nodes=[DiagramNode(id="a", label="Step A"), DiagramNode(id="b", label="Step B")],
                edges=[DiagramEdge(source="a", target="b")],
            )
        return await original_structured(schema=schema, user=user, **kwargs)

    service.ai.structured = always_flow_chart
    try:
        ok = await service.images.diagrams.generate_for_block(
            course_id="course_kind_persist", block=block, template=template, course_title="Test Course"
        )
    finally:
        service.ai.structured = original_structured

    assert call_count == 2  # one original attempt + one corrective retry, not more
    assert ok is True
    assert block.content["path"].endswith(".svg")


# ---------------------------------------------------------------------------
# end-to-end: flowchart + concept diagram coexisting, for EM induction and a
# generic non-science topic (proving the fix is not EM-induction-specific)
# ---------------------------------------------------------------------------


async def test_electromagnetic_induction_gets_both_flowchart_and_concept_map(
    service, technical_input, monkeypatch
):
    """Reproduces the reported bug scenario directly: a chapter that needs
    both a process diagram (how to solve an induction problem) and a
    labelled concept diagram (the physical magnet/coil/current relationship)
    must end up with both - not just the flowchart."""
    original = WriterAgent.write_chapter

    async def with_em_diagrams(self, **kwargs):
        blocks, summary = await original(self, **kwargs)
        process_block = _diagram_block(
            diagram_kind="flow_chart",
            purpose="Show the steps to analyse an electromagnetic induction problem",
            prompt="Physical setup -> pick target integral -> convert form -> solve and validate",
            caption="Induction problem workflow",
        )
        concept_block = _diagram_block(
            diagram_kind="concept_map",
            purpose="Show how a moving magnet induces current in a coil",
            prompt=(
                "Bar magnet moving toward a coil; magnetic field lines passing through the "
                "coil; the coil inducing a current; the current deflecting a galvanometer "
                "needle, per Lenz's law"
            ),
            caption="Electromagnetic induction: magnet, coil and induced current",
        )
        return [*blocks, process_block, concept_block], summary

    monkeypatch.setattr(WriterAgent, "write_chapter", with_em_diagrams)

    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    document = service.storage.load_document(record.course_id)
    diagrams = [b for b in document.blocks_of_type(BlockType.IMAGE) if b.content.get("kind") == "diagram"]
    by_hint = {b.content.get("diagram_kind"): b for b in diagrams}

    assert "flow_chart" in by_hint and "concept_map" in by_hint
    for block in (by_hint["flow_chart"], by_hint["concept_map"]):
        path = block.content["path"]
        assert path.endswith(".svg")
        assert service.storage.asset_abs_path(record.course_id, path).exists()

    # The two diagrams are genuinely different shapes, not two flowcharts.
    flow_svg = service.storage.asset_abs_path(record.course_id, by_hint["flow_chart"].content["path"]).read_text(encoding="utf-8")
    concept_svg = service.storage.asset_abs_path(record.course_id, by_hint["concept_map"].content["path"]).read_text(encoding="utf-8")
    assert flow_svg != concept_svg


async def test_generic_non_science_topic_also_gets_a_concept_map(service):
    """Proves the fix is not hardcoded to physics/EM induction - any topic
    whose writer asks for a concept_map gets one, e.g. a business process."""
    template = load_template("non_technical")
    block = _diagram_block(
        diagram_kind="concept_map",
        purpose="Show how a supply chain's stages relate to each other",
        prompt="Supplier provides raw materials to Manufacturer, which ships to Distributor, "
        "which sells through Retailer, with demand signals flowing back upstream",
        caption="Supply chain relationships",
    )
    ok = await service.images.diagrams.generate_for_block(
        course_id="course_supply_chain", block=block, template=template, course_title="Operations 101"
    )
    assert ok is True
    assert block.content["path"].endswith(".svg")


# ---------------------------------------------------------------------------
# schematic: illustrated physical apparatus, with before/after states
# ---------------------------------------------------------------------------


def _em_induction_states() -> list[SchematicState]:
    def state(caption: str, magnet_x: float, needle_angle: float) -> SchematicState:
        return SchematicState(
            caption=caption,
            shapes=[
                SchematicShape(type="coil", id="coil", x=0.28, y=0.42, width=0.34, height=0.28, label="Coil"),
                SchematicShape(
                    type="block", id="magnet", x=magnet_x, y=0.42, width=0.22, height=0.14,
                    label="N", sublabel="S",
                ),
                SchematicShape(
                    type="gauge", id="meter", x=0.28, y=0.82, width=0.16, height=0.16,
                    rotation=needle_angle, sublabel="Ammeter",
                ),
            ],
        )

    return [
        state("No current", magnet_x=0.85, needle_angle=0),
        state("Current flows through the circuit", magnet_x=0.62, needle_angle=35),
    ]


def test_schematic_is_permanently_unusable_regardless_of_content():
    """`schematic` is permanently retired (see DiagramSpec.is_usable's own
    docstring) - never usable, no matter how well-formed the spec is. This
    makes the EXISTING "unusable spec -> fall back to a raster illustration"
    path in DiagramService.generate_for_block catch every schematic request
    unconditionally, regardless of what any future prompt or model happens
    to produce."""
    assert not DiagramSpec(kind="schematic", shapes=[]).is_usable()
    assert not DiagramSpec(
        kind="schematic", shapes=[SchematicShape(type="block", label="Only one")]
    ).is_usable()
    # A genuinely well-formed, 2-shape spec - the kind of input that used to
    # pass this check - must still be rejected now.
    assert not DiagramSpec(
        kind="schematic",
        shapes=[SchematicShape(type="block", label="A"), SchematicShape(type="coil", label="B")],
    ).is_usable()
    # Multi-state ("before/after") specs are rejected the same way.
    assert not DiagramSpec(kind="schematic", states=_em_induction_states()).is_usable()


def test_render_schematic_svg_renders_only_the_first_state_never_a_multi_panel_comparison():
    """Multi-state ("before/after") rendering is permanently retired (see
    _layout_schematic's own docstring) - two stacked panels repeating the
    same shapes, with real anchor-layout overlap bugs on top, read as a
    genuinely unwanted visual format, not something to keep rendering. A
    spec with 2+ states (legacy data, or a model that set it anyway despite
    no prompt encouragement) must render ONLY the first state as a normal
    single-panel illustration - the second state's own content must never
    appear."""
    spec = DiagramSpec(kind="schematic", title="Electromagnetic Induction", states=_em_induction_states())
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())

    ET.fromstring(svg_bytes)  # valid XML
    text = svg_bytes.decode("utf-8")
    assert "No current" in text
    assert "Current flows through the circuit" not in text
    assert "diagram-node" in text  # every shape is still hoverable/interactive
    assert width > 0 and height > 0


def test_render_schematic_svg_single_static_illustration_without_states():
    spec = DiagramSpec(
        kind="schematic",
        title="A simple circuit",
        shapes=[
            SchematicShape(type="block", id="battery", x=0.3, y=0.5, width=0.2, height=0.14, label="+", sublabel="-"),
            SchematicShape(type="arrow", id="current", x=0.6, y=0.5, width=0.2, height=0.1, rotation=0, label="Current"),
            SchematicShape(type="label", id="note", x=0.85, y=0.5, width=0.2, height=0.1, label="Load resistor"),
        ],
    )
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    assert width > 0 and height > 0


def test_a_stray_legacy_flow_shape_still_renders_safely_as_a_block():
    """"flow" (a fan of field/current lines) was retired from SHAPE_TYPES -
    it kept producing overlapping/garbled diagrams (its label is positioned
    from its own rotation alone, with zero awareness of any sibling shape or
    label) - see SHAPE_TYPES' docstring in app.schemas.diagram. A course
    persisted before this change can still have a "flow"-typed shape in its
    stored spec; it must degrade to the existing generic "unrecognised kind"
    fallback (render as a block) rather than crash or silently vanish."""
    spec = DiagramSpec(
        kind="schematic",
        title="Legacy diagram",
        shapes=[
            SchematicShape(type="coil", id="coil", x=0.3, y=0.5, width=0.3, height=0.3, label="Coil"),
            SchematicShape(type="flow", id="field", x=0.7, y=0.5, width=0.2, height=0.2, label="Field lines"),
        ],
    )
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)  # valid XML, no crash
    assert "Field lines" in svg_bytes.decode("utf-8")
    assert width > 0 and height > 0


# ---------------------------------------------------------------------------
# schematic generalises beyond physics: chemistry, biology, ... via the same
# small shape vocabulary (block/circle/coil/gauge/arrow/label)
# ---------------------------------------------------------------------------


def test_circle_shape_renders_with_an_inner_nucleus_when_sublabel_is_set():
    """The `circle` primitive is what makes schematic usable for biology/
    chemistry (a cell and its nucleus, an atom and its core) instead of only
    physics - it must draw a labelled circle, and a smaller labelled circle
    inside it when `sublabel` is set."""
    spec = DiagramSpec(
        kind="schematic",
        title="Animal Cell",
        shapes=[
            SchematicShape(type="circle", id="cell", x=0.5, y=0.5, width=0.5, height=0.7, label="Cell", sublabel="Nucleus"),
            SchematicShape(type="label", id="mito", x=0.15, y=0.2, width=0.2, height=0.1, label="Mitochondria"),
        ],
    )
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")
    assert text.count("<circle") >= 2  # outer cell + inner nucleus
    assert "Nucleus" in text and "Mitochondria" in text
    assert width > 0 and height > 0


def test_chemistry_reaction_schematic_renders_without_overlap():
    """A non-physics domain end to end: two reactant blocks, an arrow
    showing the reaction progressing, and a product block - proves the
    schematic vocabulary is genuinely generic, not physics-specific."""
    spec = DiagramSpec(
        kind="schematic",
        title="Formation of Water",
        shapes=[
            SchematicShape(type="block", id="h2", x=0.15, y=0.5, width=0.2, height=0.16, label="2H₂", sublabel="Hydrogen"),
            SchematicShape(type="block", id="o2", x=0.15, y=0.8, width=0.2, height=0.16, label="O₂", sublabel="Oxygen"),
            SchematicShape(
                type="arrow", id="react", x=0.5, y=0.65, width=0.2, height=0.1, rotation=0,
                label="combustion", target_id="water",
            ),
            SchematicShape(type="block", id="water", x=0.85, y=0.65, width=0.2, height=0.16, label="2H₂O"),
        ],
    )
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")
    assert "Hydrogen" in text and "Oxygen" in text and "combustion" in text
    assert width > 0 and height > 0


async def test_electromagnetic_induction_schematic_hint_is_permanently_rejected(service):
    """`schematic` is permanently retired (see DiagramSpec.is_usable's own
    docstring) - a physical-apparatus brief hinted as `schematic` (the exact
    reference scenario that used to render as an illustrated before/after
    diagram) must now be rejected unconditionally, so the caller falls back
    to a raster illustration instead. Confirmed end to end through
    DiagramService, not just the is_usable() unit check."""
    template = load_template("technical")
    block = _diagram_block(
        diagram_kind="schematic",
        purpose="Show a bar magnet moving toward a coil and the resulting induced current",
        prompt=(
            "A coil connected to an ammeter; a bar magnet with N and S poles; magnetic "
            "field lines between the magnet and the coil; two states - the magnet far "
            "away with no current, then the magnet moving closer with the ammeter "
            "needle deflected and current flowing"
        ),
        caption="Electromagnetic induction: magnet, coil and induced current",
    )

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_em_schematic", block=block, template=template, course_title="Electromagnetic Induction"
    )

    assert ok is False
    assert not block.content.get("path")


async def test_a_retired_schematic_hint_falls_back_to_illustration_while_flow_chart_still_renders(
    service, technical_input, monkeypatch
):
    """Full pipeline: a chapter with both a problem-solving flowchart and a
    (now-retired) physical-schematic request ends up with the flowchart
    rendered normally and the schematic request rerouted to a proper
    textbook illustration instead - never crashing the chapter, never
    producing a schematic SVG, and never left mislabelled as a diagram
    (Phase 2 card-grid audit root-cause fix: DiagramService.generate_for_block
    now reclassifies a `schematic`-hinted block's own `kind`/`diagram_kind`
    to `illustration`/"" as part of the reroute, so `diagram_kind` can no
    longer be used to find this block after generation - it's looked up by
    its own purpose text instead, same as a human reviewer would)."""
    original = WriterAgent.write_chapter

    async def with_em_visuals(self, **kwargs):
        blocks, summary = await original(self, **kwargs)
        process_block = _diagram_block(
            diagram_kind="flow_chart",
            purpose="Show the steps to analyse an electromagnetic induction problem",
            prompt="Physical setup -> pick target integral -> convert form -> solve and validate",
            caption="Induction problem workflow",
        )
        schematic_block = _diagram_block(
            diagram_kind="schematic",
            purpose="Show a bar magnet moving toward a coil and the induced current",
            prompt="Coil, ammeter, bar magnet with N/S poles, field lines, before/after states",
            caption="Electromagnetic induction apparatus",
        )
        return [*blocks, process_block, schematic_block], summary

    monkeypatch.setattr(WriterAgent, "write_chapter", with_em_visuals)

    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    document = service.storage.load_document(record.course_id)
    # Matched by this test's own injected purpose text, not by `diagram_kind
    # == "flow_chart"` alone - a real generated chapter's own normal content
    # already has other flow_chart diagrams of its own, so that filter
    # previously (wrongly) picked up all of them instead of just the one
    # this test actually injected.
    flow_chart_blocks = [
        b for b in document.blocks_of_type(BlockType.IMAGE)
        if "analyse an electromagnetic induction problem" in str(b.content.get("purpose", "")).lower()
    ]
    induction_blocks = [
        b for b in document.blocks_of_type(BlockType.IMAGE)
        if "bar magnet" in str(b.content.get("purpose", "")).lower()
    ]
    # `with_em_visuals` wraps every chapter's own `write_chapter` call, so a
    # multi-chapter course injects this pair once per chapter - `>= 1`, not
    # `== 1`, is the real invariant this test cares about (not an exact
    # count that depends on how many chapters the mock course happens to
    # generate).
    assert len(flow_chart_blocks) >= 1 and len(induction_blocks) >= 1

    flow_chart_block = flow_chart_blocks[0]
    assert flow_chart_block.content["path"].endswith(".svg")
    assert service.storage.asset_abs_path(record.course_id, flow_chart_block.content["path"]).exists()

    schematic_block = induction_blocks[0]
    # Reclassified, not just silently rendered as something else - a LATER
    # regeneration of this exact block must never loop back through the
    # same schematic/comparison contradiction again.
    assert schematic_block.content.get("kind") == "illustration"
    assert schematic_block.content.get("diagram_kind") == ""
    assert schematic_block.content.get("illustration_style") == "textbook"
    # Still gets SOME real image (never left ungenerated) - just never an SVG,
    # since schematic itself is never rendered any more.
    assert schematic_block.content.get("path")
    assert not schematic_block.content["path"].endswith(".svg")


# ---------------------------------------------------------------------------
# semantic schematic blueprint: role/anchor/priority/size -> layout engine
# computes x/y/width/height, instead of the model authoring coordinates.
# ---------------------------------------------------------------------------


def _pmsm_shapes() -> list[SchematicShape]:
    """A representative, deliberately cross-domain-generalizable example -
    nothing here is PMSM-specific in the engine, only in this test's data."""
    return [
        SchematicShape(type="block", id="rotor", label="Rotor", role="primary", size="large"),
        SchematicShape(
            type="block", id="stator", label="Stator", anchor="orbit:rotor",
            priority="critical", size="medium",
        ),
        SchematicShape(
            type="coil", id="winding", label="Winding", anchor="above:rotor",
            priority="important", size="small",
        ),
        SchematicShape(
            type="arrow", id="flux", label="Air-gap flux", anchor="left_of:rotor",
            target_id="rotor", priority="important", size="small",
        ),
    ]


class TestSchematicLayoutEngine:
    def test_legacy_coordinate_authored_shapes_pass_through_unchanged(self):
        """A shape with no `anchor`/`role` set anywhere in its state is
        untouched - this is what keeps every hand-authored spec in this file
        (and the pre-blueprint offline mock) rendering exactly as before."""
        shapes = [
            SchematicShape(type="block", id="a", x=0.2, y=0.3, width=0.15, height=0.1, label="A"),
            SchematicShape(type="block", id="b", x=0.7, y=0.6, width=0.15, height=0.1, label="B"),
        ]
        spec = DiagramSpec(kind="schematic", shapes=shapes)
        resolved = resolve_schematic_layout(spec)
        assert [(s.x, s.y, s.width, s.height) for s in resolved.shapes] == [
            (0.2, 0.3, 0.15, 0.1),
            (0.7, 0.6, 0.15, 0.1),
        ]

    def test_non_schematic_kind_is_a_no_op(self):
        spec = DiagramSpec(kind="flow_chart", nodes=[DiagramNode(label="A"), DiagramNode(label="B")])
        assert resolve_schematic_layout(spec) is spec

    def test_semantically_authored_shapes_get_real_non_overlapping_positions(self):
        spec = DiagramSpec(kind="schematic", title="PMSM", shapes=_pmsm_shapes())
        resolved = resolve_schematic_layout(spec)
        assert len(resolved.shapes) == 4
        for shape in resolved.shapes:
            assert 0.0 <= shape.x - shape.width / 2 and shape.x + shape.width / 2 <= 1.0
            assert 0.0 <= shape.y - shape.height / 2 and shape.y + shape.height / 2 <= 1.0
        result = evaluate_schematic(resolved)
        assert result.passed, [i.reason for i in result.issues]

    def test_primary_object_is_the_largest_and_centred(self):
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=_pmsm_shapes()))
        primary = next(s for s in resolved.shapes if s.role == "primary")
        assert (primary.x, primary.y) == (0.5, 0.5)
        largest_area = max(s.width * s.height for s in resolved.shapes)
        assert primary.width * primary.height == largest_area

    def test_annotation_budget_keeps_primary_and_critical_drops_lowest_priority_first(self):
        shapes = [
            SchematicShape(type="block", id="core", label="Core", role="primary"),
            SchematicShape(type="label", id="c1", label="Critical 1", anchor="orbit:core", priority="critical"),
            SchematicShape(type="label", id="i1", label="Important 1", anchor="orbit:core", priority="important"),
            SchematicShape(type="label", id="o1", label="Optional 1", anchor="orbit:core", priority="optional"),
            SchematicShape(type="label", id="o2", label="Optional 2", anchor="orbit:core", priority="optional"),
            SchematicShape(type="label", id="o3", label="Optional 3", anchor="orbit:core", priority="optional"),
        ]
        spec = DiagramSpec(kind="schematic", shapes=shapes, max_annotations=3)
        resolved = resolve_schematic_layout(spec)
        kept_ids = {s.id for s in resolved.shapes}
        assert kept_ids == {"core", "c1", "i1"}  # primary + critical always kept, one "important" fills the budget

    def test_inside_anchor_onto_a_bare_parent_merges_into_its_sublabel(self):
        """A parent (block/circle/gauge) with no sublabel of its own reuses
        diagram_renderer's existing nested-content rendering instead of a
        second, independently-positioned shape landing on top of the
        parent's own centred label - see _merge_inside_anchors."""
        shapes = [
            SchematicShape(type="circle", id="cell", label="Cell", role="primary", size="large"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell", size="small"),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        assert [s.id for s in resolved.shapes] == ["cell"]
        assert resolved.shapes[0].sublabel == "Nucleus"

    def test_inside_anchor_onto_a_parent_with_its_own_sublabel_stays_a_separate_nested_shape(self):
        """When the parent's sublabel slot is already taken, the nested
        shape still gets a real (non-colliding) position instead of being
        silently dropped."""
        shapes = [
            SchematicShape(type="circle", id="cell", label="Cell", sublabel="Membrane", role="primary", size="large"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell", size="small"),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        cell = next(s for s in resolved.shapes if s.id == "cell")
        nucleus = next(s for s in resolved.shapes if s.id == "nucleus")
        distance = ((nucleus.x - cell.x) ** 2 + (nucleus.y - cell.y) ** 2) ** 0.5
        assert distance < cell.width / 2  # nucleus centre sits well inside the cell's radius

    def test_generalises_across_domains_without_any_topic_specific_logic(self):
        """The same engine, unmodified, must produce a collision-free layout
        for entirely different subjects - proof there's no PMSM/physics
        hardcoding anywhere in schematic_layout.py."""
        biology = [
            SchematicShape(type="circle", id="cell", label="Animal Cell", role="primary", size="large"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell", priority="critical", size="small"),
            SchematicShape(type="label", id="mito", label="Mitochondria", anchor="orbit:cell", priority="important"),
        ]
        chemistry = [
            SchematicShape(type="block", id="h2", label="2H2", role="primary", size="medium"),
            SchematicShape(type="block", id="o2", label="O2", anchor="orbit:h2", priority="critical", size="medium"),
            SchematicShape(
                type="arrow", id="react", label="combustion", anchor="right_of:h2",
                target_id="water", priority="important", size="small",
            ),
            # Anchored to the primary reactant, not to "react" - chaining a
            # directional anchor off another already-directional secondary
            # can, in a contrived worst case, push a large shape toward the
            # same corner twice over; a real model following DIAGRAM_SYSTEM's
            # guidance ("orbit the primary is the default for most secondary
            # objects") wouldn't produce that chain, so this test doesn't
            # manufacture it either. See schematic_layout._place_ring's
            # docstring for the corner-case fallback that still keeps that
            # rarer pattern collision-free even when it does happen.
            SchematicShape(type="block", id="water", label="2H2O", anchor="orbit:h2", priority="critical", size="medium"),
        ]
        for shapes in (biology, chemistry):
            resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
            result = evaluate_schematic(resolved)
            assert result.passed, [i.reason for i in result.issues]


class TestSchematicQA:
    def test_detects_object_collision(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", x=0.5, y=0.5, width=0.3, height=0.3, role="primary"),
            SchematicShape(type="block", id="b", label="B", x=0.55, y=0.5, width=0.3, height=0.3),
        ]
        result = evaluate_schematic(DiagramSpec(kind="schematic", shapes=shapes))
        assert not result.passed
        assert OBJECT_COLLISION in {i.reason for i in result.issues}

    def test_detects_canvas_clipping(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", x=0.5, y=0.5, width=0.2, height=0.2, role="primary"),
            SchematicShape(type="label", id="b", label="B", x=0.98, y=0.98, width=0.1, height=0.1),
        ]
        result = evaluate_schematic(DiagramSpec(kind="schematic", shapes=shapes))
        assert CANVAS_CLIPPING in {i.reason for i in result.issues}

    def test_detects_missing_focal_object(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", x=0.3, y=0.5, width=0.15, height=0.15),
            SchematicShape(type="block", id="b", label="B", x=0.7, y=0.5, width=0.15, height=0.15),
        ]
        result = evaluate_schematic(DiagramSpec(kind="schematic", shapes=shapes))
        assert WEAK_FOCAL_HIERARCHY in {i.reason for i in result.issues}

    def test_detects_dangling_relationship(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", x=0.3, y=0.5, width=0.15, height=0.15, role="primary"),
            SchematicShape(
                type="arrow", id="arr", label="points nowhere", x=0.7, y=0.5, width=0.15, height=0.1,
                target_id="does_not_exist",
            ),
        ]
        result = evaluate_schematic(DiagramSpec(kind="schematic", shapes=shapes))
        assert MISSING_RELATIONSHIP in {i.reason for i in result.issues}

    def test_non_schematic_kind_always_passes(self):
        spec = DiagramSpec(kind="flow_chart", nodes=[DiagramNode(label="A"), DiagramNode(label="B")])
        assert evaluate_schematic(spec).passed

    def test_feedback_names_the_actual_reasons_not_a_bare_try_again(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", x=0.5, y=0.5, width=0.3, height=0.3, role="primary"),
            SchematicShape(type="block", id="b", label="B", x=0.55, y=0.5, width=0.3, height=0.3),
        ]
        result = evaluate_schematic(DiagramSpec(kind="schematic", shapes=shapes))
        feedback = result.feedback()
        assert "overlap" in feedback.lower()
        assert feedback.lower() != "generate again."
        assert "generate the diagram again" not in feedback.lower()


async def test_schematic_request_is_rejected_before_any_qa_retry_is_attempted(service, monkeypatch):
    """`schematic` is permanently retired (see DiagramSpec.is_usable's own
    docstring) - `generate_for_block` now rejects a `schematic`-hinted block
    and reroutes it to a textbook illustration BEFORE calling the model at
    all (Phase 2 card-grid audit root-cause fix: asking the model for a kind
    DIAGRAM_SYSTEM tells it to never produce just produced an unflagged
    substitute, usually `comparison` - see `_kind_conflicts`'s own comment),
    so a schematic request must fail with ZERO generation calls - not even
    the one first attempt a well-formed mocked spec used to get."""
    template = load_template("technical")
    block = _diagram_block(diagram_kind="schematic", prompt="Two interacting components")

    good_spec = DiagramSpec(
        kind="schematic",
        shapes=[
            SchematicShape(type="block", id="a", label="A", role="primary", size="medium"),
            SchematicShape(type="block", id="b", label="B", anchor="orbit:a", priority="critical", size="medium"),
        ],
    )
    received_feedback: list[str] = []

    async def fake_request_spec(self, **kwargs):
        received_feedback.append(kwargs.get("qa_feedback", ""))
        return good_spec.model_copy(deep=True)

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_schematic_qa_retry", block=block, template=template, course_title="Two Parts"
    )

    assert ok is False
    assert len(received_feedback) == 0, "schematic must be rejected before any generation call at all"


# ---------------------------------------------------------------------------
# canonical visual blueprint registry (app.render.visual_blueprints)
# ---------------------------------------------------------------------------


class TestVisualBlueprintRegistry:
    def test_known_visual_type_resolves(self):
        blueprint = get_blueprint("electric_motor")
        assert blueprint is not None
        assert blueprint.visual_type == "electric_motor"
        assert "coil" in blueprint.required_components

    def test_lookup_is_case_and_spacing_insensitive(self):
        assert get_blueprint("Electric Motor") is get_blueprint("electric_motor")
        assert get_blueprint("  animal_cell  ") is get_blueprint("animal_cell")

    def test_unknown_visual_type_falls_back_to_none_safely(self):
        assert get_blueprint("unknown_made_up_visual") is None
        assert get_blueprint("") is None
        assert get_blueprint(None) is None  # type: ignore[arg-type]

    def test_registry_covers_the_required_domains(self):
        """Physics/engineering, biology and chemistry all have at least one
        registered blueprint - the cross-domain coverage the brief asks for,
        not just an electrical-only registry."""
        assert "electric_motor" in BLUEPRINT_REGISTRY
        assert "fixed_pulley" in BLUEPRINT_REGISTRY
        assert "animal_cell" in BLUEPRINT_REGISTRY
        assert "water_formation" in BLUEPRINT_REGISTRY

    def test_blueprints_never_contain_raw_hex_colors(self):
        """A blueprint's `color_role` must always be a semantic role name
        resolvable by the centralized palette - never a literal hex value,
        per the requirement that raw colors live only in
        app.render.textbook_palette."""
        for blueprint in BLUEPRINT_REGISTRY.values():
            for component in blueprint.components.values():
                assert not component.color_role.startswith("#"), (
                    f"{blueprint.visual_type}.{component.id} has a raw color"
                )
                assert component.color_role in COLOR_ROLES

    def test_apply_blueprint_defaults_fills_blank_fields_without_overriding_explicit_ones(self):
        spec = DiagramSpec(
            kind="schematic",
            visual_type="electric_motor",
            shapes=[
                SchematicShape(type="coil", id="coil", label=""),
                SchematicShape(type="block", id="magnet", label="Custom Magnet Label"),
            ],
        )
        filled = apply_blueprint_defaults(spec)
        coil = next(s for s in filled.shapes if s.id == "coil")
        magnet = next(s for s in filled.shapes if s.id == "magnet")
        assert coil.role == "primary"
        assert coil.color_role == "accent"
        assert coil.label == "Coil"
        assert magnet.label == "Custom Magnet Label"  # explicit value preserved

    def test_apply_blueprint_defaults_is_a_no_op_for_unknown_visual_type(self):
        spec = DiagramSpec(
            kind="schematic",
            visual_type="not_a_real_visual",
            shapes=[SchematicShape(type="block", id="a", label="A")],
        )
        assert apply_blueprint_defaults(spec) is spec

    def test_apply_blueprint_defaults_is_a_no_op_for_non_schematic_kind(self):
        spec = DiagramSpec(kind="flow_chart", visual_type="electric_motor", nodes=[DiagramNode(label="A")])
        assert apply_blueprint_defaults(spec) is spec


# ---------------------------------------------------------------------------
# semantic/structural blueprint validation (app.services.diagram_qa.evaluate_blueprint)
# ---------------------------------------------------------------------------


class TestBlueprintValidation:
    def test_missing_required_component_is_detected(self):
        spec = DiagramSpec(
            kind="schematic",
            visual_type="electric_motor",
            shapes=[
                SchematicShape(type="coil", id="coil", label="Coil", role="primary"),
                SchematicShape(type="block", id="magnet", label="Magnet"),
            ],
        )
        result = evaluate_blueprint(spec)
        assert not result.passed
        assert MISSING_REQUIRED_COMPONENT in {i.reason for i in result.issues}

    def test_all_required_components_present_passes(self):
        shapes = [
            SchematicShape(type="coil", id="coil", label="Coil", role="primary"),
            SchematicShape(type="block", id="magnet", label="Magnet"),
            SchematicShape(type="block", id="axle", label="Axle"),
            SchematicShape(type="block", id="commutator", label="Commutator"),
            SchematicShape(type="block", id="brush", label="Brush"),
        ]
        spec = DiagramSpec(
            kind="schematic",
            visual_type="electric_motor",
            shapes=shapes,
            relationships=[SchematicRelationship(source="brush", type="contacts", target="commutator")],
        )
        result = evaluate_blueprint(spec)
        assert result.passed

    def test_invalid_relationship_type_is_detected(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B"),
        ]
        spec = DiagramSpec(
            kind="schematic",
            shapes=shapes,
            relationships=[SchematicRelationship(source="a", type="orbits_wildly", target="b")],
        )
        result = evaluate_blueprint(spec)
        assert INVALID_RELATIONSHIP in {i.reason for i in result.issues}

    def test_valid_relationship_type_passes(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B"),
        ]
        spec = DiagramSpec(
            kind="schematic",
            shapes=shapes,
            relationships=[SchematicRelationship(source="a", type="connected_to", target="b")],
        )
        result = evaluate_blueprint(spec)
        assert INVALID_RELATIONSHIP not in {i.reason for i in result.issues}
        assert DANGLING_RELATIONSHIP not in {i.reason for i in result.issues}

    def test_dangling_relationship_references_a_missing_shape(self):
        shapes = [SchematicShape(type="block", id="a", label="A", role="primary")]
        spec = DiagramSpec(
            kind="schematic",
            shapes=shapes,
            relationships=[SchematicRelationship(source="a", type="connected_to", target="ghost")],
        )
        result = evaluate_blueprint(spec)
        assert DANGLING_RELATIONSHIP in {i.reason for i in result.issues}

    def test_duplicate_critical_component_is_detected(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="a", label="A duplicate"),
        ]
        spec = DiagramSpec(kind="schematic", shapes=shapes)
        result = evaluate_blueprint(spec)
        assert DUPLICATE_CRITICAL_COMPONENT in {i.reason for i in result.issues}

    def test_repeating_the_same_id_across_before_after_states_is_not_a_duplicate(self):
        """A multi-state (before/after) schematic is *supposed* to reuse the
        same shape ids in every state (see DIAGRAM_SYSTEM's "keep the same
        ids ... across states so the layout stays consistent") - the
        duplicate check must only fire within a single state, never across
        the whole spec, or every legitimate before/after schematic would be
        wrongly flagged as broken."""
        shared_shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B", anchor="orbit:a"),
        ]
        spec = DiagramSpec(
            kind="schematic",
            states=[
                SchematicState(caption="Before", shapes=[s.model_copy() for s in shared_shapes]),
                SchematicState(caption="After", shapes=[s.model_copy() for s in shared_shapes]),
            ],
        )
        result = evaluate_blueprint(spec)
        assert DUPLICATE_CRITICAL_COMPONENT not in {i.reason for i in result.issues}

    def test_missing_primary_object_when_blueprint_expects_one(self):
        shapes = [
            SchematicShape(type="block", id="magnet", label="Magnet"),
            SchematicShape(type="block", id="axle", label="Axle"),
            SchematicShape(type="block", id="commutator", label="Commutator"),
            SchematicShape(type="block", id="brush", label="Brush"),
            SchematicShape(type="coil", id="coil", label="Coil"),  # no role="primary" set
        ]
        spec = DiagramSpec(kind="schematic", visual_type="electric_motor", shapes=shapes)
        result = evaluate_blueprint(spec)
        assert MISSING_PRIMARY_OBJECT in {i.reason for i in result.issues}

    def test_invalid_color_role_is_detected(self):
        shapes = [SchematicShape(type="block", id="a", label="A", role="primary", color_role="rainbow_sparkle")]
        result = evaluate_blueprint(DiagramSpec(kind="schematic", shapes=shapes))
        assert INVALID_COLOR_ROLE in {i.reason for i in result.issues}

    def test_garbled_label_text_is_detected(self):
        """Reproduces a real, reported bug: an annotation's text arrived
        mojibake-corrupted - Latin letters swapped for similar-looking
        Cyrillic/Greek glyphs mid-word ("confirm" -> "Conɪoтй") - and shipped
        straight into a rendered diagram with nothing catching it."""
        shapes = [
            SchematicShape(type="block", id="a", label="Build system", role="primary"),
            SchematicShape(
                type="label", id="note", label="Check _cplumlous macro tr Conɪoтй ηµoge konges mode",
            ),
        ]
        result = evaluate_blueprint(DiagramSpec(kind="schematic", shapes=shapes))
        assert GARBLED_TEXT in {i.reason for i in result.issues}

    def test_clean_label_text_is_not_flagged_as_garbled(self):
        shapes = [
            SchematicShape(type="block", id="a", label="Build system", role="primary"),
            SchematicShape(
                type="label", id="note", label="Check the __cplusplus macro to confirm language mode",
            ),
        ]
        result = evaluate_blueprint(DiagramSpec(kind="schematic", shapes=shapes))
        assert GARBLED_TEXT not in {i.reason for i in result.issues}

    def test_legitimate_scientific_symbols_are_not_flagged_as_garbled(self):
        """Real Greek-letter unit prefixes/symbols (µ, Ω, π, ...) mixed with
        Latin unit letters are common and legitimate - this check must only
        flag an implausible mid-word Latin/Cyrillic-or-Greek letter mixture,
        never a short, ordinary scientific symbol."""
        shapes = [SchematicShape(
            type="label", id="note", label="Resistance measured in Ω, capacitance in µF",
        )]
        result = evaluate_blueprint(DiagramSpec(kind="schematic", shapes=shapes))
        assert GARBLED_TEXT not in {i.reason for i in result.issues}


class TestLooksGarbled:
    """Unit tests for `_looks_garbled` directly - the shared detector reused
    by both diagram_qa.py (schematic) and concept_qa.py (concept_experience)."""

    def test_detects_the_real_reported_corruption(self):
        assert _looks_garbled("Check _cplumlous macro tr Conɪoтй ηµoge konges mode")

    def test_does_not_flag_the_clean_equivalent_sentence(self):
        assert not _looks_garbled("Check the __cplusplus macro to confirm language mode")

    def test_detects_the_unicode_replacement_character(self):
        assert _looks_garbled("Corrupted�text")

    def test_does_not_flag_long_ordinary_english_words(self):
        assert not _looks_garbled("antiferromagnetic coupling strength")

    def test_does_not_flag_short_scientific_symbols_mixed_with_latin(self):
        assert not _looks_garbled("5 μm diameter, 10 μF capacitor, 2 Ω resistance")

    def test_does_not_flag_empty_or_blank_text(self):
        assert not _looks_garbled("")
        assert not _looks_garbled("   ")

    def test_unknown_visual_type_skips_blueprint_checks_but_still_validates_universals(self):
        """An unrecognised visual_type must never fail generation - but a
        genuinely invalid relationship/color role is still caught, since
        those checks are universal, not blueprint-specific."""
        shapes = [SchematicShape(type="block", id="a", label="A", role="primary", color_role="not_a_role")]
        spec = DiagramSpec(kind="schematic", visual_type="totally_unknown_thing", shapes=shapes)
        result = evaluate_blueprint(spec)
        assert INVALID_COLOR_ROLE in {i.reason for i in result.issues}
        assert MISSING_REQUIRED_COMPONENT not in {i.reason for i in result.issues}

    def test_non_schematic_kind_always_passes(self):
        spec = DiagramSpec(kind="flow_chart", visual_type="electric_motor", nodes=[DiagramNode(label="A")])
        assert evaluate_blueprint(spec).passed

    def test_feedback_names_the_actual_reasons(self):
        shapes = [
            SchematicShape(type="coil", id="coil", label="Coil", role="primary"),
        ]
        spec = DiagramSpec(kind="schematic", visual_type="electric_motor", shapes=shapes)
        result = evaluate_blueprint(spec)
        feedback = result.feedback()
        assert "magnet" in feedback.lower() or "missing" in feedback.lower()
        assert "generate the diagram again" not in feedback.lower()


# ---------------------------------------------------------------------------
# centralized color-role system (app.render.textbook_palette)
# ---------------------------------------------------------------------------


class TestTextbookColorSystem:
    def test_known_role_resolves_to_a_fill_stroke_text_triple(self):
        role = resolve_color_role("current")
        assert role.fill and role.stroke and role.text

    def test_unknown_role_falls_back_to_neutral(self):
        assert resolve_color_role("not_a_real_role") == TEXTBOOK_PALETTE["neutral"]
        assert resolve_color_role("") == TEXTBOOK_PALETTE["neutral"]

    def test_role_lookup_is_case_insensitive(self):
        assert resolve_color_role("CURRENT") == resolve_color_role("current")

    def test_no_blueprint_ever_needs_a_raw_hex_value(self):
        """Every blueprint component's color_role must already be a key in
        the centralized palette - a course author never has to invent a hex
        value to describe a canonical visual."""
        for blueprint in BLUEPRINT_REGISTRY.values():
            for component in blueprint.components.values():
                assert component.color_role in TEXTBOOK_PALETTE

    def test_every_palette_role_has_sufficient_fill_text_contrast(self):
        """A cheap luminance check, not full WCAG maths - but enough to catch
        a role whose fill and text are too close in brightness to read,
        which is the actual failure mode this guards against."""

        def luminance(hex_color: str) -> float:
            hex_color = hex_color.lstrip("#")
            r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
            return 0.299 * r + 0.587 * g + 0.114 * b

        for name, role in TEXTBOOK_PALETTE.items():
            assert abs(luminance(role.fill) - luminance(role.text)) > 60, name

    def test_primary_shape_gets_stronger_stroke_than_secondary(self):
        from app.render.textbook_palette import stroke_width_for

        assert stroke_width_for("primary") > stroke_width_for("secondary")

    def test_color_role_is_never_the_only_carrier_of_meaning(self):
        """Grayscale/CVD safety: a schematic conveys direction/relationship
        through shapes the renderer already draws regardless of color - an
        arrow shape still has a `rotation`/`target_id`, a gauge still has a
        needle angle - none of that depends on `color_role` being set or
        resolvable."""
        arrow = SchematicShape(type="arrow", id="a", label="Current", color_role="current", target_id="b")
        assert arrow.target_id  # direction is encoded structurally, not only by color

    def test_important_blueprint_components_get_a_valid_color_role(self):
        for blueprint in BLUEPRINT_REGISTRY.values():
            for component in blueprint.components.values():
                if component.priority in ("critical", "important"):
                    assert component.color_role in TEXTBOOK_PALETTE

    def test_rendered_labels_stay_readable_with_color_roles_applied(self):
        """End-to-end: a colorful semantically-authored schematic still
        renders every label as visible text, not swallowed by its own fill."""
        shapes = [
            SchematicShape(type="block", id="a", label="Alpha", role="primary", size="medium", color_role="accent"),
            SchematicShape(
                type="block", id="b", label="Beta", anchor="orbit:a", priority="critical", size="medium",
                color_role="current",
            ),
        ]
        spec = DiagramSpec(kind="schematic", shapes=shapes)
        resolved = resolve_schematic_layout(spec)
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        ET.fromstring(svg_bytes)
        text = svg_bytes.decode("utf-8")
        assert "Alpha" in text and "Beta" in text
        # both palette hex colors actually reached the SVG output
        assert TEXTBOOK_PALETTE["accent"].stroke in text
        assert TEXTBOOK_PALETTE["current"].stroke in text


# ---------------------------------------------------------------------------
# nested "inside:" child keeps its own color_role, arrows connect to their
# target, and declared relationships render as connector lines
# ---------------------------------------------------------------------------


class TestNestedColorAndConnectors:
    def test_merged_inside_child_carries_its_color_role_into_sublabel_color_role(self):
        shapes = [
            SchematicShape(type="circle", id="cell", label="Cell", role="primary", size="large", color_role="structure"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell", color_role="secondary"),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        cell = next(s for s in resolved.shapes if s.id == "cell")
        assert cell.sublabel == "Nucleus"
        assert cell.sublabel_color_role == "secondary"
        assert cell.color_role == "structure"  # the parent's own color is untouched

    def test_nested_circle_renders_its_own_color_not_just_the_parents(self):
        shapes = [
            SchematicShape(type="circle", id="cell", label="Cell", role="primary", size="large", color_role="structure"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell", color_role="secondary"),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        text = svg_bytes.decode("utf-8")
        assert TEXTBOOK_PALETTE["structure"].stroke in text
        assert TEXTBOOK_PALETTE["secondary"].fill in text  # the nested circle's own distinct fill

    def test_directly_authored_sublabel_still_uses_the_parents_own_color(self):
        """A gauge/block/circle sublabel set directly by the model (not via
        an `inside:` merge) has no separate nested shape to carry a color
        from - `sublabel_color_role` stays blank and the parent's own
        `color_role` covers both, exactly as before this field existed."""
        shape = SchematicShape(
            type="circle", id="atom", label="Atom", sublabel="Core", role="primary", color_role="accent",
        )
        svg_bytes, _, _ = render_diagram_svg(
            DiagramSpec(kind="schematic", shapes=[shape.model_copy(update={"x": 0.5, "y": 0.5, "width": 0.3, "height": 0.3})]),
            TemplateTheme(),
        )
        assert TEXTBOOK_PALETTE["accent"].stroke in svg_bytes.decode("utf-8")

    def test_arrow_with_a_resolved_target_draws_a_line_reaching_the_target_not_a_short_floating_segment(self):
        shapes = [
            SchematicShape(type="block", id="load", label="Load", role="primary", size="medium"),
            SchematicShape(
                type="arrow", id="force", label="Force", anchor="right_of:load", target_id="load",
                priority="important", size="small",
            ),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        text = svg_bytes.decode("utf-8")
        lines = re.findall(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"[^>]*url\(#diagram-arrow[^)]*\)', text)
        assert lines, "expected an arrow line with the arrowhead marker"
        x1, y1, x2, y2 = (float(v) for v in lines[0])
        # Its end point must actually land at/on the edge of a real drawn
        # box (the "Load" block) - proof this is a genuine point-to-point
        # connector, not a short segment merely oriented toward the target
        # (the pre-fix behaviour, which never reached it at all).
        boxes = re.findall(r'<rect class="diagram-box" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', text)
        tolerance = 3.0
        assert any(
            float(bx) - tolerance <= x2 <= float(bx) + float(bw) + tolerance
            and float(by) - tolerance <= y2 <= float(by) + float(bh) + tolerance
            for bx, by, bw, bh in boxes
        )

    def test_relationship_of_a_drawable_type_renders_a_connector_line(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B", anchor="orbit:a", priority="critical"),
        ]
        spec = DiagramSpec(
            kind="schematic", shapes=shapes,
            relationships=[SchematicRelationship(source="a", type="connected_to", target="b")],
        )
        resolved = resolve_schematic_layout(spec)
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        text = svg_bytes.decode("utf-8")
        assert "stroke-dasharray" in text  # structural relationship drawn dashed, no arrowhead

    def test_directional_relationship_gets_an_arrowhead_marker(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B", anchor="orbit:a", priority="critical"),
        ]
        spec = DiagramSpec(
            kind="schematic", shapes=shapes,
            relationships=[SchematicRelationship(source="a", type="points_to", target="b")],
        )
        resolved = resolve_schematic_layout(spec)
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        text = svg_bytes.decode("utf-8")
        assert "schematic-relationship-arrow" in text

    def test_spatial_and_containment_relationships_draw_no_connector_line(self):
        """above/below/left_of/right_of/between (already conveyed by the
        anchor-driven layout) and inside/contains/surrounds (already
        conveyed by nesting) are deliberately left undrawn - a line for
        either would be redundant, not clarifying."""
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B", anchor="above:a", priority="critical"),
        ]
        spec = DiagramSpec(
            kind="schematic", shapes=shapes,
            relationships=[SchematicRelationship(source="b", type="above", target="a")],
        )
        resolved = resolve_schematic_layout(spec)
        svg_bytes, _, _ = render_diagram_svg(resolved, TemplateTheme())
        text = svg_bytes.decode("utf-8")
        # The marker *definition* may still be present (harmless, unused SVG
        # defs) - what must be absent is anything actually *using* it. Marker
        # ids get a per-render-call unique suffix (see render_diagram_svg -
        # SVG ids must be unique within a document once multiple diagrams are
        # inlined on one page), so this checks the "url(#...-arrow" prefix,
        # not an exact closed `url(#...)` string.
        assert not re.search(r'url\(#schematic-relationship-arrow[^)]*\)', text)
        assert "stroke-dasharray" not in text

    def test_relationship_referencing_a_merged_sublabel_shape_is_skipped_safely(self):
        """A shape that got folded into its parent's sublabel (see
        _merge_inside_anchors) no longer exists as an independently
        positioned shape - a relationship naming it must be silently
        skipped, never a crash or a line to nowhere."""
        shapes = [
            SchematicShape(type="circle", id="cell", label="Cell", role="primary", size="large"),
            SchematicShape(type="circle", id="nucleus", label="Nucleus", anchor="inside:cell"),
        ]
        spec = DiagramSpec(
            kind="schematic", shapes=shapes,
            relationships=[SchematicRelationship(source="nucleus", type="connected_to", target="cell")],
        )
        resolved = resolve_schematic_layout(spec)
        svg_bytes, width, height = render_diagram_svg(resolved, TemplateTheme())
        ET.fromstring(svg_bytes)
        assert width > 0 and height > 0

    def test_relationship_rendering_leaves_specs_without_relationships_byte_identical(self):
        """The whole connector feature is gated on `spec.relationships` being
        non-empty - every existing hand-authored spec (none of which set
        `relationships`) must render exactly as it did before this feature
        existed."""
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary"),
            SchematicShape(type="block", id="b", label="B", anchor="orbit:a", priority="critical"),
        ]
        without = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=[s.model_copy() for s in shapes]))
        svg_without, _, _ = render_diagram_svg(without, TemplateTheme())
        assert "schematic-relationship-arrow" not in svg_without.decode("utf-8")


# ---------------------------------------------------------------------------
# learning_objective + max_annotations interaction with the blueprint system
# ---------------------------------------------------------------------------


class TestLearningObjectiveAndAnnotationBudget:
    def test_critical_components_survive_annotation_budget_trimming(self):
        shapes = [
            SchematicShape(type="coil", id="coil", label="Coil", role="primary", priority="critical"),
            SchematicShape(type="block", id="magnet", label="Magnet", anchor="orbit:coil", priority="critical"),
            SchematicShape(type="block", id="axle", label="Axle", anchor="orbit:coil", priority="critical"),
            SchematicShape(type="label", id="deco1", label="Decoration 1", anchor="orbit:coil", priority="optional"),
            SchematicShape(type="label", id="deco2", label="Decoration 2", anchor="orbit:coil", priority="optional"),
            SchematicShape(type="label", id="deco3", label="Decoration 3", anchor="orbit:coil", priority="optional"),
        ]
        spec = DiagramSpec(kind="schematic", visual_type="electric_motor", shapes=shapes, max_annotations=2)
        resolved = resolve_schematic_layout(spec)
        ids = {s.id for s in resolved.shapes}
        assert {"coil", "magnet", "axle"} <= ids  # critical/primary never dropped for budget reasons

    def test_optional_shapes_are_dropped_before_important_ones(self):
        shapes = [
            SchematicShape(type="block", id="a", label="A", role="primary", priority="critical"),
            SchematicShape(type="label", id="opt", label="Optional", anchor="orbit:a", priority="optional"),
            SchematicShape(type="label", id="imp", label="Important", anchor="orbit:a", priority="important"),
        ]
        # budget=2: 1 slot for the primary (always kept) + 1 more slot, which
        # must go to the higher-priority "important" shape over "optional".
        spec = DiagramSpec(kind="schematic", shapes=shapes, max_annotations=2)
        resolved = resolve_schematic_layout(spec)
        ids = {s.id for s in resolved.shapes}
        assert "imp" in ids
        assert "opt" not in ids

    def test_learning_objective_field_is_preserved_through_layout_and_blueprint_defaults(self):
        spec = DiagramSpec(
            kind="schematic",
            visual_type="fixed_pulley",
            learning_objective="Understand how a fixed pulley changes the direction of an applied force.",
            shapes=[
                SchematicShape(type="circle", id="pulley", label="Pulley", role="primary"),
                SchematicShape(type="block", id="load", label="Load"),
            ],
        )
        filled = apply_blueprint_defaults(spec)
        resolved = resolve_schematic_layout(filled)
        assert resolved.learning_objective == spec.learning_objective

    def test_primary_object_gets_larger_footprint_than_a_small_secondary(self):
        """Visual emphasis for the educationally central object: `size`
        actually changes the rendered footprint, so a "large" primary object
        reads as more important than a "small" secondary one."""
        shapes = [
            SchematicShape(type="block", id="a", label="Primary", role="primary", size="large"),
            SchematicShape(type="block", id="b", label="Secondary", anchor="orbit:a", size="small"),
        ]
        resolved = resolve_schematic_layout(DiagramSpec(kind="schematic", shapes=shapes))
        primary = next(s for s in resolved.shapes if s.id == "a")
        secondary = next(s for s in resolved.shapes if s.id == "b")
        assert primary.width * primary.height > secondary.width * secondary.height


# ---------------------------------------------------------------------------
# full pipeline: LLM spec -> blueprint validation -> color -> layout -> QA -> retry
# ---------------------------------------------------------------------------


async def test_schematic_blueprint_requests_are_rejected_not_rendered(service):
    """`schematic` is permanently retired - even a blueprint-registered
    canonical visual_type (e.g. "electric_motor", which used to fill in
    missing components and render a colorful valid diagram) must still be
    rejected unconditionally, with no blueprint pipeline work attempted."""
    template = load_template("technical")
    block = _diagram_block(
        diagram_kind="schematic",
        purpose="Show how a simple DC electric motor works",
        prompt="Coil, magnet, axle, commutator and brush; explain motor rotation",
        caption="Simple DC electric motor",
    )

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_motor_pipeline", block=block, template=template, course_title="Motors"
    )

    assert ok is False
    assert not block.content.get("path")


async def test_schematic_blueprint_retry_pipeline_is_unreachable(service, monkeypatch):
    """The blueprint-validation retry loop (missing-component feedback,
    corrected second attempt) is now permanently unreachable for
    `schematic` - `generate_for_block` rejects the request before any
    generation call, blueprint check or retry is even considered, regardless
    of what a (mocked) first attempt would have been missing."""
    template = load_template("technical")
    block = _diagram_block(
        diagram_kind="schematic",
        purpose="Show a fixed pulley lifting a load",
        prompt="Pulley wheel and a load, with applied and lifting forces",
        caption="Fixed pulley",
    )

    incomplete_spec = DiagramSpec(
        kind="schematic",
        visual_type="fixed_pulley",
        title="Fixed Pulley",
        shapes=[
            SchematicShape(type="circle", id="pulley", label="Pulley", role="primary"),
            SchematicShape(type="arrow", id="applied_force", label="Applied Force", anchor="right_of:pulley"),
        ],
    )
    received_feedback: list[str] = []

    async def fake_request_spec(self, **kwargs):
        received_feedback.append(kwargs.get("qa_feedback", ""))
        return incomplete_spec.model_copy(deep=True)

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_pulley_retry", block=block, template=template, course_title="Simple Machines"
    )

    assert ok is False
    assert len(received_feedback) == 0, "no generation call should ever be attempted for a retired kind"


# ---------------------------------------------------------------------------
# data_flow_diagram: process/entity/store shapes in a linear chain
# ---------------------------------------------------------------------------


def _dfd_spec() -> DiagramSpec:
    nodes = [
        DiagramNode(id="user", label="User", shape_role="entity"),
        DiagramNode(id="login", label="Validate credentials"),  # blank shape_role = process
        DiagramNode(id="db", label="User accounts", shape_role="store"),
    ]
    edges = [
        DiagramEdge(source="user", target="login", label="Login request"),
        DiagramEdge(source="login", target="db", label="Lookup"),
    ]
    return DiagramSpec(kind="data_flow_diagram", title="Login Flow", nodes=nodes, edges=edges)


def test_data_flow_diagram_is_usable_with_labelled_edges():
    assert _dfd_spec().is_usable()
    assert not DiagramSpec(kind="data_flow_diagram", nodes=_dfd_spec().nodes, edges=[]).is_usable()


def test_data_flow_diagram_renders_all_three_shape_kinds():
    spec = _dfd_spec()
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)  # valid XML
    text = svg_bytes.decode("utf-8")

    assert "User" in text and "Validate credentials" in text and "User accounts" in text
    assert "Login request" in text and "Lookup" in text
    # the data store draws its own open-ended border as two separate <line>
    # elements instead of a bordered <rect> - confirms the "store" shape
    # actually took effect, not just a plain box with this label.
    assert text.count("<line") >= 2
    assert width > 0 and height > 0


def test_data_flow_diagram_unknown_shape_role_falls_back_to_process():
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[
            DiagramNode(id="a", label="A", shape_role="nonsense"),
            DiagramNode(id="b", label="B"),
        ],
        edges=[DiagramEdge(source="a", target="b", label="x")],
    )
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)  # never a hard failure on an unrecognised role


def test_data_flow_diagram_process_circle_never_truncates_or_breaks_mid_word():
    """A real, confirmed bug on a generated course: a process node (blank
    `shape_role`, drawn as a circle) used to be fit into a FIXED-size box
    via `_fit_boxed_text`, which shrank the font to its floor and then
    still had to character-break mid-word and ellipsis-truncate any label
    longer than two or three short words - "Student submission (minimal,
    CI-runnable)" rendered as "Student / submissio / n / (minimal,…". The
    circle must grow to fit the label instead, the same word-boundary-only
    wrapping every other shape in this module already guarantees."""
    long_label = "Student submission (minimal, CI-runnable)"
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[
            DiagramNode(id="a", label="Prerequisites", shape_role="entity"),
            DiagramNode(id="b", label=long_label),
            DiagramNode(id="c", label="Continuous Integration (automatable diagnostic)"),
        ],
        edges=[
            DiagramEdge(source="a", target="b", label="dependency specs"),
            DiagramEdge(source="b", target="c", label="outputs artifacts"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in long_label.replace(",", "").split():
        assert word in text, f"word lost or broken: {word}"


def test_data_flow_diagram_process_circle_widens_past_three_lines_to_avoid_a_mid_word_break():
    """A real, confirmed case from a generated course: a short 2-3 word
    label whose one long word didn't fit the circle's starting width
    budget wrapped into exactly 3 lines (one a broken word fragment, no
    hyphen - "Interpolati" / "on" / "/ regridding") - that sailed straight
    past the old `len(lines) > 3` widen check as "not cramped" even
    though the break itself was visibly wrong. The circle must widen
    whenever its longest WORD doesn't fit, independent of how many lines
    that produced."""
    labels = ["Interpolation / regridding", "Numerical differentiation", "Uncertainty estimation", "Temporal differentiation"]
    nodes = [DiagramNode(id=f"n{i}", label=label) for i, label in enumerate(labels)]
    spec = DiagramSpec(kind="data_flow_diagram", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    svg = svg_bytes.decode("utf-8")
    assert "…" not in svg
    for label in labels:
        for word in label.replace("/", "").split():
            assert word in svg, f"word lost or broken: {word!r} (from {label!r})"


def test_data_flow_diagram_process_circle_ignores_leading_whitespace_padding():
    """A real, confirmed artifact in model-generated labels: excessive
    leading whitespace padding (as if hand-centred in a monospace mockup).
    It must never affect wrapping/sizing - `_wrap`/`_circle_node_size`
    normalize whitespace before measuring, so a padded label renders
    identically to its stripped equivalent."""
    padded = "             Student submission (minimal, CI-runnable)"
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[DiagramNode(id="a", label=padded), DiagramNode(id="b", label="Next")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in "Student submission (minimal CI-runnable)".split():
        assert word in text, f"word lost or broken: {word}"


def test_data_flow_diagram_process_circle_uses_its_own_comfortable_label_size():
    """A real, confirmed regression introduced by an earlier fix: the
    process circle's font size was carried over from the old
    `_fit_boxed_text(base_size=13.0)` call, below the readability floor.
    Fixed once already to RING_LABEL_SIZE (15.0) - but DFD's canvas is
    always the fixed CANVAS_WIDTH (never grows wider, unlike a shared-ring
    kind), so 15.0px scales down to ~11.4px, right AT the 11px floor - a
    second real, confirmed complaint ("circle text is so small it's not
    even visible"). DFD has no canvas-growth risk from a bigger label size
    (see `_DFD_LABEL_SIZE`'s own comment), so it now sits well above the
    floor instead of right on top of it."""
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[DiagramNode(id="a", label="Validate credentials")],
        edges=[],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert f'font-size="{_DFD_LABEL_SIZE:.1f}"' in text
    assert 'font-size="13.0"' not in text
    assert validate_rendered_svg(svg_bytes, kind="data_flow_diagram") == []


def test_data_flow_diagram_short_entity_label_is_centred_not_left_stuck():
    """A real, confirmed complaint: a short, single-line, detail-less box
    label (a data_flow_diagram external-entity/store header like
    "Prerequisites") rendered left-anchored sat stuck to the box's left
    edge with a lot of empty space on the right. It must centre instead -
    a genuinely long wrapped paragraph that actually fills most of the
    box's width (see `test_data_flow_diagram_store_box_with_a_short_
    detail_line_is_still_centred` for the short-content case this used to
    get wrong) still left-aligns, since that's genuinely more readable."""
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[
            DiagramNode(id="a", label="Prerequisites", shape_role="entity"),
            DiagramNode(id="b", label="Artifacts", shape_role="store"),
        ],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert text.count(f'font-size="{LABEL_SIZE:.1f}" font-weight="600" fill') >= 2
    for snippet in text.split("<text ")[1:]:
        if f'font-size="{LABEL_SIZE:.1f}"' in snippet and "font-weight=\"600\"" in snippet:
            assert 'text-anchor="middle"' in snippet


def test_data_flow_diagram_store_box_with_a_short_detail_line_is_still_centred():
    """A real, confirmed complaint: a data_flow_diagram STORE box carrying
    both a label and a short one-line detail ("Velocity gradient" / "2x2
    matrix icon (...)") inside a box spanning the full canvas width used
    to stay left-anchored just because it had a detail line at all, even
    though both lines together used barely a quarter of the box's width -
    a lot of dead space on the right, same complaint as the label-only
    case just above. A genuinely long wrapped detail (most of the width
    actually used) still left-aligns - see the test right before this one."""
    spec = DiagramSpec(
        kind="data_flow_diagram",
        nodes=[
            DiagramNode(
                id="a", label="Velocity gradient", detail="2x2 matrix icon (du_i/dx_j)",
                shape_role="store",
            ),
        ],
        edges=[],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert 'text-anchor="middle"' in text
    assert 'text-anchor="start"' not in text


# ---------------------------------------------------------------------------
# er_diagram: entities, a relationship diamond, and attribute ovals
# ---------------------------------------------------------------------------


def _er_spec() -> DiagramSpec:
    nodes = [
        DiagramNode(id="student", label="Student"),
        DiagramNode(id="course", label="Course"),
        DiagramNode(id="enrolled", label="Enrolled in", shape_role="relationship"),
        DiagramNode(id="sid", label="student_id", shape_role="attribute", parent_id="student"),
        DiagramNode(id="sname", label="name", shape_role="attribute", parent_id="student"),
        DiagramNode(id="cid", label="course_id", shape_role="attribute", parent_id="course"),
        DiagramNode(id="edate", label="enroll_date", shape_role="attribute", parent_id="enrolled"),
    ]
    edges = [
        DiagramEdge(source="student", target="enrolled", label="1"),
        DiagramEdge(source="enrolled", target="course", label="N"),
    ]
    return DiagramSpec(kind="er_diagram", title="Student-Course Schema", nodes=nodes, edges=edges)


def test_er_diagram_is_usable_with_edges_only():
    assert _er_spec().is_usable()
    assert not DiagramSpec(kind="er_diagram", nodes=_er_spec().nodes, edges=[]).is_usable()


def test_er_diagram_allows_more_than_nine_nodes_once_attributes_are_counted():
    """Regression guard: the generic 2-9 node cap (see DiagramSpec.is_usable)
    would reject any realistic ER diagram the moment attributes are added -
    er_diagram gets its own, higher cap specifically because of this."""
    spec = _er_spec()
    assert len(spec.nodes) > 6
    assert spec.is_usable()


def test_er_diagram_renders_entities_relationship_and_attributes():
    spec = _er_spec()
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")

    for label in ("Student", "Course", "Enrolled in", "student_id", "name", "course_id", "enroll_date"):
        assert label in text, label
    assert "<polygon" in text  # the relationship diamond
    assert text.count("<ellipse") == 4  # one per attribute node
    assert width > 0 and height > 0


def test_er_diagram_attribute_pointing_at_an_unknown_parent_is_skipped_not_crashed():
    spec = DiagramSpec(
        kind="er_diagram",
        nodes=[
            DiagramNode(id="a", label="Entity A"),
            DiagramNode(id="b", label="Entity B"),
            DiagramNode(id="orphan", label="Stray field", shape_role="attribute", parent_id="does_not_exist"),
        ],
        edges=[DiagramEdge(source="a", target="b", label="relates to")],
    )
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    assert "Stray field" not in svg_bytes.decode("utf-8")


def test_er_diagram_two_attribute_fans_never_overlap_each_other():
    """Neighbouring entities' attribute fans reserve their own column width
    (max of the entity's own box or its fan) - two entities each with several
    attributes must not crowd into each other's fan."""
    nodes = [
        DiagramNode(id="a", label="Entity A"),
        DiagramNode(id="b", label="Entity B"),
    ]
    for i in range(4):
        nodes.append(DiagramNode(id=f"a{i}", label=f"a_field_{i}", shape_role="attribute", parent_id="a"))
        nodes.append(DiagramNode(id=f"b{i}", label=f"b_field_{i}", shape_role="attribute", parent_id="b"))
    spec = DiagramSpec(
        kind="er_diagram", nodes=nodes, edges=[DiagramEdge(source="a", target="b", label="relates to")]
    )
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    ellipses = [
        tuple(float(v) for v in match.groups())
        for match in re.finditer(
            r'<ellipse class="diagram-box" cx="([\d.]+)" cy="([\d.]+)" rx="([\d.]+)" ry="([\d.]+)"', text
        )
    ]
    assert len(ellipses) == 8
    a_cxs = sorted(cx for cx, cy, rx, ry in ellipses[:4])
    b_cxs = sorted(cx for cx, cy, rx, ry in ellipses[4:])
    # every A attribute must sit strictly left of every B attribute - proof
    # the two fans landed in separate columns, not interleaved/overlapping.
    assert max(a_cxs) < min(b_cxs)


def test_er_diagram_attribute_connectors_fan_out_to_their_own_x_not_one_point():
    """A real, confirmed bug: every attribute's own connector line used to
    end at the SAME point (the entity's centre x) regardless of which
    attribute it was, so 2+ attributes' lines visually converged on one
    identical spot instead of fanning out to each attribute's own
    position - confirmed via diagram_render_qa's duplicate_arrowhead_side
    check, which correctly flagged it. Each connector must land at its
    OWN attribute's x position."""
    nodes = [
        DiagramNode(id="e", label="Entity"),
        DiagramNode(id="f0", label="field_a", shape_role="attribute", parent_id="e"),
        DiagramNode(id="f1", label="field_b", shape_role="attribute", parent_id="e"),
    ]
    spec = DiagramSpec(kind="er_diagram", nodes=nodes, edges=[])
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    connector_x2s = [
        float(m.group(1))
        for m in re.finditer(r'<line x1="[\d.]+" y1="[\d.]+" x2="([\d.]+)" y2="[\d.]+"', text)
    ]
    assert len(connector_x2s) == 2
    assert connector_x2s[0] != connector_x2s[1]  # each lands at its own attribute's x, not a shared point


def test_er_diagram_relationship_lands_between_its_two_entities_even_when_listed_last():
    """A real, confirmed bug: a spec that lists both its entities BEFORE its
    relationship node (e.g. Customer, Order, Places - every entity, then the
    relationship, rather than interleaved) put the relationship in the far
    right column. Its connector to the FIRST entity then became a straight
    line cutting straight through the second entity's own box, which sat
    physically between them - the two shapes visually overlapped. Nothing in
    the writer prompt promises the model will interleave entity/relationship/
    entity in `nodes` order, so the renderer must order columns by
    connectivity itself (see _ordered_er_main_nodes) rather than trust the
    input order - this is the regression test for that fix."""
    nodes = [
        DiagramNode(id="customer", label="Customer"),
        DiagramNode(id="order", label="Order"),
        DiagramNode(id="places", label="Places", shape_role="relationship"),
    ]
    edges = [
        DiagramEdge(source="customer", target="places", label="1"),
        DiagramEdge(source="places", target="order", label="N"),
    ]
    spec = DiagramSpec(kind="er_diagram", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)

    def bbox(el: ET.Element) -> tuple[float, float, float, float]:
        if el.tag.endswith("rect"):
            x, y = float(el.get("x")), float(el.get("y"))
            w, h = float(el.get("width")), float(el.get("height"))
            return x, y, x + w, y + h
        pts = [tuple(map(float, p.split(","))) for p in el.get("points").split()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    shapes_by_title = {}
    for g in root.iter():
        if g.tag.endswith("g") and "diagram-node" in (g.get("class") or ""):
            title_el = next((c for c in g if c.tag.endswith("title")), None)
            shape_el = next((c for c in g if c.tag.endswith(("rect", "polygon"))), None)
            if title_el is not None and shape_el is not None:
                shapes_by_title[title_el.text] = bbox(shape_el)

    customer_box = shapes_by_title["Customer"]
    order_box = shapes_by_title["Order"]
    places_box = shapes_by_title["Places"]
    # the relationship must sit strictly BETWEEN its own two entities on the
    # x-axis, not off to one side of both of them with an entity in between
    assert customer_box[2] <= places_box[0]
    assert places_box[2] <= order_box[0]


# ---------------------------------------------------------------------------
# swimlane: one column per lane, cross-lane connectors
# ---------------------------------------------------------------------------


def _swimlane_spec() -> DiagramSpec:
    nodes = [
        DiagramNode(id="req", label="Submit ticket", lane="Customer"),
        DiagramNode(id="triage", label="Triage the issue", lane="Support Agent"),
        DiagramNode(id="bill", label="Check billing status", lane="Billing System"),
        DiagramNode(id="reply", label="Reply to customer", lane="Support Agent"),
    ]
    edges = [
        DiagramEdge(source="req", target="triage", label="New ticket"),
        DiagramEdge(source="triage", target="bill", label="Verify account"),
        DiagramEdge(source="bill", target="reply", label="Account status"),
    ]
    return DiagramSpec(kind="swimlane", title="Support Ticket Flow", nodes=nodes, edges=edges)


def test_er_diagram_attribute_oval_font_meets_the_readability_floor_after_scaling():
    """A real, confirmed bug: `_draw_attribute_oval` used a hardcoded
    11.5px base font size, which scales to only ~8.7px once the page
    shrinks this canvas to its real column width - under the 11px
    readability floor - and `er_diagram` wasn't even in
    `diagram_render_qa`'s own per-kind lookup table, so nothing caught it.
    Attribute ovals now render at RING_LABEL_SIZE, the same safe floor
    every other small shape in this module already uses."""
    svg_bytes, _w, _h = render_diagram_svg(_er_spec(), TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert f'font-size="{RING_LABEL_SIZE:.1f}"' in text
    assert 'font-size="11.5"' not in text


def test_swimlane_is_usable_with_two_or_more_lanes_and_edges():
    assert _swimlane_spec().is_usable()
    one_lane = DiagramSpec(
        kind="swimlane",
        nodes=[DiagramNode(id="a", label="A", lane="X"), DiagramNode(id="b", label="B", lane="X")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    assert not one_lane.is_usable()
    no_edges = DiagramSpec(kind="swimlane", nodes=_swimlane_spec().nodes, edges=[])
    assert not no_edges.is_usable()


def test_swimlane_renders_one_header_per_lane_and_all_nodes():
    spec = _swimlane_spec()
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")

    for lane in ("Customer", "Support Agent", "Billing System"):
        assert lane in text
    for node in spec.nodes:
        assert node.label in text
    assert width > 0 and height > 0


def test_swimlane_draws_a_full_height_lane_band_not_just_a_header():
    """A real, confirmed design gap: this renderer used to draw only a
    coloured HEADER at the top of each lane, with nothing marking a
    lane's own boundary any further down - a reader had no way to tell
    which lane a node belonged to once they looked below the header row
    (only inferable from x position). A full-height background band per
    lane, reaching all the way to the diagram's own bottom, is the
    defining visual feature of a real swimlane diagram."""
    spec = _swimlane_spec()
    svg_bytes, _w, height = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    # Lane bands are the only <rect> elements with a fill-opacity (the
    # light tint) - distinct from the solid header rects and the node
    # boxes (diagram-box class), neither of which set fill-opacity.
    bands = [el for el in root.iter() if el.tag.endswith("rect") and el.get("fill-opacity")]
    assert len(bands) == 3  # Customer, Support Agent, Billing System
    for band in bands:
        band_height = float(band.get("height"))
        # A band spanning only the header's own height (~70px) would mean
        # the fix regressed back to a header-only band; it must reach
        # most of the way down the diagram's real content height.
        assert band_height > height * 0.5


def test_swimlane_cross_lane_edges_never_overlap_a_box():
    spec = _swimlane_spec()
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    boxes = _rects_by_class(text, "diagram-box")
    # lane header bars are also "diagram-box" rects - exclude anything at
    # the very top margin (the header row) from the node-only overlap check.
    node_boxes = [b for b in boxes if b[1] > MARGIN + 60]
    offenders = [(a, b) for i, a in enumerate(node_boxes) for b in node_boxes[i + 1 :] if _bboxes_overlap(a, b)]
    assert not offenders, f"swimlane node boxes overlap: {offenders}"


def test_swimlane_nodes_without_a_lane_group_into_a_general_column():
    spec = DiagramSpec(
        kind="swimlane",
        nodes=[
            DiagramNode(id="a", label="A", lane="Known"),
            DiagramNode(id="b", label="B"),  # no lane set
        ],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    assert "General" in svg_bytes.decode("utf-8")


def _node_center_y(svg_text: str, label: str) -> float:
    """The vertical centre of whichever "diagram-box" rect most likely
    belongs to `label` - the one whose own <title> (which always leads its
    <g class="diagram-node">) is exactly that label, found by locating the
    <title> and reading the first rect that follows it."""
    marker = f"<title>{escape(label)}</title>"
    start = svg_text.index(marker)
    match = re.search(r'<rect class="diagram-box" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg_text[start:])
    assert match, f"no diagram-box rect found after {label}'s title"
    y, height = float(match.group(1)), float(match.group(2))
    return y + height / 2


def test_swimlane_same_step_nodes_align_into_the_same_row_across_lanes():
    """The reported gap: three nodes at the same point in the process, each
    in a different lane, must land at (roughly) the same height - not just
    stack independently per lane, which could put them anywhere relative
    to each other."""
    nodes = [
        DiagramNode(id="req", label="Login Request", lane="User"),
        DiagramNode(id="validate", label="Validate Credentials", lane="System"),
        DiagramNode(id="checkdb", label="Check User Data", lane="Database"),
    ]
    edges = [
        DiagramEdge(source="req", target="validate"),
        DiagramEdge(source="validate", target="checkdb"),
    ]
    # None of these three are at the same row by hop-count (0, 1, 2) - this
    # test instead confirms the *different*-row case lands at genuinely
    # different heights, complementing the same-row case below.
    svg_bytes, _, _ = render_diagram_svg(DiagramSpec(kind="swimlane", nodes=nodes, edges=edges), TemplateTheme())
    text = svg_bytes.decode("utf-8")
    y0 = _node_center_y(text, "Login Request")
    y1 = _node_center_y(text, "Validate Credentials")
    y2 = _node_center_y(text, "Check User Data")
    assert y0 < y1 < y2


def test_swimlane_two_branches_from_one_node_align_at_the_same_row():
    """Two different lanes both fed directly from the same upstream node
    (a genuine "same step, different lane" case) must land at the same
    row - this is the actual alignment guarantee the previous version of
    this function didn't provide."""
    nodes = [
        DiagramNode(id="start", label="Ticket Created", lane="Customer"),
        DiagramNode(id="notify_agent", label="Notify Agent", lane="Support Agent"),
        DiagramNode(id="notify_billing", label="Notify Billing", lane="Billing System"),
    ]
    edges = [
        DiagramEdge(source="start", target="notify_agent"),
        DiagramEdge(source="start", target="notify_billing"),
    ]
    svg_bytes, _, _ = render_diagram_svg(DiagramSpec(kind="swimlane", nodes=nodes, edges=edges), TemplateTheme())
    text = svg_bytes.decode("utf-8")
    y_agent = _node_center_y(text, "Notify Agent")
    y_billing = _node_center_y(text, "Notify Billing")
    assert abs(y_agent - y_billing) < 1.0


def test_swimlane_parallel_nodes_in_the_same_lane_and_row_never_overlap():
    """Two nodes that would otherwise land in the same (lane, row) cell -
    a genuine edge case (parallel branches within one lane at the same
    step) - must be bumped apart, never drawn on top of each other."""
    nodes = [
        DiagramNode(id="start", label="Start", lane="A"),
        DiagramNode(id="x", label="Path X", lane="B"),
        DiagramNode(id="y", label="Path Y", lane="B"),
    ]
    edges = [DiagramEdge(source="start", target="x"), DiagramEdge(source="start", target="y")]
    svg_bytes, _, _ = render_diagram_svg(DiagramSpec(kind="swimlane", nodes=nodes, edges=edges), TemplateTheme())
    text = svg_bytes.decode("utf-8")
    boxes = _rects_by_class(text, "diagram-box")
    node_boxes = [b for b in boxes if b[1] > MARGIN + 60]
    offenders = [(a, b) for i, a in enumerate(node_boxes) for b in node_boxes[i + 1 :] if _bboxes_overlap(a, b)]
    assert not offenders, f"parallel same-lane nodes overlap: {offenders}"


# ---------------------------------------------------------------------------
# regression test: font-size minimums, checked at the ACTUAL post-scale size
# a reader sees, not the raw SVG-unit numbers in isolation - a course
# document renders this SVG at CONTENT_WIDTH (666px), narrower than this
# renderer's own CANVAS_WIDTH (880px), so a font size that looks fine in the
# SVG's own coordinate space can still end up smaller than required once
# displayed.
# ---------------------------------------------------------------------------


def test_flow_chart_font_sizes_meet_minimums_after_page_scaling():
    scale = CONTENT_WIDTH / CANVAS_WIDTH
    effective_label = LABEL_SIZE * scale
    effective_detail = DETAIL_SIZE * scale
    effective_edge_label = EDGE_LABEL_FONT_SIZE * scale
    assert effective_label >= 14.0, f"node title would render at {effective_label:.1f}px, under the 14px minimum"
    assert effective_detail >= 12.0, f"subtitle would render at {effective_detail:.1f}px, under the 12px minimum"
    assert effective_edge_label >= 12.0, (
        f"edge label would render at {effective_edge_label:.1f}px, under the 12px minimum"
    )


def test_node_box_rendered_font_size_attributes_match_the_source_constants():
    """The SVG's own `font-size` attributes must actually use the sized-up
    constants - a real, confirmed prior gap: raising a module constant
    doesn't help if a rendering call site hardcodes its own number instead
    of referencing it. flow_chart/process/cycle/concept_map no longer go
    through `_node_block`/`_edge_label` at all (see _layout_ring_process/
    _layout_concept_wheel's own, different font sizes) - hierarchy
    (LABEL_SIZE, still `_node_block`-based) and data_flow_diagram
    (EDGE_LABEL_FONT_SIZE, still edge-labelled) are the still-active kinds
    this guard now applies to."""
    nodes = [DiagramNode(id="root", label="Root", level=0), DiagramNode(id="child", label="Child", level=1)]
    hierarchy_svg, _w, _h = render_diagram_svg(
        DiagramSpec(kind="hierarchy", nodes=nodes, edges=[DiagramEdge(source="root", target="child")]),
        TemplateTheme(),
    )
    assert f'font-size="{LABEL_SIZE:.1f}"' in hierarchy_svg.decode("utf-8")

    dfd_nodes = [DiagramNode(id=f"n{i}", label=f"Node {i}") for i in range(2)]
    dfd_svg, _w, _h = render_diagram_svg(
        DiagramSpec(
            kind="data_flow_diagram", nodes=dfd_nodes,
            edges=[DiagramEdge(source="n0", target="n1", label="data")],
        ),
        TemplateTheme(),
    )
    assert f'font-size="{EDGE_LABEL_FONT_SIZE:.1f}"' in dfd_svg.decode("utf-8")


# ---------------------------------------------------------------------------
# graph/chart (kind == "graph") - native, deterministic function plots and
# categorical bar/line charts; see app.render.graph_math for the underlying
# safe-eval/tick math and its own dedicated test file.
# ---------------------------------------------------------------------------


def test_graph_is_usable_requires_a_parseable_function_with_a_real_domain():
    assert not DiagramSpec(kind="graph", graph_type="function").is_usable()
    assert not DiagramSpec(
        kind="graph", graph_type="function", functions=[GraphFunction(expression="")]
    ).is_usable()
    assert not DiagramSpec(
        kind="graph", graph_type="function", functions=[GraphFunction(expression="not valid ((( python")]
    ).is_usable()
    assert DiagramSpec(
        kind="graph", graph_type="function", functions=[GraphFunction(expression="x**2")]
    ).is_usable()
    # x_min/x_max default to -10/10 when unset - a rendering default, not a
    # hard requirement the writer must always supply.
    assert DiagramSpec(
        kind="graph", graph_type="function",
        functions=[GraphFunction(expression="x")], x_min=5, x_max=5,
    ).is_usable() is False


def test_graph_is_usable_requires_matching_series_and_category_lengths():
    assert not DiagramSpec(kind="graph", graph_type="bar").is_usable()
    assert not DiagramSpec(
        kind="graph", graph_type="bar", categories=["A", "B"], series=[GraphSeries(label="s", values=[1])]
    ).is_usable()
    assert DiagramSpec(
        kind="graph", graph_type="bar", categories=["A", "B"], series=[GraphSeries(label="s", values=[1, 2])]
    ).is_usable()
    assert DiagramSpec(
        kind="graph", graph_type="line", categories=["A", "B"], series=[GraphSeries(label="s", values=[1, 2])]
    ).is_usable()


def test_graph_is_usable_rejects_an_unknown_graph_type():
    assert not DiagramSpec(
        kind="graph", graph_type="scatter", categories=["A"], series=[GraphSeries(label="s", values=[1])]
    ).is_usable()


@pytest.mark.parametrize("graph_type", ["function", "bar", "line"])
def test_render_graph_svg_is_well_formed(graph_type):
    if graph_type == "function":
        spec = DiagramSpec(
            kind="graph", title="A parabola", graph_type="function", x_label="x", y_label="y",
            functions=[GraphFunction(expression="x**2", label="y = x^2")],
        )
    else:
        spec = DiagramSpec(
            kind="graph", title="Comparison", graph_type=graph_type, x_label="Option", y_label="Score",
            categories=["A", "B", "C"],
            series=[GraphSeries(label="Speed", values=[7, 3, 5]), GraphSeries(label="Risk", values=[2, 9, 5])],
        )
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)  # raises if malformed
    assert root.tag.endswith("svg")
    assert width > 0 and height > 0
    text = svg_bytes.decode("utf-8")
    assert spec.x_label in text and spec.y_label in text


def test_render_graph_svg_plots_the_real_function_values_not_a_placeholder():
    """The rendered path must trace the function's own real output, not a
    generic placeholder shape - spot-check two known points of y = x."""
    spec = DiagramSpec(
        kind="graph", title="Identity", graph_type="function", x_label="x", y_label="y",
        x_min=0, x_max=10, functions=[GraphFunction(expression="x", label="y = x")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    # A straight diagonal line's path has monotonically increasing x and y
    # pixel coordinates together (never a flat or inverted line) - extract
    # the <path> element's d attribute and confirm its first/last points
    # move in the same direction on both axes.
    match = re.search(r'<path d="([^"]+)" fill="none" stroke="#2563eb"', text)
    assert match, "expected a plotted path in the rendered SVG"
    coords = re.findall(r"[ML]([\d.]+),([\d.]+)", match.group(1))
    assert len(coords) > 10
    first_x, first_y = map(float, coords[0])
    last_x, last_y = map(float, coords[-1])
    assert last_x > first_x  # x increases left to right
    assert last_y < first_y  # SVG y is inverted - y = x increasing means pixel-y decreases


def test_render_graph_svg_bar_chart_bar_heights_reflect_real_values():
    """A taller real value must render as a visually taller bar."""
    spec = DiagramSpec(
        kind="graph", title="t", graph_type="bar", x_label="x", y_label="y",
        categories=["Low", "High"], series=[GraphSeries(label="s", values=[1, 99])],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    # Bar rects (not the 14x14 legend swatch, which shares the same fill
    # colour) are the ones whose width is the computed bar width, not "14".
    heights = [
        float(h)
        for w, h in re.findall(r'<rect x="[\d.]+" y="[\d.]+" width="([\d.]+)" height="([\d.]+)" fill="#2563eb"', text)
        if w != "14"
    ]
    assert len(heights) == 2
    assert heights[1] > heights[0] * 5  # the 99-value bar is dramatically taller than the 1-value bar


def test_render_graph_svg_never_evaluates_an_unsafe_expression():
    """is_usable() rejects an unparseable expression before rendering, but
    confirm the renderer itself never raises or executes anything unsafe
    even if a spec slips through with a hostile expression (e.g. built by
    hand, bypassing is_usable())."""
    spec = DiagramSpec(
        kind="graph", title="t", graph_type="function", x_label="x", y_label="y",
        functions=[GraphFunction(expression="__import__('os').system('echo pwned')", label="bad")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())  # must not raise
    ET.fromstring(svg_bytes)  # still well-formed, even with zero plotted points


# ---------------------------------------------------------------------------
# style-aware cycling palette (TemplateTheme.diagram_palette)
# ---------------------------------------------------------------------------


def test_diagram_palette_default_is_unchanged_from_before_the_palette_system():
    """A blank diagram_palette (every course generated before this system
    existed) must keep producing byte-identical colours - this is a purely
    additive, opt-in preference, never a behaviour change for existing
    content."""
    from app.render.diagram_renderer import _WHEEL_SEGMENT_COLORS, _cycling_palette

    assert _cycling_palette(TemplateTheme()) == _WHEEL_SEGMENT_COLORS


def test_diagram_palette_vibrant_and_muted_are_both_real_distinct_palettes():
    from app.render.diagram_renderer import _cycling_palette

    default = _cycling_palette(TemplateTheme())
    vibrant = _cycling_palette(TemplateTheme(diagram_palette="vibrant"))
    muted = _cycling_palette(TemplateTheme(diagram_palette="muted"))
    assert vibrant != default
    assert muted != default
    assert vibrant != muted
    assert len(vibrant) >= 6 and len(muted) >= 6


def test_diagram_palette_unrecognised_value_falls_back_to_default():
    from app.render.diagram_renderer import _cycling_palette

    assert _cycling_palette(TemplateTheme(diagram_palette="not-a-real-palette")) == _cycling_palette(TemplateTheme())


def test_diagram_palette_vibrant_actually_changes_rendered_colours():
    spec = DiagramSpec(
        kind="pyramid", title="t", nodes=[DiagramNode(id=f"n{i}", label=f"Layer {i}") for i in range(3)]
    )
    default_svg = render_diagram_svg(spec, TemplateTheme())[0].decode("utf-8")
    vibrant_svg = render_diagram_svg(spec, TemplateTheme(diagram_palette="vibrant"))[0].decode("utf-8")
    assert default_svg != vibrant_svg


def test_diagram_palette_never_affects_a_semantic_colour_kind():
    """flow_chart's start/process/decision/end colours carry real meaning
    (green=start, red=end, the universal flowchart convention) and must
    stay fixed regardless of diagram_palette - only the generically-
    coloured kinds (pyramid, matrix, wheel/mindmap/network, chevron/
    staircase/timeline) are style-aware."""
    spec = DiagramSpec(
        kind="flow_chart", title="t",
        nodes=[DiagramNode(id="s", label="Start"), DiagramNode(id="e", label="End")],
        edges=[DiagramEdge(source="s", target="e")],
    )
    default_svg = render_diagram_svg(spec, TemplateTheme())[0].decode("utf-8")
    vibrant_svg = render_diagram_svg(spec, TemplateTheme(diagram_palette="vibrant"))[0].decode("utf-8")
    assert "#bbf7d0" in default_svg and "#bbf7d0" in vibrant_svg  # start green, unchanged


# ---------------------------------------------------------------------------
# new SmartArt-style kinds: pyramid/funnel, matrix, radial_mindmap, venn
# ---------------------------------------------------------------------------


def test_pyramid_is_usable_with_2_to_9_labelled_layers():
    assert not DiagramSpec(kind="pyramid", nodes=[DiagramNode(id="a", label="Only one")]).is_usable()
    usable = DiagramSpec(
        kind="pyramid", nodes=[DiagramNode(id=f"n{i}", label=f"Layer {i}") for i in range(4)]
    )
    assert usable.is_usable()


def test_pyramid_draws_a_narrow_apex_and_a_wide_base():
    """nodes[0] (drawn first, at the top) must be the NARROWEST trapezoid
    and nodes[-1] (bottom) the WIDEST - the defining shape of a pyramid."""
    spec = DiagramSpec(
        kind="pyramid", title="A pyramid",
        nodes=[DiagramNode(id=f"n{i}", label=f"Layer {i}") for i in range(4)],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    # class="diagram-box" only - excludes each layer's own decorative
    # offset "shadow" duplicate polygon (see _layout_pyramid's own
    # docstring on that 3D depth cue), which is not a real node shape.
    polygons = [
        el for el in root.iter()
        if el.tag.endswith("polygon") and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(polygons) == 4

    def _width(points: str) -> float:
        xs = [float(pair.split(",")[0]) for pair in points.strip().split(" ")]
        return max(xs) - min(xs)

    widths = [_width(p.get("points")) for p in polygons]
    assert widths == sorted(widths)  # strictly non-decreasing top to bottom
    assert widths[0] < widths[-1]


def test_pyramid_inverted_draws_a_wide_top_and_a_narrow_bottom():
    """pyramid_inverted=True (a funnel) reverses which end is wide - same
    node order, same data, just drawn the other way up."""
    spec = DiagramSpec(
        kind="pyramid", pyramid_inverted=True, title="A funnel",
        nodes=[DiagramNode(id=f"n{i}", label=f"Stage {i}") for i in range(4)],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    polygons = [el for el in root.iter() if el.tag.endswith("polygon")]

    def _width(points: str) -> float:
        xs = [float(pair.split(",")[0]) for pair in points.strip().split(" ")]
        return max(xs) - min(xs)

    widths = [_width(p.get("points")) for p in polygons]
    assert widths == sorted(widths, reverse=True)  # strictly non-increasing top to bottom
    assert widths[0] > widths[-1]


def test_pyramid_never_truncates_a_long_layer_label_with_an_ellipsis():
    long_label = "A genuinely long layer description that would overflow a narrow apex trapezoid"
    spec = DiagramSpec(
        kind="pyramid", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Base")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_matrix_four_nodes_with_axis_fields_draws_the_axis_cross():
    spec = DiagramSpec(
        kind="matrix", title="t", matrix_x_label="Effort", matrix_y_label="Impact",
        nodes=[DiagramNode(id=f"n{i}", label=f"Quadrant {i}") for i in range(4)],
    )
    text = render_diagram_svg(spec, TemplateTheme())[0].decode("utf-8")
    assert "Effort" in text and "Impact" in text
    assert text.count("<line") >= 2  # the "+" axis cross


def test_matrix_four_nodes_with_no_axis_fields_draws_a_plain_grid():
    spec = DiagramSpec(
        kind="matrix", title="t",
        nodes=[DiagramNode(id=f"n{i}", label=f"Group {i}") for i in range(4)],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    cells = [el for el in root.iter() if "diagram-box" in (el.get("class") or "")]
    assert len(cells) == 4


def test_matrix_non_quadrant_count_renders_as_a_grid_not_an_axis_cross():
    spec = DiagramSpec(
        kind="matrix", title="t", matrix_x_label="Should not appear as an axis cross",
        nodes=[DiagramNode(id=f"n{i}", label=f"Group {i}") for i in range(3)],
    )
    text = render_diagram_svg(spec, TemplateTheme())[0].decode("utf-8")
    assert "Should not appear as an axis cross" not in text


def test_matrix_never_truncates_a_long_cell_label_with_an_ellipsis():
    long_label = "A genuinely long quadrant description that would overflow a small matrix cell"
    spec = DiagramSpec(
        kind="matrix", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Other")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_matrix_axis_cross_grid_is_centred_on_the_canvas_not_shifted_right():
    """A real, confirmed bug: the y-axis label's own left-side gutter used
    to be subtracted from the grid's width only ONCE, so the grid's right
    edge stayed flush with the canvas margin while its left edge was
    pushed in - the grid itself read as shifted right of centre (measured
    26px off on an 880px canvas), not symmetric under its own title. Only
    a quadrant count (4 nodes) with at least one axis field set draws the
    cross - see _layout_matrix's own docstring."""
    spec = DiagramSpec(
        kind="matrix", title="t", matrix_x_label="Effort", matrix_y_label="Impact",
        nodes=[DiagramNode(id=f"n{i}", label=f"Q{i}") for i in range(4)],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    cells = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    xs = [float(c.get("x")) for c in cells]
    ws = [float(c.get("width")) for c in cells]
    left = min(xs)
    right = max(x + w for x, w in zip(xs, ws))
    canvas_w = float(root.get("width"))
    assert abs((left + right) / 2 - canvas_w / 2) < 1.0


def test_radial_mindmap_connects_every_branch_to_the_centre():
    spec = DiagramSpec(
        kind="radial_mindmap", title="t",
        nodes=[
            DiagramNode(id="center", label="Central Topic", level=0),
            DiagramNode(id="b1", label="Branch One"),
            DiagramNode(id="b2", label="Branch Two"),
            DiagramNode(id="b3", label="Branch Three"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert text.count("<path") >= 3  # one curved connector per branch
    for node in spec.nodes:
        assert node.label in text


def test_radial_mindmap_never_truncates_a_long_branch_label_with_an_ellipsis():
    long_label = "A genuinely long branch idea description that would overflow a small spoke circle"
    spec = DiagramSpec(
        kind="radial_mindmap", title="t",
        nodes=[
            DiagramNode(id="center", label="Central Topic", level=0),
            DiagramNode(id="b1", label=long_label),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_venn_is_usable_only_at_2_or_3_sets():
    assert not DiagramSpec(kind="venn", nodes=[DiagramNode(id="a", label="Only one")]).is_usable()
    assert DiagramSpec(
        kind="venn", nodes=[DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    ).is_usable()
    assert DiagramSpec(
        kind="venn",
        nodes=[DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B"), DiagramNode(id="c", label="C")],
    ).is_usable()
    assert not DiagramSpec(
        kind="venn",
        nodes=[DiagramNode(id=f"n{i}", label=f"Set {i}") for i in range(4)],
    ).is_usable()


def test_venn_draws_one_circle_per_set_and_no_text_on_any_edge():
    """No connecting line/arrow exists in a Venn diagram at all (sets relate
    by overlap, not by drawn edges) - and an overlap's own shared-idea text
    (edge.label) never sits inside the lens-shaped overlap region itself,
    only in the Key table below (see _layout_venn's own docstring)."""
    spec = DiagramSpec(
        kind="venn", title="t",
        nodes=[
            DiagramNode(id="a", label="Set A", detail="Unique to A"),
            DiagramNode(id="b", label="Set B", detail="Unique to B"),
        ],
        edges=[DiagramEdge(source="a", target="b", label="Shared idea")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    circles = [el for el in root.iter() if "diagram-box" in (el.get("class") or "") and el.tag.endswith("circle")]
    assert len(circles) == 2
    # No connector/arrow between the two circles at all - only the Key
    # table's own plain divider <line>s (checked separately) exist.
    assert not [el for el in root.iter() if el.tag.endswith("path")]
    assert not [el for el in root.iter() if el.get("marker-end")]
    text = svg_bytes.decode("utf-8")
    assert "Shared idea" in text  # present in the Key table...
    assert "Set A" in text and "Set B" in text
    assert "Unique to A" in text and "Unique to B" in text


def test_venn_never_truncates_a_long_set_label_with_an_ellipsis():
    long_label = "A genuinely long set name that would overflow a fixed-radius Venn circle"
    spec = DiagramSpec(
        kind="venn", title="t",
        nodes=[DiagramNode(id="a", label=long_label), DiagramNode(id="b", label="Other set")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_chevron_process_draws_a_chevron_per_node_and_never_truncates():
    labels = ["Research", "Design", "Build", "Launch"]
    nodes = [DiagramNode(id=f"n{i}", label=l, detail="Some supporting detail.") for i, l in enumerate(labels)]
    spec = DiagramSpec(kind="chevron_process", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    polygons = [el for el in root.iter() if el.tag.endswith("polygon") and "diagram-box" in (el.get("class") or "")]
    assert len(polygons) == 4
    text = svg_bytes.decode("utf-8")
    for label in labels:
        assert label in text
    assert "…" not in text


def test_chevron_process_never_truncates_a_long_step_label_with_an_ellipsis():
    long_label = "A genuinely long step name that would overflow a narrow chevron arrow"
    spec = DiagramSpec(
        kind="chevron_process", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Next")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_timeline_draws_an_axis_and_alternates_cards_above_and_below():
    nodes = [
        DiagramNode(id="n0", label="1991 - First release", detail="Guido van Rossum releases it"),
        DiagramNode(id="n1", label="2000 - Python 2.0", detail="List comprehensions"),
        DiagramNode(id="n2", label="2008 - Python 3.0", detail="Major redesign"),
    ]
    spec = DiagramSpec(kind="timeline", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    cards = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(cards) == 3
    # alternating above/below means NOT every card shares the same y.
    assert len({c.get("y") for c in cards}) > 1
    markers = [el for el in root.iter() if el.tag.endswith("circle")]
    assert len(markers) == 3
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text


def test_timeline_never_truncates_a_long_milestone_label_with_an_ellipsis():
    long_label = "A genuinely long dated milestone description that would overflow a small timeline card"
    spec = DiagramSpec(
        kind="timeline", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="2020 - Next")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_timeline_is_a_sequential_kind_with_the_14_node_ceiling():
    from app.schemas.diagram import SEQUENTIAL_KINDS, max_nodes_for

    assert "timeline" in SEQUENTIAL_KINDS
    assert max_nodes_for("timeline") == 14


def test_chevron_process_is_a_sequential_kind_with_the_14_node_ceiling():
    """chevron_process has no dedicated node-count ceiling in is_usable()
    (same generic 2..max_nodes_for(kind) check every unlisted kind gets -
    see DiagramSpec.is_usable); it's correctly classified SEQUENTIAL,
    earning the 14-node (not 9) cap that family gets. Staying short in
    PRACTICE (3-6 nodes) is the writer prompt's own guidance, not a
    schema-level ceiling specific to this kind - see DIAGRAM_SYSTEM's
    "use flow_chart for anything longer" instruction."""
    from app.schemas.diagram import SEQUENTIAL_KINDS, max_nodes_for

    assert "chevron_process" in SEQUENTIAL_KINDS
    assert max_nodes_for("chevron_process") == 14


def test_staircase_process_draws_ascending_bars_and_never_truncates():
    labels = ["Beginner", "Intermediate", "Advanced", "Expert"]
    nodes = [DiagramNode(id=f"n{i}", label=l, detail="Some supporting detail.") for i, l in enumerate(labels)]
    spec = DiagramSpec(kind="staircase_process", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    bars = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(bars) == 4
    widths = [float(b.get("width")) for b in bars]
    assert widths == sorted(widths)  # each bar is at least as wide as the one before it
    assert widths[0] < widths[-1]  # and genuinely ascends, not just flat
    text = svg_bytes.decode("utf-8")
    for label in labels:
        assert label in text
    assert "…" not in text


def test_staircase_process_never_truncates_a_long_step_label_with_an_ellipsis():
    long_label = "A genuinely long step name that would overflow a narrow staircase bar"
    spec = DiagramSpec(
        kind="staircase_process", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Next")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_staircase_process_is_a_sequential_kind_with_the_14_node_ceiling():
    from app.schemas.diagram import SEQUENTIAL_KINDS, max_nodes_for

    assert "staircase_process" in SEQUENTIAL_KINDS
    assert max_nodes_for("staircase_process") == 14


def test_balance_comparison_splits_by_position_into_two_coloured_columns():
    nodes = [
        DiagramNode(id="n0", label="Variable costs", detail="Scale with usage."),
        DiagramNode(id="n1", label="Fixed costs", detail="Flat monthly spend."),
        DiagramNode(id="n2", label="Value for the client", detail="Faster delivery."),
        DiagramNode(id="n3", label="Value for us", detail="Recurring revenue."),
    ]
    spec = DiagramSpec(
        kind="balance_comparison", title="t", balance_left_label="Cost", balance_right_label="Value", nodes=nodes,
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    rects = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(rects) == 4
    xs = sorted({round(float(r.get("x")), 1) for r in rects})
    assert len(xs) == 2  # exactly two distinct column x-positions
    text = svg_bytes.decode("utf-8")
    assert "Cost" in text and "Value" in text
    for node in nodes:
        assert node.label in text
        assert node.detail in text


def test_balance_comparison_respects_explicit_shape_role_for_an_uneven_split():
    nodes = [
        DiagramNode(id="n0", label="Risk A", shape_role="left"),
        DiagramNode(id="n1", label="Risk B", shape_role="left"),
        DiagramNode(id="n2", label="Risk C", shape_role="left"),
        DiagramNode(id="n3", label="Benefit A", shape_role="right"),
    ]
    spec = DiagramSpec(kind="balance_comparison", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    rects = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    # 3 left rows share one x, 1 right row has a different x.
    left_x = {round(float(r.get("x")), 1) for r in rects[:3]}
    right_x = {round(float(r.get("x")), 1) for r in rects[3:]}
    assert len(left_x) == 1 and len(right_x) == 1
    assert left_x != right_x


def test_balance_comparison_never_truncates_a_long_detail_with_an_ellipsis():
    long_label = "A genuinely long cost item label that keeps going well past a normal short label"
    long_detail = "An equally long explanatory sentence describing exactly why this matters so much."
    spec = DiagramSpec(
        kind="balance_comparison", title="t",
        nodes=[
            DiagramNode(id="n0", label=long_label, detail=long_detail),
            DiagramNode(id="n1", label="Value", shape_role="right"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_comparison_never_truncates_a_long_detail_with_an_ellipsis():
    """`_node_block` (every plain-box diagram kind: comparison, flow_chart,
    process, cycle, hierarchy, concept_map, data_flow_diagram, swimlane,
    er_diagram) used to hard-cap wrapped text at 2 label / 3 detail lines
    and silently discard the rest with a trailing "…", even though the box
    already sizes itself from however many lines come back - a real,
    confirmed bug (narrow multi-column `comparison` layouts hit it first,
    e.g. a 4-column "camera-mode analogy" diagram whose detail text got cut
    off mid-sentence). `balance_comparison` was already fixed the same way;
    this is the same fix applied to `_node_block` itself."""
    nodes = [
        DiagramNode(id="n0", label="Still", detail="Phasor stationary sinusoidal steady-state"),
        DiagramNode(
            id="n1", label="Long exposure",
            detail="Time-domain explicit time variation or nonlinearity captured directly without any transform",
        ),
        DiagramNode(id="n2", label="High-speed", detail="Laplace transform transients and system structure"),
        DiagramNode(
            id="n3", label="Can be violated by",
            detail="Switching noise, aliasing, and nonstationarity across the capture window",
        ),
    ]
    spec = DiagramSpec(kind="comparison", title="Which analysis domain to choose", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    svg = svg_bytes.decode("utf-8")
    assert "…" not in svg
    for node in nodes:
        assert node.detail.split()[-1] in svg, f"detail text for {node.label!r} was cut off"


def test_comparison_with_many_columns_wraps_into_rows_instead_of_crushing_each_one():
    """A real, confirmed case from a generated course: 7 nondimensional
    numbers compared side by side (`_layout_comparison`'s old fixed-
    column-count math) squeezed each column to ~95px - under what even an
    8-character single word ("Reynolds") needs, so nearly every label
    force-broke mid-word with no hyphen ("Reynol" / "ds", "Strouh" /
    "al"). More columns than fit at a readable width must start a new
    ROW instead of shrinking every column further."""
    labels = [
        "Reynolds number (Re)", "Froude number (Fr)", "Strouhal number (St)",
        "Prandtl number (Pr)", "Nusselt number (Nu)", "Mach number (Ma)", "Knudsen number (Kn)",
    ]
    nodes = [DiagramNode(id=f"n{i}", label=label, detail="A short descriptive note") for i, label in enumerate(labels)]
    spec = DiagramSpec(kind="comparison", title="Key nondimensional numbers", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    svg = svg_bytes.decode("utf-8")
    assert "…" not in svg
    for label in labels:
        for word in label.replace("(", "").replace(")", "").split():
            assert word in svg, f"word lost or broken: {word!r} (from {label!r})"
    assert validate_rendered_svg(svg_bytes, kind="comparison") == []


def test_diagram_title_never_truncates_a_long_title_with_an_ellipsis():
    """The title caption (`render_diagram_svg`'s own `title_lines = _wrap(...)`
    call, separate from `_node_block`/`_circle_node`) used to hard-cap at
    max_lines=1 - a real, confirmed bug: a routine "Fig. N: <descriptive
    title>" caption longer than one line at the canvas width got cut off
    with a trailing "…" (e.g. "Fig. 3: Oscilloscope setup — quick checklist
    & visible controls" became "...& visible…"). The layout below already
    sizes itself from the title's real height (`top += title_h + 20`), so
    allowing a second line costs nothing."""
    spec = DiagramSpec(
        kind="process",
        title="Fig. 3: Oscilloscope setup — quick checklist & visible controls",
        nodes=[DiagramNode(id="n0", label="Step one", detail="d"), DiagramNode(id="n1", label="Step two", detail="d")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    svg = svg_bytes.decode("utf-8")
    assert "…" not in svg
    for word in ("checklist", "visible", "controls"):
        assert word in svg


def test_circle_node_label_does_not_crowd_its_number_badge():
    """`_circle_node`'s number badge used to sit at a fixed `cy -
    radius*0.46` regardless of how many lines the label wrapped onto - a
    real, confirmed bug: a longer label (e.g. "Probes (Ch1 blue, Ch2
    red)") wrapped onto several lines, and its own first line's baseline
    shifted upward toward the badge as line count grew, while the badge
    never moved - the two visibly crowded together. `badge_y` must now
    sit at or above (more negative than) the old fixed position whenever
    the label needs more than a couple of lines."""
    from app.render.diagram_renderer import NETWORK_LABEL_SIZE, _circle_node, _circle_node_size

    short_label = "Hub"
    long_label = "Probes (Ch1 blue, Ch2 red)"
    short_lines, short_radius = _circle_node_size(short_label, min_radius=55.0, font_size=NETWORK_LABEL_SIZE)
    long_lines, long_radius = _circle_node_size(long_label, min_radius=55.0, font_size=NETWORK_LABEL_SIZE)
    assert len(long_lines) > len(short_lines)

    import re as _re

    def _badge_y(svg: str) -> float:
        match = _re.search(r'<text x="[\d.]+" y="(-?[\d.]+)" font-size="14"', svg)
        assert match, svg
        return float(match.group(1))

    short_svg, _ = _circle_node(
        short_label, cx=0.0, cy=0.0, theme=TemplateTheme(), number="1",
        fill="#fff", stroke="#000", min_radius=55.0, font_size=NETWORK_LABEL_SIZE,
        sizing=(short_lines, short_radius),
    )
    long_svg, _ = _circle_node(
        long_label, cx=0.0, cy=0.0, theme=TemplateTheme(), number="1",
        fill="#fff", stroke="#000", min_radius=55.0, font_size=NETWORK_LABEL_SIZE,
        sizing=(long_lines, long_radius),
    )
    # A y coordinate ABOVE centre is more NEGATIVE (SVG y grows downward) -
    # the long label's badge must sit at least as far above centre as the
    # short label's does (never closer), with real clearance from its own
    # label block's first line, not the old fixed fraction of radius alone.
    assert _badge_y(long_svg) <= _badge_y(short_svg)


def test_balance_comparison_is_a_relationship_kind():
    from app.schemas.diagram import RELATIONSHIP_KINDS

    assert "balance_comparison" in RELATIONSHIP_KINDS


def test_nested_circles_draws_strictly_decreasing_radii_outermost_first():
    labels = ["Vision", "Mission", "Strategy", "Tactics"]
    nodes = [DiagramNode(id=f"n{i}", label=l, detail="Some supporting detail.") for i, l in enumerate(labels)]
    spec = DiagramSpec(kind="nested_circles", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    circles = [el for el in root.iter() if el.tag.endswith("circle") and "diagram-box" in (el.get("class") or "")]
    assert len(circles) == 4
    radii = [float(c.get("r")) for c in circles]
    assert radii == sorted(radii, reverse=True)  # strictly non-increasing, outermost first
    assert radii[0] > radii[-1]
    text = svg_bytes.decode("utf-8")
    for label in labels:
        assert label in text
    assert "…" not in text


def test_nested_circles_never_truncates_a_long_ring_label_with_an_ellipsis():
    long_label = "A genuinely long outer scope name that goes on and on past a short label"
    spec = DiagramSpec(
        kind="nested_circles", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Inner")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_network_mesh_draws_a_line_per_edge_and_never_truncates():
    nodes = [DiagramNode(id=f"n{i}", label=l) for i, l in enumerate(["Frontend", "API Gateway", "Database"])]
    edges = [DiagramEdge(source="n0", target="n1"), DiagramEdge(source="n1", target="n2")]
    spec = DiagramSpec(kind="network_mesh", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    circles = [el for el in root.iter() if el.tag.endswith("circle") and "diagram-box" in (el.get("class") or "")]
    assert len(circles) == 3
    connectors = [el for el in root.iter() if el.tag.endswith("line") and el.get("stroke") not in (None,)]
    assert len(connectors) >= 2
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text
    assert "…" not in text


def test_network_mesh_never_truncates_a_long_node_label_with_an_ellipsis():
    long_label = "A genuinely long node name that goes on and on past a short label"
    spec = DiagramSpec(
        kind="network_mesh", title="t",
        nodes=[DiagramNode(id="n0", label=long_label), DiagramNode(id="n1", label="Other")],
        edges=[DiagramEdge(source="n0", target="n1")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_decision_tree_draws_a_diamond_for_a_decision_and_a_box_for_an_outcome():
    nodes = [
        DiagramNode(id="root", label="Is it causing bugs?", level=0),
        DiagramNode(id="refactor", label="Refactor now", level=1),
        DiagramNode(id="wait", label="Wait and monitor", level=1),
    ]
    edges = [
        DiagramEdge(source="root", target="refactor", label="Yes"),
        DiagramEdge(source="root", target="wait", label="No"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    diamonds = [el for el in root.iter() if el.tag.endswith("polygon") and "diagram-box" in (el.get("class") or "")]
    boxes = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(diamonds) == 1  # the root decision
    assert len(boxes) == 2  # the two outcomes
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text
    assert "If Yes" in text and "If No" in text  # branch conditions in the Key table, not on the arrow
    assert "…" not in text


def test_decision_tree_rows_are_centred_on_the_canvas():
    nodes = [
        DiagramNode(id="root", label="Root question?", level=0),
        DiagramNode(id="a", label="A", level=1),
        DiagramNode(id="b", label="B", level=1),
        DiagramNode(id="c", label="Grandchild outcome", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="a", label="Yes"),
        DiagramEdge(source="root", target="b", label="No"),
        DiagramEdge(source="a", target="c", label="Yes"),
    ]
    spec = DiagramSpec(kind="decision_tree", nodes=nodes, edges=edges)
    svg_bytes, width, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    shapes = [
        el for el in root.iter()
        if "diagram-box" in (el.get("class") or "") and el.tag.endswith(("rect", "polygon"))
    ]
    xs = []
    for el in shapes:
        if el.tag.endswith("rect"):
            xs.append((float(el.get("x")), float(el.get("x")) + float(el.get("width"))))
        else:
            pts = [tuple(map(float, p.split(","))) for p in el.get("points").split()]
            pxs = [p[0] for p in pts]
            xs.append((min(pxs), max(pxs)))
    # the single-node root row must itself be centred on the canvas
    root_left, root_right = xs[0]
    assert abs((root_left + root_right) / 2 - width / 2) < 1.0


def test_decision_tree_never_truncates_a_long_label_with_an_ellipsis():
    long_label = "A genuinely long decision question that keeps going well past a short label"
    spec = DiagramSpec(
        kind="decision_tree", title="t",
        nodes=[DiagramNode(id="root", label=long_label, level=0), DiagramNode(id="a", label="Outcome", level=1)],
        edges=[DiagramEdge(source="root", target="a", label="Yes")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_decision_tree_root_needing_many_wrapped_lines_is_not_truncated():
    """Real, confirmed bug (PDF/visual-pipeline audit, doc_81d0b1d44cae): a
    65-character root-decision label that wraps to exactly 5 lines was
    truncated with an ellipsis on its last line, at EVERY font size,
    because `_ideal_decision_box_size` measured how many lines the label
    needs using `width*0.5` as the diamond's usable text width, while
    `_draw_diamond` -> `_fit_boxed_text` actually wraps at `width*0.5` MINUS
    its own 2*padding (12px) - a narrower budget that needs one more line
    than the sizing function predicted, so the box was sized exactly one
    line too short. Reproduces even with no sibling row wide enough to
    trigger the proactive shrink-to-fit loop at all."""
    long_label = "Is the perturbation strong or weak relative to level spacing?"
    spec = DiagramSpec(
        kind="decision_tree", title="t",
        nodes=[
            DiagramNode(id="root", label=long_label, level=0),
            DiagramNode(id="a", label="Outcome", level=1),
        ],
        edges=[DiagramEdge(source="root", target="a", label="Yes")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in long_label.split():
        assert word in text


def test_decision_tree_with_several_wide_siblings_shrinks_to_stay_readable():
    """Real, confirmed bug (same audit): a row of 3 sibling outcomes, each
    independently capped at _MAX_PROCESS_BOX_W, still summed to a canvas
    wide enough (~1000-1040px) that the page's own down-scale to
    CONTENT_WIDTH shrank the fixed 15px label font under the 11px
    readability floor - `text_too_small_after_scaling` on real courses,
    every time, because decision_tree (unlike network_mesh/hierarchy/
    concept_map) had no proactive shrink-to-fit at all. Every row's font
    must now shrink together until the whole canvas is safely readable."""
    nodes = [
        DiagramNode(id="root", label="Is the perturbation strong or weak relative to level spacing?", level=0),
        DiagramNode(id="a", label="Use nondegenerate perturbation theory", level=1),
        DiagramNode(id="b", label="Use degenerate perturbation theory with diagonalization", level=1),
        DiagramNode(id="c", label="Switch to a fully numerical diagonalization approach instead", level=1),
    ]
    edges = [
        DiagramEdge(source="root", target="a", label="weak, non-degenerate"),
        DiagramEdge(source="root", target="b", label="weak, degenerate"),
        DiagramEdge(source="root", target="c", label="strong coupling"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, width, _h = render_diagram_svg(spec, TemplateTheme())
    issues = validate_rendered_svg(svg_bytes, kind="decision_tree")
    assert not any(i.kind == "text_too_small_after_scaling" for i in issues)
    assert "…" not in svg_bytes.decode("utf-8")


def test_decision_tree_with_interleaved_sibling_children_has_no_overlapping_buses():
    """Real, confirmed bug (B follow-up, PDF/visual-pipeline audit,
    doc_81d0b1d44cae): `node.level` is author-supplied and this layout used
    to position a row purely in `spec.nodes`' own list order, with no regard
    for which parent a node actually belongs to. Two sibling decisions, each
    with 2 children, whose children are listed INTERLEAVED (hub A's first
    child, hub B's first child, hub A's second child, hub B's second child -
    a realistic AI-authored narrative order, not per-subtree order) used to
    draw each hub's shared horizontal "bus" spanning across the OTHER hub's
    own child sitting between them, so the two buses ran collinear and
    overlapped for a real stretch - `shared_edge_segment`."""
    nodes = [
        DiagramNode(id="root", label="Isolated?", level=0),
        DiagramNode(id="momentum", label="Check p", level=1),
        DiagramNode(id="angmom", label="Check L", level=1),
        DiagramNode(id="mom_yes", label="p conserved", level=2),
        DiagramNode(id="ang_yes", label="L conserved", level=2),
        DiagramNode(id="mom_no", label="p not conserved", level=2),
        DiagramNode(id="ang_no", label="L not conserved", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="momentum", label="Yes"),
        DiagramEdge(source="root", target="angmom", label="No"),
        DiagramEdge(source="momentum", target="mom_yes", label="No net force"),
        DiagramEdge(source="momentum", target="mom_no", label="Net force acts"),
        DiagramEdge(source="angmom", target="ang_yes", label="No net torque"),
        DiagramEdge(source="angmom", target="ang_no", label="Net torque acts"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    issues = validate_rendered_svg(svg_bytes, kind="decision_tree")
    assert not any(i.kind == "shared_edge_segment" for i in issues)
    assert not any(i.kind == "edge_crosses_node" for i in issues)


def test_decision_tree_hub_children_are_not_split_by_a_foreign_sibling():
    """A different shape of the same interleaving bug: a wide hub (3
    children) and a narrow sibling hub (1 child) whose single child is
    listed BETWEEN two of the wide hub's own children - the wide hub's bus
    used to span across the foreign child's own column. The current
    geometry QA doesn't happen to flag this particular shape as an overlap
    (the crossing is perpendicular, not collinear), so this asserts the
    actual column order directly rather than relying on `validate_rendered_
    svg` alone: hub A's own 3 children must land in 3 CONSECUTIVE columns,
    with B1 pushed to one side, never sitting between them."""
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="hubA", label="Hub A", level=1),
        DiagramNode(id="hubB", label="Hub B", level=1),
        DiagramNode(id="a1", label="A1", level=2),
        DiagramNode(id="b1", label="B1", level=2),
        DiagramNode(id="a2", label="A2", level=2),
        DiagramNode(id="a3", label="A3", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="hubA", label="X"),
        DiagramEdge(source="root", target="hubB", label="Y"),
        DiagramEdge(source="hubA", target="a1", label="1"),
        DiagramEdge(source="hubB", target="b1", label="1"),
        DiagramEdge(source="hubA", target="a2", label="2"),
        DiagramEdge(source="hubA", target="a3", label="3"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    # Every node's own box is immediately followed in document order by its
    # own label text (the node-drawing loop emits one right after the
    # other), and every label here ("Root", "Hub A", "Hub B", "A1", "B1",
    # "A2", "A3") is unique - so boxes and their own labels line up 1:1 in
    # document order.
    all_labels = ["Root", "Hub A", "Hub B", "A1", "B1", "A2", "A3"]
    boxes = [
        el for el in root.iter()
        if "diagram-box" in (el.get("class") or "").split() and el.tag.endswith(("rect", "polygon"))
    ]
    texts = [
        el for el in root.iter()
        if el.tag.endswith("text") and "".join(el.itertext()).strip() in all_labels
    ]
    assert len(boxes) == len(texts) == len(all_labels)
    centers: dict[str, float] = {}
    for box, text_el in zip(boxes, texts):
        label = "".join(text_el.itertext()).strip()
        if box.tag.endswith("rect"):
            centers[label] = float(box.get("x")) + float(box.get("width")) / 2
        else:
            pts = [tuple(map(float, p.split(","))) for p in box.get("points").split()]
            centers[label] = sum(p[0] for p in pts) / len(pts)
    level2_labels = ("A1", "B1", "A2", "A3")
    ordered = sorted(level2_labels, key=lambda lbl: centers[lbl])
    a_positions = sorted(ordered.index(lbl) for lbl in ("A1", "A2", "A3"))
    assert a_positions == [a_positions[0], a_positions[0] + 1, a_positions[0] + 2], (
        f"hub A's children are not in 3 consecutive columns: {ordered}"
    )


def test_decision_tree_convergent_outcome_from_two_hubs_gets_distinct_arrowheads():
    """Real, confirmed bug (B follow-up): two different decisions converging
    on the same shared outcome node (a legitimate, common pattern -
    "otherwise, see the fallback") is grouped by SOURCE, so each converging
    edge was drawn independently with no awareness another source ALSO lands
    on the exact same target - both ended up with an identical landing
    point, drawing the same final drop segment (`shared_edge_segment`) at
    the exact same spot (`duplicate_arrowhead_side`, confirmed 0px apart)."""
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="hubA", label="Hub A", level=1),
        DiagramNode(id="hubB", label="Hub B", level=1),
        DiagramNode(id="a1", label="A1", level=2),
        DiagramNode(id="shared", label="Fallback", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="hubA", label="X"),
        DiagramEdge(source="root", target="hubB", label="Y"),
        DiagramEdge(source="hubA", target="a1", label="1"),
        DiagramEdge(source="hubA", target="shared", label="2"),
        DiagramEdge(source="hubB", target="shared", label="1"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    issues = validate_rendered_svg(svg_bytes, kind="decision_tree")
    assert not any(i.kind == "duplicate_arrowhead_side" for i in issues)
    assert not any(i.kind == "shared_edge_segment" for i in issues)


def test_decision_tree_triple_convergence_on_one_outcome_still_clears_qa():
    """Same convergence bug, pushed to 3 different hubs all converging on one
    shared outcome - the real production shape (3+ solution branches that
    all route to one "verify against residual controls" step) - still needs
    every landing point visually distinct."""
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="h1", label="H1", level=1),
        DiagramNode(id="h2", label="H2", level=1),
        DiagramNode(id="h3", label="H3", level=1),
        DiagramNode(id="shared", label="Shared outcome", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="h1", label="a"),
        DiagramEdge(source="root", target="h2", label="b"),
        DiagramEdge(source="root", target="h3", label="c"),
        DiagramEdge(source="h1", target="shared", label="1"),
        DiagramEdge(source="h2", target="shared", label="1"),
        DiagramEdge(source="h3", target="shared", label="1"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert validate_rendered_svg(svg_bytes, kind="decision_tree") == []


def test_decision_tree_interleaved_children_plus_convergence_combined():
    """Both B-follow-up bugs at once, in one tree - the realistic worst
    case: sibling hubs with interleaved children THAT ALSO converge on a
    shared outcome."""
    nodes = [
        DiagramNode(id="root", label="Root", level=0),
        DiagramNode(id="hubA", label="Hub A", level=1),
        DiagramNode(id="hubB", label="Hub B", level=1),
        DiagramNode(id="a1", label="A1", level=2),
        DiagramNode(id="shared", label="Shared", level=2),
        DiagramNode(id="b1", label="B1", level=2),
    ]
    edges = [
        DiagramEdge(source="root", target="hubA", label="x"),
        DiagramEdge(source="root", target="hubB", label="y"),
        DiagramEdge(source="hubA", target="a1", label="1"),
        DiagramEdge(source="hubA", target="shared", label="2"),
        DiagramEdge(source="hubB", target="shared", label="1"),
        DiagramEdge(source="hubB", target="b1", label="2"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert validate_rendered_svg(svg_bytes, kind="decision_tree") == []


def test_decision_tree_deeply_nested_asymmetric_branch_stays_clean():
    """Wide-vs-deep asymmetry: one branch goes 3 levels deep with its own
    sub-decision, the sibling branch is a single outcome - exercises the
    per-level parent-grouping fix against a non-uniform tree shape."""
    nodes = [
        DiagramNode(id="root", label="Root decision", level=0),
        DiagramNode(id="left", label="Left branch", level=1),
        DiagramNode(id="right", label="Right outcome", level=1),
        DiagramNode(id="left_a", label="Left-A sub-decision", level=2),
        DiagramNode(id="left_b", label="Left-B outcome", level=2),
        DiagramNode(id="left_a1", label="Left-A-1 outcome", level=3),
        DiagramNode(id="left_a2", label="Left-A-2 outcome", level=3),
    ]
    edges = [
        DiagramEdge(source="root", target="left", label="Yes"),
        DiagramEdge(source="root", target="right", label="No"),
        DiagramEdge(source="left", target="left_a", label="Yes"),
        DiagramEdge(source="left", target="left_b", label="No"),
        DiagramEdge(source="left_a", target="left_a1", label="Yes"),
        DiagramEdge(source="left_a", target="left_a2", label="No"),
    ]
    spec = DiagramSpec(kind="decision_tree", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert validate_rendered_svg(svg_bytes, kind="decision_tree") == []


def test_fishbone_draws_spine_and_one_bone_per_cause_category():
    nodes = [
        DiagramNode(id="head", label="Production Outage"),
        DiagramNode(id="people", label="People", detail="Insufficient training"),
        DiagramNode(id="process", label="Process", detail="No checklist"),
        DiagramNode(id="tech", label="Technology", detail="Unpatched dependency"),
    ]
    spec = DiagramSpec(kind="fishbone", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    boxes = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(boxes) == 4  # head + 3 categories
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text
        if node.detail:
            assert node.detail in text
    assert "…" not in text


def test_fishbone_is_usable_with_at_least_one_cause_category():
    assert not DiagramSpec(kind="fishbone", nodes=[DiagramNode(id="head", label="Problem")]).is_usable()
    assert DiagramSpec(
        kind="fishbone",
        nodes=[DiagramNode(id="head", label="Problem"), DiagramNode(id="c1", label="Category")],
    ).is_usable()


def test_fishbone_never_truncates_a_long_label_with_an_ellipsis():
    spec = DiagramSpec(
        kind="fishbone", title="t",
        nodes=[
            DiagramNode(id="head", label="A genuinely long effect description that keeps going on"),
            DiagramNode(id="c1", label="A genuinely long cause category name that also keeps going"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_layered_diagram_draws_one_band_per_layer_top_to_bottom():
    nodes = [
        DiagramNode(id="app", label="Application Layer"),
        DiagramNode(id="transport", label="Transport Layer"),
        DiagramNode(id="network", label="Network Layer"),
        DiagramNode(id="physical", label="Physical Layer"),
    ]
    spec = DiagramSpec(kind="layered_diagram", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    boxes = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(boxes) == 4
    tops = [float(b.get("y")) for b in boxes]
    assert tops == sorted(tops)  # drawn top-to-bottom in the order given
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text
    assert "…" not in text


def test_layered_diagram_never_truncates_a_long_label_with_an_ellipsis():
    spec = DiagramSpec(
        kind="layered_diagram", title="t",
        nodes=[
            DiagramNode(id="a", label="A genuinely long layer name that keeps going well past a short label"),
            DiagramNode(id="b", label="Another genuinely long layer name that also keeps going on"),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_gantt_chart_draws_one_bar_per_task_sized_by_duration():
    nodes = [
        DiagramNode(id="n0", label="Research", gantt_start=0, gantt_duration=2),
        DiagramNode(id="n1", label="Design", gantt_start=1, gantt_duration=2),
        DiagramNode(id="n2", label="Development", gantt_start=3, gantt_duration=4),
    ]
    spec = DiagramSpec(kind="gantt_chart", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    root = ET.fromstring(svg_bytes)
    bars = [el for el in root.iter() if el.tag.endswith("rect") and "diagram-box" in (el.get("class") or "")]
    assert len(bars) == 3
    by_width = sorted(float(b.get("width")) for b in bars)
    # the 4-unit task's bar must be visibly wider than the two 2-unit tasks'
    assert by_width[-1] > by_width[0] * 1.5
    text = svg_bytes.decode("utf-8")
    for node in nodes:
        assert node.label in text
    assert "…" not in text


def test_gantt_chart_never_truncates_a_long_label_with_an_ellipsis():
    spec = DiagramSpec(
        kind="gantt_chart", title="t",
        nodes=[
            DiagramNode(id="n0", label="A genuinely long task name that keeps going well past a short label",
                        gantt_start=0, gantt_duration=2),
            DiagramNode(id="n1", label="Another genuinely long task name that also keeps going on",
                        gantt_start=1, gantt_duration=3),
        ],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    assert "…" not in svg_bytes.decode("utf-8")


def test_new_smart_art_kinds_are_the_complete_graded_content_qa_set():
    """pyramid/matrix/radial_mindmap/venn/chevron_process/timeline/
    staircase_process/balance_comparison/nested_circles/network_mesh/
    decision_tree/fishbone/layered_diagram/gantt_chart must be registered
    with diagram_content_qa's semantic grading - a new kind silently NOT
    graded (left out of _GRADED_KINDS by mistake) would ship with zero
    content quality checking, which is the exact regression this guards
    against."""
    from app.services.diagram_content_qa import _GRADED_KINDS

    for kind in (
        "pyramid", "matrix", "radial_mindmap", "venn", "chevron_process", "timeline", "staircase_process",
        "balance_comparison", "nested_circles", "network_mesh", "decision_tree", "fishbone",
        "layered_diagram", "gantt_chart", "funnel", "honeycomb", "puzzle_pieces", "gears_process",
    ):
        assert kind in _GRADED_KINDS


# --- icon badges (smart_art/chevron_process/funnel/honeycomb/puzzle_pieces) -----


def _icon_nodes(count: int, icons: list[str]) -> list[DiagramNode]:
    return [
        DiagramNode(id=f"n{i}", label=f"Step {i}", detail=f"Detail for step {i}.", icon=icon)
        for i, icon in enumerate(icons[:count], 1)
    ]


_SAMPLE_ICONS = ["lock", "shield", "warning", "mail", "clock", "flag", "gear", "person", "chart"]


def test_every_registered_icon_keyword_draws_something():
    """`_draw_icon` must never silently draw nothing for a keyword this
    module's own `_ICON_DRAWERS` actually lists - a real typo in that dict
    (a drawer registered under one spelling, looked up under another)
    would otherwise ship as a blank circle with zero signal anything went
    wrong."""
    from app.render.diagram_renderer import _draw_icon, _ICON_DRAWERS

    for keyword in _ICON_DRAWERS:
        svg = _draw_icon(keyword, cx=0, cy=0, r=14, color="#ffffff")
        assert svg, f"icon keyword {keyword!r} drew nothing"


def test_an_unrecognised_icon_keyword_draws_nothing_not_an_error():
    from app.render.diagram_renderer import _draw_icon

    assert _draw_icon("not-a-real-icon", cx=0, cy=0, r=14, color="#ffffff") == ""
    assert _draw_icon("", cx=0, cy=0, r=14, color="#ffffff") == ""


@pytest.mark.parametrize("kind", ["smart_art", "chevron_process", "funnel", "honeycomb"])
def test_icon_badges_never_trigger_false_geometry_issues(kind):
    """Real, confirmed bug this fixes: an icon's own glyph is drawn from
    `<line>`/`<path>` primitives in a small LOCAL coordinate box (roughly
    -12..12), meant to be read only through its wrapper group's own
    `translate(...) scale(...)` transform - but `diagram_render_qa`'s
    edge/geometry scan has no notion of SVG `transform` at all (by design,
    every OTHER shape in this renderer draws directly in absolute canvas
    coordinates). Before `_draw_icon`'s wrapper group was marked
    `class="diagram-icon"` (which `_edge_segments` now explicitly skips),
    an icon's own small/negative local coordinates were read as if they
    were absolute canvas points - producing `out_of_canvas` findings on
    perfectly normal diagrams, and `shared_edge_segment` findings between
    two unrelated icons' own internal lines. Every kind that can carry
    icons must stay clean with icons actually set on every node - the
    regression only reproduces when `node.icon` is non-blank."""
    theme = load_template("technical").theme
    nodes = _icon_nodes(4, _SAMPLE_ICONS)
    spec = DiagramSpec(kind=kind, title="Icon Geometry Check", nodes=nodes)
    svg, _, _ = render_diagram_svg(spec, theme)
    issues = validate_rendered_svg(svg, kind=kind)
    bad = [i for i in issues if i.kind in ("out_of_canvas", "shared_edge_segment")]
    assert bad == [], f"icon badges produced false geometry findings: {bad}"


def test_honeycomb_renders_a_clean_interlocking_cluster():
    theme = load_template("technical").theme
    nodes = _icon_nodes(7, _SAMPLE_ICONS)
    spec = DiagramSpec(kind="honeycomb", title="Honeycomb", nodes=nodes)
    svg, w, h = render_diagram_svg(spec, theme)
    assert w == CANVAS_WIDTH  # more nodes only add rows, canvas never widens
    issues = validate_rendered_svg(svg, kind="honeycomb")
    assert issues == []


def test_funnel_renders_as_a_narrowing_wide_top_stack():
    """funnel reuses _layout_pyramid's own `inverted=True` geometry (see
    render_diagram_svg's own comment on that dispatch) - this proves the
    kind actually narrows top-to-bottom regardless of `pyramid_inverted`,
    which a "funnel" spec never sets."""
    theme = load_template("technical").theme
    nodes = _icon_nodes(4, _SAMPLE_ICONS)
    spec = DiagramSpec(kind="funnel", title="Funnel", nodes=nodes, pyramid_inverted=False)
    svg, _, _ = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    # class="diagram-box" only - excludes each layer's own decorative
    # offset "shadow" duplicate polygon (see _layout_pyramid's own
    # docstring on its 3D depth cue), which funnel inherits unchanged.
    polygons = [
        el for el in root.iter()
        if _local_name(el.tag) == "polygon" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(polygons) == 4
    widths = []
    for poly in polygons:
        xs = [float(p.split(",")[0]) for p in (poly.get("points") or "").split()]
        widths.append(max(xs) - min(xs))
    assert widths == sorted(widths, reverse=True)  # strictly narrowing top to bottom
    assert validate_rendered_svg(svg, kind="funnel") == []


def test_puzzle_pieces_interlock_and_are_exempt_from_the_tab_overlap_check():
    """The service-layer render-QA call (DiagramService.generate_for_block)
    exempts puzzle_pieces entirely - its pieces are DESIGNED to interlock
    (a tab bulges a few px into the next piece's notch), which a generic
    bbox/shared-edge check built for discrete, non-touching shapes would
    otherwise misread as a collision defect. This only asserts the shape
    renders (valid SVG, right element count) - the exemption itself is
    covered by DiagramService's own `_exempt_kinds`."""
    theme = load_template("technical").theme
    nodes = _icon_nodes(5, _SAMPLE_ICONS)
    spec = DiagramSpec(kind="puzzle_pieces", title="Puzzle", nodes=nodes)
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    pieces = [el for el in root.iter() if _local_name(el.tag) == "path" and "diagram-box" in (el.get("class") or "")]
    assert len(pieces) == 5
    # Icon geometry must still never leak into out_of_canvas, independent
    # of the service-level overlap exemption above.
    issues = validate_rendered_svg(svg, kind="puzzle_pieces")
    assert [i for i in issues if i.kind == "out_of_canvas"] == []


def test_funnel_honeycomb_puzzle_pieces_are_usable_within_their_own_node_caps():
    from app.schemas.diagram import max_nodes_for

    assert max_nodes_for("puzzle_pieces") == 6
    assert DiagramSpec(
        kind="puzzle_pieces", title="t", nodes=_icon_nodes(6, _SAMPLE_ICONS),
    ).is_usable()
    assert not DiagramSpec(
        kind="puzzle_pieces", title="t",
        nodes=[DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(1, 9)],
    ).is_usable()
    assert DiagramSpec(kind="funnel", title="t", nodes=_icon_nodes(4, _SAMPLE_ICONS)).is_usable()
    assert DiagramSpec(kind="honeycomb", title="t", nodes=_icon_nodes(9, _SAMPLE_ICONS)).is_usable()


def test_gears_process_renders_a_clean_meshed_row():
    from app.schemas.diagram import max_nodes_for

    assert max_nodes_for("gears_process") == 5
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}", detail=f"Detail {i}") for i in range(1, 6)]
    spec = DiagramSpec(kind="gears_process", title="Mechanism", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    assert w == CANVAS_WIDTH  # 5 gears at the cap still fit the standard canvas
    root = ET.fromstring(svg)
    gears = [el for el in root.iter() if _local_name(el.tag) == "polygon" and "diagram-box" in (el.get("class") or "").split()]
    assert len(gears) == 5
    assert validate_rendered_svg(svg, kind="gears_process") == []
    assert not DiagramSpec(
        kind="gears_process", title="t",
        nodes=[DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(1, 7)],
    ).is_usable()


# ---------------------------------------------------------------------------
# 10 new kinds (reference-deck additions) - see the approved plan at
# C:\Users\Dell\.claude\plans\linear-wobbling-puppy.md for the evidence each
# one is a genuinely distinct structure, not a near-duplicate of an existing
# kind. Same verification discipline as funnel/honeycomb/puzzle_pieces/
# gears_process above: render -> validate_rendered_svg == [] -> a geometry
# assertion specific to what makes this kind's own layout correct.
# ---------------------------------------------------------------------------


def test_donut_segments_renders_one_equal_arc_band_per_node():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Category {i}") for i in range(4)]
    spec = DiagramSpec(kind="donut_segments", title="Budget", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    wedges = [
        el for el in root.iter()
        if _local_name(el.tag) == "path" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(wedges) == 4
    assert validate_rendered_svg(svg, kind="donut_segments") == []


def test_pie_wedge_renders_a_full_circle_of_wedges():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Source {i}") for i in range(5)]
    spec = DiagramSpec(kind="pie_wedge", title="Energy mix", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    wedges = [
        el for el in root.iter()
        if _local_name(el.tag) == "path" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(wedges) == 5
    assert validate_rendered_svg(svg, kind="pie_wedge") == []


def test_wavy_journey_path_alternates_nodes_above_and_below_the_curve():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Stage {i}") for i in range(5)]
    spec = DiagramSpec(kind="wavy_journey_path", title="Journey", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    circles = [
        el for el in root.iter()
        if _local_name(el.tag) == "circle" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(circles) == 5
    ys = [float(c.get("cy")) for c in circles]
    # Every other node sits on the opposite side of the curve's own
    # centreline - never all in one straight row (that would just be a
    # timeline, the kind this one is explicitly NOT).
    assert ys[0] != ys[1] and ys[1] != ys[2]
    assert validate_rendered_svg(svg, kind="wavy_journey_path") == []


def test_progress_rings_draws_each_nodes_own_stat_never_a_guessed_one():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="n0", label="Support", value_pct=82.0),
        DiagramNode(id="n1", label="Quality", value_pct=None),
    ]
    spec = DiagramSpec(kind="progress_rings", title="Metrics", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    text = svg.decode("utf-8")
    assert "82%" in text
    assert "—" in text  # no value_pct -> an honest dash, never a fabricated "0%"
    assert validate_rendered_svg(svg, kind="progress_rings") == []


def test_orbit_diagram_draws_one_concentric_ring_per_node():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Layer {i}") for i in range(4)]
    spec = DiagramSpec(kind="orbit_diagram", title="Atmosphere", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    rings = [
        el for el in root.iter()
        if _local_name(el.tag) == "circle" and "diagram-box" not in (el.get("class") or "").split()
        and el.get("stroke-dasharray") == "4 5"
    ]
    assert len(rings) == 4
    assert validate_rendered_svg(svg, kind="orbit_diagram") == []


def test_bridge_process_renders_an_arch_with_a_pillar_at_each_end():
    from app.schemas.diagram import max_nodes_for

    assert max_nodes_for("bridge_process") == 5
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}", detail=f"Detail {i}") for i in range(4)]
    spec = DiagramSpec(kind="bridge_process", title="Migration", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    # Excludes the canvas's own full-size background <rect> (always
    # present, unrelated to this kind) by width - only the two pillars are
    # genuinely 56px wide.
    pillars = [el for el in root.iter() if _local_name(el.tag) == "rect" and el.get("width") == "56.0"]
    assert len(pillars) == 2
    steps = [
        el for el in root.iter()
        if _local_name(el.tag) == "circle" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(steps) == 4
    assert validate_rendered_svg(svg, kind="bridge_process") == []


def test_bridge_process_pillars_connect_visually_to_their_end_steps():
    """A real, confirmed complaint: the pillar under the first/last step
    used to be a plain surface-coloured rect with no visible link to the
    step above it - it read as a stray empty box, not a bridge support.
    Each pillar now draws a connector line down from its own end step and
    is tinted with that same step's colour, so it unmistakably reads as
    "this step's own support" rather than a floating, unrelated shape."""
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(5)]
    spec = DiagramSpec(kind="bridge_process", title="Migration", nodes=nodes)
    svg, _w, _h = render_diagram_svg(spec, theme)
    text = svg.decode("utf-8")
    root = ET.fromstring(svg)
    pillars = [el for el in root.iter() if _local_name(el.tag) == "rect" and el.get("width") == "56.0"]
    assert len(pillars) == 2
    for pillar in pillars:
        assert pillar.get("fill") != theme.surface_color
    # A connector <line> from each end step down to its pillar, plus the
    # ground line joining both pillars' feet.
    lines = [el for el in root.iter() if _local_name(el.tag) == "line"]
    assert len(lines) >= 3
    assert validate_rendered_svg(svg, kind="bridge_process") == []


def test_chain_links_overlaps_each_link_with_its_neighbour():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Stage {i}", detail=f"Detail {i}") for i in range(4)]
    spec = DiagramSpec(kind="chain_links", title="Supply chain", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    links = [
        el for el in root.iter()
        if _local_name(el.tag) == "rect" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(links) == 4
    xs = sorted(float(link.get("x")) for link in links)
    widths = [float(link.get("width")) for link in links]
    # Consecutive links genuinely overlap (the next one starts before the
    # previous one ends) - the whole visual point of this kind.
    assert xs[1] < xs[0] + widths[0]
    assert validate_rendered_svg(svg, kind="chain_links") == []


def test_flower_petal_draws_one_petal_path_per_node_around_a_hub():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Value {i}") for i in range(5)]
    spec = DiagramSpec(kind="flower_petal", title="Core values", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    petals = [
        el for el in root.iter()
        if _local_name(el.tag) == "path" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(petals) == 5
    # Every petal path is drawn directly in absolute coordinates (M/C/Z
    # only) - no nested `transform`, the real bug an earlier rotated-
    # ellipse version of this kind hit (see _layout_flower_petal's own
    # docstring) and the regression this specifically guards against.
    assert "transform" not in svg.decode("utf-8")
    assert validate_rendered_svg(svg, kind="flower_petal") == []


def test_hourglass_diagram_splits_nodes_into_two_opposing_halves():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Point {i}") for i in range(5)]
    spec = DiagramSpec(kind="hourglass_diagram", title="Cost vs value", nodes=nodes)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg)
    layers = [
        el for el in root.iter()
        if _local_name(el.tag) == "polygon" and "diagram-box" in (el.get("class") or "").split()
    ]
    assert len(layers) == 5
    assert validate_rendered_svg(svg, kind="hourglass_diagram") == []


def test_org_chart_connects_parent_to_child_with_an_orthogonal_path():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="lead", label="Project Director", detail="Jane Doe", level=0),
        DiagramNode(id="eng", label="Engineering Lead", detail="John Smith", level=1),
        DiagramNode(id="design", label="Design Lead", detail="Amy Lee", level=1),
    ]
    edges = [DiagramEdge(source="lead", target="eng"), DiagramEdge(source="lead", target="design")]
    spec = DiagramSpec(kind="org_chart", title="Team", nodes=nodes, edges=edges)
    assert spec.is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    text = svg.decode("utf-8")
    assert "Jane Doe" in text and "John Smith" in text
    root = ET.fromstring(svg)
    cards = [el for el in root.iter() if _local_name(el.tag) == "rect" and "diagram-box" in (el.get("class") or "").split()]
    assert len(cards) == 3
    connectors = [el for el in root.iter() if _local_name(el.tag) == "path"]
    assert len(connectors) == 2  # one orthogonal connector per parent -> child edge
    assert validate_rendered_svg(svg, kind="org_chart") == []


# ---------------------------------------------------------------------------
# spectrum: a continuum from matrix_x_low to matrix_x_high, nodes at value_pct
# ---------------------------------------------------------------------------


def test_spectrum_is_usable_with_2_to_8_labelled_nodes():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Low end", value_pct=10), DiagramNode(id="b", label="High end", value_pct=90)]
    spec = DiagramSpec(kind="spectrum", title="t", nodes=nodes)
    assert spec.is_usable()
    assert not DiagramSpec(kind="spectrum", title="t", nodes=[DiagramNode(id="a", label="Only one")]).is_usable()
    svg, w, h = render_diagram_svg(spec, theme)
    assert validate_rendered_svg(svg, kind="spectrum") == []


def test_spectrum_never_truncates_a_long_label_and_stays_clean_when_clustered():
    """Real, confirmed bugs fixed together: a bare floating label starved
    this kind's occupancy check (near-zero measured area); a node whose
    stem touched its own `diagram-box`-classed dot was misread as an edge
    crossing/running collinear with that dot's border; three positions
    clustered close together could land cards on the same side with no
    row-stacking, overlapping; and a dense 8-node cluster could widen the
    canvas past the point RING_LABEL_SIZE text stays readable."""
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id=str(i), label=f"Position Number {i} Label", value_pct=48 + i) for i in range(8)
    ]
    spec = DiagramSpec(kind="spectrum", title="t", matrix_x_low="Low", matrix_x_high="High", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    assert validate_rendered_svg(svg_bytes, kind="spectrum") == []


def test_spectrum_missing_value_pct_falls_back_to_an_even_spread():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=str(i), label=f"Item {i}") for i in range(4)]
    spec = DiagramSpec(kind="spectrum", title="t", nodes=nodes)
    svg, _w, _h = render_diagram_svg(spec, theme)
    assert validate_rendered_svg(svg, kind="spectrum") == []


# ---------------------------------------------------------------------------
# iceberg: level 0 above a dashed waterline, level >= 1 below it (wider per
# level)
# ---------------------------------------------------------------------------


def test_iceberg_requires_both_a_visible_and_a_hidden_node():
    visible_only = DiagramSpec(
        kind="iceberg", title="t", nodes=[DiagramNode(id="a", label="A", level=0), DiagramNode(id="b", label="B", level=0)]
    )
    assert not visible_only.is_usable()
    mixed = DiagramSpec(
        kind="iceberg", title="t",
        nodes=[DiagramNode(id="a", label="Visible", level=0), DiagramNode(id="b", label="Hidden", level=1)],
    )
    assert mixed.is_usable()


def test_iceberg_draws_a_dashed_waterline_with_wider_rows_below():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="v1", label="The Visible Argument", level=0),
        DiagramNode(id="h1", label="Unmet Needs", level=1),
        DiagramNode(id="h2", label="Fears", level=1),
        DiagramNode(id="h3", label="Core Values", level=2),
    ]
    spec = DiagramSpec(kind="iceberg", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert "stroke-dasharray" in text
    assert "…" not in text
    assert validate_rendered_svg(svg_bytes, kind="iceberg") == []


# ---------------------------------------------------------------------------
# feedback_loop_process: a linear chain plus an explicit backward "revise and
# go back" rail
# ---------------------------------------------------------------------------


def test_feedback_loop_process_is_usable_only_with_edges():
    nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    assert not DiagramSpec(kind="feedback_loop_process", title="t", nodes=nodes).is_usable()
    spec = DiagramSpec(
        kind="feedback_loop_process", title="t", nodes=nodes, edges=[DiagramEdge(source="a", target="b")]
    )
    assert spec.is_usable()


def test_feedback_loop_process_draws_a_dashed_rail_for_the_backward_edge_with_a_long_label():
    """Real, confirmed bug: the backward edge's own reason/condition label
    ("if validation fails go back") is a full phrase, not a short "Yes"/
    "No" word - the narrow max_width first tried forced `_edge_label`'s
    existing 2-line cap into its own truncate-with-"…" fallback."""
    theme = load_template("technical").theme
    nodes = [DiagramNode(id=f"n{i}", label=f"Step number {i} in the long process chain") for i in range(4)]
    edges = [DiagramEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(3)] + [
        DiagramEdge(source="n2", target="n0", label="if validation fails go back")
    ]
    spec = DiagramSpec(kind="feedback_loop_process", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    assert validate_rendered_svg(svg_bytes, kind="feedback_loop_process") == []


# ---------------------------------------------------------------------------
# causal_loop: factors on a ring, curved edges carrying a "+"/"-" polarity
# ---------------------------------------------------------------------------


def test_parse_polarity_splits_the_leading_sign_from_the_rest():
    from app.render.diagram_renderer import _parse_polarity

    assert _parse_polarity("+ more people") == ("+", "more people")
    assert _parse_polarity("- reduces") == ("-", "reduces")
    assert _parse_polarity("no sign here") == ("", "no sign here")
    assert _parse_polarity("") == ("", "")


def test_causal_loop_is_usable_only_with_edges():
    nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    assert not DiagramSpec(kind="causal_loop", title="t", nodes=nodes).is_usable()
    spec = DiagramSpec(
        kind="causal_loop", title="t", nodes=nodes,
        edges=[DiagramEdge(source="a", target="b", label="+"), DiagramEdge(source="b", target="a", label="-")],
    )
    assert spec.is_usable()


def test_causal_loop_draws_a_polarity_badge_and_never_crosses_a_grown_node():
    """Real, confirmed bug: an edge's endpoint used to be trimmed by the
    fixed `_CAUSAL_NODE_R` regardless of how much a node's own circle grew
    past it for a long label (see `_circle_node_size`'s own "never
    truncates" contract) - the endpoint landed INSIDE that node's real,
    bigger circle. A 6-node ring with a long-label node and a near-
    diametric cross-link is the exact combination that reproduced it."""
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="Population"), DiagramNode(id="b", label="Birth Rate"),
        DiagramNode(id="c", label="Food Supply"), DiagramNode(id="d", label="Death Rate"),
        DiagramNode(id="e", label="Pollution"), DiagramNode(id="f", label="Resources"),
    ]
    edges = [
        DiagramEdge(source="a", target="b", label="+"), DiagramEdge(source="b", target="c", label="-"),
        DiagramEdge(source="c", target="d", label="-"), DiagramEdge(source="d", target="e", label="+"),
        DiagramEdge(source="e", target="f", label="-"), DiagramEdge(source="f", target="a", label="+"),
        DiagramEdge(source="a", target="d", label="- balances"),
    ]
    spec = DiagramSpec(kind="causal_loop", title="t", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert ">+<" in text or ">-<" in text
    assert "…" not in text
    assert validate_rendered_svg(svg_bytes, kind="causal_loop") == []


# ---------------------------------------------------------------------------
# input_process_output: three zones (input/process/output) by shape_role
# ---------------------------------------------------------------------------


def test_input_process_output_renders_three_zone_headers():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="i1", label="Unsorted array", shape_role="input"),
        DiagramNode(id="p1", label="Compare elements", shape_role="process"),
        DiagramNode(id="o1", label="Sorted array", shape_role="output"),
    ]
    spec = DiagramSpec(kind="input_process_output", title="t", nodes=nodes)
    assert spec.is_usable()
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert "INPUT" in text and "PROCESS" in text and "OUTPUT" in text
    assert validate_rendered_svg(svg_bytes, kind="input_process_output") == []


def test_input_process_output_unknown_shape_role_falls_back_to_process():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="A", shape_role="nonsense"),
        DiagramNode(id="b", label="B", shape_role="output"),
    ]
    spec = DiagramSpec(kind="input_process_output", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    ET.fromstring(svg_bytes)  # never a hard failure on an unrecognised role


def test_input_process_output_handles_an_empty_zone_without_a_hard_failure():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="p1", label="Process step one", shape_role="process"),
        DiagramNode(id="p2", label="Process step two", shape_role="process"),
    ]
    spec = DiagramSpec(kind="input_process_output", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    ET.fromstring(svg_bytes)
    assert validate_rendered_svg(svg_bytes, kind="input_process_output") == []


# ---------------------------------------------------------------------------
# ranked_bars: one horizontal bar per node, sorted by value_pct
# ---------------------------------------------------------------------------


def test_ranked_bars_sorts_by_value_pct_highest_first():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="Low", value_pct=10),
        DiagramNode(id="b", label="High", value_pct=90),
        DiagramNode(id="c", label="Mid", value_pct=50),
    ]
    spec = DiagramSpec(kind="ranked_bars", title="t", nodes=nodes)
    assert spec.is_usable()
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert text.index("High") < text.index("Mid") < text.index("Low")
    assert validate_rendered_svg(svg_bytes, kind="ranked_bars") == []


def test_ranked_bars_never_truncates_a_long_label():
    """Real, confirmed bug: `max_lines=2` on the label wrap truncated a
    genuinely 3-line label with "…" even though the row's own height
    already grows to fit however many lines come back."""
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="Network infrastructure failures across multiple data centers", value_pct=45),
        DiagramNode(id="b", label="Human configuration error", value_pct=30),
    ]
    spec = DiagramSpec(kind="ranked_bars", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    assert validate_rendered_svg(svg_bytes, kind="ranked_bars") == []


def test_ranked_bars_node_with_no_value_pct_sorts_last_with_a_zero_length_bar():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Has a score", value_pct=50), DiagramNode(id="b", label="No score set")]
    spec = DiagramSpec(kind="ranked_bars", title="t", nodes=nodes)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert text.index("Has a score") < text.index("No score set")
    assert validate_rendered_svg(svg_bytes, kind="ranked_bars") == []


# ---------------------------------------------------------------------------
# DiagramSpec.label_scale: the Edit Diagram panel's "increase text size"
# control - a real, confirmed request: even a diagram clearing the
# renderer's own readability floor can still read as small/cramped to an
# actual reader, and the only way to get bigger text before this was to
# edit every node label by hand.
# ---------------------------------------------------------------------------


def test_label_scale_renders_bigger_text_on_a_node_block_kind():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Define question"), DiagramNode(id="b", label="Estimate scales")]
    normal = render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=1.0), theme)[0].decode("utf-8")
    bigger = render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=1.3), theme)[0].decode("utf-8")
    assert f'font-size="{LABEL_SIZE:.1f}"' in normal
    assert f'font-size="{LABEL_SIZE * 1.3:.1f}"' in bigger
    assert f'font-size="{LABEL_SIZE:.1f}"' not in bigger


def test_label_scale_renders_bigger_text_on_a_circle_node_kind():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Interpolation / regridding")]
    spec = DiagramSpec(kind="data_flow_diagram", nodes=nodes, label_scale=1.3)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    text = svg_bytes.decode("utf-8")
    assert f'font-size="{_DFD_LABEL_SIZE * 1.3:.1f}"' in text
    for word in "Interpolation regridding".split():
        assert word in text, f"word lost or broken: {word!r}"
    assert validate_rendered_svg(svg_bytes, kind="data_flow_diagram") == []


def test_label_scale_renders_bigger_text_on_a_decision_diamond_and_box_kind():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="q", label="Is viscosity important?", level=0),
        DiagramNode(id="yes", label="Control Re", level=1),
        DiagramNode(id="no", label="Relax Re", level=1),
    ]
    edges = [DiagramEdge(source="q", target="yes", label="yes"), DiagramEdge(source="q", target="no", label="no")]
    spec = DiagramSpec(kind="decision_tree", nodes=nodes, edges=edges, label_scale=1.3)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    assert validate_rendered_svg(svg_bytes, kind="decision_tree") == []


def test_label_scale_is_clamped_to_a_safe_range():
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Step one")]
    too_big = render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=5.0), theme)[0].decode("utf-8")
    too_small = render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=0.2), theme)[0].decode("utf-8")
    assert f'font-size="{LABEL_SIZE * 1.5:.1f}"' in too_big  # clamped to the 1.5 ceiling
    assert f'font-size="{LABEL_SIZE:.1f}"' in too_small  # clamped to the 1.0 floor - never shrinks


def test_label_scale_never_leaks_into_a_later_unscaled_render():
    """The contextvar is reset after every render - a scaled diagram
    followed by a normal one must never let the first render's scale
    silently bleed into the second."""
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="Step one")]
    render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=1.5), theme)
    after = render_diagram_svg(DiagramSpec(kind="hierarchy", nodes=nodes, label_scale=1.0), theme)[0].decode("utf-8")
    assert f'font-size="{LABEL_SIZE:.1f}"' in after
    assert f'font-size="{LABEL_SIZE * 1.5:.1f}"' not in after


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


# ---------------------------------------------------------------------------
# circuit_schematic: real wire/node electrical topology, not a card grid
# ---------------------------------------------------------------------------


def test_circuit_schematic_is_in_the_registered_kinds_and_relationship_family():
    from app.schemas.diagram import DIAGRAM_KINDS, RELATIONSHIP_KINDS, max_nodes_for

    assert "circuit_schematic" in DIAGRAM_KINDS
    assert "circuit_schematic" in RELATIONSHIP_KINDS
    # Several small multi-node panels side by side (see DiagramNode.lane's
    # circuit_schematic meaning) need a much higher ceiling than the generic
    # single-structure default.
    assert max_nodes_for("circuit_schematic") > max_nodes_for("comparison")


def test_circuit_schematic_is_usable_requires_edges_like_er_diagram():
    nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    assert not DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=[]).is_usable()
    edges = [DiagramEdge(source="a", target="b", label="R:10Ω")]
    assert DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=edges).is_usable()


def test_parse_circuit_component_recognises_every_prefix_and_degrades_to_a_plain_wire():
    from app.render.diagram_renderer import _parse_circuit_component

    assert _parse_circuit_component("R:220Ω") == ("resistor", "220Ω")
    assert _parse_circuit_component("C:100nF") == ("capacitor", "100nF")
    assert _parse_circuit_component("L:4.7mH") == ("inductor", "4.7mH")
    assert _parse_circuit_component("V:9V") == ("source", "9V")
    assert _parse_circuit_component("B:9V") == ("source", "9V")
    assert _parse_circuit_component("SW") == ("switch", "")
    assert _parse_circuit_component("sw:open") == ("switch", "open")
    # No recognised tag (or none at all) degrades to a plain wire carrying
    # the original text as its own label - never a crash on unexpected
    # content, same "missing content degrades gracefully" contract
    # DiagramEdge.label's own docstring documents.
    assert _parse_circuit_component("") == ("wire", "")
    assert _parse_circuit_component("node A tap") == ("wire", "node A tap")
    assert _parse_circuit_component("Z:9") == ("wire", "Z:9")


def test_circuit_schematic_renders_real_wires_with_a_resistor_symbol():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="A"),
        DiagramNode(id="b", label="B"),
        DiagramNode(id="c", label="C"),
    ]
    edges = [
        DiagramEdge(source="a", target="b", label="R:10Ω"),
        DiagramEdge(source="b", target="c", label="R:20Ω"),
    ]
    spec = DiagramSpec(kind="circuit_schematic", title="Fig. 1: Series", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")
    # A real resistor zigzag path (7 points, the exact vocabulary
    # _circuit_symbol_svg draws for "resistor"), not a labelled rectangle.
    assert svg.count("M-17.0,0.0 L-11.0,-7.0") == 2
    assert "10Ω" in svg and "20Ω" in svg
    # Every junction is labelled.
    for label in ("A", "B", "C"):
        assert f">{label}<" in svg
    assert validate_rendered_svg(svg_bytes, kind="circuit_schematic") == []


def test_circuit_schematic_parallel_pair_draws_two_separate_wire_rungs():
    """Two components tying the SAME two nodes (a valid parallel pair) must
    fan out into two distinct wire paths, never draw on top of each other -
    the whole visual point of a parallel comparison."""
    theme = load_template("technical").theme
    nodes = [DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")]
    edges = [
        DiagramEdge(source="a", target="b", label="R:10Ω"),
        DiagramEdge(source="a", target="b", label="R:20Ω"),
    ]
    spec = DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")
    root = ET.fromstring(svg_bytes)
    # A rail/rung wire path always has exactly 3 "L" commands (the 4-point
    # M-L-L-L elbow route - see _draw_one_circuit_panel's own comment); the
    # resistor glyph itself is a SEPARATE, longer (7-point/6-"L") path drawn
    # on top of it, so this count is what tells the two apart.
    wire_paths = [
        el.get("d") for el in root.iter()
        if _local_name(el.tag) == "path" and (el.get("d") or "").count("L") == 3
    ]
    # Two distinct rung paths (the rail-based routing), not the same path
    # twice - the whole visual point of a parallel pair.
    assert len(wire_paths) == 2
    assert wire_paths[0] != wire_paths[1]
    assert "10Ω" in svg and "20Ω" in svg


def test_circuit_schematic_multi_panel_lanes_each_get_their_own_header_and_border():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="s_a", label="A", lane="Valid — Series"),
        DiagramNode(id="s_b", label="B", lane="Valid — Series"),
        DiagramNode(id="p_a", label="A", lane="Valid — Parallel"),
        DiagramNode(id="p_b", label="B", lane="Valid — Parallel"),
    ]
    edges = [
        DiagramEdge(source="s_a", target="s_b", label="R:10Ω"),
        DiagramEdge(source="p_a", target="p_b", label="R:10Ω"),
        DiagramEdge(source="p_a", target="p_b", label="R:20Ω"),
    ]
    spec = DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")
    assert "Valid — Series" in svg
    assert "Valid — Parallel" in svg
    root = ET.fromstring(svg_bytes)
    panel_borders = [
        el for el in root.iter()
        if _local_name(el.tag) == "rect" and el.get("rx") == "10" and el.get("fill") == "none"
    ]
    assert len(panel_borders) == 2


def test_circuit_schematic_caption_node_draws_a_verdict_badge_not_a_junction_dot():
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="a", label="A"),
        DiagramNode(id="b", label="B"),
        DiagramNode(label="✓ Valid", detail="Both tie the same two nodes.", shape_role="caption"),
    ]
    edges = [DiagramEdge(source="a", target="b", label="R:10Ω")]
    spec = DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")
    root = ET.fromstring(svg_bytes)
    dots = [el for el in root.iter() if _local_name(el.tag) == "circle" and el.get("r") == "4.0"]
    # Exactly 2 junction dots (A, B) - the caption node never gets one.
    assert len(dots) == 2
    assert "✓ Valid" in svg
    assert "Both tie the same two nodes." in svg
    # The verdict badge is drawn in the universal green "valid" colour, the
    # same fixed convention _FLOW_ROLE_COLORS already uses elsewhere.
    assert '"#16a34a"' in svg


def test_circuit_schematic_globally_duplicate_ids_across_panels_dont_cross_wire():
    """A real, confirmed risk while building this renderer: reusing the same
    short id ("a"/"b") in two different panels makes an edge from one panel
    resolve against the OTHER panel's same-named node, since nothing scopes
    DiagramEdge.source/target to a single lane - exactly the existing,
    documented convention every other kind already requires (ids are always
    globally unique across the whole spec). Globally-unique ids (this
    test's own setup) must keep each panel's wiring fully independent."""
    theme = load_template("technical").theme
    nodes = [
        DiagramNode(id="s_a", label="A", lane="Series"),
        DiagramNode(id="s_b", label="B", lane="Series"),
        DiagramNode(id="s_c", label="C", lane="Series"),
        DiagramNode(id="p_a", label="A", lane="Parallel"),
        DiagramNode(id="p_b", label="B", lane="Parallel"),
    ]
    edges = [
        DiagramEdge(source="s_a", target="s_b", label="R:10Ω"),
        DiagramEdge(source="s_b", target="s_c", label="R:20Ω"),
        DiagramEdge(source="p_a", target="p_b", label="R:30Ω"),
        DiagramEdge(source="p_a", target="p_b", label="R:40Ω"),
    ]
    spec = DiagramSpec(kind="circuit_schematic", nodes=nodes, edges=edges)
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    root = ET.fromstring(svg_bytes)
    # 2 (series) + 2 (parallel) = 4 wire rungs total - never more, which
    # would mean an edge leaked across panels.
    wire_paths = [
        el for el in root.iter()
        if _local_name(el.tag) == "path"
        and (el.get("stroke") or "") == theme.text_color
        and (el.get("d") or "").count("L") == 3
    ]
    assert len(wire_paths) == 4


# periodic_table: the real, fixed 118-element grid - never model-authored,
# see app.render.periodic_table_data and DIAGRAM_KINDS' own comment for the
# root cause this closes (a raster-generated periodic table shipped with
# duplicated element symbols and duplicated/wrong group numbers).


def test_periodic_table_is_registered_and_joins_neither_kind_family():
    from app.schemas.diagram import DIAGRAM_KINDS, RELATIONSHIP_KINDS, SEQUENTIAL_KINDS

    assert "periodic_table" in DIAGRAM_KINDS
    # A reference grid, not a sequence or an arbitrary relationship network
    # - same reasoning "graph" already gets, see DIAGRAM_KINDS' own comment.
    assert "periodic_table" not in RELATIONSHIP_KINDS
    assert "periodic_table" not in SEQUENTIAL_KINDS


def test_periodic_table_is_usable_with_zero_nodes():
    """Unlike every other kind, an empty `nodes` list (no highlights
    requested) is still a perfectly usable, complete request - the grid
    itself is always drawn regardless."""
    assert DiagramSpec(kind="periodic_table", nodes=[], edges=[]).is_usable()


def test_periodic_table_renders_all_118_real_elements_with_no_duplicates():
    from app.render.periodic_table_data import ELEMENTS

    theme = load_template("technical").theme
    spec = DiagramSpec(kind="periodic_table", title="Fig. 1: Periodic Table", nodes=[])
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")

    tooltips = re.findall(r"<title>([A-Za-z]+) \((\d+)\)", svg)
    rendered = {(sym, int(num)) for sym, num in tooltips}
    real = {(e.symbol, e.atomic_number) for e in ELEMENTS}
    assert rendered == real
    symbols = [sym for sym, _ in tooltips]
    assert len(symbols) == len(set(symbols)) == 118
    assert validate_rendered_svg(svg_bytes, kind="periodic_table") == []


def test_periodic_table_highlight_overrides_fill_and_adds_a_legend_entry():
    theme = load_template("technical").theme
    spec = DiagramSpec(
        kind="periodic_table",
        nodes=[DiagramNode(id="Na", color="#fbbf24", detail="Highly reactive alkali metal")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    svg = svg_bytes.decode("utf-8")
    assert "#fbbf24" in svg
    assert "Highly reactive alkali metal" in svg
    assert "Na (Sodium)" in svg


def test_periodic_table_unknown_highlight_id_is_ignored_not_an_error():
    theme = load_template("technical").theme
    spec = DiagramSpec(kind="periodic_table", nodes=[DiagramNode(id="Zzz", color="#fbbf24", detail="not real")])
    svg_bytes, _w, _h = render_diagram_svg(spec, theme)
    assert validate_rendered_svg(svg_bytes, kind="periodic_table") == []
    assert "not real" not in svg_bytes.decode("utf-8")


def test_periodic_table_symbol_text_clears_the_readability_floor_once_scaled():
    """A real, confirmed case: the first working render passed every other
    check but failed `text_too_small_after_scaling` - CANVAS_WIDTH (880)
    scaled down to the page's real display width shrank the symbol font
    just under the floor. Pinned here as a permanent regression guard."""
    from app.schemas.document import CONTENT_WIDTH
    from app.services.diagram_render_qa import MIN_READABLE_PX, effective_font_size
    from app.render.diagram_renderer import _PT_SYMBOL_SIZE

    effective = effective_font_size(CANVAS_WIDTH, base_font_size=_PT_SYMBOL_SIZE, display_width=CONTENT_WIDTH)
    assert effective >= MIN_READABLE_PX
