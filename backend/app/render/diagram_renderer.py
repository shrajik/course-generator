"""DiagramSpec -> SVG.

Pure and AI-free, the same philosophy as `app.render.html_renderer`: the model
only supplies structured content (nodes/edges/kind); every pixel here is
produced by deterministic code, on the course's own theme colours, so a
diagram always renders crisp labels and never drifts from the brand.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

from app.render.textbook_palette import resolve_color_role, stroke_width_for
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
# Bumped up from 15/12.5 - a course document renders this SVG scaled down to
# fit its block width (often noticeably narrower than this diagram's own
# intrinsic canvas), so the on-page text ends up smaller than these numbers
# look in isolation. Sized here so it still reads clearly after that shrink.
LABEL_SIZE = 17.5
DETAIL_SIZE = 14.0
LINE_HEIGHT = 1.35
BOX_PADDING = 16.0
BADGE_RADIUS = 15.0
_SANS_RATIO = 0.56
# Bold glyphs are noticeably wider than the plain-weight ratio above
# accounts for - `course.document.layout` already applies an equivalent
# factor (`_BOLD_FACTOR`) to its own text estimator for exactly this
# reason. Every node's `label` renders at font-weight 600 (see
# `_node_block`), so wrapping it with the plain ratio underestimates its
# real width - confirmed real: a 76-character bold label passed the
# plain-ratio character budget (78 chars) yet still visually overflowed
# its box in a real browser render.
_BOLD_RATIO = _SANS_RATIO * 1.12

# --- schematic panel geometry -------------------------------------------
PANEL_HEIGHT = 300.0
PANEL_GAP = 30.0
PANEL_CAPTION_SIZE = 14.5

# --- flow_chart/process semantic colouring ------------------------------
# Fixed, not theme-derived - a flowchart's start/end/decision colouring is a
# universal convention (green = start, red = end, purple = decision), the
# same reason app.render.concept_experience_renderer's palette is also fixed
# rather than following the course's brand theme. Role is auto-detected from
# the node's own in/out-degree (see _flow_role), never a field the model has
# to set - a linear chain's first node has no incoming edge (start), its
# last has no outgoing edge (end), and any node with 2+ outgoing edges is
# inherently a decision, by construction.
_FLOW_ROLE_COLORS = {
    "start": ("#bbf7d0", "#16a34a"),  # green
    "end": ("#fecaca", "#dc2626"),  # red
    "decision": ("#e9d5ff", "#7e22ce"),  # purple
    "process": ("#bfdbfe", "#1d4ed8"),  # blue
}

# hierarchy: root = purple, its direct children = teal, everything deeper =
# a plain neutral fill - same fixed, universal-convention colouring as
# _FLOW_ROLE_COLORS above, indexed by level (clamped to the last entry for
# any level deeper than this list covers).
_HIERARCHY_LEVEL_COLORS = [
    ("#ddd6fe", "#6d28d9"),  # purple - root
    ("#99f6e4", "#0f766e"),  # teal - direct children
    ("#f1f5f9", "#64748b"),  # neutral - everything deeper
]


def _wrap(text: str, *, font_size: float, width: float, max_lines: int = 3, bold: bool = False) -> list[str]:
    """Greedy word wrap; mirrors the ratio `course.document.layout` uses so
    diagram text density looks consistent with the rest of the page. Pass
    `bold=True` for any text that renders at font-weight >= 600 (every
    node's `label` does) - see `_BOLD_RATIO`'s own docstring for why this
    matters; wrapping bold text with the plain-weight ratio underestimates
    its real width and lets it overflow its box even though it "fit" the
    (wrong) character budget.

    A single "word" longer than one whole line on its own (a long
    identifier, URL, or hyphen-free compound term with no spaces to break
    on) is force-broken mid-word across as many lines as it needs, the
    same safeguard `course.document.layout.wrapped_line_count` already has
    for ordinary page text - this module's own wrap never had it, and a
    real 76-character unbroken bold label confirmed the gap: it rendered as
    one line that overflowed straight past its node's right edge instead of
    wrapping (caught by `app.services.diagram_render_qa`'s text-overflow
    check)."""
    text = " ".join((text or "").split())
    if not text:
        return []
    ratio = _BOLD_RATIO if bold else _SANS_RATIO
    chars_per_line = max(int(width / (font_size * ratio)), 6)
    words = text.split(" ")
    lines: list[str] = []
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if current:
            lines.append(" ".join(current))
            current, length = [], 0

    for word in words:
        while len(word) > chars_per_line:
            flush()
            lines.append(word[:chars_per_line])
            word = word[chars_per_line:]
        add = len(word) + (1 if current else 0)
        if current and length + add > chars_per_line:
            flush()
            current, length = [word], len(word)
        else:
            current.append(word)
            length += add
    flush()
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
    box_shape: str = "rounded",
    colors: tuple[str, str] | None = None,
) -> tuple[str, float]:
    """One box: label (bold) + wrapped detail. Returns (svg, height).
    `emphasis` marks the central/focal subject of a concept map - a heavier,
    accent-toned box so a reader's eye lands on it first. `colors`, when
    given, is (fill, stroke) and overrides `emphasis`'s own colour choice -
    for flow_chart's semantic start/end/process colouring (see
    _FLOW_ROLE_COLORS), which is about the node's ROLE, not focal emphasis.
    `box_shape` only changes the background's own border - every text/
    wrapping/tooltip rule below is completely shape-independent, so a new
    shape can never reintroduce a text-fit bug already solved for the
    default "rounded" box:
    "rounded" (default) - a rounded rectangle, same as always.
    "sharp" - a plain rectangle (data_flow_diagram's external entity,
    er_diagram's entity).
    "store" - an open-ended rectangle: top/bottom border only, no left/right
    sides - the standard notation for a data_flow_diagram data store.
    "pill" - a fully-rounded stadium shape (flow_chart's start/end nodes)."""
    inner_w = width - 2 * BOX_PADDING - (28.0 if badge else 0.0)
    text_x = x + BOX_PADDING + (28.0 if badge else 0.0)
    label_lines = _wrap(node.label, font_size=LABEL_SIZE, width=inner_w, max_lines=2, bold=True) or ["—"]
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

    if colors is not None:
        fill, stroke = colors
        stroke_width = 2.0
    else:
        fill = theme.accent_soft if emphasis else theme.surface_color
        stroke = theme.accent_color if emphasis else theme.border_color
        stroke_width = 2.5 if emphasis else 1.5
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    if box_shape == "pill":
        parts.append(
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
            f'rx="{height / 2:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )
    elif box_shape == "store":
        # Open-ended: a borderless fill (for text contrast) plus separate
        # top/bottom lines only - never left/right, which is what makes this
        # notation read as "store" rather than "box" at a glance.
        parts.append(
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
            f'fill="{fill}" stroke="none"/>'
        )
        parts.append(
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + width:.1f}" y2="{y:.1f}" '
            f'stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )
        parts.append(
            f'<line x1="{x:.1f}" y1="{y + height:.1f}" x2="{x + width:.1f}" y2="{y + height:.1f}" '
            f'stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )
    else:
        rx = 0 if box_shape == "sharp" else 10
        parts.append(
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
            f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )
    if badge is not None:
        cy = y + BOX_PADDING + BADGE_RADIUS - 2
        cx = x + BOX_PADDING + BADGE_RADIUS - 6
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{BADGE_RADIUS:.1f}" fill="{theme.accent_color}"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 4.5:.1f}" font-size="13" font-weight="700" '
            f'fill="#ffffff" text-anchor="middle" font-family="{escape(theme.font_family)}">{escape(badge)}</text>'
        )
    parts.append(label_svg)
    if detail_svg:
        parts.append(detail_svg)
    group = f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'
    return group, height


EDGE_LABEL_FONT_SIZE = 13.0
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


_PILL_W = 200.0
_PILL_H = 56.0
_DECISION_W = 210.0
_DECISION_H = 116.0


def _draw_pill(node: DiagramNode, *, cx: float, cy: float, theme: TemplateTheme, colors: tuple[str, str]) -> str:
    """A flow_chart start/end node - fully centred text via _fit_boxed_text
    (same shrink-to-fit safety net _draw_diamond/_draw_attribute_oval already
    rely on), never the free-flowing label+detail block _node_block draws -
    a pill is small and its rounded ends leave less safe width near the
    caps, so centring keeps it simple and safe rather than reusing machinery
    built for a much bigger box. `detail` (rare on a genuine Start/End node)
    still reaches the hover tooltip, same convention as every other shape."""
    fill, stroke = colors
    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<rect class="diagram-box" x="{cx - _PILL_W / 2:.1f}" y="{cy - _PILL_H / 2:.1f}" '
        f'width="{_PILL_W:.1f}" height="{_PILL_H:.1f}" rx="{_PILL_H / 2:.1f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=_PILL_W * 0.75, height=_PILL_H * 0.7,
            color=theme.text_color, theme=theme, base_size=15.0,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _flow_degrees(
    nodes: list[DiagramNode], edges: list[DiagramEdge]
) -> tuple[dict[str, int], dict[str, list[DiagramEdge]]]:
    by_id = {n.id for n in nodes if n.id}
    in_degree = {n.id: 0 for n in nodes if n.id}
    out_edges: dict[str, list[DiagramEdge]] = {}
    for edge in edges:
        if edge.source in by_id and edge.target in by_id:
            out_edges.setdefault(edge.source, []).append(edge)
            in_degree[edge.target] = in_degree.get(edge.target, 0) + 1
    return in_degree, out_edges


def _layout_flow_chart(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """flow_chart/process: a vertical spine with semantic start (green
    pill) / end (red pill) / process (blue box) colouring, auto-detected
    from each node's own in/out-degree - a node with no incoming edge is
    the start, one with no outgoing edge is an end, one with 2 outgoing
    edges is a decision (drawn as a purple diamond, its two branches placed
    side by side below it, labelled from each edge's own `label`, e.g.
    "Yes"/"No"). A branch that loops back to a node already drawn earlier
    on the spine gets a curved return arrow instead of a new box - the
    classic "flowchart with a loop" shape. A branch may run any number of
    plain steps deep (no depth limit) before it dead-ends, loops back, or -
    the common "both outcomes lead to the same next step" pattern -
    reconverges with the OTHER branch at a shared node, which is then drawn
    once, continuing the main spine below both columns.

    Still narrower than a fully general graph layout: only symmetric shapes
    are given real branch geometry - both branches dead-end, both loop back,
    or both reconverge at the exact same node. Anything more tangled (one
    branch reconverging while the other dead-ends, a branch running through
    a second decision, branches reconverging at two different nodes) falls
    all the way back to plain linear stacking in `nodes`' own order instead
    of guessing at a layout - the same "never worse than simple, never
    crash" contract every other fallback in this module already keeps."""
    in_degree, out_edges = _flow_degrees(nodes, edges)
    by_id = {n.id: n for n in nodes if n.id}
    out_degree = {nid: len(es) for nid, es in out_edges.items()}
    edge_labels = {(e.source, e.target): e.label for e in edges}

    _START_WORDS = {"start", "begin", "initial"}
    _END_WORDS = {"end", "stop", "finish", "done"}

    def role_of(nid: str) -> str:
        # Decision is structural (2+ outgoing edges is unambiguous - anything
        # with two paths forward IS a decision, whatever it's labelled).
        # Start/end deliberately is NOT structural, unlike an earlier version
        # of this function: a node with no incoming edge isn't necessarily a
        # procedure's "Start" - it's just as often the first of two plain
        # things being related (an eye's "Cornea" pointing to its "Lens"),
        # which still needs its own visible `detail` line, not a pill that
        # can only show a short centred label. Matching the label itself
        # (as the reference flowcharts this mirrors always explicitly do)
        # only fires the pill styling when the content actually calls for it.
        if out_degree.get(nid, 0) >= 2:
            return "decision"
        label = by_id[nid].label.strip().lower()
        if label in _START_WORDS:
            return "start"
        if label in _END_WORDS:
            return "end"
        return "process"

    box_w = CANVAS_WIDTH - 2 * MARGIN
    x_center = MARGIN + box_w / 2
    gap = 50.0
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    y = top
    visited: set[str] = set()

    def draw_arrow_down(x: float, y_from: float, y_to: float, label: str = "") -> None:
        elements.append(
            f'<line x1="{x:.1f}" y1="{y_from:.1f}" x2="{x:.1f}" y2="{y_to:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        if label.strip():
            elements.append(_edge_label(x, (y_from + y_to) / 2, label, theme))

    def draw_main_node(nid: str, y: float) -> float:
        """Draws node `nid` centred on the spine at `y`; returns its height."""
        node = by_id[nid]
        role = role_of(nid)
        visited.add(nid)
        if role == "decision":
            elements.append(
                _draw_diamond(
                    node, cx=x_center, cy=y + _DECISION_H / 2,
                    width=_DECISION_W, height=_DECISION_H, theme=theme,
                    colors=_FLOW_ROLE_COLORS["decision"],
                )
            )
            return _DECISION_H
        if role in ("start", "end"):
            elements.append(_draw_pill(node, cx=x_center, cy=y + _PILL_H / 2, theme=theme, colors=_FLOW_ROLE_COLORS[role]))
            return _PILL_H
        svg, height = _node_block(node, x=MARGIN, y=y, width=box_w, theme=theme, colors=_FLOW_ROLE_COLORS["process"])
        elements.append(svg)
        return height

    def resolve_branch(edge: DiagramEdge, decision_id: str) -> tuple[str, list[str]] | None:
        """Follows a decision branch forward through plain (out-degree <= 1)
        nodes, as far as it safely can. Returns ("end", [ids]) if it
        dead-ends (every id is a new box to draw), ("loop", [ids]) if it
        eventually points back at the decision itself, or ("continue",
        [ids]) if it reaches a node that something ELSE also points to
        (in-degree >= 2) - a reconvergence candidate. For "loop" and
        "continue", the LAST id is the target/merge node itself - already
        drawn (loop) or not yet drawn but possibly shared with the other
        branch (continue) - never a new box in that branch's own column;
        every id before it is. None if it's more tangled than this function
        supports: a second decision partway through, a dead/missing
        reference, or a branch long enough to revisit one of its own nodes
        (impossible in a real DAG - only a malformed spec could trigger it,
        and the bound below exists purely so that can never spin forever)."""
        chain: list[str] = []
        current = edge.target
        for _ in range(len(by_id) + 1):
            if current == decision_id:
                return ("loop", chain + [current])
            if current not in by_id or current in chain or current in visited:
                return None
            if role_of(current) == "decision":
                return None
            if in_degree.get(current, 0) >= 2:
                return ("continue", chain + [current])
            chain.append(current)
            if out_degree.get(current, 0) == 0:
                return ("end", chain)
            next_edges = out_edges.get(current, [])
            if len(next_edges) != 1:
                return None
            current = next_edges[0].target
        return None

    order = [n.id for n in nodes if n.id]
    idx = 0
    previous_bottom: float | None = None
    previous_id: str | None = None
    while idx < len(order):
        nid = order[idx]
        if nid in visited:
            idx += 1
            continue
        role = role_of(nid)
        if role != "decision":
            if previous_bottom is not None:
                draw_arrow_down(x_center, previous_bottom, y, edge_labels.get((previous_id, nid), ""))
            height = draw_main_node(nid, y)
            previous_bottom = y + height
            previous_id = nid
            y = previous_bottom + gap
            idx += 1
            continue

        decision_branches = out_edges.get(nid, [])[:2]
        resolved = [resolve_branch(edge, nid) for edge in decision_branches]
        kinds = [r[0] for r in resolved] if all(resolved) else []
        # Only symmetric shapes get real branch geometry: both dead-end,
        # both loop back, or both "continue" into the exact same node (the
        # reconverge pattern). Anything else - one branch reconverging while
        # the other doesn't, or reconverging at two different nodes - is
        # more tangled than this function lays out; fall back rather than
        # guess (see the docstring).
        merge_id: str | None = None
        if kinds.count("continue") == 1:
            valid_shape = False
        elif kinds == ["continue", "continue"]:
            merge_id = resolved[0][1][-1]
            valid_shape = merge_id == resolved[1][1][-1]
        else:
            valid_shape = True
        if len(decision_branches) != 2 or any(r is None for r in resolved) or not valid_shape:
            # Doesn't match a shape this function knows how to lay out - no
            # branch geometry for it, but every remaining node still gets
            # the exact same semantic colouring and plain vertical stacking
            # as the rest of this diagram (via the same draw_main_node/
            # draw_arrow_down every other node already goes through) rather
            # than reusing the old, neutral-coloured, numbered _layout_vertical -
            # a shape too tangled to branch is not a reason to look like a
            # different diagram halfway through.
            for fallback_id in order:
                if fallback_id in visited:
                    continue
                if previous_bottom is not None:
                    draw_arrow_down(
                        x_center, previous_bottom, y, edge_labels.get((previous_id, fallback_id), "")
                    )
                fallback_height = draw_main_node(fallback_id, y)
                previous_bottom = y + fallback_height
                previous_id = fallback_id
                y = previous_bottom + gap
            break

        if previous_bottom is not None:
            draw_arrow_down(x_center, previous_bottom, y, edge_labels.get((previous_id, nid), ""))
        height = draw_main_node(nid, y)
        decision_bottom = y + height
        col_w = box_w / 2 - 16.0
        col_gap = 32.0
        left_x = MARGIN
        right_x = MARGIN + col_w + col_gap
        branch_y = decision_bottom + gap
        branch_bottoms: list[float] = []
        loop_targets: list[tuple[float, float, str, str]] = []  # (from_x, from_y, target_id, label)
        merge_from: list[tuple[float, float]] = []  # (x, y) of each branch's own last box, into the merge node

        for branch_index, (edge, result) in enumerate(zip(decision_branches, resolved)):
            col_x = left_x if branch_index == 0 else right_x
            kind, chain = result
            col_cx = col_x + col_w / 2
            draw_arrow_down(col_cx, decision_bottom, branch_y, edge.label)
            # A branch drawn to the side never shares the shared spine's
            # full-width boxes - narrower, so two columns can sit side by
            # side without crowding.
            if kind == "end":
                by_hop_y = branch_y
                for hop_id in chain:
                    hop_node = by_id[hop_id]
                    hop_role = role_of(hop_id)
                    visited.add(hop_id)
                    if hop_role in ("start", "end"):
                        elements.append(
                            _draw_pill(
                                hop_node, cx=col_cx, cy=by_hop_y + _PILL_H / 2,
                                theme=theme, colors=_FLOW_ROLE_COLORS[hop_role],
                            )
                        )
                        hop_h = _PILL_H
                    else:
                        svg, hop_h = _node_block(
                            hop_node, x=col_x, y=by_hop_y, width=col_w, theme=theme,
                            colors=_FLOW_ROLE_COLORS["process"],
                        )
                        elements.append(svg)
                    if hop_id != chain[-1]:
                        draw_arrow_down(col_cx, by_hop_y + hop_h, by_hop_y + hop_h + gap)
                    by_hop_y += hop_h + gap
                branch_bottoms.append(by_hop_y - gap)
            else:  # "loop"/"continue": every id but the last is a real new box; the last is the target/merge
                by_hop_y = branch_y
                for hop_id in chain[:-1]:
                    hop_node = by_id[hop_id]
                    visited.add(hop_id)
                    svg, hop_h = _node_block(
                        hop_node, x=col_x, y=by_hop_y, width=col_w, theme=theme,
                        colors=_FLOW_ROLE_COLORS["process"],
                    )
                    elements.append(svg)
                    draw_arrow_down(col_cx, by_hop_y + hop_h, by_hop_y + hop_h + gap)
                    by_hop_y += hop_h + gap
                if kind == "loop":
                    loop_targets.append((col_cx, by_hop_y - gap, chain[-1], ""))
                else:
                    merge_from.append((col_cx, by_hop_y - gap))
                branch_bottoms.append(by_hop_y - gap)

        # Loop-back arrows: routed out to the side and back up to the
        # decision's own edge, well clear of the branch columns in between.
        for from_x, from_y, target_id, _label in loop_targets:
            side_x = MARGIN - 24.0 if from_x < x_center else CANVAS_WIDTH - MARGIN + 24.0
            target_y = decision_bottom - height / 2  # roughly the decision's own vertical centre
            elements.append(
                f'<path d="M{from_x:.1f},{from_y:.1f} L{side_x:.1f},{from_y:.1f} '
                f'L{side_x:.1f},{target_y:.1f} L{x_center + _DECISION_W / 2:.1f},{target_y:.1f}" '
                f'fill="none" stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )

        y = max(branch_bottoms) if branch_bottoms else branch_y

        if merge_id is not None:
            # Both branches reconverge here - draw the shared node once,
            # centred back on the main spine, with both columns' arrows
            # bending in to meet it (an angled line, not the plain vertical
            # draw_arrow_down every other connector in this function uses -
            # this is the one place two columns genuinely merge back into
            # one point, which a straight-down line can't reach from either
            # side).
            merge_y = y + gap
            for from_x, from_y in merge_from:
                elements.append(
                    f'<line x1="{from_x:.1f}" y1="{from_y:.1f}" x2="{x_center:.1f}" y2="{merge_y:.1f}" '
                    f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
                )
            merge_height = draw_main_node(merge_id, merge_y)
            previous_bottom = merge_y + merge_height
            previous_id = merge_id
            y = previous_bottom + gap
            idx += 1
            continue

        # Nothing continues the main spine after a resolved end/loop branch
        # (see the docstring) - if a well-formed spec somehow still has more
        # nodes queued up, the next one starts fresh rather than drawing a
        # stale arrow back to the decision's own position.
        previous_bottom = None
        previous_id = None
        idx += 1

    total_height = y - top
    return elements, total_height


_DFD_COLORS = {
    "": ("#fecaca", "#dc2626"),  # process - red circle (the reference notation)
    "entity": ("#bfdbfe", "#1d4ed8"),  # external entity - blue box
    "store": ("#fef08a", "#ca8a04"),  # data store - yellow open box
}
_DFD_PROCESS_R = 68.0


def _draw_process_circle(
    node: DiagramNode, *, cx: float, cy: float, theme: TemplateTheme, colors: tuple[str, str]
) -> str:
    """A data_flow_diagram process step - the standard DFD notation draws
    this as a circle, not a box. Centred text via _fit_boxed_text, same
    shrink-to-fit safety net as every other non-rectangular shape in this
    module; the box passed to it (side length == the radius) sits well
    inside the circle's own true inscribed square (side == radius*sqrt(2))."""
    fill, stroke = colors
    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<ellipse class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" rx="{_DFD_PROCESS_R:.1f}" '
        f'ry="{_DFD_PROCESS_R:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=_DFD_PROCESS_R, height=_DFD_PROCESS_R,
            color=theme.text_color, theme=theme, base_size=13.0,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _layout_dfd(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A data_flow_diagram: the same stacked/connected geometry
    _layout_vertical already has hardened (arrows, wrapped edge labels, no
    overlap) - the only real difference is each node's own shape and colour,
    per `node.shape_role` (see _DFD_COLORS): process (a red circle, the
    default and the standard DFD notation for one), external entity (a blue
    sharp-cornered box) or data store (a yellow open-ended box). Not
    numbered like a process/flow_chart's steps - a DFD's nodes are typed
    elements, not ordered steps. Deliberately linear (each node connects
    only to the next) - a genuine feedback/back-edge would need arrows
    weaving back through the stack, which risks crossing or overlapping the
    boxes between them; a real course topic that needs one can still name it
    in an edge label without drawing the loop, or use two adjacent nodes and
    describe the feedback in the surrounding text instead."""
    edge_labels = {(e.source, e.target): e.label for e in edges}
    box_w = CANVAS_WIDTH - 2 * MARGIN
    gap = 60.0
    x = MARGIN
    y = top
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]

    previous_bottom: float | None = None
    for index, node in enumerate(nodes):
        if previous_bottom is not None:
            mid_x = x + box_w / 2
            elements.append(
                f'<line x1="{mid_x:.1f}" y1="{previous_bottom:.1f}" x2="{mid_x:.1f}" '
                f'y2="{y - 4:.1f}" stroke="{theme.accent_color}" stroke-width="2" '
                f'marker-end="url(#diagram-arrow)"/>'
            )
            key = (nodes[index - 1].id, node.id)
            elements.append(_edge_label(mid_x, (previous_bottom + y - 4) / 2, edge_labels.get(key, ""), theme))

        role = node.shape_role.strip().lower()
        colors = _DFD_COLORS.get(role, _DFD_COLORS[""])
        if role in ("entity", "store"):
            svg, height = _node_block(
                node, x=x, y=y, width=box_w, theme=theme,
                box_shape="sharp" if role == "entity" else "store", colors=colors,
            )
            elements.append(svg)
        else:
            cy = y + _DFD_PROCESS_R
            elements.append(_draw_process_circle(node, cx=x + box_w / 2, cy=cy, theme=theme, colors=colors))
            height = _DFD_PROCESS_R * 2
        previous_bottom = y + height
        y = previous_bottom + gap

    total_height = y - gap - top if nodes else 0.0
    return elements, total_height


def _swimlane_rows(nodes: list[DiagramNode], edges: list[DiagramEdge]) -> dict[str, int]:
    """Each node's row = 1 + the furthest (longest-path) predecessor's own
    row, 0 for a node with none - the standard DAG "layering" rule, so a
    node is always drawn after everything that feeds into it, however many
    hops away. Guarded against a cycle (shouldn't occur in a real swimlane,
    but this must never hang even on a malformed one) by treating a node
    already being resolved as row 0 rather than recursing into it again.
    Once every row is set, nodes sharing both a lane AND a row (parallel
    branches within one lane at the same step) are bumped down one row at a
    time until each lane has at most one node per row - the only thing that
    would otherwise still be able to overlap."""
    by_id = {n.id: n for n in nodes if n.id}
    in_edges: dict[str, list[str]] = {}
    for edge in edges:
        if edge.source in by_id and edge.target in by_id:
            in_edges.setdefault(edge.target, []).append(edge.source)

    rows: dict[str, int] = {}

    def resolve(nid: str, visiting: set[str]) -> int:
        if nid in rows:
            return rows[nid]
        if nid in visiting:
            rows[nid] = 0
            return 0
        visiting.add(nid)
        preds = [p for p in in_edges.get(nid, []) if p in by_id]
        row = 0 if not preds else 1 + max(resolve(p, visiting) for p in preds)
        visiting.discard(nid)
        rows[nid] = row
        return row

    for node in nodes:
        if node.id:
            resolve(node.id, set())

    claimed: set[tuple[str, int]] = set()
    for node in nodes:
        if not node.id:
            continue
        lane = node.lane.strip() or "General"
        row = rows[node.id]
        while (lane, row) in claimed:
            row += 1
        claimed.add((lane, row))
        rows[node.id] = row

    return rows


_LANE_HEADER_H = 52.0
_LANE_COLORS = [
    ("#dbeafe", "#1d4ed8"),  # blue
    ("#e2e8f0", "#475569"),  # slate/gray
    ("#fed7aa", "#c2410c"),  # orange
    ("#dcfce7", "#15803d"),  # green
    ("#fbcfe8", "#be185d"),  # pink
]


def _layout_swimlane(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A swimlane: one coloured-header column per distinct `node.lane`
    (lanes appear left to right in the order first seen). Nodes are grouped
    into ROWS - step 0 is whichever node(s) have no incoming edge, step N is
    1 + the furthest predecessor's own step (see `_swimlane_rows`) - so a
    node in one lane lines up with whatever's happening in every other lane
    at that same point in the process, with an empty gap in a lane that
    isn't doing anything that step - the standard swimlane reading. Edges
    are drawn as direct box-to-box connectors via _shorten_to_box_edge (the
    same mechanism er_diagram/concept_map already use), whether they cross
    lanes or stay within one - a swimlane's whole point is showing work
    crossing role/actor boundaries, so a cross-lane edge is the common case,
    not a special one."""
    lanes: list[str] = []
    for node in nodes:
        lane = node.lane.strip() or "General"
        if lane not in lanes:
            lanes.append(lane)
    if not lanes:
        return [], 0.0

    total_w = CANVAS_WIDTH - 2 * MARGIN
    lane_gap = 20.0
    lane_w = max((total_w - lane_gap * (len(lanes) - 1)) / len(lanes), 140.0)
    body_top = top + _LANE_HEADER_H + 24.0

    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    lane_x: dict[str, float] = {}
    x = MARGIN
    for index, lane in enumerate(lanes):
        fill, stroke = _LANE_COLORS[index % len(_LANE_COLORS)]
        lane_x[lane] = x
        elements.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{lane_w:.1f}" height="{_LANE_HEADER_H:.1f}" '
            f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        elements.append(
            _fit_boxed_text(
                x + lane_w / 2, top + _LANE_HEADER_H / 2, lane, width=lane_w * 0.85,
                height=_LANE_HEADER_H * 0.65, color=stroke, theme=theme, base_size=14.5,
            )
        )
        x += lane_w + lane_gap

    positions: dict[str, tuple[float, float, float, float]] = {}
    node_w = lane_w - 20.0
    rows = _swimlane_rows(nodes, edges)
    rows_grouped: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        if node.id:
            rows_grouped.setdefault(rows[node.id], []).append(node)

    y = body_top
    for row_index in sorted(rows_grouped):
        row_height = 0.0
        for node in rows_grouped[row_index]:
            lane = node.lane.strip() or "General"
            node_x = lane_x[lane] + 10.0
            svg, height = _node_block(node, x=node_x, y=y, width=node_w, theme=theme)
            elements.append(svg)
            positions[node.id] = (node_x + node_w / 2, y + height / 2, node_w, height)
            row_height = max(row_height, height)
        y += row_height + 24.0

    for edge in edges:
        a = positions.get(edge.source)
        b = positions.get(edge.target)
        if not a or not b:
            continue
        start, end = _shorten_to_box_edge((a[0], a[1]), (b[0], b[1]), w1=a[2], h1=a[3], w2=b[2], h2=b[3])
        elements.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        if edge.label.strip():
            mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            elements.append(_edge_label(mid[0], mid[1] - 10, edge.label, theme, max_width=110.0))

    max_bottom = y - 24.0 if rows_grouped else body_top
    return elements, max_bottom - top


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
    """Nodes grouped into rows by `level`, connected to their parent edge.
    Each row's colour comes from _HIERARCHY_LEVEL_COLORS (root/children/
    deeper), the same fixed semantic-colour convention flow_chart uses -
    a hierarchy's depth is exactly what the colour is there to show at a
    glance, before a reader even reads a single label."""
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
        row_colors = _HIERARCHY_LEVEL_COLORS[min(level, len(_HIERARCHY_LEVEL_COLORS) - 1)]
        for node in row_nodes:
            svg, height = _node_block(node, x=x, y=y, width=box_w, theme=theme, colors=row_colors)
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


def _bisecting_angle(a1: float, a2: float) -> float:
    """The angle 'between' a1 and a2 along their *shorter* arc - e.g. bisect
    (10°, 350°) as 0°, not 180°. Averaging the two angles directly breaks
    exactly at the wraparound point; this normalises the difference into
    (-π, π] first so the result always points the intuitive way, including
    for two satellites on nearly opposite sides of the ring, where averaging
    their *positions* instead would land almost exactly on the centre and
    give no usable outward direction at all."""
    diff = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
    return a1 + diff / 2


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
    sat_angles: dict[str, float] = {}
    for index, sat in enumerate(satellites):
        angle = (2 * math.pi * index / count) - math.pi / 2
        sx = cx + radius * math.cos(angle)
        sy = cy + radius * math.sin(angle)
        positions[sat_ids[index]] = (sx, sy, sat_w, sat_heights[index])
        sat_angles[sat_ids[index]] = angle

    # Relationship lines first, so node boxes sit cleanly on top of them.
    for edge in edges:
        p1 = positions.get(edge.source)
        p2 = positions.get(edge.target)
        if not p1 or not p2:
            continue
        start, end = _shorten_to_box_edge(
            (p1[0], p1[1]), (p2[0], p2[1]), w1=p1[2], h1=p1[3], w2=p2[2], h2=p2[3]
        )
        if focal_id in (edge.source, edge.target):
            # A spoke to/from the focal node: straight, as before - these
            # never cross anything else since every satellite sits on the
            # same ring around the one shared centre.
            elements.append(
                f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
                f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
            mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        else:
            # A relationship between two satellites (not through the focal
            # node) is real and common - e.g. "std::move produces an
            # xvalue" - but a straight chord between two ring positions cuts
            # across the focal box and every satellite in between, which is
            # exactly what read as "messy" crossing lines. Bow the line
            # outward instead, along the direction that bisects the two
            # satellites' own ring angles (not their averaged *position*,
            # which collapses toward the centre for near-opposite satellites
            # and would leave the curve just as central as a straight line).
            theta = _bisecting_angle(sat_angles[edge.source], sat_angles[edge.target])
            bulge = radius * 0.55 + 30.0
            ctrl = (cx + bulge * math.cos(theta), cy + bulge * math.sin(theta))
            elements.append(
                f'<path d="M{start[0]:.1f},{start[1]:.1f} Q{ctrl[0]:.1f},{ctrl[1]:.1f} '
                f'{end[0]:.1f},{end[1]:.1f}" fill="none" stroke="{theme.accent_color}" '
                f'stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
            mid = (
                0.25 * start[0] + 0.5 * ctrl[0] + 0.25 * end[0],
                0.25 * start[1] + 0.5 * ctrl[1] + 0.25 * end[1],
            )
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
# er_diagram: entities (sharp-cornered boxes) and relationships (diamonds) in
# a row, connected by edges; attributes (ovals) fan out above whichever
# entity/relationship they belong to (DiagramNode.parent_id). Each entity's
# own column reserves whichever is wider - its box or its attribute fan - so
# two neighbouring columns' attribute fans can never collide with each
# other, the same "reserve by content, never let neighbours overlap"
# approach the rest of this module already uses elsewhere.
# ---------------------------------------------------------------------------

_ER_ATTR_W = 108.0
_ER_ATTR_H = 44.0
_ER_ATTR_GAP = 14.0
_ER_COLUMN_GAP = 44.0
_ER_ENTITY_W = 170.0
_ER_DIAMOND_W = 128.0
_ER_DIAMOND_H = 84.0
_ER_ATTR_ROW_GAP = 30.0  # vertical clearance for the attribute->parent leader line


def _draw_diamond(
    node: DiagramNode, *, cx: float, cy: float, width: float, height: float, theme: TemplateTheme,
    colors: tuple[str, str] | None = None,
) -> str:
    """A relationship (er_diagram) or decision (flow_chart), drawn as a
    rhombus. Text stays within the middle half of the shape's height (where
    a rhombus is at least half as wide as its full width), and
    _fit_boxed_text's own shrink-to-fit is the second, independent safety
    net - the same belt-and-suspenders approach every other shape in this
    module already relies on for text it can't fully control the length of.
    `colors`, when given, overrides the theme-derived default - flow_chart's
    decision diamonds are always semantic purple (see _FLOW_ROLE_COLORS),
    regardless of the course's own theme. `detail`, when set (a decision's
    own condition text, say), reaches the hover tooltip even though the
    diamond itself only ever shows the short `label`."""
    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
    points = (
        f"{cx:.1f},{cy - height / 2:.1f} {cx + width / 2:.1f},{cy:.1f} "
        f"{cx:.1f},{cy + height / 2:.1f} {cx - width / 2:.1f},{cy:.1f}"
    )
    fill, stroke = colors if colors is not None else (theme.accent_soft, theme.accent_color)
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<polygon class="diagram-box" points="{points}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="2"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=width * 0.5, height=height * 0.5,
            color=theme.text_color, theme=theme, base_size=12.5,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _draw_attribute_oval(
    node: DiagramNode, *, cx: float, cy: float, width: float, height: float, theme: TemplateTheme
) -> str:
    """An attribute, drawn as an ellipse. The safe inscribed rectangle for
    centred text in an ellipse is width/√2 by height/√2 - comfortably wider
    than the 0.7/0.6 used here, left deliberately smaller as extra margin,
    plus _fit_boxed_text's own shrink-to-fit underneath."""
    tooltip = node.label.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<ellipse class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" rx="{width / 2:.1f}" ry="{height / 2:.1f}" '
        f'fill="{theme.surface_color}" stroke="{theme.border_color}" stroke-width="1.5"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=width * 0.7, height=height * 0.6,
            color=theme.text_color, theme=theme, base_size=11.5, weight=500,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _layout_er(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    is_attribute = lambda n: n.shape_role.strip().lower() == "attribute"  # noqa: E731
    main_nodes = [n for n in nodes if not is_attribute(n)]
    if not main_nodes:
        return [], CANVAS_WIDTH, 0.0

    main_ids = {n.id for n in main_nodes if n.id}
    attrs_by_parent: dict[str, list[DiagramNode]] = {}
    for attr in nodes:
        if is_attribute(attr) and attr.parent_id in main_ids:
            attrs_by_parent.setdefault(attr.parent_id, []).append(attr)

    def is_relationship(n: DiagramNode) -> bool:
        return n.shape_role.strip().lower() == "relationship"

    def own_width(n: DiagramNode) -> float:
        return _ER_DIAMOND_W if is_relationship(n) else _ER_ENTITY_W

    def fan_width(n: DiagramNode) -> float:
        attrs = attrs_by_parent.get(n.id, [])
        if not attrs:
            return 0.0
        return len(attrs) * _ER_ATTR_W + (len(attrs) - 1) * _ER_ATTR_GAP

    col_widths = [max(own_width(n), fan_width(n)) for n in main_nodes]
    total_w = sum(col_widths) + _ER_COLUMN_GAP * (len(main_nodes) - 1)
    canvas_w = max(total_w + 2 * MARGIN, CANVAS_WIDTH)

    has_attrs = any(attrs_by_parent.values())
    attr_top = top
    main_top = top + (_ER_ATTR_H + _ER_ATTR_ROW_GAP if has_attrs else 0.0)

    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    x = (canvas_w - total_w) / 2
    # id -> (centre_x, centre_y, bbox_w, bbox_h) - centre/bbox, not top-left,
    # since a diamond's own draw call is centre-anchored while a box's is
    # top-left-anchored; this table normalises both for the edge connectors.
    positions: dict[str, tuple[float, float, float, float]] = {}
    main_bottom = main_top

    for node, col_w in zip(main_nodes, col_widths):
        cx = x + col_w / 2
        if is_relationship(node):
            elements.append(
                _draw_diamond(
                    node, cx=cx, cy=main_top + _ER_DIAMOND_H / 2,
                    width=_ER_DIAMOND_W, height=_ER_DIAMOND_H, theme=theme,
                )
            )
            positions[node.id] = (cx, main_top + _ER_DIAMOND_H / 2, _ER_DIAMOND_W, _ER_DIAMOND_H)
            main_bottom = max(main_bottom, main_top + _ER_DIAMOND_H)
        else:
            svg, height = _node_block(
                node, x=cx - _ER_ENTITY_W / 2, y=main_top, width=_ER_ENTITY_W, theme=theme, box_shape="sharp"
            )
            elements.append(svg)
            positions[node.id] = (cx, main_top + height / 2, _ER_ENTITY_W, height)
            main_bottom = max(main_bottom, main_top + height)

        attrs = attrs_by_parent.get(node.id, [])
        if attrs:
            fan_w = len(attrs) * _ER_ATTR_W + (len(attrs) - 1) * _ER_ATTR_GAP
            ax = cx - fan_w / 2
            for attr in attrs:
                a_cx = ax + _ER_ATTR_W / 2
                a_cy = attr_top + _ER_ATTR_H / 2
                elements.append(
                    _draw_attribute_oval(attr, cx=a_cx, cy=a_cy, width=_ER_ATTR_W, height=_ER_ATTR_H, theme=theme)
                )
                elements.append(
                    f'<line x1="{a_cx:.1f}" y1="{a_cy + _ER_ATTR_H / 2:.1f}" x2="{cx:.1f}" y2="{main_top:.1f}" '
                    f'stroke="{theme.border_color}" stroke-width="1.5"/>'
                )
                ax += _ER_ATTR_W + _ER_ATTR_GAP
        x += col_w + _ER_COLUMN_GAP

    for edge in edges:
        a = positions.get(edge.source)
        b = positions.get(edge.target)
        if not a or not b:
            continue
        start, end = _shorten_to_box_edge(
            (a[0], a[1]), (b[0], b[1]), w1=a[2], h1=a[3], w2=b[2], h2=b[3]
        )
        elements.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2"/>'
        )
        if edge.label.strip():
            mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            elements.append(_edge_label(mid[0], mid[1] - 10, edge.label, theme, max_width=90.0))

    return elements, canvas_w, main_bottom - top


# ---------------------------------------------------------------------------
# schematic: a small, topic-agnostic vocabulary of illustrated primitives
# (block/coil/gauge/arrow/label) instead of labelled boxes - for a
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


def _fit_boxed_text(
    cx: float, cy: float, text: str, *, width: float, height: float, color: str,
    theme: TemplateTheme, base_size: float = 13.0, weight: int = 700,
    min_size: float = 9.5, padding: float = 6.0,
) -> str:
    """Centers `text` inside a box of the given pixel size, wrapping to
    multiple lines and - only if that still doesn't fit - shrinking the
    font down to `min_size` (and, as a last resort, letting `_wrap`'s own
    ellipsis truncate it) so a long model-generated label can never bleed
    outside its own shape into whatever sits next to it. The deterministic
    layout engine fixes every schematic shape's box size before this runs,
    so growing the box itself isn't an option here - fitting the text to
    the box (not the other way around) is what keeps a shape's own
    footprint - and the collision/clipping guarantees QA already checked
    against it - unchanged."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    usable_w = max(width - 2 * padding, 10.0)
    usable_h = max(height - 2 * padding, 10.0)
    size = base_size
    bold = weight >= 600
    lines: list[str] = []
    while True:
        max_lines = max(int(usable_h // (size * LINE_HEIGHT)), 1)
        full_lines = _wrap(text, font_size=size, width=usable_w, max_lines=1000, bold=bold)
        if len(full_lines) <= max_lines or size <= min_size:
            lines = _wrap(text, font_size=size, width=usable_w, max_lines=max_lines, bold=bold)
            break
        size -= 0.75
    line_gap = size * LINE_HEIGHT
    first_y = cy - line_gap * (len(lines) - 1) / 2 + size * 0.35
    svg, _ = _text(cx, first_y, lines, font_size=size, weight=weight, color=color, font_family=theme.font_family)
    return svg


def _caption_text(
    cx: float, top_y: float, text: str, *, width: float, color: str, theme: TemplateTheme,
    size: float = 12.5, weight: int = 500, max_lines: int = 2,
) -> str:
    """A caption sitting *beside/below* a shape (a coil/gauge's name, a
    circle's outer label) rather than inside a fixed box - wrapped to a
    reasonable width so a long label doesn't run into a neighbouring shape,
    but without `_fit_boxed_text`'s font-shrinking since there's no hard
    height ceiling here the way there is inside a shape's own outline."""
    lines = _wrap(text, font_size=size, width=max(width, 70.0), max_lines=max_lines, bold=weight >= 600)
    if not lines:
        return ""
    svg, _ = _text(cx, top_y, lines, font_size=size, weight=weight, color=color, font_family=theme.font_family)
    return svg


def _shape_group(shape: SchematicShape, inner_svg: str) -> str:
    tooltip = shape.label.strip()
    if shape.sublabel.strip():
        tooltip = f"{tooltip} / {shape.sublabel.strip()}" if tooltip else shape.sublabel.strip()
    title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
    return f'<g class="diagram-node" tabindex="0" role="img">{title}{inner_svg}</g>'


def _schematic_colors(shape: SchematicShape, theme: TemplateTheme) -> tuple[str, str, str, float]:
    """(fill, stroke, text_color, stroke_width) for a schematic shape's main
    body. A blank `color_role` (every legacy/coordinate-authored shape, and
    any shape a model simply didn't set one on) falls back to exactly the
    theme colors this renderer always used - zero visual change for those.
    A primary shape's stroke is heavier regardless of color, reinforcing
    focal hierarchy the same way concept_map's `emphasis` flag already does
    for labelled boxes."""
    stroke_width = stroke_width_for(shape.role)
    if not shape.color_role.strip():
        return theme.surface_color, theme.border_color, theme.text_color, stroke_width
    role = resolve_color_role(shape.color_role)
    return role.fill, role.stroke, role.text, stroke_width


def _draw_block(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    x, y = cx - w / 2, cy - h / 2
    fill, stroke, text_color, stroke_width = _schematic_colors(shape, theme)
    if shape.sublabel.strip():
        # A two-part object (a magnet's N/S poles, a battery's +/- terminals,
        # any two-state block) - solid, high-contrast halves so the split
        # reads clearly at a glance. The left/primary half carries the
        # shape's semantic color (or the theme's accent, unchanged, when no
        # color_role is set); the right/secondary half stays a neutral dark
        # tone either way - two strong colors on one block would fight the
        # "don't make every object equally saturated" rule.
        half_w = w / 2
        left_fill = stroke if shape.color_role.strip() else theme.accent_color
        parts = [
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{half_w:.1f}" height="{h:.1f}" '
            f'fill="{left_fill}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            f'<rect class="diagram-box" x="{x + half_w:.1f}" y="{y:.1f}" width="{half_w:.1f}" height="{h:.1f}" '
            f'fill="{theme.text_color}" stroke="{theme.border_color}" stroke-width="1.5"/>',
            _fit_boxed_text(
                x + half_w / 2, cy, shape.label, width=half_w, height=h, color="#ffffff",
                theme=theme, base_size=14,
            ),
            _fit_boxed_text(
                x + half_w + half_w / 2, cy, shape.sublabel, width=half_w, height=h, color="#ffffff",
                theme=theme, base_size=14,
            ),
        ]
    else:
        parts = [
            f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>',
            _fit_boxed_text(cx, cy, shape.label, width=w, height=h, color=text_color, theme=theme, base_size=13),
        ]
    return _shape_group(shape, "".join(parts))


def _draw_circle(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    """A round object - a cell, an atom, a planet, a seed - generic across
    biology/chemistry/astronomy the same way `block` is generic for
    rectangular objects. With a `sublabel`, draws a smaller labelled circle
    inside it (a nucleus, a core, an embryo)."""
    radius = min(w, h) / 2
    fill, stroke, text_color, stroke_width = _schematic_colors(shape, theme)
    if shape.sublabel.strip():
        inner_r = radius * 0.42
        # A merged "inside:" child (see _merge_inside_anchors) carries its
        # own color_role separately in `sublabel_color_role` - use that when
        # set so a nested object (a nucleus) reads as its own distinct
        # semantic color instead of just a smaller copy of the parent's.
        # Falls back to the parent's own color_role for a directly-authored
        # sublabel (a gauge caption, a block's second half - no separate
        # nested shape ever existed to have its own color).
        inner_role = shape.sublabel_color_role.strip() or shape.color_role.strip()
        inner_fill = resolve_color_role(inner_role).fill if inner_role else theme.accent_soft
        inner_stroke = resolve_color_role(inner_role).stroke if inner_role else theme.accent_color
        parts = [
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>',
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{inner_r:.1f}" fill="{inner_fill}" '
            f'stroke="{inner_stroke}" stroke-width="1.5"/>',
            _fit_boxed_text(
                cx, cy, shape.sublabel, width=inner_r * 1.6, height=inner_r * 1.6, color=inner_stroke,
                theme=theme, base_size=11,
            ),
            _caption_text(cx, cy + radius + 16, shape.label, width=max(radius * 2.2, 90), color=theme.text_color, theme=theme, size=13, weight=700),
        ]
    else:
        parts = [
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>',
            _fit_boxed_text(
                cx, cy, shape.label, width=radius * 1.6, height=radius * 1.6, color=text_color,
                theme=theme, base_size=13,
            ),
        ]
    return _shape_group(shape, "".join(parts))


def _draw_coil(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    loops = max(int(w / 24), 4)
    step = w / loops
    left = cx - w / 2
    _, stroke, text_color, stroke_width = _schematic_colors(shape, theme)
    loop_stroke = stroke if shape.color_role.strip() else theme.text_color
    parts = [
        f'<ellipse cx="{left + step * (i + 0.5):.1f}" cy="{cy:.1f}" rx="{step * 0.55:.1f}" ry="{h / 2:.1f}" '
        f'fill="none" stroke="{loop_stroke}" stroke-width="{stroke_width}"/>'
        for i in range(loops)
    ]
    parts.append(
        _caption_text(
            cx, cy + h / 2 + 18, shape.label, width=max(w, 90), color=text_color, theme=theme,
            size=12.5, weight=700,
        )
    )
    return _shape_group(shape, "".join(parts))


def _draw_gauge(shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme) -> str:
    radius = min(w, h) / 2
    angle = math.radians(shape.rotation - 90)  # rotation=0 -> needle points up (resting)
    needle_len = radius * 0.72
    nx, ny = cx + needle_len * math.cos(angle), cy + needle_len * math.sin(angle)
    fill, stroke, text_color, stroke_width = _schematic_colors(shape, theme)
    needle_color = stroke if shape.color_role.strip() else theme.accent_color
    parts = [
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{stroke_width}"/>',
        f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{needle_color}" '
        f'stroke-width="2.5" stroke-linecap="round"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{needle_color}"/>',
        _caption_text(
            cx, cy + radius + 18, shape.sublabel or shape.label, width=max(radius * 2.2, 90),
            color=theme.text_color, theme=theme, size=12.5, weight=700,
        ),
    ]
    return _shape_group(shape, "".join(parts))


def _draw_arrow(
    shape: SchematicShape, cx: float, cy: float, w: float, h: float, theme: TemplateTheme,
    connector: tuple[tuple[float, float], tuple[float, float]] | None = None,
) -> str:
    line_color = resolve_color_role(shape.color_role).stroke if shape.color_role.strip() else theme.accent_color
    if connector is not None:
        # A real point-to-point connector: `target_id` resolved to another
        # shape in this state, so the arrow is drawn from its own box edge
        # all the way to that shape's box edge (see _draw_shape) instead of
        # a short segment floating near its own anchor position - what makes
        # "applied force -> pulley" or "reaction -> water" actually read as
        # touching the thing it's pointing at.
        (sx, sy), (ex, ey) = connector
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        dx, dy = ex - sx, ey - sy
        dist = max(math.hypot(dx, dy), 1.0)
        # Label offset perpendicular to the connector, not along it - keeps
        # it clear of the line itself regardless of the connector's angle.
        px, py = -dy / dist, dx / dist
        lx, ly = mx + px * 14, my + py * 14
        parts = [
            f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{line_color}" stroke-width="3" marker-end="url(#diagram-arrow)"/>',
            _caption_text(lx, ly, shape.label, width=130, color=line_color, theme=theme, size=12, weight=700),
        ]
        return _shape_group(shape, "".join(parts))

    length = max(w, h)
    angle = math.radians(shape.rotation)
    dx, dy = (length / 2) * math.cos(angle), (length / 2) * math.sin(angle)
    parts = [
        f'<line x1="{cx - dx:.1f}" y1="{cy - dy:.1f}" x2="{cx + dx:.1f}" y2="{cy + dy:.1f}" '
        f'stroke="{line_color}" stroke-width="3" marker-end="url(#diagram-arrow)"/>',
        _caption_text(cx, cy - 14, shape.label, width=130, color=line_color, theme=theme, size=12, weight=700),
    ]
    return _shape_group(shape, "".join(parts))


def _draw_label(shape: SchematicShape, cx: float, cy: float, w: float, theme: TemplateTheme) -> str:
    # A standalone annotation has no fill behind it (it sits directly on the
    # page background), so its color_role tints the text itself rather than
    # a box - every palette stroke color is dark/saturated enough to stay
    # readable on white, unlike the light `fill` tones meant for behind text.
    text_color = resolve_color_role(shape.color_role).stroke if shape.color_role.strip() else theme.text_color
    lines = _wrap(shape.label, font_size=DETAIL_SIZE, width=max(w, 80), max_lines=3) or [""]
    svg, _ = _text(
        cx, cy, lines, font_size=DETAIL_SIZE, weight=500, color=text_color,
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
    boxes: dict[str, tuple[float, float, float, float]],
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
    if kind == "arrow":
        connector = None
        target_box = boxes.get(shape.target_id) if shape.target_id else None
        if target_box is not None:
            tx, ty, tw, th = target_box
            connector = _shorten_to_box_edge((cx, cy), (tx, ty), w1=w, h1=h, w2=tw, h2=th)
        return _draw_arrow(resolved, cx, cy, w, h, theme, connector=connector)
    return _draw_label(shape, cx, cy, w, theme)


# Declared relationships that are worth drawing as an actual connector line
# between the two shapes' resolved positions - a physical/functional link a
# reader would otherwise have to infer from proximity alone. Split into
# "directional" (rendered with an arrowhead - something clearly flows/points
# from source to target) and "structural" (rendered dashed, no arrowhead - a
# mutual link with no implied direction, like two parts touching or being
# fastened together). Spatial-only descriptors (above/below/left_of/right_of/
# between) are already conveyed by the anchor-driven layout itself, and
# containment (inside/contains/surrounds) is already conveyed by nesting/the
# sublabel merge - drawing a line for either would be redundant, not
# clarifying, so both are deliberately left undrawn here.
_RELATIONSHIP_DIRECTIONAL_TYPES = {"points_to", "flows_into"}
_RELATIONSHIP_STRUCTURAL_TYPES = {"connected_to", "attached_to", "contacts", "passes_through", "rotates_around"}
_DRAWABLE_RELATIONSHIP_TYPES = _RELATIONSHIP_DIRECTIONAL_TYPES | _RELATIONSHIP_STRUCTURAL_TYPES


def _draw_relationship_connectors(
    relationships, *, boxes: dict[str, tuple[float, float, float, float]], theme: TemplateTheme,
) -> list[str]:
    """One subtle connector line per declared relationship whose type is
    drawable and whose source/target both resolve to a real shape in this
    state - e.g. a merged-into-sublabel id (see _merge_inside_anchors)
    simply has no entry in `boxes` and is silently skipped, never an error.
    Deliberately muted/thin relative to the shapes' own semantic colors:
    this is a structural hint, not another colored object competing for
    attention."""
    elements: list[str] = []
    for rel in relationships:
        rel_type = rel.type.strip().lower()
        if rel_type not in _DRAWABLE_RELATIONSHIP_TYPES or rel.source == rel.target:
            continue
        box1, box2 = boxes.get(rel.source), boxes.get(rel.target)
        if not box1 or not box2:
            continue
        x1, y1, w1, h1 = box1
        x2, y2, w2, h2 = box2
        start, end = _shorten_to_box_edge((x1, y1), (x2, y2), w1=w1, h1=h1, w2=w2, h2=h2)
        if rel_type in _RELATIONSHIP_DIRECTIONAL_TYPES:
            elements.append(
                f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
                f'stroke="{theme.muted_color}" stroke-width="1.5" '
                f'marker-end="url(#schematic-relationship-arrow)"/>'
            )
        else:
            elements.append(
                f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
                f'stroke="{theme.muted_color}" stroke-width="1.5" stroke-dasharray="4 3"/>'
            )
    return elements


def _layout_schematic(spec: DiagramSpec, *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    states = spec.states if spec.states else [SchematicState(caption="", shapes=spec.shapes)]
    panel_w = CANVAS_WIDTH - 2 * MARGIN
    elements = [_arrow_marker("diagram-arrow", theme.muted_color)]
    if spec.relationships:
        elements.append(_arrow_marker("schematic-relationship-arrow", theme.muted_color))

    y = top
    for state in states:
        panel_y0 = y
        elements.append(
            f'<rect x="{MARGIN:.1f}" y="{panel_y0:.1f}" width="{panel_w:.1f}" height="{PANEL_HEIGHT:.1f}" '
            f'rx="10" fill="{theme.page_background}" stroke="{theme.border_color}" stroke-width="1.5"/>'
        )
        # One box per identified shape (centre x/y + pixel width/height) -
        # the single source of truth `_shape_rotation` (target direction),
        # `_draw_shape`'s arrow-target lookup and the relationship connectors
        # below all resolve other shapes' positions from, so a target/
        # relationship endpoint is always measured against the same numbers
        # actually drawn on screen.
        boxes = {
            shape.id: (
                MARGIN + shape.x * panel_w, panel_y0 + shape.y * PANEL_HEIGHT,
                max(shape.width, 0.04) * panel_w, max(shape.height, 0.04) * PANEL_HEIGHT,
            )
            for shape in state.shapes
            if shape.id
        }
        positions = {shape_id: (x, y) for shape_id, (x, y, _, _) in boxes.items()}
        if spec.relationships:
            elements.extend(
                _draw_relationship_connectors(spec.relationships, boxes=boxes, theme=theme)
            )
        for shape in state.shapes:
            elements.append(
                _draw_shape(
                    shape, panel_x0=MARGIN, panel_y0=panel_y0, panel_w=panel_w, panel_h=PANEL_HEIGHT,
                    positions=positions, boxes=boxes, theme=theme,
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
        title_lines = _wrap(spec.title, font_size=21, width=CANVAS_WIDTH - 2 * MARGIN, max_lines=1, bold=True)
        title_svg, title_h = _text(
            CANVAS_WIDTH / 2,
            top + 21,
            title_lines,
            font_size=21,
            weight=700,
            color=theme.text_color,
            font_family=theme.font_family,
        )
        top += title_h + 20

    canvas_w = CANVAS_WIDTH
    if kind in ("flow_chart", "process"):
        elements, content_h = _layout_flow_chart(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "data_flow_diagram":
        elements, content_h = _layout_dfd(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "swimlane":
        elements, content_h = _layout_swimlane(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "cycle":
        elements, canvas_w, content_h = _layout_cycle(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "hierarchy":
        elements, content_h, canvas_w = _layout_hierarchy(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "comparison":
        elements, content_h = _layout_comparison(spec.nodes, theme=theme, top=top)
    elif kind in ("concept_map", "conceptual"):
        elements, canvas_w, content_h = _layout_concept_map(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "er_diagram":
        elements, canvas_w, content_h = _layout_er(spec.nodes, spec.edges, theme=theme, top=top)
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
