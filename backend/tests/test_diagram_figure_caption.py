"""app.services.diagram_service._with_figure_caption - the "Fig. N: " prefix
the new textbook-style ring/wheel diagrams (flow_chart/process/cycle/
concept_map/conceptual) get on their own title, used both as the caption
drawn above the diagram and (stripped of the prefix - see
app.render.diagram_renderer._FIGURE_PREFIX_RE) as the ring's own center
circle label."""

from __future__ import annotations

import re

from app.course.templates.registry import load_template
from app.render.diagram_renderer import render_diagram_svg
from app.schemas.blocks import BlockType
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec
from app.schemas.document import Block, BlockMeta
from app.services.diagram_service import _with_figure_caption


def _block(chapter_number: int) -> Block:
    return Block(type=BlockType.IMAGE, content={}, meta=BlockMeta(chapter_number=chapter_number))


def _spec(kind: str, title: str) -> DiagramSpec:
    return DiagramSpec(kind=kind, title=title, nodes=[DiagramNode(id="a", label="x")])


def test_prefixes_a_ring_kind_title_with_the_chapter_number():
    out = _with_figure_caption(_spec("flow_chart", "A procedure"), _block(3))
    assert out.title == "Fig. 3: A procedure"


def test_is_idempotent_when_called_twice_on_the_same_spec():
    once = _with_figure_caption(_spec("flow_chart", "A procedure"), _block(3))
    twice = _with_figure_caption(once, _block(3))
    assert twice.title == "Fig. 3: A procedure"


def test_leaves_the_title_unchanged_when_chapter_number_is_unknown():
    out = _with_figure_caption(_spec("flow_chart", "No chapter"), _block(0))
    assert out.title == "No chapter"


def test_leaves_the_title_unchanged_for_a_kind_the_ring_wheel_rendering_does_not_cover():
    out = _with_figure_caption(_spec("hierarchy", "A tree"), _block(5))
    assert out.title == "A tree"


def test_a_blank_title_still_gets_a_figure_number():
    out = _with_figure_caption(_spec("concept_map", ""), _block(2))
    assert out.title == "Fig. 2"


def test_covers_every_ring_or_wheel_kind():
    for kind in ("flow_chart", "process", "cycle", "concept_map", "conceptual"):
        out = _with_figure_caption(_spec(kind, "Title"), _block(1))
        assert out.title == "Fig. 1: Title", kind


def test_the_prefixed_caption_never_appears_twice_in_the_rendered_ring():
    """A real, confirmed bug caught on a live-generated diagram: the ring's
    own center circle used to repeat the EXACT SAME "Fig. N: ..." string
    already drawn as the caption above the canvas - redundant and cramped.
    render_diagram_svg's own dispatch strips that prefix (see
    _FIGURE_PREFIX_RE) before reusing the title for the center circle."""
    nodes = [DiagramNode(id=f"n{i}", label=f"Step {i}") for i in range(3)]
    edges = [DiagramEdge(source="n0", target="n1"), DiagramEdge(source="n1", target="n2")]
    prefixed = _with_figure_caption(
        DiagramSpec(kind="flow_chart", title="Illustrate What is RAG", nodes=nodes, edges=edges), _block(1)
    )
    assert prefixed.title == "Fig. 1: Illustrate What is RAG"

    svg_bytes, _w, _h = render_diagram_svg(prefixed, load_template("technical_v1").theme)
    svg = svg_bytes.decode("utf-8")
    # The SVG's own <title> metadata tag (screen-reader/tooltip text, not a
    # visible element) and the one visible heading <text> above the canvas
    # both legitimately carry the full "Fig. 1: ..." caption - what must
    # NOT happen is a THIRD, visible repeat of it inside the ring's own
    # center circle.
    visible_text_elements = re.findall(r"<text\b[^>]*>.*?</text>", svg, flags=re.DOTALL)
    visible_text = "".join(visible_text_elements)
    assert visible_text.count("Fig. 1:") == 1
    assert "Illustrate What is RAG" in visible_text  # the bare title still appears (the center circle)
