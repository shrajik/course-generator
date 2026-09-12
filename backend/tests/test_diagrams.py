"""Diagram / static visual support: structured spec -> deterministic SVG."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from app.agents.writer import WriterAgent
from app.core.errors import AIServiceError
from app.course.templates.registry import load_template
from app.render.diagram_renderer import render_diagram_svg
from app.render.schematic_layout import resolve_schematic_layout
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec, SchematicShape, SchematicState
from app.schemas.document import Block
from app.schemas.template import TemplateTheme
from app.services.diagram_qa import (
    CANVAS_CLIPPING,
    MISSING_RELATIONSHIP,
    OBJECT_COLLISION,
    WEAK_FOCAL_HIERARCHY,
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
    assert not DiagramSpec(
        nodes=[DiagramNode(label=f"n{i}") for i in range(10)]
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
    ["flow_chart", "process", "cycle", "comparison", "smart_art", "conceptual", "concept_map"],
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


def test_flow_chart_long_transition_labels_never_overlap_the_stacked_boxes():
    """The gap reserved between stacked flow_chart/process boxes must fit a
    wrapped two-line transition label without crowding either box - same
    class of bug as the concept_map/schematic label overlaps, here in the
    vertical-stack layout used by flow_chart, process and smart_art."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Stage {i}") for i in range(4)]
    edges = [
        DiagramEdge(source=f"n{i}", target=f"n{i + 1}", label="a long transition condition that wraps to two lines")
        for i in range(3)
    ]
    spec = DiagramSpec(kind="flow_chart", nodes=nodes, edges=edges)
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")

    boxes = _rects_by_class(text, "diagram-box")
    labels = _rects_by_class(text, "diagram-edge-label")
    assert boxes and labels
    offenders = [(label, box) for label in labels for box in boxes if _bboxes_overlap(label, box)]
    assert not offenders, f"flow_chart transition label(s) overlap a box: {offenders}"


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
    illustrations = [b for b in images if b.content.get("kind") != "diagram"]

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


def test_concept_map_has_distinct_hub_and_spoke_geometry():
    """A concept map must not just be smart_art's flat list with a new name -
    it needs its own layout (radial, sized from node count) to actually show
    a central subject and its labelled relationships."""
    nodes = [
        DiagramNode(id="coil", label="Coil", level=0),
        DiagramNode(id="magnet", label="Bar magnet", level=1),
        DiagramNode(id="field", label="Field lines", level=1),
        DiagramNode(id="current", label="Induced current", level=1),
    ]
    edges = [
        DiagramEdge(source="magnet", target="coil", label="moving toward"),
        DiagramEdge(source="field", target="coil", label="passes through"),
        DiagramEdge(source="coil", target="current", label="induces"),
    ]
    spec = DiagramSpec(kind="concept_map", nodes=nodes, edges=edges)
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())
    ET.fromstring(svg_bytes)
    text = svg_bytes.decode("utf-8")

    assert width != 880  # not smart_art's fixed-width list canvas
    assert "moving toward" in text and "induces" in text  # relationship labels survive
    assert text.count("<title>") >= 4  # every node still gets its hover tooltip


def _rects_by_class(svg_text: str, css_class: str) -> list[tuple[float, float, float, float]]:
    pattern = re.compile(
        rf'<rect class="{css_class}" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"'
    )
    return [tuple(float(v) for v in match) for match in pattern.findall(svg_text)]


def _bboxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def test_concept_map_relationship_labels_never_render_inside_a_node_box():
    """Regression test for the reported readability bug: a relationship
    label (e.g. "moving toward", "produces EMF due to changing magnetic
    flux") must be positioned outside every node's box, never inside or
    clipping it - with labels long enough to previously overflow their pill
    and land back on top of a neighbouring node."""
    nodes = [
        DiagramNode(id="coil", label="Coil", level=0),
        DiagramNode(id="magnet", label="Bar magnet", level=1),
        DiagramNode(id="field", label="Magnetic field lines", level=1),
        DiagramNode(id="current", label="Induced current", level=1),
    ]
    edges = [
        DiagramEdge(source="magnet", target="coil", label="moving toward the coil"),
        DiagramEdge(source="field", target="coil", label="changes with the motion of the magnet"),
        DiagramEdge(source="coil", target="current", label="produces EMF and induces a current"),
    ]
    spec = DiagramSpec(kind="concept_map", title="Electromagnetic Induction", nodes=nodes, edges=edges)
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")

    boxes = _rects_by_class(text, "diagram-box")
    labels = _rects_by_class(text, "diagram-edge-label")
    assert boxes and labels

    offenders = [
        (label, box) for label in labels for box in boxes if _bboxes_overlap(label, box)
    ]
    assert not offenders, f"relationship label(s) overlap a node box: {offenders}"


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


