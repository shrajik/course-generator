"""Rendered-SVG geometry QA (app.services.diagram_render_qa) - the check that
was missing for the whole node+edge diagram family (flow_chart, hierarchy,
concept_map, data_flow_diagram, er_diagram, swimlane): schematic already had
`evaluate_schematic`, everything else had nothing. Covers the validator
directly against hand-crafted SVG fixtures with KNOWN violations (proving it
actually catches something, not just that real diagrams happen to pass), a
clean-SVG baseline, a broad sweep of real generated diagrams across every
kind and several stress shapes, and the DiagramService retry/fallback wiring.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from app.course.templates.registry import load_template
from app.render.diagram_renderer import render_diagram_svg
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec
from app.services.diagram_render_qa import _node_boxes, validate_rendered_svg

THEME = load_template("technical").theme


def _svg(*, width=400, height=300, body="") -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"><title>t</title>{body}</svg>'
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# hand-crafted fixtures with known violations
# ---------------------------------------------------------------------------


def test_two_overlapping_node_boxes_are_caught():
    body = (
        '<rect class="diagram-box" x="10" y="10" width="100" height="60"/>'
        '<rect class="diagram-box" x="60" y="30" width="100" height="60"/>'
    )
    issues = validate_rendered_svg(_svg(body=body))
    assert any(i.kind == "node_overlap" for i in issues)


def test_non_overlapping_node_boxes_are_clean():
    body = (
        '<rect class="diagram-box" x="10" y="10" width="100" height="60"/>'
        '<rect class="diagram-box" x="10" y="100" width="100" height="60"/>'
    )
    assert validate_rendered_svg(_svg(body=body)) == []


def test_an_edge_line_straight_through_an_unrelated_node_is_caught():
    """Two nodes stacked far apart with a third node sitting directly in the
    path between them - a genuine "line runs through a node" case, distinct
    from a line legitimately touching the boundary of the two nodes it
    actually connects."""
    body = (
        '<rect class="diagram-box" x="10" y="10" width="80" height="40"/>'
        '<rect class="diagram-box" x="10" y="130" width="80" height="40"/>'
        '<rect class="diagram-box" x="10" y="70" width="80" height="40"/>'
        '<line x1="50" y1="10" x2="50" y2="170"/>'
    )
    issues = validate_rendered_svg(_svg(height=220, body=body))
    assert any(i.kind == "edge_crosses_node" for i in issues)


def test_an_edge_that_only_touches_its_own_two_node_boundaries_is_clean():
    body = (
        '<rect class="diagram-box" x="10" y="10" width="80" height="40"/>'
        '<rect class="diagram-box" x="10" y="90" width="80" height="40"/>'
        '<line x1="50" y1="50" x2="50" y2="90"/>'
    )
    assert validate_rendered_svg(_svg(height=150, body=body)) == []


def test_a_curved_path_crossing_a_node_is_also_caught():
    """Swimlane's cross-lane edges use a quadratic-bezier `Q` path, not a
    straight `<line>` - the sampler must walk that curve too, not just
    straight segments. The control point pulls the curve's midpoint down
    through the node's own vertical span, so the curve genuinely dips
    through it rather than staying flat above it."""
    body = (
        '<rect class="diagram-box" x="150" y="40" width="80" height="40"/>'
        '<path d="M10,10 Q190,110 370,10"/>'
    )
    issues = validate_rendered_svg(_svg(width=400, height=100, body=body))
    assert any(i.kind == "edge_crosses_node" for i in issues)


def test_an_element_positioned_outside_the_canvas_is_caught():
    body = '<rect class="diagram-box" x="10" y="10" width="60" height="30"/><line x1="10" y1="10" x2="500" y2="10"/>'
    issues = validate_rendered_svg(_svg(width=200, height=200, body=body))
    assert any(i.kind == "out_of_canvas" for i in issues)


def test_malformed_svg_is_reported_not_raised():
    issues = validate_rendered_svg(b"<svg><not-closed")
    assert len(issues) == 1
    assert issues[0].kind == "unparseable_svg"


def test_the_background_rect_is_never_mistaken_for_a_node():
    """The canvas-filling background `<rect>` (no `class="diagram-box"`)
    would overlap literally every node if it were ever counted as one.
    Checked via `overlapping_pairs`/`_node_boxes` directly rather than the
    full `validate_rendered_svg` - this fixture's own tiny, mostly-empty
    canvas is a deliberately minimal shape (unrelated to what this test is
    about) that the (correct, separately-tested) occupancy check would
    otherwise also flag."""
    body = (
        '<rect x="0" y="0" width="400" height="300" fill="#fff"/>'
        '<rect class="diagram-box" x="10" y="10" width="60" height="30"/>'
    )
    root = ET.fromstring(_svg(body=body))
    assert _node_boxes(root) == [(10.0, 10.0, 70.0, 40.0)]  # only the ONE real node shape counted


# ---------------------------------------------------------------------------
# shared segments, border-hugging edges, duplicate arrowheads, font minimums
# ---------------------------------------------------------------------------


def test_two_edges_sharing_a_long_vertical_segment_are_caught():
    """Two entirely different connectors both routed through the exact same
    margin line - the reported bug: "Keep task -> End" and "Escalate ->
    Assess request" were indistinguishable where they overlapped."""
    body = (
        '<line x1="800" y1="50" x2="800" y2="500"/>'
        '<line x1="800" y1="200" x2="800" y2="650"/>'
    )
    issues = validate_rendered_svg(_svg(width=900, height=700, body=body))
    assert any(i.kind == "shared_edge_segment" for i in issues)


