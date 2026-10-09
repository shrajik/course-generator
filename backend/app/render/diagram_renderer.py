"""DiagramSpec -> SVG.

Pure and AI-free, the same philosophy as `app.render.html_renderer`: the model
only supplies structured content (nodes/edges/kind); every pixel here is
produced by deterministic code, on the course's own theme colours, so a
diagram always renders crisp labels and never drifts from the brand.
"""

from __future__ import annotations

import math
import uuid
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
# A course document renders this SVG scaled down to fit its block width -
# CONTENT_WIDTH (666px) at this canvas's own 880px, a ~0.757x scale even
# for a perfectly ordinary, non-oversized diagram (see
# app.services.diagram_render_qa.readability_issues, which computes this
# exact ratio). Sized so the ON-PAGE, POST-SCALE result meets a firm
# minimum: >= 14px for a node title, >= 12px for a subtitle/detail line,
# working backward from that scale factor (14 / 0.757 = 18.5, 12 / 0.757 =
# 15.9) - not just "looks reasonable in isolation" the way the previous
# 17.5/14.0 values were picked, which left the DETAIL line at an
# effective ~10.6px post-scale, under even a lenient reading-size floor.
LABEL_SIZE = 19.0
DETAIL_SIZE = 16.0
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


# Same post-scale reasoning as LABEL_SIZE/DETAIL_SIZE above - 12 / 0.757 = 15.9.
EDGE_LABEL_FONT_SIZE = 16.0
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
    # Same calibration _wrap already uses for every node box (confirmed
    # against real browser rendering - see _wrap's own docstring), not an
    # independent estimate: a mismatched ratio here left label pills ~27%
    # narrower than their real text, so the text overflowed the pill and
    # got clipped by whichever node box painted over it afterward (a real,
    # confirmed case: "Charge <-> Queue item" rendered as "harge <-> Queue
    # item" in an exported PDF).
    width = min(max(longest * EDGE_LABEL_FONT_SIZE * _SANS_RATIO + 14, 40.0), max_width)
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


