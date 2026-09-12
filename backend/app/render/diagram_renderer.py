"""DiagramSpec -> SVG.

Pure and AI-free, the same philosophy as `app.render.html_renderer`: the model
only supplies structured content (nodes/edges/kind); every pixel here is
produced by deterministic code, on the course's own theme colours, so a
diagram always renders crisp labels and never drifts from the brand.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

from app.schemas.diagram import (
    SHAPE_TYPES,
    DiagramEdge,
    DiagramNode,
    DiagramSpec,
    SchematicShape,
    SchematicState,
)
from app.schemas.template import TemplateTheme

CANVAS_WIDTH = 880.0
MARGIN = 40.0
LABEL_SIZE = 15.0
DETAIL_SIZE = 12.5
LINE_HEIGHT = 1.35
BOX_PADDING = 14.0
BADGE_RADIUS = 14.0
_SANS_RATIO = 0.56

# --- schematic panel geometry -------------------------------------------
PANEL_HEIGHT = 300.0
PANEL_GAP = 30.0
PANEL_CAPTION_SIZE = 13.0


def _wrap(text: str, *, font_size: float, width: float, max_lines: int = 3) -> list[str]:
    """Greedy word wrap; mirrors the ratio `course.document.layout` uses so
    diagram text density looks consistent with the rest of the page."""
    text = " ".join((text or "").split())
    if not text:
        return []
    chars_per_line = max(int(width / (font_size * _SANS_RATIO)), 6)
    words = text.split(" ")
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        add = len(word) + (1 if current else 0)
        if current and length + add > chars_per_line:
            lines.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += add
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [lines[max_lines - 1].rstrip() + "…"]
    return lines


def _text(
    x: float,
    y: float,
    lines: list[str],
    *,
    font_size: float,
    weight: int,
    color: str,
    font_family: str,
    anchor: str = "middle",
) -> tuple[str, float]:
    if not lines:
        return "", 0.0
    spans = []
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else font_size * LINE_HEIGHT
        spans.append(f'<tspan x="{x:.1f}" dy="{dy:.1f}">{escape(line)}</tspan>')
    svg = (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{font_size:.1f}" '
        f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}" '
        f'font-family="{escape(font_family)}">{"".join(spans)}</text>'
    )
    return svg, font_size * LINE_HEIGHT * len(lines)


def _arrow_marker(id_: str, color: str) -> str:
    return (
        f'<marker id="{id_}" viewBox="0 0 10 10" refX="8" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>'
    )


def _node_block(
    node: DiagramNode,
    *,
    x: float,
    y: float,
    width: float,
    theme: TemplateTheme,
    badge: str | None = None,
    emphasis: bool = False,
) -> tuple[str, float]:
    """One rounded box: label (bold) + wrapped detail. Returns (svg, height).
    `emphasis` marks the central/focal subject of a concept map - a heavier,
    accent-toned box so a reader's eye lands on it first."""
    inner_w = width - 2 * BOX_PADDING - (28.0 if badge else 0.0)
    text_x = x + BOX_PADDING + (28.0 if badge else 0.0)
    label_lines = _wrap(node.label, font_size=LABEL_SIZE, width=inner_w, max_lines=2) or ["—"]
    detail_lines = _wrap(node.detail, font_size=DETAIL_SIZE, width=inner_w, max_lines=3)

    content_top = y + BOX_PADDING
    label_svg, label_h = _text(
        text_x,
        content_top + LABEL_SIZE,
        label_lines,
        font_size=LABEL_SIZE,
        weight=600,
        color=theme.text_color,
        font_family=theme.font_family,
        anchor="start",
    )
    bottom = content_top + label_h
    detail_svg = ""
    if detail_lines:
        bottom += 4
        detail_svg, detail_h = _text(
            text_x,
            bottom + DETAIL_SIZE * 0.9,
            detail_lines,
            font_size=DETAIL_SIZE,
            weight=400,
            color=theme.muted_color,
            font_family=theme.font_family,
            anchor="start",
        )
        bottom += detail_h

    height = max(bottom - y + BOX_PADDING, 2 * BOX_PADDING + LABEL_SIZE + 6)

    # The tooltip carries the *full* label/detail even when the box itself had
    # to truncate the wrapped text - hovering always reveals everything.
    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()

    fill = theme.accent_soft if emphasis else theme.surface_color
    stroke = theme.accent_color if emphasis else theme.border_color
    stroke_width = 2.5 if emphasis else 1.5
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
        f'rx="10" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
    )
    if badge is not None:
        cy = y + BOX_PADDING + BADGE_RADIUS - 2
        cx = x + BOX_PADDING + BADGE_RADIUS - 6
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{BADGE_RADIUS:.1f}" fill="{theme.accent_color}"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 4.5:.1f}" font-size="12" font-weight="700" '
            f'fill="#ffffff" text-anchor="middle" font-family="{escape(theme.font_family)}">{escape(badge)}</text>'
        )
    parts.append(label_svg)
    if detail_svg:
        parts.append(detail_svg)
    group = f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'
    return group, height


