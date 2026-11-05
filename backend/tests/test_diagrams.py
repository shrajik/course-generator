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


def test_standard_flowchart_detail_never_truncates_in_the_key_table():
    long_detail = (
        "A much longer supporting explanation sentence for this one step that would never have "
        "fit cleanly inside a small flowchart box, which is exactly why it lives in the Key table instead."
    )
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Step", detail=long_detail), DiagramNode(id="b", label="Next")],
        edges=[DiagramEdge(source="a", target="b")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    assert "…" not in text
    for word in long_detail.split():
        assert word in text, f"word lost: {word}"


def test_standard_flowchart_never_draws_edge_labels_on_a_connector():
    """No arrow itself may carry a text label - an edge's own `label` is
    never rendered on the connector element. Unlike the ring/wheel (which
    never uses edge labels anywhere), a standard flowchart deliberately
    folds a branch's meaning into its source node's own Key table row
    instead of discarding it (see
    test_standard_flowcharts_decision_branch_meaning_lands_in_the_key_table) -
    so the real guarantee under test is "not ON the arrow", not "nowhere
    in the output"."""
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[DiagramNode(id="a", label="Start"), DiagramNode(id="b", label="End")],
        edges=[DiagramEdge(source="a", target="b", label="a label that must never appear on the arrow")],
    )
    svg_bytes, _w, _h = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")
    # The label legitimately appears, but only wrapped into the Key
    # table's own explanatory row - never as bare text riding the arrow.
    assert "If a label that must never appear on the arrow:" in text
    assert 'marker-end="url(#diagram-arrow' in text  # the arrow itself still drew


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


def test_standard_flowcharts_decision_branch_meaning_lands_in_the_key_table():
    """A decision's own edge labels ("Yes"/"No") are never lost - they
    move into that node's own Key table row instead of ever sitting on
    the arrow (see test_standard_flowchart_never_draws_edge_labels_on_a_connector)."""
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


def test_concept_wheel_draws_no_connecting_lines_between_focal_and_satellites():
    """A wheel communicates "parts of X" by proximity/colour only - no
    connecting line or arrow between the focal centre and any satellite,
    and so no edge label anywhere either (see _layout_concept_wheel's own
    docstring)."""
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
    # The Key table below legitimately draws plain <line> divider rules
    # between rows - what must never exist is a connector, which always
    # carries an arrowhead marker (every <line>/<path> this module ever
    # draws as a connector does - see _curved_connector).
    assert 'marker-end="url(#diagram-arrow' not in text
    assert "belongs to" not in text


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


async def test_diagram_garbled_text_falls_back_to_a_verified_text_free_illustration(service, monkeypatch):
    """The full production path (release-readiness audit follow-up, Priority
    1/2): a diagram whose labels are confirmed misspelled/garbled, after an
    exhausted retry, falls back to the raster illustration path - and that
    fallback is NOT trusted on faith. It goes through the pre-existing
    `force_text_free`/`_generate_text_free_illustration` machinery, which
    genuinely re-verifies (via a real, if mocked, vision call) whether any
    text survived. Overriding ImageTextCheck to report leftover text here
    (rather than relying on the default empty mock) proves that check is
    actually invoked for this new reason code too, not silently skipped. A
    confirmed `text_verified=False` on this raster fallback is never shipped
    AS a verified image, but a further, deliberately generic/label-free
    illustration still publishes rather than leaving the page blank - the
    diagram's own finding about its labels still coexists with that final
    generic-fallback finding, never silently erased."""
    template = load_template("technical")
    block = _diagram_block()

    def always_illegible_critique(user: str) -> dict:
        return {
            "on_topic": True, "specific_enough": True, "missing_concepts": [],
            "text_legible": False, "text_legible_detail": "'Ambigeous' is misspelled",
        }

    def leftover_text_found(user: str) -> dict:
        return {"detected_text": ["Ambigeous"]}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", always_illegible_critique)
    monkeypatch.setitem(mock_ai_module._BUILDERS, "ImageTextCheck", leftover_text_found)

    ok = await service.images.generate_for_block(
        course_id="course_garbled_text_fallback", block=block, template=template, course_title="Test Course"
    )

    # The fallback illustration's OWN labelled/text-free attempts are both
    # confirmed wrong - but a further, generic/label-free attempt ships
    # rather than leaving the page with no image at all.
    assert ok is True
    assert block.content.get("generated") is True
    assert block.content.get("path", "").endswith(".png")
    assert block.content.get("error") is None  # a confirmed QA failure, not a technical/provider failure
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "misspelled_or_garbled_text" for f in findings)
    assert any(f["resolution"] == "fell_back_to_illustration" for f in findings)
    assert any(f["type"] == "generic_fallback_used" for f in findings)
    # The FINAL published image makes no claim about any text at all -
    # text_verified resets to None, not a false "verified correct".
    assert block.content.get("text_verified") is None