# A pill's label is a flowchart "title" (Start/End) - same >= 14px
# post-scale floor as LABEL_SIZE (see its own comment for the 0.757
# standard-canvas scale factor this assumes: 14 / 0.757 = 18.5). min_size
# is pinned to the same value (no shrinking) rather than left at
# _fit_boxed_text's own lenient default - Start/End labels are always
# short by convention, so the fixed pill width never actually needs the
# shrink-to-fit escape hatch, and pinning it means one never silently
# fires and drops below the floor.
_PILL_LABEL_SIZE = 19.0


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
            color=theme.text_color, theme=theme, base_size=_PILL_LABEL_SIZE, min_size=_PILL_LABEL_SIZE,
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
) -> tuple[list[str], float, float]:
    """flow_chart/process: a single vertical spine with semantic start
    (green pill) / end (red pill) / process (blue box) / decision (purple
    diamond) colouring, auto-detected from each node's own in/out-degree.
    A decision's two branches are never drawn as two equal-weight columns -
    one (whichever leads deeper into the procedure: through another
    decision, or the longer plain chain) stays ON the spine, directly
    below the decision, so every consecutive pair of steps in the
    procedure's own main line connects with a short straight arrow; the
    other (the quicker exit - a dead end, or a branch that loops back to
    an earlier step) is drawn once, in a single column beside the spine,
    fed by a short connector off the decision's own right vertex. This is
    the classic flowchart convention (a decision's primary path continues
    down, its alternate exits to the side) and it is what a real reported
    case needed: "Select assignee" chained through three more steps into a
    SECOND decision before finally reaching an "End" three decisions of
    branching later - forcing it into a side column (the previous
    version's design) left it visibly misaligned with everything below it,
    since only the spine's own column width and position are shared by
    every ordinary hop.

    A side branch's own further edge (to a node not yet drawn - a shared
    "End" fed by more than one branch - or one already drawn earlier - a
    loop back to reassess) is deferred to the final sweep at the bottom of
    this function, which finds it once its target's position is known and
    routes it through its own dedicated margin lane (see `_next_lane_x`) -
    never sharing a vertical run with any other deferred edge, and always
    entering its target from the side (never the top, which is reserved
    for the spine's own incoming arrow).

    Only a decision whose SIDE branch resolves to a simple, single
    "column" outcome (a dead end, or a loop/reconvergence with at least one
    real step of its own) gets this treatment; anything more tangled (a
    missing target, a side branch that itself runs into further branching)
    falls back to drawing just the decision and its two immediate branch
    targets, one on the spine and one beside it, and lets the outer walk's
    own predecessor lookup (`connect_real`) and the final sweep pick up
    whatever comes after - the same "never worse than simple, never crash"
    contract every other fallback in this module already keeps."""
    in_degree, out_edges = _flow_degrees(nodes, edges)
    by_id = {n.id: n for n in nodes if n.id}
    out_degree = {nid: len(es) for nid, es in out_edges.items()}
    in_edges: dict[str, list[DiagramEdge]] = {}
    for edge in edges:
        if edge.source in by_id and edge.target in by_id:
            in_edges.setdefault(edge.target, []).append(edge)

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

    # One shared width for every "process" box (main-spine AND side-column
    # alike), sized to the longest label/detail actually present - not a
    # fixed fraction of the canvas. A real reported case: a side-column
    # box sat directly above a main-spine box with neither its left edge
    # nor its own width matching - visibly misaligned, since the two
    # widths came from two entirely different formulas. Clamped so a short
    # label doesn't produce a cramped, barely-wider-than-text box, and a
    # very long one wraps onto more lines instead of stretching the whole
    # diagram to fit a single outlier.
    _MIN_PROCESS_W = 240.0
    _MAX_PROCESS_W = 420.0

    def _ideal_process_width(node: DiagramNode) -> float:
        label_w = len(node.label.strip()) * LABEL_SIZE * _BOLD_RATIO
        detail_w = len(node.detail.strip()) * DETAIL_SIZE * _SANS_RATIO if node.detail.strip() else 0.0
        return max(label_w, detail_w) + 2 * BOX_PADDING

    # >= 12px effective post-scale (see LABEL_SIZE's own comment for the
    # 0.757 standard-canvas scale factor this assumes: 12 / 0.757 = 15.9).
    # base_size == min_size (passed to _draw_diamond below) so this never
    # shrinks below that floor - the diamond is sized to fit it instead
    # (see _ideal_decision_size), the same "enlarge the shape, don't
    # shrink the text" rule every other size in this module now follows.
    _DIAMOND_LABEL_SIZE = 16.0
    _MIN_DECISION_W = 220.0
    _MAX_DECISION_W = 420.0
    _MIN_DECISION_H = 130.0

    def _ideal_decision_size(node: DiagramNode) -> tuple[float, float]:
        # _draw_diamond gives the label a usable box of (width * 0.5,
        # height * 0.5) - a rhombus is only that wide/tall at its own
        # horizontal/vertical midline - so size the diamond so a 2-line
        # wrap at _DIAMOND_LABEL_SIZE fits inside that half-box without
        # _fit_boxed_text ever needing to shrink or truncate it.
        label = node.label.strip() or "?"
        lines = _wrap(label, font_size=_DIAMOND_LABEL_SIZE, width=260.0, max_lines=2, bold=True) or [label]
        longest = max(len(line) for line in lines)
        line_w = longest * _DIAMOND_LABEL_SIZE * _BOLD_RATIO
        text_h = len(lines) * _DIAMOND_LABEL_SIZE * LINE_HEIGHT
        width = max(_MIN_DECISION_W, min(_MAX_DECISION_W, (line_w + 24.0) / 0.5))
        height = max(_MIN_DECISION_H, (text_h + 24.0) / 0.5)
        return width, height

    process_ids = [nid for nid in by_id if role_of(nid) == "process"]
    process_w = (
        max(_MIN_PROCESS_W, min(_MAX_PROCESS_W, max(_ideal_process_width(by_id[nid]) for nid in process_ids)))
        if process_ids
        else _MIN_PROCESS_W
    )
    decision_sizes: dict[str, tuple[float, float]] = {
        nid: _ideal_decision_size(by_id[nid]) for nid in by_id if role_of(nid) == "decision"
    }
    # The spine's own column must be at least as wide as the widest
    # decision diamond too, or a long question would overflow it.
    if decision_sizes:
        process_w = max(process_w, max(w for w, _h in decision_sizes.values()))

    gap = 50.0
    col_gap = 40.0
    # The side column holds a decision's quick-exit branch - in practice a
    # short label ("Keep Task", "Escalate issue"), never the longest thing
    # in the diagram. Capped independently, and MUCH narrower than the
    # spine's own `process_w` ceiling - giving it the SAME width as the
    # spine (this function's own earlier design) meant a long spine label
    # doubled the canvas's total width even when nothing in the side
    # column needed anywhere near that much room, which - confirmed real -
    # pushed the effective on-page font size for EVERY node below its own
    # 14px floor even for an ordinary-looking flowchart. A side label
    # longer than this still renders correctly; it simply wraps onto more
    # lines instead of growing the canvas, the same trade-off `process_w`
    # itself makes once IT hits its own ceiling.
    _MAX_SIDE_W = 260.0
    side_w = min(process_w, _MAX_SIDE_W)
    # Grow the canvas (never shrink the boxes back down to fit a fixed
    # width - see the user's own "increase the viewBox if needed so
    # nothing shrinks when scaled") whenever the content-sized columns
    # would need more than the standard canvas provides.
    needed_w = process_w + col_gap + side_w + 2 * MARGIN
    canvas_w = max(CANVAS_WIDTH, needed_w)
    x_center = MARGIN + process_w / 2
    side_left_x = x_center + process_w / 2 + col_gap
    side_cx = side_left_x + side_w / 2
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    y = top
    visited: set[str] = set()
    # Every drawn node's own (centre-x, top-y, bottom-y, width), updated
    # the instant it's drawn - the authoritative source `connect_real` (and
    # the final sweep below) look a node's real predecessor up in, rather
    # than assuming "whatever was drawn immediately before this" is that
    # predecessor. Width is carried alongside so a long-distance connector
    # can leave/enter at the exact centre of a box's own left/right edge
    # (see `_route_lane`) instead of guessing at one shared column width.
    positions: dict[str, tuple[float, float, float, float]] = {}
    # Every (source, target) pair that has ALREADY had its arrow drawn by
    # any of this function's specialised paths (a spine hop, a decision's
    # side branch, an inner side-chain hop) - the final sweep below uses
    # this to add only the edges nothing else accounted for, never a
    # duplicate line on top of one already drawn.
    drawn_edges: set[tuple[str, str]] = set()

    # Dedicated margin lanes for every long-distance connector (a side
    # branch's own deferred edge, or the rare cross-column fallback
    # connection) - each call to `_next_lane_x` hands out the NEXT unused
    # lane, so two connectors can never share a vertical run (a real
    # reported case: "Keep task -> End" and "Escalate -> Assess request"
    # both routed through the exact same margin line, making the two
    # arrows visually indistinguishable). Anchored to this function's own
    # base canvas width - if more lanes end up used than that width
    # comfortably fits, the canvas is simply widened at the very end (see
    # `final_canvas_w` below); every lane's own x only ever depends on its
    # index, never on the final width, so nothing already drawn needs to move.
    _LANE_GAP = 18.0
    _lane_start_x = canvas_w - MARGIN + 16.0
    _lane_count = [0]
    # Whether any far lane ended up with a label - most never do (a side
    # branch's own DEFERRED merge/loop edge, the only kind that reaches a
    # far lane, rarely carries its own label; a decision's own immediate
    # branch label lives on the short LOCAL connector instead - see
    # `_decision_to_side_connector`). The canvas only needs to reserve
    # real room for a label's own width when one is actually drawn.
    _lane_label_used = [False]

    def _next_lane_x() -> float:
        lane_x = _lane_start_x + _lane_count[0] * _LANE_GAP
        _lane_count[0] += 1
        return lane_x

    # A dedicated x within the gap between the spine and the side column
    # for EACH local elbow's own vertical run - never one shared constant
    # for every one of them (a real, confirmed case: two entirely
    # different local elbows, reaching the same target from different
    # entry sides, both turned at the exact same x, and their vertical
    # runs overlapped for a real stretch even though their entry POINTS
    # differed). Spaced closely (the gap itself is narrow) but never
    # closer than the clearance rule, and never past the side column's
    # own left edge - `min()` below reuses the last lane rather than
    # overflow into it on a rare 4th+ call.
    _LOCAL_LANE_GAP = 10.0
    _local_lane_start_x = x_center + process_w / 2 + 16.0
    _local_lane_max_x = side_left_x - 16.0
    _local_lane_count = [0]

    def _next_local_mid_x() -> float:
        mid_x = _local_lane_start_x + _local_lane_count[0] * _LOCAL_LANE_GAP
        _local_lane_count[0] += 1
        return min(mid_x, _local_lane_max_x)

    # Which side of a given target node each deferred/side connector has
    # already claimed - shared by every long-distance routing helper below
    # so a node fed by more than one such connector (a real, legitimate
    # case: two entirely different decisions' own quick-exit branches both
    # reaching a shared "End", or two edges both reconverging on the same
    # side-column box) always spreads them across DIFFERENT sides, never
    # stacking two arrowheads on the one side both would naturally prefer.
    _entry_sides_used: dict[str, set[str]] = {}

    def _claim_entry_side(target_id: str, preference: list[str]) -> str:
        used = _entry_sides_used.setdefault(target_id, set())
        for side in preference:
            if side not in used:
                used.add(side)
                return side
        # Every preferred side already taken (a rare 3+-way convergence) -
        # reuse the last one rather than crash; a visually crowded but
        # still-correct arrow beats a missing one.
        used.add(preference[-1])
        return preference[-1]

    # Every edge label is placed BESIDE its line, never on top of it (a
    # real reported case: a loop-back's label sat centred ON its own
    # line, which then visibly struck through the text). Vertical lines
    # (ordinary spine hops, and the long margin lanes) offset the label to
    # the right, where the rest of that row is empty by construction -
    # nothing else is ever drawn at that height between two adjacent
    # columns/lanes. Short decision-to-side connectors instead place their
    # label just past the decision's own edge and above the line (see
    # `_decision_to_side_connector`) - the gap between the spine and the
    # side column is comfortable vertically but too narrow, for a short
    # branch, to safely offset a label sideways within it.
    _LABEL_OFFSET = 46.0

    def _place_label_vertical(line_x: float, mid_y: float, label: str) -> None:
        if label.strip():
            elements.append(_edge_label(line_x + _LABEL_OFFSET, mid_y, label, theme, max_width=110.0))

    def draw_arrow_down(x: float, y_from: float, y_to: float, label: str = "") -> None:
        elements.append(
            f'<line x1="{x:.1f}" y1="{y_from:.1f}" x2="{x:.1f}" y2="{y_to:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        _place_label_vertical(x, (y_from + y_to) / 2, label)

    # How many long-distance connectors have already been routed into a
    def _route_lane(
        target_id: str,
        source_pos: tuple[float, float, float, float], target_pos: tuple[float, float, float, float], label: str
    ) -> None:
        """A long-distance connector between two boxes that don't share a
        centre-x: out to a fresh dedicated lane from the SOURCE's own
        right edge, down (or up) the lane to the target's row, then in to
        the TARGET's own right (or, for a second connector into the same
        target - see `_claim_entry_side` - bottom) side - never the top,
        which is reserved for that box's own straight-down spine arrow.
        Every node this function ever routes a long-distance connector to
        sits at or right of the main spine (there is no column further
        left than the spine itself in this layout), so routing via the
        right margin is always the shorter, always-clear choice - never a
        raw diagonal, and never sharing a lane with any other connector.

        The final run into the target can still land on a row some OTHER
        already-drawn box happens to share with the target - not reserved
        for the target alone (a real case: a decision's dead-end side box
        ended up on the exact same row as "End", purely because "End" was
        next in the spec's own node order right after that decision, and
        the lane's straight run into End's own centre cut straight
        through that side box sitting between the lane and End on that
        row). When that happens, the run detours below (or above,
        whichever needs less of a detour) whatever blocks it, with one
        extra short hop - still entering the target at the exact centre
        of its own side, the final segment either way."""
        entry_side = _claim_entry_side(target_id, ["right", "bottom"])

        source_cx, source_top, source_bottom, source_w = source_pos
        target_cx, target_top, target_bottom, target_w = target_pos
        lane_x = _next_lane_x()
        source_cy = (source_top + source_bottom) / 2
        target_cy = (target_top + target_bottom) / 2
        exit_x = source_cx + source_w / 2
        entry_x = target_cx + target_w / 2 if entry_side == "right" else target_cx
        entry_y = target_cy if entry_side == "right" else target_bottom

        lo_x, hi_x = (entry_x, lane_x) if entry_x < lane_x else (lane_x, entry_x)
        blockers = [
            pos for pos in positions.values()
            if pos is not source_pos and pos is not target_pos
            and pos[1] < entry_y < pos[2]
            and pos[0] - pos[3] / 2 < hi_x and pos[0] + pos[3] / 2 > lo_x
        ]
        if entry_side == "right":
            if not blockers:
                elements.append(
                    f'<path d="M{exit_x:.1f},{source_cy:.1f} L{lane_x:.1f},{source_cy:.1f} '
                    f'L{lane_x:.1f},{entry_y:.1f} L{entry_x:.1f},{entry_y:.1f}" fill="none" '
                    f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
                )
            else:
                below_y = max(pos[2] for pos in blockers) + 16.0
                above_y = min(pos[1] for pos in blockers) - 16.0
                detour_y = below_y if abs(below_y - entry_y) <= abs(above_y - entry_y) else above_y
                elements.append(
                    f'<path d="M{exit_x:.1f},{source_cy:.1f} L{lane_x:.1f},{source_cy:.1f} '
                    f'L{lane_x:.1f},{detour_y:.1f} L{entry_x:.1f},{detour_y:.1f} '
                    f'L{entry_x:.1f},{entry_y:.1f}" fill="none" '
                    f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
                )
        else:
            # Bottom entry: the lane runs on past the target's own bottom
            # edge, then in - never level with the target's own row, so
            # it never needs a same-row detour the way a right-side entry
            # sometimes does.
            past_y = target_bottom + 24.0
            elements.append(
                f'<path d="M{exit_x:.1f},{source_cy:.1f} L{lane_x:.1f},{source_cy:.1f} '
                f'L{lane_x:.1f},{past_y:.1f} L{entry_x:.1f},{past_y:.1f} '
                f'L{entry_x:.1f},{entry_y:.1f}" fill="none" '
                f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
        if label.strip():
            _lane_label_used[0] = True
        _place_label_vertical(lane_x, (source_cy + target_cy) / 2, label)

    def connect_real(nid: str, target_cx: float, target_y: float) -> None:
        """Draws the arrow into `nid` from whichever of its real SAME-
        COLUMN predecessors (per the spec's own edges) has already been
        drawn - never fabricated, and never dependent on drawing order
        matching spec order. Only handles a same-column predecessor
        (`nid` isn't drawn yet, so only its own top y is known - not
        enough to attach a cross-column connector at the precise centre
        of a real side); any cross-column predecessor, drawn or not, is
        left for the final sweep once `nid` itself has a full position
        (top AND bottom). Draws nothing when no same-column predecessor
        has been drawn yet: an honest gap, not a guess - a real, confirmed
        case: a side branch's own deferred merge edge (e.g. "Close ->
        End") had ALREADY been drawn (its source, the side box, comes
        first in the spec's own edge order) by the time "End" - the
        target, reached separately via the spine's own straight-down
        continuation - was processed here, so this used to seize it and
        draw a top-centre entry for it, indistinguishable from the
        spine's own genuine top-centre arrow into the very same node."""
        for edge in in_edges.get(nid, []):
            source_pos = positions.get(edge.source)
            if source_pos is None:
                continue
            source_cx, _source_top, source_bottom, _source_w = source_pos
            if abs(source_cx - target_cx) < 0.5:
                draw_arrow_down(target_cx, source_bottom, target_y, edge.label)
                drawn_edges.add((edge.source, nid))
                return

    def draw_main_node(nid: str, y: float) -> float:
        """Draws node `nid` centred on the spine at `y`; returns its height."""
        node = by_id[nid]
        role = role_of(nid)
        visited.add(nid)
        if role == "decision":
            dw, dh = decision_sizes[nid]
            elements.append(
                _draw_diamond(
                    node, cx=x_center, cy=y + dh / 2, width=dw, height=dh, theme=theme,
                    colors=_FLOW_ROLE_COLORS["decision"], base_size=_DIAMOND_LABEL_SIZE, min_size=_DIAMOND_LABEL_SIZE,
                )
            )
            positions[nid] = (x_center, y, y + dh, dw)
            return dh
        if role in ("start", "end"):
            elements.append(_draw_pill(node, cx=x_center, cy=y + _PILL_H / 2, theme=theme, colors=_FLOW_ROLE_COLORS[role]))
            positions[nid] = (x_center, y, y + _PILL_H, _PILL_W)
            return _PILL_H
        svg, height = _node_block(
            node, x=x_center - process_w / 2, y=y, width=process_w, theme=theme, colors=_FLOW_ROLE_COLORS["process"]
        )
        elements.append(svg)
        positions[nid] = (x_center, y, y + height, process_w)
        return height

    def draw_side_chain(
        start_y: float, chain: list[str], kind: str
    ) -> tuple[tuple[float, float, float, float] | None, float]:
        """Draws a side branch's own new boxes, stacked in the single side
        column beside the spine: every id in `chain` for a genuine dead
        end ("end" - there is no merge/loop target, the last id IS a real
        box), or every id but the last for "loop"/"continue" (the last id
        is the target already-drawn-earlier or not-yet-drawn node this
        branch eventually reaches - never a new box here; the final sweep
        finds and connects it once both ends are known). Returns the first
        new box's own (cx, top, bottom, width) - or None when this branch
        has no new box of its own at all - and the bottom y of the last
        one drawn."""
        new_ids = chain if kind == "end" else chain[:-1]
        by_y = start_y
        first_pos: tuple[float, float, float, float] | None = None
        prev_id: str | None = None
        for hop_id in new_ids:
            hop_node = by_id[hop_id]
            hop_role = role_of(hop_id)
            visited.add(hop_id)
            if hop_role in ("start", "end"):
                elements.append(
                    _draw_pill(hop_node, cx=side_cx, cy=by_y + _PILL_H / 2, theme=theme, colors=_FLOW_ROLE_COLORS[hop_role])
                )
                hop_h, hop_w = _PILL_H, _PILL_W
            else:
                svg, hop_h = _node_block(
                    hop_node, x=side_left_x, y=by_y, width=side_w, theme=theme, colors=_FLOW_ROLE_COLORS["process"]
                )
                elements.append(svg)
                hop_w = side_w
            positions[hop_id] = (side_cx, by_y, by_y + hop_h, hop_w)
            if first_pos is None:
                first_pos = positions[hop_id]
            if prev_id is not None:
                draw_arrow_down(side_cx, by_y - gap, by_y)
                drawn_edges.add((prev_id, hop_id))
            prev_id = hop_id
            by_y += hop_h + gap
        bottom = by_y - gap if new_ids else start_y
        return first_pos, bottom

    def _decision_to_side_connector(
        target_id: str,
        decision_pos: tuple[float, float, float, float], target_pos: tuple[float, float, float, float], label: str
    ) -> None:
        """The short connector from a decision's own right vertex (or, for
        the rare fallback/final-sweep case, any spine box's own right
        edge) to its side branch's box: normally entering the target's
        own LEFT side - a straight horizontal line when the side box sits
        at the source's own row, or a small right-then-down (or up) elbow
        when it sits a row below/above (both orthogonal, never a raw
        diagonal). A SECOND connector into the same target (see
        `_claim_entry_side`) - a real, legitimate case: two different
        real edges both reconverging on the same side-column box - enters
        from the TOP instead, never stacking a second arrowhead on the
        left side the first one already claimed. The label sits just past
        the source's own edge, above the line - not offset sideways along
        it, since the gap between the spine and the side column is often
        too narrow to fit a label beside a line without it spilling into
        the neighbouring column.

        Every elbow turns in the MIDDLE OF THE GAP between the spine's
        own full column width and the side column - never merely between
        the source's own edge and the target, which can be much narrower
        than the spine's process boxes (a diamond is sized to its own
        question text, not to the widest box on the spine) and so land
        the turn INSIDE a wider spine box drawn below it (a real,
        confirmed case: the turn for "Delegate? -> Keep Task" cut
        straight through "Select Assignee", the spine's own next box,
        because the diamond's right vertex sat well left of that box's
        own right edge)."""
        d_cx, d_top, d_bottom, d_w = decision_pos
        t_cx, t_top, t_bottom, t_w = target_pos
        d_cy = (d_top + d_bottom) / 2
        t_cy = (t_top + t_bottom) / 2
        exit_x = d_cx + d_w / 2
        entry_side = _claim_entry_side(target_id, ["left", "top"])
        if entry_side == "left":
            entry_x = t_cx - t_w / 2
            if abs(d_cy - t_cy) < 0.5:
                elements.append(
                    f'<line x1="{exit_x:.1f}" y1="{d_cy:.1f}" x2="{entry_x:.1f}" y2="{t_cy:.1f}" '
                    f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
                )
            else:
                mid_x = _next_local_mid_x()
                elements.append(
                    f'<path d="M{exit_x:.1f},{d_cy:.1f} L{mid_x:.1f},{d_cy:.1f} '
                    f'L{mid_x:.1f},{t_cy:.1f} L{entry_x:.1f},{t_cy:.1f}" fill="none" '
                    f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
                )
        else:
            mid_x = _next_local_mid_x()
            stub_y = t_top - 20.0
            elements.append(
                f'<path d="M{exit_x:.1f},{d_cy:.1f} L{mid_x:.1f},{d_cy:.1f} '
                f'L{mid_x:.1f},{stub_y:.1f} L{t_cx:.1f},{stub_y:.1f} '
                f'L{t_cx:.1f},{t_top:.1f}" fill="none" '
                f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
        if label.strip():
            elements.append(_edge_label(exit_x + 26.0, d_cy - 22.0, label, theme, max_width=120.0))

    def _route_cross_column(
        target_id: str,
        source_pos: tuple[float, float, float, float], target_pos: tuple[float, float, float, float], label: str
    ) -> None:
        """Dispatches a deferred cross-column edge to whichever routing is
        actually safe for it. Spine -> side stays LOCAL, turning in the
        gap between the two columns exactly like `_decision_to_side_connector`
        (safe at any row, since nothing is ever drawn in that gap) - a real
        confirmed case: two SIBLING branches on the very same row (one on
        the spine, one beside it) sent this out to the far margin lane
        instead, and the straight run out to that lane cut right through
        the sibling side box sitting between the spine and the lane.
        Anything else (most commonly side -> spine, e.g. a side branch's
        own deferred merge/loop edge) genuinely needs the far lane -
        going the short way would cut through every spine box in
        between."""
        if abs(source_pos[0] - x_center) < 0.5 and abs(target_pos[0] - side_cx) < 0.5:
            _decision_to_side_connector(target_id, source_pos, target_pos, label)
        else:
            _route_lane(target_id, source_pos, target_pos, label)

    _BRANCH_KIND_RANK = {"decision": 3, "continue": 2, "end": 1, "loop": 0}

    def _classify_branch(edge: DiagramEdge, decision_id: str) -> tuple[str, list[str]] | None:
        """Follows a decision branch forward through plain nodes, as far as
        it safely can. Returns ("end", [ids]) if it dead-ends (every id,
        INCLUDING the terminal one, is a new box to draw), ("loop", [ids])
        if it reaches a node ALREADY drawn earlier on the page (a backward
        loop - e.g. "escalate and reassess"), ("continue", [ids]) if it
        reaches a node that something ELSE also points to (in-degree >= 2)
        and that ISN'T drawn yet (a forward reconvergence, e.g. a shared
        "End" fed by more than one branch), or ("decision", [ids]) if it
        runs into ANOTHER decision - a chain of decisions is exactly as
        supported as a single one; that next decision is left for the
        outer walk to give its own full treatment in its own turn, never
        drawn here. For "loop"/"continue"/"decision" the last id is the
        target itself (omitted for "decision", since that decision draws
        itself); every id before it is a new box. None only for a
        genuinely malformed spec (a dangling edge, or a branch long enough
        to revisit one of its own nodes - impossible in a real DAG; the
        bound below exists purely so that can never spin forever)."""
        chain: list[str] = []
        current = edge.target
        for _ in range(len(by_id) + 1):
            if current not in by_id or current in chain:
                return None
            if current in visited:
                return ("loop", chain + [current])
            if role_of(current) == "decision":
                return ("decision", chain)
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

    def draw_decision_fallback(nid: str, decision_branches: list[DiagramEdge], y: float) -> float:
        """A decision too tangled for the clean spine/side split (see the
        docstring): draws the diamond and just its two immediate branch
        targets - the first on the spine, the second beside it - then lets
        the outer walk's own predecessor lookup and the final sweep pick
        up whatever comes after either one. Returns the y to resume at."""
        connect_real(nid, x_center, y)
        height = draw_main_node(nid, y)
        decision_pos = positions[nid]
        decision_bottom = y + height
        branch_y = decision_bottom + gap
        bottoms = [branch_y]
        for branch_index, edge in enumerate(decision_branches):
            if edge.target not in by_id or edge.target in visited:
                continue
            target = by_id[edge.target]
            target_role = role_of(edge.target)
            on_spine = branch_index == 0
            col_cx = x_center if on_spine else side_cx
            col_x = (x_center - process_w / 2) if on_spine else side_left_x
            col_w = process_w if on_spine else side_w
            if target_role in ("start", "end"):
                elements.append(
                    _draw_pill(target, cx=col_cx, cy=branch_y + _PILL_H / 2, theme=theme, colors=_FLOW_ROLE_COLORS[target_role])
                )
                target_h, target_w = _PILL_H, _PILL_W
            else:
                svg, target_h = _node_block(
                    target, x=col_x, y=branch_y, width=col_w, theme=theme, colors=_FLOW_ROLE_COLORS["process"]
                )
                elements.append(svg)
                target_w = col_w
            positions[edge.target] = (col_cx, branch_y, branch_y + target_h, target_w)
            visited.add(edge.target)
            if on_spine:
                draw_arrow_down(x_center, decision_bottom, branch_y, edge.label)
            else:
                _decision_to_side_connector(edge.target, decision_pos, positions[edge.target], edge.label)
            drawn_edges.add((nid, edge.target))
            bottoms.append(branch_y + target_h)
        return max(bottoms) + gap

    order = [n.id for n in nodes if n.id]
    idx = 0
    while idx < len(order):
        nid = order[idx]
        if nid in visited:
            idx += 1
            continue
        role = role_of(nid)
        if role != "decision":
            connect_real(nid, x_center, y)
            height = draw_main_node(nid, y)
            y = y + height + gap
            idx += 1
            continue

        decision_branches = out_edges.get(nid, [])[:2]
        results = [_classify_branch(e, nid) for e in decision_branches] if len(decision_branches) == 2 else []
        usable = len(decision_branches) == 2 and all(r is not None for r in results)
        usable = usable and all(e.target not in visited for e in decision_branches)
        side_i = 0
        side_new_ids: list[str] = []
        if usable:
            ranked = sorted(range(2), key=lambda i: _BRANCH_KIND_RANK[results[i][0]] * 1000 + len(results[i][1]))
            side_i = ranked[0]
            side_kind, side_chain = results[side_i]
            side_new_ids = side_chain if side_kind == "end" else side_chain[:-1]
            # A side branch that itself runs into further branching, or
            # points DIRECTLY at a merge/loop target with no box of its
            # own to anchor the short decision->side connector to, is more
            # tangled than this layout handles cleanly - fall back.
            if side_kind == "decision" or not side_new_ids:
                usable = False

        if not usable:
            y = draw_decision_fallback(nid, decision_branches, y)
            idx += 1
            continue

        side_edge = decision_branches[side_i]
        side_kind, side_chain = results[side_i]

        connect_real(nid, x_center, y)
        height = draw_main_node(nid, y)
        decision_pos = positions[nid]
        decision_bottom = y + height
        branch_y = decision_bottom + gap

        first_side_pos, _side_bottom = draw_side_chain(branch_y, side_chain, side_kind)
        assert first_side_pos is not None  # guaranteed by the `side_new_ids` guard above
        _decision_to_side_connector(side_edge.target, decision_pos, first_side_pos, side_edge.label)
        drawn_edges.add((nid, side_edge.target))

        # The centre branch is deliberately NOT drawn here: its first hop
        # (and everything after it, including a second decision) is picked
        # up by this same outer walk in the spec's own node order, via
        # `connect_real` finding this decision as its real predecessor -
        # exactly the ordinary plain-node path above, giving it a plain
        # straight-down arrow for free, on the spine's own shared column.
        y = branch_y
        idx += 1

    # Final sweep: any real edge whose source AND target both ended up
    # drawn, but whose arrow no specialised path above accounted for -
    # every side branch's own deferred edge (to a not-yet-drawn merge
    # target, or an already-drawn loop target) lands here, plus the rare
    # decision-fallback leftover. Routed through its own dedicated lane
    # (see `_route_lane`), entering the target from the side, never the
    # top - so a node fed by both the spine and a side branch always shows
    # two arrowheads on two different sides of itself, never the same spot
    # twice (a real reported case: a loop-back and the spine's own
    # incoming arrow both entering at a node's top, indistinguishable).
    for edge in edges:
        if (edge.source, edge.target) in drawn_edges:
            continue
        source_pos = positions.get(edge.source)
        target_pos = positions.get(edge.target)
        if source_pos is None or target_pos is None:
            continue
        source_cx, _source_top, source_bottom, _source_w = source_pos
        target_cx, target_top, _target_bottom, _target_w = target_pos
        if abs(source_cx - target_cx) < 0.5:
            draw_arrow_down(target_cx, source_bottom, target_top, edge.label)
        else:
            _route_cross_column(edge.target, source_pos, target_pos, edge.label)
        drawn_edges.add((edge.source, edge.target))

    # Lanes are anchored to this function's OWN base canvas_w (see
    # `_next_lane_x`) - if more ended up used than that width comfortably
    # fits, the canvas (and the caller's viewBox) is simply widened here;
    # every lane's own x only ever depended on its index, never on the
    # final width, so nothing already drawn needs to move.
    if _lane_count[0] > 0:
        rightmost_lane_x = _lane_start_x + (_lane_count[0] - 1) * _LANE_GAP
        padding = 116.0 if _lane_label_used[0] else 16.0
        canvas_w = max(canvas_w, rightmost_lane_x + padding)

    total_height = y - top
    return elements, canvas_w, total_height


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


def _focal_node(nodes: list[DiagramNode], edges: list[DiagramEdge]) -> DiagramNode:
    """The node the diagram should actually centre on - chosen by real
    connectivity (how many edges touch it), never by declared `level` or
    list position alone. A low-degree node forced into the focal role is a
    visually poor centre: every edge among the OTHER, better-connected
    nodes still has to bow around the ring to reach its real neighbours
    instead of radiating cleanly from the centre - a real, confirmed case:
    a 7-satellite concept map where the `level == 0` node had only 1 real
    connection while 6 of its satellites were actually wired to each other
    in 3 unrelated pairs, producing messy, crossing bulged chords. Falls
    back to the old level/position rule only when it's at least as
    connected as the degree-based pick (e.g. no edges at all, or a genuine
    tie), so a well-formed single-hub spec renders exactly as before."""
    degree: dict[str, int] = {}
    for edge in edges:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    default = next((n for n in nodes if n.level == 0), nodes[0])
    best = max(nodes, key=lambda n: degree.get(n.id, 0))
    return best if degree.get(best.id, 0) > degree.get(default.id, 0) else default


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

    focal = _focal_node(nodes, edges)
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

    # Relationship lines first, so node boxes sit cleanly on top of their
    # stub ends - but edge LABELS are collected separately and painted last
    # (see below), never interleaved with the lines: a label sitting near a
    # box edge must stay fully legible even when its pill's estimated
    # position runs close to that box, which painting it before the boxes
    # could clip (a real, confirmed case: a label's leading character
    # vanished under a node box painted on top of it).
    edge_labels: list[str] = []
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
        edge_labels.append(_edge_label(mid[0], mid[1], edge.label, theme, max_width=CONCEPT_MAP_LABEL_MAX_WIDTH))

    focal_svg, _ = _node_block(
        focal, x=cx - focal_w / 2, y=cy - focal_h / 2, width=focal_w, theme=theme, emphasis=True
    )
    elements.append(focal_svg)
    for index, sat in enumerate(satellites):
        sx, sy, w, h = positions[sat_ids[index]]
        svg, _ = _node_block(sat, x=sx - w / 2, y=sy - h / 2, width=w, theme=theme, badge=str(index + 1))
        elements.append(svg)
    elements.extend(edge_labels)

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
    colors: tuple[str, str] | None = None, base_size: float = 12.5, min_size: float = 9.5,
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
            color=theme.text_color, theme=theme, base_size=base_size, min_size=min_size,
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
    # Multi-state ("before/after") rendering is permanently retired - see
    # app.agents.prompts' schematic guidance, which no longer tells the
    # writer to populate `states` at all. A real, confirmed case: two
    # stacked panels repeating the same cramped shape set, with real
    # overlap bugs in the anchor layout on top of it, read as a genuinely
    # unwanted visual format, not just a bug to patch. Only the FIRST state
    # (or `spec.shapes` directly, the normal single-panel case) is ever
    # rendered now - any stray `states` entry beyond the first (legacy data,
    # or a model that set it anyway despite no prompt encouragement) is
    # silently ignored rather than drawn, so this format can never render
    # again regardless of what a spec happens to contain.
    states = [spec.states[0]] if spec.states else [SchematicState(caption="", shapes=spec.shapes)]
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
        elements, canvas_w, content_h = _layout_flow_chart(spec.nodes, spec.edges, theme=theme, top=top)
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
    # Every layout function in this module emits a fixed, literal marker id
    # ("diagram-arrow", "schematic-relationship-arrow") - harmless for one
    # SVG in isolation, but SVG/HTML ids must be unique within a DOCUMENT,
    # and the frontend inlines potentially many diagram SVGs into the SAME
    # page DOM (see app/render... consumed by frontend/src/components/blocks/
    # ImageBlock.tsx's `dangerouslySetInnerHTML`, one per diagram block on a
    # page). A real, confirmed bug: two diagrams on one page both define
    # id="diagram-arrow", so every marker-end="url(#diagram-arrow)"
    # reference becomes ambiguous once inlined together - a duplicate id is
    # undefined behaviour per spec, and a real browser can silently fail to
    # resolve it, rendering a line with no arrowhead at all, which reads as
    # "not connected to its target" even though the line's own endpoints are
    # mathematically correct. Suffixing every marker id (both the
    # `<marker id="...">` definition and every reference to it) with a
    # random, per-render-call-unique tag closes this for every layout
    # function at once, at the one chokepoint every one of them already
    # funnels through, without threading an id parameter through ~20 call
    # sites individually.
    unique = uuid.uuid4().hex[:8]
    body = body.replace("diagram-arrow", f"diagram-arrow-{unique}")
    body = body.replace("schematic-relationship-arrow", f"schematic-relationship-arrow-{unique}")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}" role="img">'
        f"{doc_title}{style}"
        f'<rect x="0" y="0" width="{canvas_w:.0f}" height="{canvas_h:.0f}" fill="{theme.page_background}"/>'
        f"{title_svg}{body}</svg>"
    )
    return svg.encode("utf-8"), int(round(canvas_w)), int(round(canvas_h))