EDGE_LABEL_FONT_SIZE = 11.0
EDGE_LABEL_MAX_WIDTH = 200.0
EDGE_LABEL_LINE_GAP = 14.0  # vertical distance between wrapped lines


def _edge_label(x: float, y: float, text: str, theme: TemplateTheme, *, max_width: float = EDGE_LABEL_MAX_WIDTH) -> str:
    """A small pill sitting on an edge, naming the relationship it
    represents. Wraps to at most 2 lines instead of overflowing its pill -
    same visual style as the single-line version when text is short, just
    taller when it isn't."""
    text = text.strip()
    if not text:
        return ""
    inner_w = max_width - 16.0
    lines = _wrap(text, font_size=EDGE_LABEL_FONT_SIZE, width=inner_w, max_lines=2)
    if not lines:
        return ""
    longest = max(len(line) for line in lines)
    width = min(max(longest * 6.5 + 14, 40.0), max_width)
    height = 18.0 if len(lines) == 1 else 18.0 + EDGE_LABEL_LINE_GAP
    rect = (
        f'<rect class="diagram-edge-label" x="{x - width / 2:.1f}" y="{y - height / 2:.1f}" '
        f'width="{width:.1f}" height="{height:.1f}" rx="9" fill="{theme.page_background}" '
        f'stroke="{theme.border_color}"/>'
    )
    start_y = y - (len(lines) - 1) * EDGE_LABEL_LINE_GAP / 2 + 4
    spans = [
        f'<tspan x="{x:.1f}" dy="{0.0 if i == 0 else EDGE_LABEL_LINE_GAP:.1f}">{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    ]
    text_svg = (
        f'<text x="{x:.1f}" y="{start_y:.1f}" font-size="{EDGE_LABEL_FONT_SIZE:.1f}" fill="{theme.muted_color}" '
        f'text-anchor="middle" font-family="{escape(theme.font_family)}">{"".join(spans)}</text>'
    )
    return rect + text_svg


# ---------------------------------------------------------------------------
# layouts
# ---------------------------------------------------------------------------


def _layout_vertical(
    nodes: list[DiagramNode],
    edges: list[DiagramEdge],
    *,
    theme: TemplateTheme,
    connected: bool,
    top: float,
) -> tuple[list[str], float]:
    """Stacked boxes, numbered. Connected kinds get an arrow + optional edge label."""
    edge_labels = {(e.source, e.target): e.label for e in edges}
    box_w = CANVAS_WIDTH - 2 * MARGIN
    # Reserve enough vertical room for a wrapped, two-line edge label (up to
    # ~32px tall) plus real clearance above/below it - a tighter gap could
    # let a long transition label crowd the boxes on either side.
    gap = 60.0 if connected else 26.0
    x = MARGIN
    y = top
    elements: list[str] = []
    if connected:
        elements.append(_arrow_marker("diagram-arrow", theme.accent_color))

    previous_bottom: float | None = None
    for index, node in enumerate(nodes):
        if connected and previous_bottom is not None:
            mid_x = x + box_w / 2
            elements.append(
                f'<line x1="{mid_x:.1f}" y1="{previous_bottom:.1f}" x2="{mid_x:.1f}" '
                f'y2="{y - 4:.1f}" stroke="{theme.accent_color}" stroke-width="2" '
                f'marker-end="url(#diagram-arrow)"/>'
            )
            key = (nodes[index - 1].id, node.id)
            elements.append(_edge_label(mid_x, (previous_bottom + y - 4) / 2, edge_labels.get(key, ""), theme))

        svg, height = _node_block(
            node, x=x, y=y, width=box_w, theme=theme, badge=str(index + 1)
        )
        elements.append(svg)
        previous_bottom = y + height
        y = previous_bottom + gap

    total_height = y - gap - top if nodes else 0.0
    return elements, total_height


def _layout_cycle(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """Nodes arranged around a ring, connected head-to-tail."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    box_w = 220.0
    box_h = 92.0
    radius = max(170.0, count * 42.0)
    cx = radius + box_w / 2 + 20
    cy = top + radius + box_h / 2 + 20
    width = 2 * cx
    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]

    centers: list[tuple[float, float]] = []
    for index in range(count):
        angle = (2 * math.pi * index / count) - math.pi / 2
        nx = cx + radius * math.cos(angle)
        ny = cy + radius * math.sin(angle)
        centers.append((nx, ny))

    for index, node in enumerate(nodes):
        nx, ny = centers[index]
        nxt_x, nxt_y = centers[(index + 1) % count]
        # Shorten the connecting line so it starts/ends at the box edge, not its centre.
        dx, dy = nxt_x - nx, nxt_y - ny
        dist = max(math.hypot(dx, dy), 1.0)
        ux, uy = dx / dist, dy / dist
        start = (nx + ux * box_w / 2, ny + uy * box_h / 2)
        end = (nxt_x - ux * box_w / 2, nxt_y - uy * box_h / 2)
        elements.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        svg, height = _node_block(
            node, x=nx - box_w / 2, y=ny - box_h / 2, width=box_w, theme=theme, badge=str(index + 1)
        )
        elements.append(svg)

    height_total = cy + radius + box_h / 2 + 20 - top
    return elements, width, height_total


def _layout_hierarchy(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """Nodes grouped into rows by `level`, connected to their parent edge."""
    rows: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        rows.setdefault(max(node.level, 0), []).append(node)
    levels = sorted(rows)

    box_w = 190.0
    row_gap = 56.0
    positions: dict[str, tuple[float, float, float]] = {}  # id -> (cx, top_y, bottom_y)
    elements: list[str] = []
    elements.append(_arrow_marker("diagram-arrow", theme.accent_color))

    y = top
    max_row_width = 0.0
    for level in levels:
        row_nodes = rows[level]
        row_width = len(row_nodes) * box_w + (len(row_nodes) - 1) * 24.0
        max_row_width = max(max_row_width, row_width)
        x = MARGIN
        row_height = 0.0
        for node in row_nodes:
            svg, height = _node_block(node, x=x, y=y, width=box_w, theme=theme)
            elements.append(svg)
            cx = x + box_w / 2
            positions[node.id or node.label] = (cx, y, y + height)
            row_height = max(row_height, height)
            x += box_w + 24.0
        y += row_height + row_gap

    for edge in edges:
        parent = positions.get(edge.source)
        child = positions.get(edge.target)
        if not parent or not child:
            continue
        elements.append(
            f'<path d="M{parent[0]:.1f},{parent[2]:.1f} '
            f'C{parent[0]:.1f},{(parent[2] + child[1]) / 2:.1f} '
            f'{child[0]:.1f},{(parent[2] + child[1]) / 2:.1f} {child[0]:.1f},{child[1] - 2:.1f}" '
            f'fill="none" stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )

    canvas_w = max(max_row_width + 2 * MARGIN, CANVAS_WIDTH)
    return elements, y - row_gap - top, canvas_w


def _ray_box_exit(cx: float, cy: float, ux: float, uy: float, half_w: float, half_h: float) -> tuple[float, float]:
    """Where a ray from (cx, cy) in direction (ux, uy) exits an axis-aligned
    box of half-size (half_w, half_h) centred there - the true rectangle
    boundary, not an approximation. Simple per-axis scaling (`ux*half_w`,
    `uy*half_h`) looks similar but actually traces a point on the ellipse
    inscribed in the box, which is always strictly inside it except on the
    two axes - exactly wrong for pulling a line back to touch the box."""
    candidates = []
    if abs(ux) > 1e-9:
        candidates.append(half_w / abs(ux))
    if abs(uy) > 1e-9:
        candidates.append(half_h / abs(uy))
    t = min(candidates) if candidates else 0.0
    return (cx + ux * t, cy + uy * t)


def _shorten_to_box_edge(
    p1: tuple[float, float], p2: tuple[float, float], *, w1: float, h1: float, w2: float, h2: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pull a connecting line back from box centres to box edges, along the
    line between them, so arrows (and any label placed on the line) touch
    the boxes instead of crossing into them."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist = max(math.hypot(dx, dy), 1.0)
    ux, uy = dx / dist, dy / dist
    start = _ray_box_exit(p1[0], p1[1], ux, uy, w1 / 2, h1 / 2)
    end = _ray_box_exit(p2[0], p2[1], -ux, -uy, w2 / 2, h2 / 2)
    return start, end


CONCEPT_MAP_LABEL_MAX_WIDTH = 150.0  # narrower than the default pill - leaves room beside neighbouring spokes
# The reserved gap between focal/satellite box edges must comfortably fit
# the label pill *and* its own left/right margin - a gap merely equal to
# the label's own max width still lets it touch (or, for a near-horizontal
# or near-vertical spoke where the pill's screen-aligned width lines up
# with the radial direction, overlap) the boxes on either side.
CONCEPT_MAP_LABEL_GAP = CONCEPT_MAP_LABEL_MAX_WIDTH + 40.0


def _layout_concept_map(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A central subject with labelled components/relationships arranged
    around it - what a physical/structural phenomenon (a mechanism, an
    organ, a reaction) needs and a flow chart or a plain list cannot show:
    named things connected by named relationships, with no implied order.
    """
    if not nodes:
        return [], CANVAS_WIDTH, 0.0

    focal = next((n for n in nodes if n.level == 0), nodes[0])
    satellites = [n for n in nodes if n is not focal] or [focal]
    is_self_referential = satellites == [focal]
    if is_self_referential:
        satellites = []

    count = max(len(satellites), 1)
    focal_w, focal_h_box = 230.0, 80.0
    sat_w, sat_h_box = 200.0, 84.0

    # Measure each box's real (content-dependent) height up front, so both
    # the radius and the later "pull the line back to the box edge" maths
    # are based on what's actually drawn - a short label renders a
    # noticeably shorter box than the constants above, and a long one can
    # render taller; either mismatch could otherwise leave a relationship
    # label sitting inside a real box the assumed size didn't predict.
    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    focal_id = focal.id or "__focal__"
    _, focal_h = _node_block(focal, x=0.0, y=0.0, width=focal_w, theme=theme, emphasis=True)
    sat_ids: list[str] = []
    sat_heights: list[float] = []
    for index, sat in enumerate(satellites):
        _, sat_h = _node_block(sat, x=0.0, y=0.0, width=sat_w, theme=theme, badge=str(index + 1))
        sat_ids.append(sat.id or f"__sat{index}__")
        sat_heights.append(sat_h)
    max_sat_h = max(sat_heights, default=sat_h_box)

    # Radius must be big enough that a relationship label can sit on the
    # spoke *between* the two box edges without touching either one - a
    # radius derived only from node count (as before) could leave near-zero
    # gap for small satellite counts, pushing the label onto the boxes.
    box_clearance_radius = focal_w / 2 + sat_w / 2 + CONCEPT_MAP_LABEL_GAP
    # Also keep adjacent satellites (and the labels beside them) from
    # crowding each other as count grows or boxes render taller - use each
    # box's bounding-circle radius so a tall box (a long node label wrapped
    # to 2-3 lines) still gets enough clearance, not just a wide one.
    sat_bounding_radius = math.hypot(sat_w, max_sat_h) / 2
    neighbour_clearance_radius = (
        (2 * sat_bounding_radius + 60.0) / (2 * math.sin(math.pi / count)) if count >= 2 else 0.0
    )
    radius = max(190.0, count * 50.0, box_clearance_radius, neighbour_clearance_radius)
    cx = radius + max(focal_w, sat_w) / 2 + 20
    cy = top + radius + max_sat_h / 2 + 20
    canvas_w = 2 * cx

    positions: dict[str, tuple[float, float, float, float]] = {focal_id: (cx, cy, focal_w, focal_h)}
    for index, sat in enumerate(satellites):
        angle = (2 * math.pi * index / count) - math.pi / 2
        sx = cx + radius * math.cos(angle)
        sy = cy + radius * math.sin(angle)
        positions[sat_ids[index]] = (sx, sy, sat_w, sat_heights[index])

    # Relationship lines first, so node boxes sit cleanly on top of them.
    for edge in edges:
        p1 = positions.get(edge.source)
        p2 = positions.get(edge.target)
        if not p1 or not p2:
            continue
        start, end = _shorten_to_box_edge(
            (p1[0], p1[1]), (p2[0], p2[1]), w1=p1[2], h1=p1[3], w2=p2[2], h2=p2[3]
        )
        elements.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        elements.append(_edge_label(mid[0], mid[1], edge.label, theme, max_width=CONCEPT_MAP_LABEL_MAX_WIDTH))

    focal_svg, _ = _node_block(
        focal, x=cx - focal_w / 2, y=cy - focal_h / 2, width=focal_w, theme=theme, emphasis=True
    )
    elements.append(focal_svg)
    for index, sat in enumerate(satellites):
        sx, sy, w, h = positions[sat_ids[index]]
        svg, _ = _node_block(sat, x=sx - w / 2, y=sy - h / 2, width=w, theme=theme, badge=str(index + 1))
        elements.append(svg)

    height_total = cy + radius + max_sat_h / 2 + 20 - top
    return elements, canvas_w, height_total


# ---------------------------------------------------------------------------
# schematic: a small, topic-agnostic vocabulary of illustrated primitives
# (block/coil/gauge/flow/arrow/label) instead of labelled boxes - for a
# physical apparatus or mechanism (a magnet and coil, a circuit, a lab
# setup) that reads better as a simplified textbook illustration than as a
# node graph. See SchematicShape/SchematicState in app.schemas.diagram.
# ---------------------------------------------------------------------------


def _centered_text(
    cx: float, cy: float, text: str, *, color: str, theme: TemplateTheme, size: float = 13.0, weight: int = 700
) -> str:
    text = text.strip()
    if not text:
        return ""
    return (
        f'<text x="{cx:.1f}" y="{cy + size * 0.35:.1f}" font-size="{size:.1f}" font-weight="{weight}" '
        f'fill="{color}" text-anchor="middle" font-family="{escape(theme.font_family)}">{escape(text)}</text>'
    )


def _shape_group(shape: SchematicShape, inner_svg: str) -> str:
    tooltip = shape.label.strip()
    if shape.sublabel.strip():
        tooltip = f"{tooltip} / {shape.sublabel.strip()}" if tooltip else shape.sublabel.strip()
    title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
    return f'<g class="diagram-node" tabindex="0" role="img">{title}{inner_svg}</g>'


def _draw_block(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    x, y = cx - w / 2, cy - h / 2
    if shape.sublabel.strip():
        # A two-part object (a magnet's N/S poles, a battery's +/- terminals,
        # any two-state block) - each half gets a different theme tone so the
        # split reads clearly without hardcoding colours.
        half_w = w / 2
        parts = [
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{half_w:.1f}" height="{h:.1f}" '
            f'fill="{theme.accent_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            f'<rect class="diagram-box" x="{x + half_w:.1f}" y="{y:.1f}" width="{half_w:.1f}" height="{h:.1f}" '
            f'fill="{theme.text_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            _centered_text(x + half_w / 2, cy, shape.label, color="#ffffff", theme=theme, size=14),
            _centered_text(x + half_w + half_w / 2, cy, shape.sublabel, color="#ffffff", theme=theme, size=14),
        ]
    else:
        parts = [
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" '
            f'fill="{theme.surface_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            _centered_text(cx, cy, shape.label, color=theme.text_color, theme=theme, size=13),
        ]
    return _shape_group(shape, "".join(parts))


def _draw_circle(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    """A round object - a cell, an atom, a planet, a seed - generic across
    biology/chemistry/astronomy the same way `block` is generic for
    rectangular objects. With a `sublabel`, draws a smaller labelled circle
    inside it (a nucleus, a core, an embryo)."""
    radius = min(w, h) / 2
    if shape.sublabel.strip():
        inner_r = radius * 0.42
        parts = [
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="{theme.surface_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{inner_r:.1f}" fill="{theme.accent_soft}" '
            f'stroke="{theme.accent_color}" stroke-width="1.5"/>',
            _centered_text(cx, cy, shape.sublabel, color=theme.accent_color, theme=theme, size=11, weight=700),
            _centered_text(cx, cy + radius + 16, shape.label, color=theme.text_color, theme=theme, size=13),
        ]
    else:
        parts = [
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="{theme.surface_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            _centered_text(cx, cy, shape.label, color=theme.text_color, theme=theme, size=13),
        ]
    return _shape_group(shape, "".join(parts))


def _draw_coil(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    loops = max(int(w / 24), 4)
    step = w / loops
    left = cx - w / 2
    parts = [
        f'<ellipse cx="{left + step * (i + 0.5):.1f}" cy="{cy:.1f}" rx="{step * 0.55:.1f}" ry="{h / 2:.1f}" '
        f'fill="none" stroke="{theme.text_color}" stroke-width="2"/>'
        for i in range(loops)
    ]
    parts.append(_centered_text(cx, cy + h / 2 + 18, shape.label, color=theme.text_color, theme=theme, size=12.5))
    return _shape_group(shape, "".join(parts))


def _draw_gauge(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    radius = min(w, h) / 2
    angle = math.radians(shape.rotation - 90)  # rotation=0 -> needle points up (resting)
    needle_len = radius * 0.72
    nx, ny = cx + needle_len * math.cos(angle), cy + needle_len * math.sin(angle)
    parts = [
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{theme.surface_color}" '
        f'stroke="{theme.border_color}" stroke-width="1.5"/>',
        f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{theme.accent_color}" '
        f'stroke-width="2.5" stroke-linecap="round"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{theme.accent_color}"/>',
        _centered_text(cx, cy + radius + 18, shape.sublabel or shape.label, color=theme.text_color, theme=theme, size=12.5),
    ]
    return _shape_group(shape, "".join(parts))


def _draw_flow(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    """A fan of curved lines converging on (cx, cy) from direction
    `rotation` (0=east, 90=south, 180=west, 270=north) - field lines,
    current, airflow or liquid flow, generically."""
    count = max(3, min(7, round(3 + shape.intensity * 5)))
    length = max(w, h)
    spread = 64.0
    parts = []
    for i in range(count):
        frac = (i / (count - 1) - 0.5) if count > 1 else 0.0
        angle = math.radians(shape.rotation + frac * spread)
        sx, sy = cx + length * math.cos(angle), cy + length * math.sin(angle)
        mx, my = (cx + sx) / 2, (cy + sy) / 2
        parts.append(
            f'<path d="M{sx:.1f},{sy:.1f} Q{mx:.1f},{my:.1f} {cx:.1f},{cy:.1f}" fill="none" '
            f'stroke="{theme.muted_color}" stroke-width="1.5" marker-end="url(#diagram-arrow)"/>'
        )
    # The label sits to the *side* of the fan (perpendicular to the flow
    # direction), not further along it - `rotation` points back toward
    # whatever the lines originate near (e.g. a magnet), so continuing in
    # that direction routinely lands the label on top of the *other* end of
    # the flow (e.g. the coil it flows into). A bounded perpendicular offset
    # keeps it clear of both ends regardless of orientation.
    perp_angle = math.radians(shape.rotation + 90)
    label_offset = min(length * 0.5, 90.0) + 14.0
    lx, ly = cx + label_offset * math.cos(perp_angle), cy + label_offset * math.sin(perp_angle)
    parts.append(_centered_text(lx, ly, shape.label, color=theme.muted_color, theme=theme, size=11.5, weight=500))
    return _shape_group(shape, "".join(parts))


def _draw_arrow(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    length = max(w, h)
    angle = math.radians(shape.rotation)
    dx, dy = (length / 2) * math.cos(angle), (length / 2) * math.sin(angle)
    parts = [
        f'<line x1="{cx - dx:.1f}" y1="{cy - dy:.1f}" x2="{cx + dx:.1f}" y2="{cy + dy:.1f}" '
        f'stroke="{theme.accent_color}" stroke-width="3" marker-end="url(#diagram-arrow)"/>',
        _centered_text(cx, cy - 14, shape.label, color=theme.accent_color, theme=theme, size=12),
    ]
    return _shape_group(shape, "".join(parts))


def _draw_label(shape: SchematicShape, cx: float, cy: float, w: float, theme: TemplateTheme) -> str:
    lines = _wrap(shape.label, font_size=DETAIL_SIZE, width=max(w, 80), max_lines=3) or [""]
    svg, _ = _text(
        cx, cy, lines, font_size=DETAIL_SIZE, weight=500, color=theme.text_color,
        font_family=theme.font_family,
    )
    return _shape_group(shape, svg)


def _shape_rotation(shape: SchematicShape, cx: float, cy: float, positions: dict[str, tuple[float, float]]) -> float:
    if shape.target_id and shape.target_id in positions:
        tx, ty = positions[shape.target_id]
        return math.degrees(math.atan2(ty - cy, tx - cx))
    return shape.rotation


def _draw_shape(
    shape: SchematicShape,
    *,
    panel_x0: float,
    panel_y0: float,
    panel_w: float,
    panel_h: float,
    positions: dict[str, tuple[float, float]],
    theme: TemplateTheme,
) -> str:
    cx = panel_x0 + shape.x * panel_w
    cy = panel_y0 + shape.y * panel_h
    w = max(shape.width, 0.04) * panel_w
    h = max(shape.height, 0.04) * panel_h
    kind = shape.type if shape.type in SHAPE_TYPES else "block"
    if kind == "block":
        return _draw_block(shape, cx, cy, w, h, theme)
    if kind == "circle":
        return _draw_circle(shape, cx, cy, w, h, theme)
    if kind == "coil":
        return _draw_coil(shape, cx, cy, w, h, theme)
    if kind == "gauge":
        return _draw_gauge(shape, cx, cy, w, h, theme)
    resolved = shape.model_copy(update={"rotation": _shape_rotation(shape, cx, cy, positions)})
    if kind == "flow":
        return _draw_flow(resolved, cx, cy, w, h, theme)
    if kind == "arrow":
        return _draw_arrow(resolved, cx, cy, w, h, theme)
    return _draw_label(shape, cx, cy, w, theme)


def _layout_schematic(spec: DiagramSpec, *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    states = spec.states if spec.states else [SchematicState(caption="", shapes=spec.shapes)]
    panel_w = CANVAS_WIDTH - 2 * MARGIN
    elements = [_arrow_marker("diagram-arrow", theme.muted_color)]

    y = top
    for state in states:
        panel_y0 = y
        elements.append(
            f'<rect x="{MARGIN:.1f}" y="{panel_y0:.1f}" width="{panel_w:.1f}" height="{PANEL_HEIGHT:.1f}" '
            f'rx="10" fill="{theme.page_background}" stroke="{theme.border_color}" stroke-width="1.5"/>'
        )
        positions = {
            shape.id: (MARGIN + shape.x * panel_w, panel_y0 + shape.y * PANEL_HEIGHT)
            for shape in state.shapes
            if shape.id
        }
        for shape in state.shapes:
            elements.append(
                _draw_shape(
                    shape, panel_x0=MARGIN, panel_y0=panel_y0, panel_w=panel_w, panel_h=PANEL_HEIGHT,
                    positions=positions, theme=theme,
                )
            )
        if state.caption.strip():
            caption_svg, _ = _text(
                CANVAS_WIDTH / 2, panel_y0 + PANEL_HEIGHT - 16, [state.caption.strip()],
                font_size=PANEL_CAPTION_SIZE, weight=600, color=theme.muted_color,
                font_family=theme.font_family,
            )
            elements.append(caption_svg)
        y = panel_y0 + PANEL_HEIGHT + PANEL_GAP

    return elements, y - PANEL_GAP - top


def _layout_comparison(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """Side-by-side columns, one per thing being compared."""
    count = max(len(nodes), 1)
    gutter = 20.0
    col_w = (CANVAS_WIDTH - 2 * MARGIN - (count - 1) * gutter) / count
    elements: list[str] = []
    tallest = 0.0
    x = MARGIN
    for node in nodes:
        svg, height = _node_block(node, x=x, y=top, width=col_w, theme=theme)
        elements.append(svg)
        tallest = max(tallest, height)
        x += col_w + gutter
    return elements, tallest


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def render_diagram_svg(spec: DiagramSpec, theme: TemplateTheme) -> tuple[bytes, int, int]:
    """Render a validated `DiagramSpec` to a standalone SVG document.

    Returns (utf-8 svg bytes, intrinsic width, intrinsic height) so the caller
    can store them exactly like a generated image's dimensions.
    """
    kind = spec.normalised_kind()
    top = MARGIN
    title_svg = ""
    if spec.title.strip():
        title_lines = _wrap(spec.title, font_size=18, width=CANVAS_WIDTH - 2 * MARGIN, max_lines=1)
        title_svg, title_h = _text(
            CANVAS_WIDTH / 2,
            top + 18,
            title_lines,
            font_size=18,
            weight=700,
            color=theme.text_color,
            font_family=theme.font_family,
        )
        top += title_h + 20

    canvas_w = CANVAS_WIDTH
    if kind in ("flow_chart", "process"):
        elements, content_h = _layout_vertical(
            spec.nodes, spec.edges, theme=theme, connected=True, top=top
        )
    elif kind == "cycle":
        elements, canvas_w, content_h = _layout_cycle(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "hierarchy":
        elements, content_h, canvas_w = _layout_hierarchy(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "comparison":
        elements, content_h = _layout_comparison(spec.nodes, theme=theme, top=top)
    elif kind in ("concept_map", "conceptual"):
        elements, canvas_w, content_h = _layout_concept_map(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "schematic":
        elements, content_h = _layout_schematic(spec, theme=theme, top=top)
    else:  # smart_art: numbered list, no connecting arrows
        elements, content_h = _layout_vertical(
            spec.nodes, spec.edges, theme=theme, connected=False, top=top
        )

    canvas_h = max(top + content_h + MARGIN, top + MARGIN)

    # Hover/focus feedback only takes effect where the caller inlines this SVG
    # into the page DOM (not when it's referenced via `<img src>`) - the
    # frontend does that for diagrams specifically, see ImageBlock.tsx.
    style = (
        "<style>.diagram-node{cursor:pointer}"
        ".diagram-node .diagram-box{transition:stroke .15s ease,fill .15s ease}"
        ".diagram-node:hover .diagram-box,.diagram-node:focus .diagram-box{"
        f"stroke:{theme.accent_color};stroke-width:2.5;fill:{theme.accent_soft}}}"
        ".diagram-node:focus{outline:none}</style>"
    )
    doc_title = f"<title>{escape(spec.title.strip() or 'Diagram')}</title>"

    body = "".join(elements)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}" role="img">'
        f"{doc_title}{style}"
        f'<rect x="0" y="0" width="{canvas_w:.0f}" height="{canvas_h:.0f}" fill="{theme.page_background}"/>'
        f"{title_svg}{body}</svg>"
    )
    return svg.encode("utf-8"), int(round(canvas_w)), int(round(canvas_h))