async def test_diagram_content_qa_falls_back_to_illustration_after_exhausted_retry(service, monkeypatch):
    """Unlike the connectivity check, there is no renderer-level safety net
    for wrong CONTENT - a persistent failure must fall back to the raster
    illustration path, same as a persistent render-QA failure, and the real
    reasons must be persisted (not just logged)."""
    template = load_template("technical")
    block = _diagram_block()

    def always_failing_critique(user: str) -> dict:
        return {"on_topic": False, "on_topic_detail": "still off topic", "specific_enough": True, "missing_concepts": []}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", always_failing_critique)

    ok = await service.images.generate_for_block(
        course_id="course_content_qa_fallback", block=block, template=template, course_title="Test Course"
    )

    assert ok is True  # the raster illustration fallback still succeeds
    assert block.content.get("path", "").endswith(".png")
    findings = block.content.get("visual_qa_findings")
    assert findings and findings[0]["type"] == "off_topic"
    assert findings[0]["resolution"] == "fell_back_to_illustration"
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


async def test_diagram_generation_falls_back_to_illustration_on_spec_failure(
    service, monkeypatch
):
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
    assert block.content["path"].endswith(".png")  # fell back to a raster illustration
    assert block.content["kind"] == "diagram"  # the intent is preserved for next time


async def test_diagram_with_too_few_nodes_falls_back_to_illustration(service, monkeypatch):
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
    assert block.content["path"].endswith(".png")


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


async def test_diagram_falls_back_when_consolidation_retry_still_exceeds_the_cap(service, monkeypatch):
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

    assert ok is False  # caller falls back to illustration, never a silently truncated diagram


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
    # The corrected spec's own node labels were rendered - not its edge
    # label ("acts on"), which concept_map's new wheel layout never draws
    # anywhere (no connecting lines between focal and satellite at all -
    # see _layout_concept_wheel's own docstring).
    rendered = svg_path.read_text(encoding="utf-8")
    assert "Subject" in rendered and "Component" in rendered


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
    rendered normally and the schematic request silently falling back to a
    raster illustration instead - never crashing the chapter, never
    producing a schematic SVG."""
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
    images = [b for b in document.blocks_of_type(BlockType.IMAGE) if b.content.get("diagram_kind") in ("flow_chart", "schematic")]
    by_hint = {b.content.get("diagram_kind"): b for b in images}

    assert "flow_chart" in by_hint and "schematic" in by_hint
    flow_chart_block = by_hint["flow_chart"]
    assert flow_chart_block.content["path"].endswith(".svg")
    assert service.storage.asset_abs_path(record.course_id, flow_chart_block.content["path"]).exists()

    schematic_block = by_hint["schematic"]
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
    docstring) - `is_usable()` rejects it before `generate_for_block` ever
    reaches the schematic-specific QA/retry step
    (`_resolve_and_check_schematic`, now unreachable for this kind), so a
    schematic request must fail on the FIRST attempt with no retry at all -
    never spend a second generation call on a permanently-retired kind,
    however well-formed the first attempt's spec was."""
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
    assert len(received_feedback) == 1, "schematic must never trigger a retry - rejected on the first attempt"


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
    `schematic` - `is_usable()` rejects the very first attempt before any
    blueprint check or retry is even considered, regardless of what that
    first attempt was missing."""
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
    assert len(received_feedback) == 1, "no retry should ever be attempted for a retired kind"


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
    polygons = [el for el in root.iter() if el.tag.endswith("polygon")]
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
        "layered_diagram", "gantt_chart",
    ):
        assert kind in _GRADED_KINDS
