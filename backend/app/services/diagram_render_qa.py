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

from app.render.diagram_renderer import (
    BALANCE_LABEL_SIZE,
    BOX_PADDING,
    CHEVRON_LABEL_SIZE,
    GANTT_LABEL_SIZE,
    LABEL_SIZE,
    NESTED_CIRCLES_LABEL_SIZE,
    NETWORK_LABEL_SIZE,
    RING_LABEL_SIZE,
    STAIRCASE_LABEL_SIZE,
    STANDARD_FLOWCHART_LABEL_SIZE,
    TIMELINE_LABEL_SIZE,
    _BRIDGE_LABEL_SIZE,
    _CHAIN_LABEL_SIZE,
    _CIRCUIT_LABEL_SIZE,
    _DFD_LABEL_SIZE,
    _DONUT_LABEL_SIZE,
    _GEAR_LABEL_SIZE,
    _HONEYCOMB_LABEL_SIZE,
    _HOURGLASS_LABEL_SIZE,
    _JOURNEY_LABEL_SIZE,
    _ORBIT_LABEL_SIZE,
    _ORG_CHART_LABEL_SIZE,
    _PETAL_LABEL_SIZE,
    _PIE_LABEL_SIZE,
    _PT_SYMBOL_SIZE,
    _PROGRESS_RING_LABEL_SIZE,
    _PUZZLE_LABEL_SIZE,
    _RANKED_BAR_LABEL_SIZE,
    _SANS_RATIO,
    _SPECTRUM_LABEL_SIZE,
)
from app.schemas.document import CONTENT_WIDTH

_SVG_NS = "{http://www.w3.org/2000/svg}"

# Below this, a label's SVG-unit font size, once scaled down to the page's
# real display width, would render uncomfortably small. Not the full 14px
# node-title minimum itself (that's what diagram_renderer.LABEL_SIZE is
# now sized to hit AT THE STANDARD 880px canvas - see its own docstring) -
# this is the safety-net floor for the GENERAL render-QA gate, shared
# across every diagram kind, including concept_map/hierarchy whose radial/
# tree layouts can legitimately grow somewhat wider than 880px for benign
# structural reasons (a handful of spokes or siblings) that aren't the
# "diagram genuinely became unreadable" case this check exists to catch
# (confirmed real: an 8-child hierarchy's canvas grew to 1768px, dropping
# well under even this floor). A stricter 14px floor here made ordinary,
# moderate diagrams retry/fall back unnecessarily.
MIN_READABLE_PX = 11.0

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
    if tag == "path":
        return _path_bbox(el.get("d") or "")
    return None


_PATH_CMD_RE = re.compile(r"([MLCAZ])\s*([-\d.,\s]*)")


def _path_bbox(d: str) -> BBox | None:
    """An approximate bounding box for an arbitrary path `d` string - the
    newer node kinds that draw their own main shape as a `<path>` (a donut/
    pie wedge's arc sector, a flower petal's bezier outline) need SOME
    overlap/occupancy coverage, where previously `_shape_bbox` simply had
    no case for `path` at all and these shapes were invisible to every
    check below. Every command's own real endpoint is a candidate point -
    for `C` (cubic bezier), its CONTROL points too, since a bezier curve
    never leaves the convex hull of its own control points, so including
    them only WIDENS the box, never misses real extent in that direction.
    `A` (elliptical arc) contributes only its endpoint, not the arc's own
    bulge past the endpoint-to-endpoint chord - a known, accepted
    approximation (deriving the true arc extent needs the circle's centre,
    not available from the `d` string's numbers alone without re-deriving
    it) - acceptable here because this module's own arcs are used for
    either many equal wedges around a shared centre (whose endpoints
    collectively approximate the full circle's extent once aggregated
    across all of them) or a background band already exempted from the
    checks that would otherwise suffer from it (see bridge_process's own
    exemption below). Deliberately NOT reused by `_edge_segments`/
    `_walk_path`, which intentionally stay blind to curve commands so a
    node's own filled-shape path is never misread as a connector crossing
    itself - see that function's own docstring."""
    points: list[tuple[float, float]] = []
    for cmd, args in _PATH_CMD_RE.findall(d):
        nums = [float(n) for n in re.findall(r"-?[\d.]+", args)]
        if cmd in ("M", "L") and len(nums) >= 2:
            points.append((nums[0], nums[1]))
        elif cmd == "C" and len(nums) >= 6:
            points.append((nums[0], nums[1]))
            points.append((nums[2], nums[3]))
            points.append((nums[4], nums[5]))
        elif cmd == "A" and len(nums) >= 7:
            points.append((nums[5], nums[6]))
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _node_boxes(root: ET.Element) -> list[BBox]:
    boxes: list[BBox] = []
    for el in root.iter():
        if "diagram-box" not in (el.get("class") or "").split():
            continue
        box = _shape_bbox(el)
        if box is not None:
            boxes.append(box)
    return boxes