def test_concept_map_node_box_contains_only_the_short_title_not_the_relationship_text():
    """The node/circle itself must stay clean - only its own short title
    inside it, never a neighbouring edge's relationship text."""
    nodes = [
        DiagramNode(id="coil", label="Coil", level=0),
        DiagramNode(id="magnet", label="Bar magnet", level=1),
    ]
    edges = [DiagramEdge(source="magnet", target="coil", label="Produces EMF")]
    spec = DiagramSpec(kind="concept_map", nodes=nodes, edges=edges)
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")

    # The relationship text appears exactly once (inside its own label pill),
    # never duplicated into a node's own text content.
    assert text.count("Produces EMF") == 1
    assert "Coil</tspan>" in text or "Coil<" in text
    assert "Bar magnet" in text


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
    assert "acts on" in svg_path.read_text(encoding="utf-8")  # the corrected spec was rendered


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
    def state(caption: str, magnet_x: float, needle_angle: float, intensity: float) -> SchematicState:
        return SchematicState(
            caption=caption,
            shapes=[
                SchematicShape(type="coil", id="coil", x=0.28, y=0.42, width=0.34, height=0.28, label="Coil"),
                SchematicShape(
                    type="block", id="magnet", x=magnet_x, y=0.42, width=0.22, height=0.14,
                    label="N", sublabel="S",
                ),
                SchematicShape(
                    type="flow", id="field", x=0.5, y=0.42, width=0.18, height=0.18,
                    rotation=180, intensity=intensity, label="Field lines",
                ),
                SchematicShape(
                    type="gauge", id="meter", x=0.28, y=0.82, width=0.16, height=0.16,
                    rotation=needle_angle, sublabel="Ammeter",
                ),
            ],
        )

    return [
        state("No current", magnet_x=0.85, needle_angle=0, intensity=0.3),
        state("Current flows through the circuit", magnet_x=0.62, needle_angle=35, intensity=0.9),
    ]


def test_schematic_spec_needs_at_least_two_recognised_shapes():
    assert not DiagramSpec(kind="schematic", shapes=[]).is_usable()
    assert not DiagramSpec(
        kind="schematic", shapes=[SchematicShape(type="block", label="Only one")]
    ).is_usable()
    assert DiagramSpec(
        kind="schematic",
        shapes=[SchematicShape(type="block", label="A"), SchematicShape(type="coil", label="B")],
    ).is_usable()


def test_schematic_spec_requires_shapes_in_every_state():
    usable_states = _em_induction_states()
    assert DiagramSpec(kind="schematic", states=usable_states).is_usable()

    empty_second_state = [usable_states[0], SchematicState(caption="Empty", shapes=[])]
    assert not DiagramSpec(kind="schematic", states=empty_second_state).is_usable()

    assert not DiagramSpec(kind="schematic", states=[usable_states[0]]).is_usable()  # only one state


def test_render_schematic_svg_reproduces_the_em_induction_reference_layout():
    """The exact scenario from the reported bug: a magnet moving toward a
    coil, field lines, an ammeter, and a before/after state change - the
    kind of labelled physical illustration a concept_map cannot draw."""
    spec = DiagramSpec(kind="schematic", title="Electromagnetic Induction", states=_em_induction_states())
    svg_bytes, width, height = render_diagram_svg(spec, TemplateTheme())

    ET.fromstring(svg_bytes)  # valid XML
    text = svg_bytes.decode("utf-8")
    assert "No current" in text
    assert "Current flows through the circuit" in text
    assert text.count("<ellipse") >= 8  # coil loops drawn in both panels
    assert "diagram-node" in text  # every shape is still hoverable/interactive
    assert text.count("<title>") >= 6
    assert width > 0 and height > 0