def test_two_edges_merely_touching_at_a_shared_endpoint_are_clean():
    """Two DIFFERENT edges converging on the same point (an ordinary
    reconvergence) share only a short stub near that point, not a real
    parallel stretch for any meaningful distance - not the same defect as
    two edges running alongside each other for a real distance."""
    body = (
        '<line x1="600" y1="50" x2="600" y2="300"/>'
        '<line x1="600" y1="280" x2="600" y2="300"/>'
    )
    issues = validate_rendered_svg(_svg(width=900, height=400, body=body))
    assert not any(i.kind == "shared_edge_segment" for i in issues)


def test_an_edge_hugging_an_unrelated_nodes_border_is_caught():
    """An edge segment running directly ALONG an unrelated node's border -
    distinct from `edge_crosses_node` (passing through the INTERIOR): this
    reads as if the line were part of that node's own outline rather than
    a distinct arrow, even though it never technically enters the box. The
    edge's own two real endpoints (500,200) and (500,50) sit nowhere near
    the unrelated box, so it is never mistaken for one of the edge's own
    connected nodes - only the path's own middle stretch, 8px outside the
    box's right edge, is the actual violation."""
    body = (
        '<rect class="diagram-box" x="100" y="10" width="200" height="400"/>'
        '<path d="M500,200 L308,200 L308,50 L500,50"/>'
    )
    issues = validate_rendered_svg(_svg(width=900, height=500, body=body))
    assert any(i.kind == "edge_collinear_with_node_border" for i in issues)


def test_an_edge_only_touching_its_own_connected_nodes_border_is_clean():
    """The same near-border geometry as above, but this time the node it
    runs close to genuinely IS one of the edge's own two connected
    nodes - touching your own endpoint's boundary is not a collision."""
    body = (
        '<rect class="diagram-box" x="100" y="10" width="200" height="40"/>'
        '<rect class="diagram-box" x="100" y="200" width="200" height="40"/>'
        '<line x1="200" y1="50" x2="200" y2="200"/>'
    )
    issues = validate_rendered_svg(_svg(width=500, height=300, body=body))
    assert not any(i.kind == "edge_collinear_with_node_border" for i in issues)


