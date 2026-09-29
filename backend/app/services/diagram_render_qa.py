"""Validates the ACTUAL rendered SVG a diagram produces - not the pre-render
`DiagramSpec`, the literal `<rect>`/`<ellipse>`/`<polygon>`/`<line>`/`<path>`
elements a reader will actually see. `app.services.diagram_qa` already does
this for `schematic` (checking the spec's own resolved x/y/w/h fields before
rendering); the node+edge diagram family (flow_chart, hierarchy, concept_map,
data_flow_diagram, er_diagram, swimlane) had no equivalent check at all - every
node/decision/entity shape in that family is rendered with a shared
`class="diagram-box"` marker (see diagram_renderer.py's `_node_block`,
`_draw_pill`, `_draw_diamond`, `_draw_process_circle`, `_draw_attribute_oval`),
which is what makes a single, shape-agnostic parser possible here.

This never re-derives layout or fixes anything - it only inspects the SVG
diagram_renderer.py already produced. For this deterministic renderer, the
SVG *is* the rendered output (no further browser layout step can move
anything), so this is a genuine "render output" check, not a proxy for one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from app.render.diagram_renderer import BOX_PADDING, LABEL_SIZE, _SANS_RATIO
from app.schemas.document import CONTENT_WIDTH

_SVG_NS = "{http://www.w3.org/2000/svg}"

# Below this, a label's SVG-unit font size, once scaled down to the page's
# real display width, would render smaller than is comfortably readable -
# a widely-used minimum body-text floor (print and screen alike).
MIN_READABLE_PX = 9.0

# Below this fraction of its own canvas actually covered by drawn content, a
# diagram reads as mostly accidental empty space rather than deliberate
# breathing room. Deliberately very low: a legitimate, minimal concept_map
# (just 2 nodes - a subject and one component, a genuinely complete small
# diagram) measured at ~10% occupancy by design - its radial layout reserves
# canvas room independent of how few satellites actually end up placed. This
# threshold exists to catch unambiguous, extreme emptiness only; "never
# remove intentional whitespace" means erring toward under-detection here,
# not chasing a tighter bar without a confirmed real example of the failure
# it would catch.
MIN_OCCUPANCY = 0.05

# How much a node's own box is shrunk before checking whether an edge passes
# through it - an edge legitimately TOUCHES the boundary of the two nodes it
# connects (that's not a collision), so a small inward margin keeps that from
# ever being mistaken for "passes through a node's interior". Only a
# genuinely unrelated third node being crossed produces a finding.
_NODE_INSET = 3.0
# How much of each edge segment's own ends to ignore when checking for a
# node crossing - the segment legitimately starts/ends AT a node boundary.
_EDGE_ENDPOINT_MARGIN = 6.0
_EDGE_SAMPLE_STEP = 4.0


@dataclass(frozen=True)
class RenderQAIssue:
    kind: str  # "node_overlap" | "edge_crosses_node" | "out_of_canvas"
    detail: str


def _local(tag: str) -> str:
    return tag[len(_SVG_NS):] if tag.startswith(_SVG_NS) else tag


def _f(el: ET.Element, name: str, default: float = 0.0) -> float:
    try:
        return float(el.get(name, default))
    except (TypeError, ValueError):
        return default


BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


def _shape_bbox(el: ET.Element) -> BBox | None:
    """The bounding box of one `class="diagram-box"` shape, whatever its
    element type - `_node_boxes` collects these across a whole document;
    `text_overflow_issues` needs the SAME logic per-element (one shape at a
    time, inside its own `<g class="diagram-node">`), so both share it."""
    tag = _local(el.tag)
    if tag == "rect":
        x, y = _f(el, "x"), _f(el, "y")
        return (x, y, x + _f(el, "width"), y + _f(el, "height"))
    if tag == "ellipse":
        cx, cy, rx, ry = _f(el, "cx"), _f(el, "cy"), _f(el, "rx"), _f(el, "ry")
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag == "circle":
        cx, cy, r = _f(el, "cx"), _f(el, "cy"), _f(el, "r")
        return (cx - r, cy - r, cx + r, cy + r)
    if tag == "polygon":
        points = _parse_points(el.get("points") or "")
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (min(xs), min(ys), max(xs), max(ys))
    return None


def _node_boxes(root: ET.Element) -> list[BBox]:
    boxes: list[BBox] = []
    for el in root.iter():
        if "diagram-box" not in (el.get("class") or "").split():
            continue
        box = _shape_bbox(el)
        if box is not None:
            boxes.append(box)
    return boxes


def _parse_points(raw: str) -> list[tuple[float, float]]:
    pairs = re.findall(r"(-?[\d.]+)[,\s]+(-?[\d.]+)", raw)
    return [(float(x), float(y)) for x, y in pairs]


def _edge_segments(root: ET.Element) -> list[list[tuple[float, float]]]:
    """Every edge as a polyline of points (straight `<line>`s are 2 points;
    a `<path>` is walked command-by-command, sampling quadratic-bezier `Q`
    segments so a curved swimlane cross-lane edge is checked too, not just
    the straight ones)."""
    segments: list[list[tuple[float, float]]] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "line":
            segments.append([(_f(el, "x1"), _f(el, "y1")), (_f(el, "x2"), _f(el, "y2"))])
        elif tag == "path" and el.get("d"):
            points = _walk_path(el.get("d") or "")
            if len(points) >= 2:
                segments.append(points)
    return segments


def _walk_path(d: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    cur = (0.0, 0.0)
    for cmd, args in re.findall(r"([MLQ])\s*([-\d.,\s]+)", d):
        nums = [float(n) for n in re.findall(r"-?[\d.]+", args)]
        if cmd in ("M", "L") and len(nums) >= 2:
            cur = (nums[0], nums[1])
            points.append(cur)
        elif cmd == "Q" and len(nums) >= 4:
            ctrl, end = (nums[0], nums[1]), (nums[2], nums[3])
            for t in (0.25, 0.5, 0.75, 1.0):
                x = (1 - t) ** 2 * cur[0] + 2 * (1 - t) * t * ctrl[0] + t**2 * end[0]
                y = (1 - t) ** 2 * cur[1] + 2 * (1 - t) * t * ctrl[1] + t**2 * end[1]
                points.append((x, y))
            cur = end
    return points


def _inset(box: BBox, margin: float) -> BBox:
    x0, y0, x1, y1 = box
    return (x0 + margin, y0 + margin, x1 - margin, y1 - margin)


def _point_in_box(point: tuple[float, float], box: BBox) -> bool:
    x0, y0, x1, y1 = box
    return x0 < point[0] < x1 and y0 < point[1] < y1


def _boxes_overlap(a: BBox, b: BBox) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def _sample_segment(p0: tuple[float, float], p1: tuple[float, float]) -> list[tuple[float, float]]:
    length = ((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2) ** 0.5
    if length < 1e-6:
        return [p0]
    steps = max(int(length / _EDGE_SAMPLE_STEP), 1)
    return [
        (p0[0] + (p1[0] - p0[0]) * t / steps, p0[1] + (p1[1] - p0[1]) * t / steps)
        for t in range(steps + 1)
    ]


def effective_font_size(
    canvas_width: float, *, base_font_size: float = LABEL_SIZE, display_width: float = CONTENT_WIDTH
) -> float:
    """The label font size as it will ACTUALLY render once this diagram's
    SVG - drawn at `canvas_width` px in its own coordinate space - is
    scaled down (or up) to fit the page's real display width. SVG text
    scales with the whole document, so a diagram whose canvas legitimately
    grew wide (to avoid node overlap - see `_layout_hierarchy`'s row
    fan-out, `_layout_concept_map`'s spoke placement) can still end up
    UNREADABLE once shown at the page's ~666px content column, even though
    the diagram's own geometry has zero collisions. Confirmed real case: an
    8-child hierarchy's canvas grew to 1768px wide, which shrinks its
    17.5px label text down to ~6.6px once displayed at content width -
    well below any reasonable reading size."""
    if canvas_width <= 0:
        return base_font_size
    scale = min(display_width / canvas_width, 1.0)
    return base_font_size * scale


def readability_issues(canvas_width: float, *, display_width: float = CONTENT_WIDTH) -> list[RenderQAIssue]:
    effective = effective_font_size(canvas_width, display_width=display_width)
    if effective < MIN_READABLE_PX:
        return [RenderQAIssue(
            "text_too_small_after_scaling",
            f"canvas is {canvas_width:.0f}px wide; displayed at {display_width:.0f}px its "
            f"{LABEL_SIZE:.1f}px label text would render at only {effective:.1f}px, "
            f"below the {MIN_READABLE_PX:.0f}px readability floor",
        )]
    return []


def occupancy_ratio(root: ET.Element, canvas_w: float, canvas_h: float) -> float:
    """The fraction of the canvas actually covered by the bounding box of
    every drawn node - low means the diagram is a small island in a mostly
    empty canvas (nodes compressed into one corner, a canvas sized for
    content that didn't end up needing all of it), not a deliberately
    spacious layout. Returns 1.0 (never a false "too empty" claim) when
    there is nothing to measure, e.g. an empty diagram some OTHER check
    already flags."""
    nodes = _node_boxes(root)
    if not nodes or canvas_w <= 0 or canvas_h <= 0:
        return 1.0
    x0 = min(b[0] for b in nodes)
    y0 = min(b[1] for b in nodes)
    x1 = max(b[2] for b in nodes)
    y1 = max(b[3] for b in nodes)
    content_area = max(x1 - x0, 0.0) * max(y1 - y0, 0.0)
    return content_area / (canvas_w * canvas_h)


def occupancy_issues(root: ET.Element, canvas_w: float, canvas_h: float) -> list[RenderQAIssue]:
    """Deliberately conservative (`MIN_OCCUPANCY` is low): a real diagram
    legitimately has generous margins and gaps between nodes by design
    (see `MARGIN`/`BLOCK_GAP`-equivalent spacing throughout
    diagram_renderer.py) - this exists to catch ACCIDENTAL emptiness from a
    bad layout calculation (e.g. a canvas sized for content that didn't
    end up needing it), never to push back on ordinary, intentional
    whitespace."""
    ratio = occupancy_ratio(root, canvas_w, canvas_h)
    if ratio < MIN_OCCUPANCY:
        return [RenderQAIssue(
            "excessive_whitespace",
            f"drawn content occupies only {ratio:.0%} of the {canvas_w:.0f}x{canvas_h:.0f} canvas",
        )]
    return []


# A little slack for the width estimate's own rounding (same spirit as
# _TOLERANCE) - flag only a genuinely meaningful overflow.
_TEXT_OVERFLOW_TOLERANCE = 4.0


def text_overflow_issues(root: ET.Element) -> list[RenderQAIssue]:
    """Every `<g class="diagram-node">` group's own text, checked against
    its own `diagram-box` shape's right edge - using the SAME character-
    width estimate (`_SANS_RATIO`) `_wrap` itself wraps with, so this stays
    consistent with what "fits" means elsewhere in the renderer. Confirmed
    real gap: a single unbroken 79-character label (no spaces to wrap on)
    rendered as one line that overflowed straight past its node's right
    edge - `_wrap` now force-breaks a word that long on its own (see its
    own docstring), so this exists as the permanent regression guard for
    that class of bug, not because it currently finds anything."""
    issues: list[RenderQAIssue] = []
    for group in root.iter():
        if _local(group.tag) != "g" or "diagram-node" not in (group.get("class") or "").split():
            continue
        box = next(
            (b for el in group.iter() if "diagram-box" in (el.get("class") or "").split()
             for b in [_shape_bbox(el)] if b is not None),
            None,
        )
        if box is None:
            continue
        right_edge = box[2] - BOX_PADDING
        for text_el in group.iter():
            if _local(text_el.tag) != "text":
                continue
            font_size = _f(text_el, "font-size", 15.0)
            for tspan in text_el:
                if _local(tspan.tag) != "tspan" or not (tspan.text or "").strip():
                    continue
                tspan_x = _f(tspan, "x", _f(text_el, "x"))
                estimated_width = len(tspan.text) * font_size * _SANS_RATIO
                overflow = (tspan_x + estimated_width) - right_edge - _TEXT_OVERFLOW_TOLERANCE
                if overflow > 0:
                    issues.append(RenderQAIssue(
                        "text_overflows_node",
                        f"text '{tspan.text[:40]}' estimated {overflow:.0f}px past its node's right edge",
                    ))
    return issues


def validate_rendered_svg(svg_bytes: bytes) -> list[RenderQAIssue]:
    """Parses `svg_bytes` (exactly what `render_diagram_svg` produced) and
    checks the geometry a reader will actually see:
    - two node shapes overlapping each other,
    - an edge passing through the interior of a node it isn't legitimately
      touching (text always lives inside its own node's box in this
      renderer, so this also catches "a line runs through node text"),
    - a node's own text overflowing past its own box's right edge,
    - anything drawn outside the diagram's own canvas bounds,
    - label text that would render too small to read once the diagram is
      scaled to the page's real display width (see `readability_issues`),
    - drawn content occupying too little of its own canvas (see
      `occupancy_issues`).
    Returns an empty list for a clean diagram. Never raises on malformed
    SVG - a parse failure is reported as a single issue, not a crash."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError as exc:
        return [RenderQAIssue("unparseable_svg", str(exc))]

    canvas_w = _f(root, "width")
    canvas_h = _f(root, "height")
    nodes = _node_boxes(root)
    edges = _edge_segments(root)
    issues: list[RenderQAIssue] = [
        *text_overflow_issues(root),
        *readability_issues(canvas_w),
        *occupancy_issues(root, canvas_w, canvas_h),
    ]

    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if _boxes_overlap(a, b):
                issues.append(RenderQAIssue("node_overlap", f"{a} overlaps {b}"))

    inset_nodes = [_inset(box, _NODE_INSET) for box in nodes]
    for edge in edges:
        for p0, p1 in zip(edge, edge[1:]):
            samples = _sample_segment(p0, p1)
            # Trim samples near either endpoint of the WHOLE edge (not each
            # sub-segment) so a legitimate touch at the connected node's
            # boundary is never flagged.
            trimmed = [
                pt for pt in samples
                if _distance(pt, edge[0]) > _EDGE_ENDPOINT_MARGIN
                and _distance(pt, edge[-1]) > _EDGE_ENDPOINT_MARGIN
            ]
            for pt in trimmed:
                for box in inset_nodes:
                    if _point_in_box(pt, box):
                        issues.append(RenderQAIssue("edge_crosses_node", f"edge point {pt} inside {box}"))
                        break

    all_points = [pt for box in nodes for pt in ((box[0], box[1]), (box[2], box[3]))]
    all_points += [pt for edge in edges for pt in edge]
    for x, y in all_points:
        if x < -1.0 or y < -1.0 or x > canvas_w + 1.0 or y > canvas_h + 1.0:
            issues.append(RenderQAIssue("out_of_canvas", f"point ({x:.1f}, {y:.1f}) outside {canvas_w}x{canvas_h}"))

    return issues


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