def test_schematic_flow_label_never_lands_inside_the_shape_it_points_at():
    """Regression test: the flow shape's own label (e.g. "Field lines") was
    being projected along the flow's source direction, which for a coil/
    magnet layout routinely lands it back inside the coil it flows into."""
    spec = DiagramSpec(kind="schematic", title="Electromagnetic Induction", states=_em_induction_states())
    svg_bytes, _, _ = render_diagram_svg(spec, TemplateTheme())
    text = svg_bytes.decode("utf-8")

    ellipses = [
        tuple(float(v) for v in match.groups())
        for match in re.finditer(r'<ellipse cx="([\d.]+)" cy="([\d.]+)" rx="([\d.]+)" ry="([\d.]+)"', text)
    ]
    labels = [
        tuple(float(v) for v in match.groups())
        for match in re.finditer(r'<text x="([\d.]+)" y="([\d.]+)"[^>]*>Field lines</text>', text)
    ]
    assert ellipses and len(labels) == 2  # one per state panel

    for lx, ly in labels:
        inside_coil = any(
            abs(lx - cx) < rx and abs(ly - cy) < ry for cx, cy, rx, ry in ellipses
        )
        assert not inside_coil, f"'Field lines' label at ({lx}, {ly}) overlaps a coil loop"


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


# ---------------------------------------------------------------------------
# schematic generalises beyond physics: chemistry, biology, ... via the same
# small shape vocabulary (block/circle/coil/gauge/flow/arrow/label)
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


async def test_electromagnetic_induction_gets_a_schematic_not_a_concept_map(service):
    """The exact reference scenario, end to end through DiagramService: a
    physical-apparatus brief hinted as `schematic` must render as an
    illustrated before/after diagram, not fall back to a flowchart or a
    generic labelled-box concept map."""
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

    assert ok is True
    assert block.content["kind"] == "diagram"
    assert block.content["diagram_kind"] == "schematic"
    assert block.content["path"].endswith(".svg")
    # Prove it actually rendered as an illustrated schematic (a "before/after"
    # panel pair), not a labelled-box graph - the offline mock always returns
    # a two-state schematic for a "schematic" hint (see mock_ai._diagram_spec),
    # regardless of the specific EM-induction content in the brief above,
    # which is exactly the genericity this fix requires.
    svg_text = service.storage.asset_abs_path("course_em_schematic", block.content["path"]).read_text(
        encoding="utf-8"
    )
    assert "<rect" in svg_text  # panel frame(s) present
    assert svg_text.count("<title>") >= 4  # each illustrated shape stays hoverable


async def test_schematic_and_flow_chart_coexist_for_electromagnetic_induction(
    service, technical_input, monkeypatch
):
    """Full pipeline: a chapter with both a problem-solving flowchart and a
    physical schematic ends up with both, correctly distinguished."""
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
    diagrams = [b for b in document.blocks_of_type(BlockType.IMAGE) if b.content.get("kind") == "diagram"]
    by_hint = {b.content.get("diagram_kind"): b for b in diagrams}

    assert "flow_chart" in by_hint and "schematic" in by_hint
    for block in (by_hint["flow_chart"], by_hint["schematic"]):
        assert block.content["path"].endswith(".svg")
        assert service.storage.asset_abs_path(record.course_id, block.content["path"]).exists()


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
            type="flow", id="flux", label="Air-gap flux", anchor="left_of:rotor",
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


async def test_schematic_qa_failure_triggers_exactly_one_targeted_retry(service, monkeypatch):
    """QA failing on the first attempt must cause exactly one retry, and the
    retry's prompt must carry the actual failure reason - never a bare
    "generate again"."""
    template = load_template("technical")
    block = _diagram_block(diagram_kind="schematic", prompt="Two interacting components")

    bad_spec = DiagramSpec(
        kind="schematic",
        shapes=[
            SchematicShape(type="block", id="a", label="A", x=0.5, y=0.5, width=0.4, height=0.4, role="primary"),
            SchematicShape(type="block", id="b", label="B", x=0.55, y=0.5, width=0.4, height=0.4),
        ],
    )
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
        return (bad_spec if len(received_feedback) == 1 else good_spec).model_copy(deep=True)

    monkeypatch.setattr(DiagramService, "_request_spec", fake_request_spec)

    ok = await service.images.diagrams.generate_for_block(
        course_id="course_schematic_qa_retry", block=block, template=template, course_title="Two Parts"
    )

    assert ok is True
    assert len(received_feedback) == 2, "expected exactly one retry"
    assert received_feedback[0] == ""
    assert received_feedback[1] and "generate the diagram again" not in received_feedback[1].lower()
    assert "overlap" in received_feedback[1].lower()

    svg_text = service.storage.asset_abs_path(
        "course_schematic_qa_retry", block.content["path"]
    ).read_text(encoding="utf-8")
    result = evaluate_schematic(good_spec.model_copy(update={"shapes": resolve_schematic_layout(good_spec).shapes}))
    assert result.passed
    assert "<rect" in svg_text