def test_two_arrowheads_landing_on_the_same_node_side_are_caught():
    """The reported bug: an ordinary spine arrow and an unrelated loop-back
    both entered the same node at its top - visually indistinguishable,
    a single ambiguous connection rather than two distinct real edges."""
    body = (
        '<rect class="diagram-box" x="100" y="100" width="200" height="60"/>'
        '<line x1="200" y1="20" x2="200" y2="100"/>'
        '<line x1="500" y1="20" x2="203" y2="100"/>'
    )
    issues = validate_rendered_svg(_svg(width=600, height=200, body=body))
    assert any(i.kind == "duplicate_arrowhead_side" for i in issues)


def test_two_arrowheads_on_different_sides_of_the_same_node_are_clean():
    body = (
        '<rect class="diagram-box" x="100" y="100" width="200" height="60"/>'
        '<line x1="200" y1="20" x2="200" y2="100"/>'
        '<line x1="500" y1="130" x2="300" y2="130"/>'
    )
    issues = validate_rendered_svg(_svg(width=600, height=200, body=body))
    assert not any(i.kind == "duplicate_arrowhead_side" for i in issues)


def test_title_text_below_the_14px_floor_after_scaling_is_caught():
    """A flow_chart/process node's own title, rendered small enough that
    once scaled to the page's real display width it drops under the
    14px minimum - checked directly against `font_size_issues` (not
    `validate_rendered_svg`, which only runs this check for
    kind="flow_chart"/"process")."""
    from app.services.diagram_render_qa import font_size_issues

    body = (
        '<g class="diagram-node"><rect class="diagram-box" x="10" y="10" width="200" height="60"/>'
        '<text x="20" y="40" font-size="15" font-weight="600">A Node Title</text></g>'
    )
    root = ET.fromstring(_svg(body=body))
    issues = font_size_issues(root, canvas_w=880, display_width=666)  # 15 * 666/880 = 11.35px
    assert any(i.kind == "font_below_minimum" and "title" in i.detail for i in issues)


def test_title_text_at_the_14px_floor_after_scaling_is_clean():
    from app.services.diagram_render_qa import font_size_issues

    body = (
        '<g class="diagram-node"><rect class="diagram-box" x="10" y="10" width="200" height="60"/>'
        '<text x="20" y="40" font-size="19" font-weight="600">A Node Title</text></g>'
    )
    root = ET.fromstring(_svg(body=body))
    issues = font_size_issues(root, canvas_w=880, display_width=666)  # 19 * 666/880 = 14.38px
    assert issues == []


def test_diamond_text_below_the_12px_floor_after_scaling_is_caught():
    from app.services.diagram_render_qa import font_size_issues

    body = (
        '<g class="diagram-node"><polygon class="diagram-box" points="100,10 200,60 100,110 0,60"/>'
        '<text x="100" y="65" font-size="12" font-weight="700">Decide?</text></g>'
    )
    root = ET.fromstring(_svg(body=body))
    issues = font_size_issues(root, canvas_w=880, display_width=666)  # 12 * 666/880 = 9.08px
    assert any(i.kind == "font_below_minimum" and "diamond" in i.detail for i in issues)


def test_edge_label_below_the_12px_floor_after_scaling_is_caught():
    from app.services.diagram_render_qa import font_size_issues

    body = (
        '<rect class="diagram-edge-label" x="40" y="40" width="60" height="18"/>'
        '<text x="70" y="53" font-size="12">Yes</text>'
    )
    root = ET.fromstring(_svg(body=body))
    issues = font_size_issues(root, canvas_w=880, display_width=666)  # 12 * 666/880 = 9.08px
    assert any(i.kind == "font_below_minimum" and "edge label" in i.detail for i in issues)