def _node_shapes(root: ET.Element) -> list[ET.Element]:
    """The `class="diagram-box"` elements themselves (not just their
    bounding box) - `edge_crosses_node` needs the real shape, because a
    circle's axis-aligned bounding SQUARE is a bad stand-in for containment:
    a point can sit well outside the circle (e.g. near a corner of that
    square, at ~45 degrees) while still falling inside the square, which
    would falsely flag an edge that cleanly clears the circle."""
    return [el for el in root.iter() if "diagram-box" in (el.get("class") or "").split()]


def _parse_points(raw: str) -> list[tuple[float, float]]:
    pairs = re.findall(r"(-?[\d.]+)[,\s]+(-?[\d.]+)", raw)
    return [(float(x), float(y)) for x, y in pairs]


def _iter_excluding_icons(el: ET.Element):
    """Like `el.iter()`, but never descends into a `class="diagram-icon"`
    group - see `_edge_segments`'s own docstring on why icon artwork must
    never be read as this module's "edge"/"node" geometry."""
    if "diagram-icon" in (el.get("class") or "").split():
        return
    yield el
    for child in el:
        yield from _iter_excluding_icons(child)


def _edge_segments(root: ET.Element) -> list[list[tuple[float, float]]]:
    """Every edge as a polyline of points (straight `<line>`s are 2 points;
    a `<path>` is walked command-by-command, sampling quadratic-bezier `Q`
    segments so a curved swimlane cross-lane edge is checked too, not just
    the straight ones).

    Deliberately skips every element inside a `class="diagram-icon"` group
    (see `_draw_icon`/`_icon_badge`, app.render.diagram_renderer) - an
    icon's own glyph is built from `<line>`/`<path>` primitives in a small
    LOCAL coordinate box (roughly -12..12), meant to be read only through
    the `translate(...) scale(...)` transform its wrapper group applies.
    This function has no notion of SVG `transform` at all (same as every
    other geometry check in this module - see this file's own module
    docstring on why every layout function draws directly in absolute
    canvas coordinates with no nested transforms, EXCEPT icon groups, the
    one deliberate exception); reading an icon's raw local-space
    coordinates as if they were absolute edge points is exactly what
    produced real, confirmed false positives (`out_of_canvas` on an icon's
    own small negative coordinates, `shared_edge_segment` between two
    unrelated icons' internal lines) once icons were added - never a real
    geometry defect in the diagram itself."""
    segments: list[list[tuple[float, float]]] = []
    for el in _iter_excluding_icons(root):
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


def _point_in_shape(point: tuple[float, float], el: ET.Element, margin: float) -> bool:
    """Like `_point_in_box`, but circle-aware: a circle's real containment
    test is Euclidean distance-from-centre, not its bounding square -
    see `_node_shapes`."""
    if _local(el.tag) == "circle":
        cx, cy, r = _f(el, "cx"), _f(el, "cy"), _f(el, "r")
        return _distance(point, (cx, cy)) < (r - margin)
    box = _shape_bbox(el)
    if box is None:
        return False
    return _point_in_box(point, _inset(box, margin))


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