def test_font_size_issues_is_scoped_to_flow_chart_and_process_kinds():
    """A concept_map/hierarchy diagram can legitimately grow wide for
    benign structural reasons (see MIN_READABLE_PX's own docstring) -
    this stricter per-role floor only applies when `kind` is
    "flow_chart"/"process", whose own font-size constants are
    specifically calibrated to clear it (see LABEL_SIZE's docstring)."""
    body = (
        '<g class="diagram-node"><rect class="diagram-box" x="10" y="10" width="200" height="60"/>'
        '<text x="20" y="40" font-size="15" font-weight="600">A Node Title</text></g>'
    )
    svg_bytes = _svg(width=880, body=body)
    assert any(i.kind == "font_below_minimum" for i in validate_rendered_svg(svg_bytes, kind="flow_chart"))
    assert not any(i.kind == "font_below_minimum" for i in validate_rendered_svg(svg_bytes, kind="concept_map"))
    assert not any(i.kind == "font_below_minimum" for i in validate_rendered_svg(svg_bytes))


# ---------------------------------------------------------------------------
# readability (effective font size after page-scaling) and occupancy
# ---------------------------------------------------------------------------


def test_effective_font_size_is_unchanged_when_the_canvas_already_fits_the_page():
    from app.render.diagram_renderer import LABEL_SIZE
    from app.services.diagram_render_qa import effective_font_size

    assert effective_font_size(600, display_width=666) == LABEL_SIZE


def test_effective_font_size_shrinks_for_a_canvas_wider_than_the_page():
    from app.services.diagram_render_qa import effective_font_size

    # confirmed real case: an 8-child hierarchy's canvas grew to 1768px
    assert effective_font_size(1768, display_width=666) == pytest.approx(7.16, abs=0.05)


def test_readability_issues_is_clean_for_a_canvas_within_the_page_width():
    from app.services.diagram_render_qa import readability_issues

    assert readability_issues(880, display_width=666) == []


def test_readability_issues_flags_a_canvas_much_wider_than_the_page():
    from app.services.diagram_render_qa import readability_issues

    issues = readability_issues(1768, display_width=666)
    assert len(issues) == 1
    assert issues[0].kind == "text_too_small_after_scaling"


def test_occupancy_ratio_is_1_when_there_is_nothing_to_measure():
    from app.services.diagram_render_qa import occupancy_ratio

    empty_root = ET.fromstring(_svg(body=""))
    assert occupancy_ratio(empty_root, 400, 300) == 1.0


def test_occupancy_issues_flags_content_compressed_into_a_tiny_corner():
    from app.services.diagram_render_qa import occupancy_issues

    # a single small node in the corner of a huge, otherwise-empty canvas.
    body = '<rect class="diagram-box" x="10" y="10" width="40" height="20"/>'
    root = ET.fromstring(_svg(width=2000, height=2000, body=body))
    issues = occupancy_issues(root, 2000, 2000)
    assert len(issues) == 1
    assert issues[0].kind == "excessive_whitespace"


def test_occupancy_issues_does_not_flag_a_legitimate_small_diagram():
    """A real, minimal 2-node concept_map (a subject and one component -
    a genuinely complete small diagram) measures ~10% occupancy by design
    (its radial layout reserves canvas room independent of satellite
    count) - this must never be flagged as accidental whitespace."""
    spec = DiagramSpec(
        kind="concept_map",
        nodes=[DiagramNode(id="s", label="Subject", level=0), DiagramNode(id="p", label="Component", level=1)],
        edges=[DiagramEdge(source="p", target="s", label="acts on")],
    )
    root = ET.fromstring(_render(spec))
    canvas_w, canvas_h = float(root.get("width")), float(root.get("height"))
    from app.services.diagram_render_qa import occupancy_issues

    assert occupancy_issues(root, canvas_w, canvas_h) == []


# ---------------------------------------------------------------------------
# real generated diagrams across every kind and several stress shapes
# ---------------------------------------------------------------------------


def _render(spec: DiagramSpec):
    svg, _w, _h = render_diagram_svg(spec, THEME)
    return svg


def test_a_branching_and_reconverging_flow_chart_renders_clean():
    spec = DiagramSpec(
        kind="flow_chart",
        nodes=[
            DiagramNode(id="start", label="Start"),
            DiagramNode(id="check", label="Check input validity"),
            DiagramNode(id="fix", label="Sanitize and normalize the input"),
            DiagramNode(id="process", label="Process the request"),
            DiagramNode(id="end", label="End"),
        ],
        edges=[
            DiagramEdge(source="start", target="check"),
            DiagramEdge(source="check", target="fix", label="Invalid"),
            DiagramEdge(source="check", target="process", label="Valid"),
            DiagramEdge(source="fix", target="process"),
            DiagramEdge(source="process", target="end"),
        ],
    )
    assert validate_rendered_svg(_render(spec), kind="flow_chart") == []


def test_a_five_lane_swimlane_with_long_labels_renders_clean():
    steps = [
        ("Client sends authenticated request", "Client"),
        ("Gateway validates request headers", "API Gateway"),
        ("Auth service verifies JWT token", "Auth Service"),
        ("Cache layer checks for cached response", "Cache Layer"),
        ("Database queries the requested records", "Database"),
        ("Cache layer stores the new response", "Cache Layer"),
        ("Gateway formats the final response", "API Gateway"),
        ("Client receives the response payload", "Client"),
    ]
    nodes = [DiagramNode(id=f"s{i}", label=label, lane=lane) for i, (label, lane) in enumerate(steps)]
    edges = [DiagramEdge(source=f"s{i - 1}", target=f"s{i}") for i in range(1, len(steps))]
    spec = DiagramSpec(kind="swimlane", nodes=nodes, edges=edges)
    assert validate_rendered_svg(_render(spec)) == []


def test_a_wide_eight_child_hierarchy_has_no_collisions_but_is_flagged_unreadable():
    """No node/edge geometry problem - the wide fan-out layout itself is
    collision-free, exactly like the narrower stress cases below. But its
    canvas legitimately grows to fit 8 siblings side by side, and THAT is a
    real, separate defect `readability_issues` exists to catch: displayed
    at the page's content width, the resulting text is too small to read
    (see test_diagram_service.py's real end-to-end repair test for this
    exact scenario recovering via retry)."""
    nodes = [DiagramNode(id="root", label="Course Catalog", level=0)]
    edges = []
    for i in range(8):
        nid = f"c{i}"
        nodes.append(DiagramNode(id=nid, label=f"Category {i + 1}: Advanced Topic Name", level=1))
        edges.append(DiagramEdge(source="root", target=nid))
    spec = DiagramSpec(kind="hierarchy", nodes=nodes, edges=edges)
    issues = validate_rendered_svg(_render(spec))
    assert {issue.kind for issue in issues} == {"text_too_small_after_scaling"}


def test_an_eight_spoke_concept_map_with_long_labels_renders_clean():
    labels = [
        "Greenhouse gas emissions from fossil fuels",
        "Rising sea levels threaten coastal cities",
        "Extreme weather events increase in frequency",
        "Deforestation reduces carbon absorption capacity",
        "Ocean acidification harms marine ecosystems",
        "Melting polar ice caps and glaciers",
        "Biodiversity loss across many ecosystems",
        "Agricultural disruption and food insecurity",
    ]
    nodes = [DiagramNode(id="c", label="Climate Change", level=0)]
    edges = []
    for i, label in enumerate(labels):
        nodes.append(DiagramNode(id=f"n{i}", label=label, level=1))
        edges.append(DiagramEdge(source="c", target=f"n{i}", label="affects" if i % 2 == 0 else "caused by"))
    spec = DiagramSpec(kind="concept_map", nodes=nodes, edges=edges)
    assert validate_rendered_svg(_render(spec)) == []