def readability_issues(
    canvas_width: float, *, base_font_size: float = LABEL_SIZE, display_width: float = CONTENT_WIDTH
) -> list[RenderQAIssue]:
    effective = effective_font_size(canvas_width, base_font_size=base_font_size, display_width=display_width)
    if effective < MIN_READABLE_PX:
        return [RenderQAIssue(
            "text_too_small_after_scaling",
            f"canvas is {canvas_width:.0f}px wide; displayed at {display_width:.0f}px its "
            f"{base_font_size:.1f}px label text would render at only {effective:.1f}px, "
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
            # `x` means a different edge of the text depending on its own
            # `text-anchor` - a real, confirmed gap: this always assumed
            # "start" (x = the text's own LEFT edge, `_node_block`'s only
            # convention), so a `text-anchor="middle"` element (every
            # ring/wheel circle's own label - see _circle_node/_text) had
            # its right edge measured as `x + full_width` instead of
            # `x + half_width`, double-counting half the text and reporting
            # a false overflow on centered text that was never actually
            # too wide for its node.
            anchor = text_el.get("text-anchor") or "start"
            for tspan in text_el:
                if _local(tspan.tag) != "tspan" or not (tspan.text or "").strip():
                    continue
                tspan_x = _f(tspan, "x", _f(text_el, "x"))
                estimated_width = len(tspan.text) * font_size * _SANS_RATIO
                if anchor == "middle":
                    text_right_edge = tspan_x + estimated_width / 2
                elif anchor == "end":
                    text_right_edge = tspan_x
                else:
                    text_right_edge = tspan_x + estimated_width
                overflow = text_right_edge - right_edge - _TEXT_OVERFLOW_TOLERANCE
                if overflow > 0:
                    issues.append(RenderQAIssue(
                        "text_overflows_node",
                        f"text '{tspan.text[:40]}' estimated {overflow:.0f}px past its node's right edge",
                    ))
    return issues


# Minimum on-page font size by role, once this diagram's canvas is scaled
# to the page's real display width (same scale factor `effective_font_size`
# uses) - the user's own explicit per-role minimums, distinct from the
# single MIN_READABLE_PX safety-net floor above (that one exists to catch
# a diagram that's ACCIDENTALLY too small overall via the LABEL_SIZE
# constant; this checks every ACTUAL rendered font-size attribute against
# its own role's floor, so a smaller role - a subtitle, an edge label, a
# decision diamond's question text - can never quietly render under its
# own minimum even when the node title itself is fine).
_FONT_FLOOR_TITLE = 14.0
_FONT_FLOOR_SUBTITLE = 12.0
_FONT_FLOOR_EDGE_LABEL = 12.0
_FONT_FLOOR_DIAMOND = 12.0


def font_size_issues(root: ET.Element, canvas_w: float, *, display_width: float = CONTENT_WIDTH) -> list[RenderQAIssue]:
    """Every `<text>` element actually drawn, checked against its own
    role's minimum once scaled to the page's real display width. A
    `diagram-node` group containing a `<polygon>` is a decision diamond
    (>= 12px); within any other `diagram-node` group, `font-weight >= 600`
    is a title/label (>= 14px) and anything lighter is a subtitle/detail
    line (>= 12px). An edge label - a `<rect class="diagram-edge-label">`
    immediately followed by its own `<text>`, siblings rather than a
    group (see `diagram_renderer._edge_label`) - is checked separately
    (>= 12px)."""
    if canvas_w <= 0:
        return []
    scale = min(display_width / canvas_w, 1.0)
    issues: list[RenderQAIssue] = []
    for group in root.iter():
        if _local(group.tag) != "g" or "diagram-node" not in (group.get("class") or "").split():
            continue
        is_diamond = any(_local(el.tag) == "polygon" for el in group)
        for text_el in group.iter():
            if _local(text_el.tag) != "text":
                continue
            font_size = _f(text_el, "font-size", 0.0)
            weight = int(_f(text_el, "font-weight", 400))
            effective = font_size * scale
            if is_diamond:
                category, floor = "diamond", _FONT_FLOOR_DIAMOND
            elif weight >= 600:
                category, floor = "title", _FONT_FLOOR_TITLE
            else:
                category, floor = "subtitle", _FONT_FLOOR_SUBTITLE
            if effective < floor - 0.05:
                preview = "".join(t.text or "" for t in text_el).strip()[:40]
                issues.append(RenderQAIssue(
                    "font_below_minimum",
                    f"{category} text '{preview}' renders at {effective:.1f}px, below the {floor:.0f}px minimum",
                ))
    children = list(root)
    for i, el in enumerate(children):
        if _local(el.tag) != "rect" or "diagram-edge-label" not in (el.get("class") or "").split():
            continue
        if i + 1 >= len(children) or _local(children[i + 1].tag) != "text":
            continue
        text_el = children[i + 1]
        font_size = _f(text_el, "font-size", 0.0)
        effective = font_size * scale
        if effective < _FONT_FLOOR_EDGE_LABEL - 0.05:
            preview = "".join(t.text or "" for t in text_el).strip()[:40]
            issues.append(RenderQAIssue(
                "font_below_minimum",
                f"edge label '{preview}' renders at {effective:.1f}px, below the {_FONT_FLOOR_EDGE_LABEL:.0f}px minimum",
            ))
    return issues


_SHARED_SEGMENT_MIN_OVERLAP = 28.0
_COLLINEAR_TOLERANCE = 1.0


def shared_segment_issues(root: ET.Element) -> list[RenderQAIssue]:
    """Two DIFFERENT edges must never run along the same line for any real
    stretch - a reader can't tell which arrow is which where they overlap
    (a real reported case: a loop-back connector and an unrelated
    long-distance connector both routed through the exact same margin
    line). Decomposes every edge into its own axis-aligned sub-segments
    (this renderer only ever draws orthogonal lines, never a diagonal) and
    flags any two, from DIFFERENT edges, that are collinear with a real
    overlapping stretch - not merely touching at a shared endpoint, which
    is an ordinary elbow joint, not a collision."""
    edges = _edge_segments(root)
    verticals: list[tuple[int, float, float, float]] = []
    horizontals: list[tuple[int, float, float, float]] = []
    for idx, points in enumerate(edges):
        for p0, p1 in zip(points, points[1:]):
            if abs(p0[0] - p1[0]) < 0.5 and abs(p0[1] - p1[1]) >= 0.5:
                verticals.append((idx, p0[0], min(p0[1], p1[1]), max(p0[1], p1[1])))
            elif abs(p0[1] - p1[1]) < 0.5 and abs(p0[0] - p1[0]) >= 0.5:
                horizontals.append((idx, p0[1], min(p0[0], p1[0]), max(p0[0], p1[0])))
    issues: list[RenderQAIssue] = []
    for group, axis in ((verticals, "vertical"), (horizontals, "horizontal")):
        for i in range(len(group)):
            idx_a, line_a, a0, a1 = group[i]
            for j in range(i + 1, len(group)):
                idx_b, line_b, b0, b1 = group[j]
                if idx_a == idx_b or abs(line_a - line_b) > _COLLINEAR_TOLERANCE:
                    continue
                overlap = min(a1, b1) - max(a0, b0)
                if overlap > _SHARED_SEGMENT_MIN_OVERLAP:
                    issues.append(RenderQAIssue(
                        "shared_edge_segment",
                        f"two different edges both run {axis} along {line_a:.1f} for {overlap:.0f}px",
                    ))
    return issues


# How close a segment may run to a node's border before it reads as
# "running along" that border rather than merely passing near it - the
# user's own explicit 12px.
_BORDER_CLEARANCE = 12.0
_BORDER_OVERLAP_MIN = 4.0
# How close a point must be to a box to count as "this edge's own
# endpoint touching its own connected node" - generous enough to cover
# every shape this renderer draws (a pill's rounded cap, a diamond's
# vertex) sitting slightly outside the shape's own straight bounding box.
_OWN_NODE_TOLERANCE = 8.0


def _touches_box(point: tuple[float, float], box: BBox, tol: float = _OWN_NODE_TOLERANCE) -> bool:
    x0, y0, x1, y1 = box
    return (x0 - tol) <= point[0] <= (x1 + tol) and (y0 - tol) <= point[1] <= (y1 + tol)


def border_collinear_issues(root: ET.Element) -> list[RenderQAIssue]:
    """No edge segment may run along - or within the required clearance of
    - the border of a node it ISN'T actually connected to (a real reported
    case: a connector's own approach ran directly along an unrelated
    node's border, reading as part of that node's own outline rather than
    a distinct arrow). A node the edge legitimately starts or ends AT (its
    own two connected boxes) is exempt - an edge always touches its own
    endpoints' borders by definition, and that's not a collision."""
    boxes = _node_boxes(root)
    edges = _edge_segments(root)
    issues: list[RenderQAIssue] = []
    for edge in edges:
        if len(edge) < 2:
            continue
        own_boxes = [b for b in boxes if _touches_box(edge[0], b) or _touches_box(edge[-1], b)]
        for p0, p1 in zip(edge, edge[1:]):
            vertical = abs(p0[0] - p1[0]) < 0.5 and abs(p0[1] - p1[1]) >= 0.5
            horizontal = abs(p0[1] - p1[1]) < 0.5 and abs(p0[0] - p1[0]) >= 0.5
            if not (vertical or horizontal):
                continue
            seg_x0, seg_x1 = min(p0[0], p1[0]), max(p0[0], p1[0])
            seg_y0, seg_y1 = min(p0[1], p1[1]), max(p0[1], p1[1])
            for box in boxes:
                if box in own_boxes:
                    continue
                bx0, by0, bx1, by1 = box
                if vertical:
                    dist = min(abs(p0[0] - bx0), abs(p0[0] - bx1))
                    overlap = min(seg_y1, by1) - max(seg_y0, by0)
                else:
                    dist = min(abs(p0[1] - by0), abs(p0[1] - by1))
                    overlap = min(seg_x1, bx1) - max(seg_x0, bx0)
                if dist < _BORDER_CLEARANCE and overlap > _BORDER_OVERLAP_MIN:
                    issues.append(RenderQAIssue(
                        "edge_collinear_with_node_border",
                        f"edge segment runs within {dist:.1f}px of an unrelated node's border {box} for {overlap:.0f}px",
                    ))
    return issues


_ARROWHEAD_SIDE_TOLERANCE = 6.0
# How close two landing points on the SAME side of the SAME box need to be
# (along that side - x for top/bottom, y for left/right) to count as a real
# duplicate - genuinely overlapping/indistinguishable - rather than two
# legitimately distinct connectors that both happen to land on that side. A
# real, confirmed false positive: an er_diagram entity with 2+ attribute
# ovals fanning out across its own top edge (standard ER notation, each
# attribute's own connector landing at its own x) used to be flagged here
# just for having "2 arrowheads on its top side", even though they were
# over 100px apart and nowhere near visually ambiguous. Two points only a
# few px apart (the ACTUAL "read as one line" case the real reported bug -
# a spine arrow and a loop-back both entering at the same spot - this
# tolerance is built around) still get caught.
_DUPLICATE_LANDING_TOLERANCE = 20.0


def label_overlaps_node_issues(root: ET.Element) -> list[RenderQAIssue]:
    """A text label's own drawn pill (`class="diagram-edge-label"`, the rect
    `app.render.diagram_renderer._edge_label` draws for a concept_map/wheel-
    style edge relationship) sitting on top of a NODE shape it isn't
    actually naming.

    Root-cause fix (production course audit): a real course's concept_map
    had several satellite-to-satellite edges converging on the same target
    (e.g. "Shells"/"Anomalies" both relating to BOTH "I1" and "EA") - the
    in-process layout retry (`_layout_concept_wheel`'s own `_curve_clears_
    nodes`/`_label_collides`) only checked the thin CURVE path and other
    LABELS' positions, never whether the much wider label PILL itself
    overlapped an uninvolved node's circle, so a label ended up sitting
    almost entirely on top of a target circle's own text - and this file
    had no check that would ever have caught it: `validate_rendered_svg`
    returned a clean `[]` on that exact diagram. This closes that gap as an
    independent, final verification pass (the layout retry loop's own
    avoidance logic is a best-effort first line of defence, not proof).

    Uses the same circle-aware containment reasoning `_point_in_shape`
    already established (a circle's axis-aligned bounding SQUARE is a bad
    stand-in for its real shape - a label could sit well outside the
    circle while still falling inside that square) via a closest-point-on-
    rect-to-circle-centre test, not a naive bbox-vs-bbox overlap, for a
    circle node; a plain bbox overlap otherwise, matching `_boxes_overlap`'s
    existing convention for every other shape kind this file already
    understands."""
    issues: list[RenderQAIssue] = []
    labels = [el for el in root.iter() if "diagram-edge-label" in (el.get("class") or "").split()]
    if not labels:
        return issues
    shapes = _node_shapes(root)
    for label_el in labels:
        lx, ly = _f(label_el, "x"), _f(label_el, "y")
        lw, lh = _f(label_el, "width"), _f(label_el, "height")
        label_box = (lx, ly, lx + lw, ly + lh)
        for shape in shapes:
            if _local(shape.tag) == "circle":
                cx, cy, r = _f(shape, "cx"), _f(shape, "cy"), _f(shape, "r")
                closest_x = min(max(cx, label_box[0]), label_box[2])
                closest_y = min(max(cy, label_box[1]), label_box[3])
                if _distance((cx, cy), (closest_x, closest_y)) < r - _NODE_INSET:
                    issues.append(RenderQAIssue(
                        "label_overlaps_node",
                        f"edge label {tuple(round(v, 1) for v in label_box)} overlaps circle "
                        f"(cx={cx:.1f}, cy={cy:.1f}, r={r:.1f})",
                    ))
                    break
            else:
                box = _shape_bbox(shape)
                if box is not None and _boxes_overlap(label_box, box):
                    issues.append(RenderQAIssue(
                        "label_overlaps_node",
                        f"edge label {tuple(round(v, 1) for v in label_box)} overlaps node {box}",
                    ))
                    break
    return issues


def label_overlaps_label_issues(root: ET.Element) -> list[RenderQAIssue]:
    """Two edge-relationship label pills (`class="diagram-edge-label"`)
    sitting on top of EACH OTHER - a different defect from `label_overlaps_
    node_issues` just above (a label vs. a node circle), confirmed as its
    own, separately-occurring gap: a real course's concept_map had several
    edges converging on the same hub get long enough labels that the
    in-process layout retry's `_label_collides` check (a cheap midpoint-
    distance estimate, used only to decide whether to keep growing a
    curve's bulge) exhausted its bounded attempts without finding a fully
    clear position and shipped its best, still-overlapping attempt - and,
    exactly like the node-overlap gap this file already closed once,
    nothing here ever verified the two PILLS' own real rectangles against
    each other afterward as an independent, final check. Plain bbox overlap
    is correct here (unlike the circle case above) - a label pill is always
    an axis-aligned rect, never a circle."""
    issues: list[RenderQAIssue] = []
    labels = [el for el in root.iter() if "diagram-edge-label" in (el.get("class") or "").split()]
    boxes = []
    for label_el in labels:
        lx, ly = _f(label_el, "x"), _f(label_el, "y")
        lw, lh = _f(label_el, "width"), _f(label_el, "height")
        boxes.append((lx, ly, lx + lw, ly + lh))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _boxes_overlap(boxes[i], boxes[j]):
                issues.append(RenderQAIssue(
                    "label_overlaps_label",
                    f"edge label {tuple(round(v, 1) for v in boxes[i])} overlaps "
                    f"edge label {tuple(round(v, 1) for v in boxes[j])}",
                ))
    return issues


def duplicate_arrowhead_issues(root: ET.Element) -> list[RenderQAIssue]:
    """Two arrows landing on (nearly) the same spot on the same node side
    are visually indistinguishable, reading as a single ambiguous
    connection rather than two distinct ones (a real reported case: an
    ordinary spine arrow and an unrelated loop-back both entered the same
    node at its top). Two connectors merely sharing a SIDE - without
    landing close together on it - are not flagged; a legitimately fanned-
    out set of connectors (e.g. an ER entity's own attribute ovals) is
    normal notation, not an overlap (see _DUPLICATE_LANDING_TOLERANCE)."""
    boxes = _node_boxes(root)
    edges = _edge_segments(root)
    # (box_i, side) -> list of this landing's own position ALONG that side
    # (x for top/bottom, y for left/right) - compared pairwise below so
    # only genuinely CLOSE landings count as a duplicate, not just any two
    # sharing the same side.
    side_landings: dict[tuple[int, str], list[float]] = {}
    for edge in edges:
        if len(edge) < 2:
            continue
        end = edge[-1]
        for i, box in enumerate(boxes):
            x0, y0, x1, y1 = box
            tol = _ARROWHEAD_SIDE_TOLERANCE
            if abs(end[1] - y0) < tol and x0 - tol <= end[0] <= x1 + tol:
                side, along = "top", end[0]
            elif abs(end[1] - y1) < tol and x0 - tol <= end[0] <= x1 + tol:
                side, along = "bottom", end[0]
            elif abs(end[0] - x0) < tol and y0 - tol <= end[1] <= y1 + tol:
                side, along = "left", end[1]
            elif abs(end[0] - x1) < tol and y0 - tol <= end[1] <= y1 + tol:
                side, along = "right", end[1]
            else:
                continue
            side_landings.setdefault((i, side), []).append(along)
            break
    issues: list[RenderQAIssue] = []
    for (box_i, side), positions in side_landings.items():
        positions = sorted(positions)
        for a, b in zip(positions, positions[1:]):
            if b - a < _DUPLICATE_LANDING_TOLERANCE:
                issues.append(RenderQAIssue(
                    "duplicate_arrowhead_side",
                    f"node {boxes[box_i]} has 2 arrowheads {b - a:.1f}px apart on its {side} side",
                ))
                break
    return issues


def validate_rendered_svg(svg_bytes: bytes, *, kind: str = "") -> list[RenderQAIssue]:
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
      `occupancy_issues`),
    - two different edges running along the same segment (see
      `shared_segment_issues`),
    - an edge's own relationship-label pill sitting on top of a node circle
      or box it isn't naming (see `label_overlaps_node_issues`),
    - an edge running collinear with an unrelated node's border (see
      `border_collinear_issues`),
    - a node side receiving more than one arrowhead (see
      `duplicate_arrowhead_issues`),
    - any rendered text below its own role's minimum font size (see
      `font_size_issues`) - only for `kind="flow_chart"`/`"process"`,
      whose own font-size constants (`diagram_renderer.LABEL_SIZE` etc.)
      are specifically calibrated to clear these floors (see their own
      comments); other kinds' layouts can legitimately grow wide for
      benign structural reasons this stricter, per-role floor would
      otherwise flag unnecessarily (the same tension `MIN_READABLE_PX`'s
      own docstring explains) - they still get the general, more lenient
      `readability_issues` check above.
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
    # cycle/concept_map render as a ring/wheel of circles
    # (app.render.diagram_renderer._layout_ring_process/_layout_concept_wheel),
    # deliberately at RING_LABEL_SIZE - smaller than LABEL_SIZE, since a
    # circle has less usable room than a box. radial_mindmap/venn also draw
    # circles at this same size (_circle_node/_fit_boxed_text at
    # _VENN_LABEL_SIZE, close enough to share this bucket rather than add a
    # third one for a 1pt difference). flow_chart/process render as
    # a standard oval/rectangle/diamond flowchart instead
    # (_layout_standard_flowchart) at its own, different
    # STANDARD_FLOWCHART_LABEL_SIZE (the smallest size it actually uses -
    # its pill shape renders larger). readability_issues needs the REAL
    # base size each kind actually draws at to judge "too small once
    # scaled", never the old box convention's LABEL_SIZE for any of these.
    _RING_KINDS = ("cycle", "concept_map", "conceptual", "radial_mindmap", "venn")
    _STANDARD_FLOWCHART_KINDS = ("flow_chart", "process", "decision_tree", "fishbone", "layered_diagram")
    if kind in _RING_KINDS or kind == "er_diagram":
        # er_diagram mixes `_node_block` entity boxes with `_draw_attribute_
        # oval`'s smaller attribute ovals (also sized at RING_LABEL_SIZE - a
        # real, confirmed bug: they used to render at a hardcoded 11.5px,
        # under the readability floor once scaled, and this kind wasn't even
        # in this lookup table to catch it). Using the smaller size here is
        # the conservative floor: if RING_LABEL_SIZE-based text clears the
        # readability check, the larger box text automatically does too,
        # and a genuinely too-small circle/oval (the actual shape this
        # check exists to catch) never gets missed by being judged against
        # the bigger box size instead.
        label_base_size = RING_LABEL_SIZE
    elif kind == "data_flow_diagram":
        # Its own, larger floor (see `_DFD_LABEL_SIZE`'s own comment on
        # diagram_renderer - a real, confirmed complaint that RING_LABEL_
        # SIZE-sized circles, scaled down to this kind's always-fixed
        # canvas width, sat right AT the 11px readability floor instead of
        # comfortably above it) - still the smaller of the two shapes this
        # kind mixes (`_node_block` boxes at LABEL_SIZE=19.0, circles at
        # _DFD_LABEL_SIZE=18.0), so it's still the real conservative floor.
        label_base_size = _DFD_LABEL_SIZE
    elif kind in _STANDARD_FLOWCHART_KINDS:
        label_base_size = STANDARD_FLOWCHART_LABEL_SIZE
    elif kind == "chevron_process":
        label_base_size = CHEVRON_LABEL_SIZE
    elif kind == "timeline":
        label_base_size = TIMELINE_LABEL_SIZE
    elif kind == "staircase_process":
        label_base_size = STAIRCASE_LABEL_SIZE
    elif kind == "balance_comparison":
        label_base_size = BALANCE_LABEL_SIZE
    elif kind == "nested_circles":
        label_base_size = NESTED_CIRCLES_LABEL_SIZE
    elif kind == "network_mesh":
        label_base_size = NETWORK_LABEL_SIZE
    elif kind == "gantt_chart":
        label_base_size = GANTT_LABEL_SIZE
    elif kind == "honeycomb":
        label_base_size = _HONEYCOMB_LABEL_SIZE
    elif kind == "puzzle_pieces":
        label_base_size = _PUZZLE_LABEL_SIZE
    elif kind == "gears_process":
        label_base_size = _GEAR_LABEL_SIZE
    elif kind == "donut_segments":
        label_base_size = _DONUT_LABEL_SIZE
    elif kind == "pie_wedge":
        label_base_size = _PIE_LABEL_SIZE
    elif kind == "orbit_diagram":
        label_base_size = _ORBIT_LABEL_SIZE
    elif kind == "progress_rings":
        label_base_size = _PROGRESS_RING_LABEL_SIZE
    elif kind == "bridge_process":
        label_base_size = _BRIDGE_LABEL_SIZE
    elif kind == "chain_links":
        label_base_size = _CHAIN_LABEL_SIZE
    elif kind == "flower_petal":
        label_base_size = _PETAL_LABEL_SIZE
    elif kind == "hourglass_diagram":
        label_base_size = _HOURGLASS_LABEL_SIZE
    elif kind == "org_chart":
        label_base_size = _ORG_CHART_LABEL_SIZE
    elif kind == "wavy_journey_path":
        label_base_size = _JOURNEY_LABEL_SIZE
    elif kind == "spectrum":
        label_base_size = _SPECTRUM_LABEL_SIZE
    elif kind == "causal_loop":
        label_base_size = RING_LABEL_SIZE
    elif kind == "ranked_bars":
        label_base_size = _RANKED_BAR_LABEL_SIZE
    elif kind == "circuit_schematic":
        label_base_size = _CIRCUIT_LABEL_SIZE
    elif kind == "periodic_table":
        label_base_size = _PT_SYMBOL_SIZE
    else:
        # iceberg/feedback_loop_process/input_process_output all draw
        # their node content through `_node_block` at LABEL_SIZE, same as
        # this default - no dedicated entry needed for them.
        label_base_size = LABEL_SIZE
    issues: list[RenderQAIssue] = [
        *text_overflow_issues(root),
        *readability_issues(canvas_w, base_font_size=label_base_size),
        *occupancy_issues(root, canvas_w, canvas_h),
        *shared_segment_issues(root),
        # Gated to the wheel-style kinds this fix actually targeted and
        # verified (concept_map/conceptual) - a broader rollout surfaced a
        # genuine but entirely unrelated, unverified pre-existing overlap
        # in `feedback_loop_process`'s long-label backward edge, out of
        # this fix's own scope to investigate or fix; narrowing here avoids
        # turning on a new failure for a kind nobody has checked against it
        # yet, same reasoning several of this function's other per-kind
        # exemptions below already use.
        *(label_overlaps_node_issues(root) if kind in ("concept_map", "conceptual") else []),
        *(label_overlaps_label_issues(root) if kind in ("concept_map", "conceptual") else []),
        # Same reasoning as the edge_crosses_node exemption just below:
        # gantt_chart's tick gridlines are DESIGNED to run collinear with
        # (behind) every task bar in their column - that's the standard
        # Gantt reference-grid convention, not a connector drifting along
        # an unrelated node's border.
        *(border_collinear_issues(root) if kind != "gantt_chart" else []),
        # network_mesh draws genuinely undirected connections - plain,
        # 65%-opacity lines/curves, never an arrowhead marker (see
        # _layout_network_mesh) - so even a close landing there is a much
        # smaller visual risk than two actual ARROWHEADS converging (this
        # check's own motivating case). A real, dense topology (9 edges
        # across 6 nodes - well past the "2-8 nodes" guidance, closer to a
        # complete graph than typical real content) can still produce an
        # occasional few-px-apart landing even after _DUPLICATE_LANDING_
        # TOLERANCE's fix for the unrelated ER false-positive below -
        # exempting this kind specifically keeps that fix from also
        # re-litigating an already-verified-clean network render.
        # donut_segments/pie_wedge/flower_petal/bridge_process join for
        # the same reason as their own edge_crosses_node exemption above -
        # this check's `_edge_segments` input picks up each one's own
        # node-shape `<path>` as if it were a real connector, and every
        # one of those "lands" near the shapes' shared centre point -
        # never an actual ambiguous arrowhead pair, since none of these
        # kinds draws an arrow at all.
        *(
            duplicate_arrowhead_issues(root)
            if kind not in ("network_mesh", "donut_segments", "pie_wedge", "flower_petal", "bridge_process")
            else []
        ),
        # font_size_issues checks role-specific floors calibrated to
        # _node_block's box title/subtitle structure (LABEL_SIZE/
        # DETAIL_SIZE). flow_chart/process used to draw that structure and
        # were the only kinds this ever applied to; they no longer do (see
        # _RING_KINDS above - both now route to the ring instead), so this
        # check no longer has anything left to apply to. Kept (rather than
        # deleted) in case a future kind reintroduces that same box
        # structure. readability_issues above still covers every kind's
        # real font size either way.
        *(font_size_issues(root, canvas_w) if kind in () else []),
    ]

    # venn's own circles are SUPPOSED to overlap - that overlap is the
    # entire semantic point of the diagram (_layout_venn), not a layout
    # bug - so this is one of the kinds exempt from the overlap check, the
    # same "a real, structural reason this kind's geometry doesn't fit the
    # generic rule" exemption schematic/graph already get elsewhere in
    # this module's own docstring. nested_circles is the same - its whole
    # point is FULL containment (an inner ring sits entirely inside every
    # ring around it, by design - see _layout_nested_circles). honeycomb
    # joins them for a related but distinct reason: adjacent hexagons
    # TESSELLATE (share a real edge, by design - see _layout_honeycomb),
    # which means their axis-aligned bounding BOXES always overlap in the
    # corner regions outside the hexagon's own hexagonal outline, even
    # though the actual painted shapes never do - a real, confirmed false
    # positive on every honeycomb cluster, not a rare edge case.
    # chain_links joins them for the same reason as honeycomb: each link
    # legitimately overlaps its neighbour by design (the "interlocking
    # chain" look - see _layout_chain_links). donut_segments/pie_wedge/
    # flower_petal join for a DIFFERENT but related structural reason:
    # every one of their own node shapes is a `<path>` whose own true
    # outline this module can only bound approximately (`_path_bbox`'s own
    # docstring on why) - and, more fundamentally, every wedge/petal
    # SHARES the same centre point by construction (a pie/donut slice's
    # point, a petal's own base), so even a perfectly accurate bounding
    # box for each one would still overlap every other one right at that
    # shared point - an unavoidable, structural property of a
    # radiates-from-one-centre shape family, not a real collision between
    # two things that are supposed to stay apart.
    _OVERLAP_EXEMPT_KINDS = (
        "venn", "nested_circles", "honeycomb", "chain_links",
        "donut_segments", "pie_wedge", "flower_petal",
    )
    if kind not in _OVERLAP_EXEMPT_KINDS:
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                if _boxes_overlap(a, b):
                    issues.append(RenderQAIssue("node_overlap", f"{a} overlaps {b}"))

    # gantt_chart's own "edges" (per _edge_segments, which treats any
    # <line>/<path> generically) are really its reference-grid tick
    # lines, not connectors - those are DRAWN to run the full height of
    # the chart, behind every task bar, the standard Gantt convention
    # (see _layout_gantt_chart). This check's whole premise ("a connector
    # accidentally passing through an unrelated node") doesn't apply to a
    # background gridline doing exactly what it's there to do. bridge_
    # process joins it for the same reason: its arched band (a <path>, by
    # construction - an arc needs one) is a decorative background shape
    # that every step node legitimately sits ON TOP of (see
    # _layout_bridge_process) - every sampled point along that band near a
    # node is the band doing its job, not a connector straying into an
    # unrelated shape. donut_segments/pie_wedge/flower_petal join for the
    # same "shared centre point" structural reason _OVERLAP_EXEMPT_KINDS
    # documents just above - every one of their own `<path>` node shapes
    # meets every other one exactly at that shared centre, which an
    # edge-vs-node containment check can't tell apart from a real
    # connector straying into an unrelated node.
    node_shapes = _node_shapes(root)
    if kind not in ("gantt_chart", "bridge_process", "donut_segments", "pie_wedge", "flower_petal"):
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
                    for shape in node_shapes:
                        if _point_in_shape(pt, shape, _NODE_INSET):
                            box = _shape_bbox(shape)
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