def test_a_maximum_fourteen_node_flow_chart_renders_clean():
    labels = [
        "User query + goal", "Optional retrieve", "Empty retrieval error", "Prompt assembly",
        "Decide whether to use a tool", "Deterministic chain LLM", "Agent LLM", "Call tool",
        "Bad tool parameters error", "Observe tool response", "Parse and return final answer",
        "Iterate or finish", "Finish", "End",
    ]
    nodes = [DiagramNode(id=f"n{i}", label=label) for i, label in enumerate(labels)]
    edges = [DiagramEdge(source=f"n{i - 1}", target=f"n{i}") for i in range(1, len(nodes))]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    assert len(nodes) == 14
    assert validate_rendered_svg(_render(spec), kind="flow_chart") == []


# ---------------------------------------------------------------------------
# DiagramService wiring: retry then fallback on a persistently bad render
# ---------------------------------------------------------------------------


async def test_diagram_service_falls_back_when_the_rendered_output_keeps_failing_qa(service, monkeypatch):
    from app.render import diagram_renderer as renderer_module
    from app.schemas.document import Block
    from app.schemas.blocks import BlockType

    template = load_template("technical")
    block = Block(
        type=BlockType.IMAGE,
        content={
            "kind": "diagram",
            "purpose": "Show the stages of the pipeline",
            "prompt": "Four sequential stages: gather, plan, build, review",
            "caption": "Figure: pipeline stages",
        },
    )

    def always_broken_render(spec, theme):
        # A real SVG, but deliberately geometrically broken - an edge drawn
        # straight through a node that sits between two others.
        body = (
            '<rect class="diagram-box" x="10" y="10" width="80" height="40"/>'
            '<rect class="diagram-box" x="10" y="130" width="80" height="40"/>'
            '<rect class="diagram-box" x="10" y="70" width="80" height="40"/>'
            '<line x1="50" y1="10" x2="50" y2="170"/>'
        )
        svg = _svg(height=220, body=body)
        return svg, 400, 220

    monkeypatch.setattr(renderer_module, "render_diagram_svg", always_broken_render)
    # DiagramService imported the function by name, so patch its own reference too.
    monkeypatch.setattr("app.services.diagram_service.render_diagram_svg", always_broken_render)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_render_qa_fallback", block=block, template=template, course_title="Test Course"
    )

    assert ok is False  # caller falls back to a (now text-free) illustration, never a known-bad diagram


# ---------------------------------------------------------------------------
# a REAL, non-mocked collision: only the AI's structured() response is
# controlled (the same, already-established pattern every other test in this
# suite uses to control "what the model returned") - rendering, validation,
# retry and re-rendering are all the genuine production code path. This is
# the actual bug that motivated `readability_issues`: an 8-child hierarchy's
# canvas legitimately grows to 1768px to avoid node overlap, but at the
# page's ~666px display width that shrinks its label text to ~6.6px -
# unreadable, even though the diagram's own node geometry has zero overlaps.
# ---------------------------------------------------------------------------


def _wide_hierarchy_spec() -> DiagramSpec:
    """8 direct children under one root - the maximum a hierarchy's node
    cap (9) allows in a single flat level - long enough labels to force
    real canvas growth, reproducing the confirmed real defect."""
    nodes = [DiagramNode(id="root", label="Course Catalog", level=0)]
    edges = []
    for i in range(8):
        nid = f"c{i}"
        nodes.append(DiagramNode(id=nid, label=f"Category {i + 1}: Advanced Topic Name", level=1))
        edges.append(DiagramEdge(source="root", target=nid))
    return DiagramSpec(kind="hierarchy", nodes=nodes, edges=edges)


def _narrow_hierarchy_spec() -> DiagramSpec:
    """The same subject, restructured into a genuinely deep/narrow tree (2
    branches of 2, one level deeper) instead of one flat 8-wide fan - what a
    real consolidation retry should produce: same kind of information, a
    canvas that stays readable at the page's display width. Confirmed by
    direct rendering: 912px wide vs. the wide spec's 1768px - comfortably
    under the ~1295px ceiling `readability_issues` allows at content width."""
    nodes = [
        DiagramNode(id="root", label="Course Catalog", level=0),
        DiagramNode(id="g1", label="Beginner Tracks", level=1),
        DiagramNode(id="g2", label="Advanced Tracks", level=1),
    ]
    edges = [DiagramEdge(source="root", target="g1"), DiagramEdge(source="root", target="g2")]
    for i in range(2):
        nid = f"g1_{i}"
        nodes.append(DiagramNode(id=nid, label=f"Category {i + 1}", level=2))
        edges.append(DiagramEdge(source="g1", target=nid))
    for i in range(2):
        nid = f"g2_{i}"
        nodes.append(DiagramNode(id=nid, label=f"Category {i + 3}", level=2))
        edges.append(DiagramEdge(source="g2", target=nid))
    return DiagramSpec(kind="hierarchy", nodes=nodes, edges=edges)


async def test_a_real_unreadable_wide_hierarchy_triggers_repair_and_recovers(service, monkeypatch):
    """Proves the actual production repair path: generate (real render) ->
    real validation detects the readability failure -> real retry triggers
    -> a genuinely different, narrower spec is rendered again -> final
    validation passes -> the diagram that ships has zero issues. Nothing
    about rendering or validation is mocked - only the model's two
    responses are, the same way every other spec-controlling test here
    works."""
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block
    from app.services.diagram_service import DiagramService

    template = load_template("technical")
    block = Block(
        type=BlockType.IMAGE,
        content={
            "kind": "diagram",
            "purpose": "Show the course catalog's category structure",
            "prompt": "A course catalog broken into its top-level categories",
            "caption": "Course catalog structure",
        },
    )
    responses = [_wide_hierarchy_spec(), _narrow_hierarchy_spec()]
    calls: list[str] = []

    async def fake_request_spec(self, **kwargs):
        calls.append(kwargs.get("qa_feedback", ""))
        return responses[len(calls) - 1].model_copy(deep=True)

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_real_collision_recovers", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert len(calls) == 2, "the wide first attempt must have triggered exactly one real retry"
    assert "too small" in calls[1].lower() or "layout problems" in calls[1].lower()

    # re-validate the ACTUAL saved asset independently - not trusting the
    # service's own internal accounting of "it passed".
    svg_text = service.storage.asset_abs_path(
        "course_real_collision_recovers", block.content["path"]
    ).read_text(encoding="utf-8")
    assert validate_rendered_svg(svg_text.encode("utf-8")) == []
    assert "Beginner Tracks" in svg_text  # the recovered (narrow) spec is what actually shipped
    assert "Course Catalog" in svg_text


async def test_a_persistently_unreadable_hierarchy_falls_back_after_the_bounded_retry(service, monkeypatch):
    """The other half of the same real path: if the retry ALSO comes back
    genuinely too wide, the bounded (exactly one retry) design falls back
    to illustration rather than looping or shipping a known-unreadable
    diagram."""
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block
    from app.services.diagram_service import DiagramService

    template = load_template("technical")
    block = Block(
        type=BlockType.IMAGE,
        content={
            "kind": "diagram",
            "purpose": "Show the course catalog's category structure",
            "prompt": "A course catalog broken into its top-level categories",
            "caption": "Course catalog structure",
        },
    )
    calls: list[str] = []

    async def fake_request_spec(self, **kwargs):
        calls.append(kwargs.get("qa_feedback", ""))
        return _wide_hierarchy_spec().model_copy(deep=True)  # every attempt is equally too wide

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_real_collision_persists", block=block, template=template, course_title="Test Course"
    )

    assert ok is False
    assert len(calls) == 2  # exactly one retry attempted - never unbounded
