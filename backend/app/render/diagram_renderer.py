"""DiagramSpec -> SVG.

Pure and AI-free, the same philosophy as `app.render.html_renderer`: the model
only supplies structured content (nodes/edges/kind); every pixel here is
produced by deterministic code, on the course's own theme colours, so a
diagram always renders crisp labels and never drifts from the brand.
"""

from __future__ import annotations

import contextvars
import math
import re
import uuid
from typing import Callable
from xml.sax.saxutils import escape

from app.render.graph_math import nice_ticks, sample_function
from app.render.textbook_palette import resolve_color_role, stroke_width_for
from app.schemas.diagram import (
    SHAPE_TYPES,
    DiagramEdge,
    DiagramNode,
    DiagramSpec,
    SchematicShape,
    SchematicState,
)
from app.schemas.document import CONTENT_WIDTH
from app.schemas.template import TemplateTheme

# The current render's "increase text size" multiplier (DiagramSpec.
# label_scale, clamped to [1.0, 1.5] - see that field's own docstring) -
# set once, at the top of `render_diagram_svg`, and read by every "owns its
# own box/circle sizing math" primitive below (`_node_block`, `_circle_
# node_size`, `_draw_diamond`, `_draw_process_box`, `_fit_boxed_text`,
# `_anchored_label`, `_edge_label`, `_key_table_rows`, `_ideal_process_
# box_size`, `_ideal_decision_box_size`, `_ideal_fishbone_cause_size`,
# `_draw_attribute_oval`) via `_scaled_font_size()`. A contextvar (not a
# plain module global) specifically so this never leaks across concurrent
# renders - `render_diagram_svg` can be called from several requests at
# once (a course generating many diagrams in parallel), and a plain global
# would let one render's scale silently bleed into another's. Each of
# those primitives applies it EXACTLY ONCE, at its own entry point, to
# whatever nominal font_size it was called with - `_wrap`/`_text`
# themselves stay unscaled (they draw/measure at whatever font_size they
# are handed), so nothing downstream of one of these entry points ever
# double-scales.
_label_scale_ctx: contextvars.ContextVar[float] = contextvars.ContextVar("_diagram_label_scale", default=1.0)


def _scaled_font_size(font_size: float) -> float:
    return font_size * _label_scale_ctx.get()


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
# A ring/wheel node's own label font size (_circle_node's default) -
# smaller than LABEL_SIZE (a per-node circle has less real estate than a
# box), but NOT arbitrarily so: even the smallest possible diagram canvas
# (CANVAS_WIDTH, 880px) scales down to ~0.757x at the page's real
# CONTENT_WIDTH (666px) - confirmed real, measured floor: a 13.0px label
# scales to <10px on the page, under MIN_READABLE_PX
# (app.services.diagram_render_qa) in every case, not just a crowded one.
# 15.0 clears that floor with real margin even in the worst case.
# app.services.diagram_render_qa.readability_issues needs this exact value
# too (not LABEL_SIZE) to correctly judge whether a wide multi-node ring's
# text is still legible once scaled to page width.
RING_LABEL_SIZE = 15.0
KEY_TABLE_LABEL_SIZE = 13.5
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

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _tint(hex_color: str, *, toward_white: float = 0.82) -> str:
    """A light fill tone for a bold `hex_color` stroke - blends toward white
    by `toward_white` (0..1), matching the existing palette's own light-fill/
    dark-stroke convention (e.g. "#bbf7d0" is roughly "#16a34a" blended 85%
    toward white)."""
    text = hex_color.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    blend = lambda c: round(c + (255 - c) * toward_white)  # noqa: E731
    return f"#{blend(r):02x}{blend(g):02x}{blend(b):02x}"


def _node_colors(node: DiagramNode, role: str) -> tuple[str, str]:
    """(fill, stroke) for drawing `node` - Edit Visuals (flowchart editor):
    the node's own `color` override when it's a valid hex, else the role's
    existing fixed _FLOW_ROLE_COLORS pair, byte-identical to before this
    override existed."""
    if _HEX_RE.match(node.color.strip()):
        stroke = node.color.strip()
        return (_tint(stroke), stroke)
    return _FLOW_ROLE_COLORS.get(role, _FLOW_ROLE_COLORS["process"])

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


# ---------------------------------------------------------------------------
# icon badges (smart_art/chevron_process/funnel/honeycomb/puzzle_pieces)
# ---------------------------------------------------------------------------

# Every drawer below is pure geometry (circles/rects/polygons/lines only,
# no text glyph, no emoji/unicode symbol, no external asset) - the same
# reason every OTHER shape in this module is drawn this way (see this
# file's own module docstring): a glyph font falls back inconsistently
# across the editor, the Playwright PDF export and a reader's own device,
# which is exactly the "never garbled, never a font-dependent surprise"
# guarantee this whole renderer exists to give diagram text. Each drawer
# takes a LOCAL coordinate box of roughly -12..12 (a 24x24 "icon grid",
# the same convention a real icon font's glyphs are designed to) and is
# wrapped in a `translate(cx,cy) scale(r/14)` group by `_draw_icon` - so a
# drawer never needs to know its own final size or position.
def _icon_person(color: str) -> str:
    return f'<circle cx="0" cy="-5" r="4.2" fill="{color}"/><path d="M-7,10 Q0,-2 7,10 Z" fill="{color}"/>'


def _icon_people(color: str) -> str:
    return (
        f'<circle cx="-5" cy="-5" r="3.4" fill="{color}"/><path d="M-10,9 Q-5,0 0,9 Z" fill="{color}"/>'
        f'<circle cx="5" cy="-5" r="3.4" fill="{color}"/><path d="M0,9 Q5,0 10,9 Z" fill="{color}"/>'
    )


def _icon_gear(color: str) -> str:
    teeth = []
    for i in range(8):
        angle = math.pi * 2 * i / 8
        x1, y1 = 7.0 * math.cos(angle), 7.0 * math.sin(angle)
        x2, y2 = 10.5 * math.cos(angle), 10.5 * math.sin(angle)
        teeth.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="3.2"/>')
    return "".join(teeth) + f'<circle cx="0" cy="0" r="6.5" fill="none" stroke="{color}" stroke-width="2.6"/>'


def _icon_lock(color: str) -> str:
    return (
        f'<path d="M-5,-2 L-5,-5 A5,5 0 0 1 5,-5 L5,-2" fill="none" stroke="{color}" stroke-width="2.4"/>'
        f'<rect x="-7" y="-2" width="14" height="11" rx="2" fill="{color}"/>'
    )


def _icon_shield(color: str) -> str:
    return f'<path d="M0,-11 L9,-6 L9,2 Q9,9 0,12 Q-9,9 -9,2 L-9,-6 Z" fill="{color}"/>'


def _icon_chart_bar(color: str) -> str:
    return (
        f'<rect x="-9" y="2" width="5" height="9" fill="{color}"/>'
        f'<rect x="-2" y="-4" width="5" height="15" fill="{color}"/>'
        f'<rect x="5" y="-9" width="5" height="20" fill="{color}"/>'
    )


def _icon_chart_line(color: str) -> str:
    return (
        f'<polyline points="-10,8 -3,0 3,4 10,-8" fill="none" stroke="{color}" stroke-width="2.4" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="10" cy="-8" r="2" fill="{color}"/>'
    )


def _icon_idea(color: str) -> str:
    return (
        f'<circle cx="0" cy="-3" r="7" fill="{color}"/>'
        f'<rect x="-3.5" y="5" width="7" height="4" rx="1.2" fill="{color}"/>'
    )


def _icon_check(color: str) -> str:
    return f'<polyline points="-7,0 -2,6 8,-7" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'


def _icon_warning(color: str) -> str:
    # The exclamation mark is cut OUT of the filled triangle (even-odd
    # fill) rather than drawn on top in a second, hardcoded colour - any
    # fixed colour there would turn invisible against a same-coloured icon
    # (confirmed: white-on-white when this draws as a white icon on an
    # accent-coloured badge, this module's own default).
    return (
        f'<path fill-rule="evenodd" fill="{color}" d="M0,-11 L11,9 L-11,9 Z '
        f"M-1.3,-4 L1.3,-4 L0.9,3 L-0.9,3 Z "
        f'M-1.5,5.5 A1.5,1.5 0 1 0 1.5,5.5 A1.5,1.5 0 1 0 -1.5,5.5 Z"/>'
    )


def _icon_clock(color: str) -> str:
    return (
        f'<circle cx="0" cy="0" r="10" fill="none" stroke="{color}" stroke-width="2.4"/>'
        f'<line x1="0" y1="0" x2="0" y2="-6" stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>'
        f'<line x1="0" y1="0" x2="4.5" y2="2" stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>'
    )


def _icon_target(color: str) -> str:
    return (
        f'<circle cx="0" cy="0" r="10" fill="none" stroke="{color}" stroke-width="2.2"/>'
        f'<circle cx="0" cy="0" r="6" fill="none" stroke="{color}" stroke-width="2.2"/>'
        f'<circle cx="0" cy="0" r="2" fill="{color}"/>'
    )


def _icon_arrow(color: str) -> str:
    return f'<path d="M-9,-6 L3,-6 L3,-10 L11,0 L3,10 L3,6 L-9,6 Z" fill="{color}"/>'


def _icon_document(color: str) -> str:
    # The two text lines are CUT OUT of the filled page shape (even-odd
    # fill) rather than drawn on top in a second, hardcoded colour - see
    # `_icon_warning`'s own comment on why a fixed overlay colour goes
    # invisible against a same-coloured icon. The folded top-right corner
    # is already absent from the outer path itself (the diagonal from
    # (3,-10) to (7,-6) cuts it off), no separate cutout needed for that.
    return (
        f'<path fill-rule="evenodd" fill="{color}" d="M-7,-10 L3,-10 L7,-6 L7,10 L-7,10 Z '
        f"M-4,-0.7 L4,-0.7 L4,0.7 L-4,0.7 Z "
        f'M-4,3.3 L4,3.3 L4,4.7 L-4,4.7 Z"/>'
    )


def _icon_database(color: str) -> str:
    return (
        f'<rect x="-8" y="-7" width="16" height="14" fill="{color}"/>'
        f'<ellipse cx="0" cy="7" rx="8" ry="3" fill="{color}"/>'
        f'<ellipse cx="0" cy="-7" rx="8" ry="3" fill="{color}"/>'
    )


def _icon_cloud(color: str) -> str:
    return (
        f'<circle cx="-5" cy="2" r="5.5" fill="{color}"/><circle cx="3" cy="-1" r="7" fill="{color}"/>'
        f'<circle cx="9" cy="3" r="4.5" fill="{color}"/><rect x="-9" y="2" width="20" height="6" rx="3" fill="{color}"/>'
    )


def _icon_globe(color: str) -> str:
    return (
        f'<circle cx="0" cy="0" r="10" fill="none" stroke="{color}" stroke-width="2.2"/>'
        f'<ellipse cx="0" cy="0" rx="4.2" ry="10" fill="none" stroke="{color}" stroke-width="1.8"/>'
        f'<line x1="-10" y1="0" x2="10" y2="0" stroke="{color}" stroke-width="1.8"/>'
    )


def _icon_mail(color: str) -> str:
    # The flap is CUT OUT of the filled envelope (even-odd fill), same
    # reasoning as `_icon_warning`/`_icon_document` above.
    return (
        f'<path fill-rule="evenodd" fill="{color}" d="M-10,-6 L10,-6 L10,7 L-10,7 Z '
        f'M-10,-6 L0,2 L10,-6 Z"/>'
    )


def _icon_search(color: str) -> str:
    return (
        f'<circle cx="-1.5" cy="-1.5" r="6.5" fill="none" stroke="{color}" stroke-width="2.4"/>'
        f'<line x1="3.5" y1="3.5" x2="10" y2="10" stroke="{color}" stroke-width="2.8" stroke-linecap="round"/>'
    )


def _icon_flag(color: str) -> str:
    return (
        f'<line x1="-6" y1="-10" x2="-6" y2="11" stroke="{color}" stroke-width="2.2"/>'
        f'<path d="M-6,-9 L9,-5 L-6,-1 Z" fill="{color}"/>'
    )


def _icon_star(color: str) -> str:
    points = []
    for i in range(10):
        radius = 10.5 if i % 2 == 0 else 4.4
        angle = math.pi / 2 * 3 + math.pi * i / 5
        points.append(f"{radius * math.cos(angle):.1f},{-radius * math.sin(angle):.1f}")
    return f'<polygon points="{" ".join(points)}" fill="{color}"/>'


def _icon_money(color: str) -> str:
    return (
        f'<circle cx="0" cy="0" r="10.5" fill="none" stroke="{color}" stroke-width="2.2"/>'
        f'<line x1="0" y1="-6" x2="0" y2="6" stroke="{color}" stroke-width="2"/>'
        f'<path d="M-3.5,-3.5 Q4,-3.5 4,-0.5 Q4,2.5 -3.5,2.5 Q4,2.5 4,5.5 Q4,8.5 -3.5,8.5" '
        f'fill="none" stroke="{color}" stroke-width="1.8" transform="translate(0,-2.5) scale(0.8)"/>'
    )


def _icon_calendar(color: str) -> str:
    return (
        f'<rect x="-9" y="-7" width="18" height="17" rx="1.5" fill="{color}"/>'
        f'<rect x="-9" y="-7" width="18" height="5" fill="#ffffff" opacity="0.4"/>'
        f'<line x1="-5" y1="-10" x2="-5" y2="-5" stroke="{color}" stroke-width="2" stroke-linecap="round"/>'
        f'<line x1="5" y1="-10" x2="5" y2="-5" stroke="{color}" stroke-width="2" stroke-linecap="round"/>'
    )


def _icon_link(color: str) -> str:
    return (
        f'<rect x="-10" y="-4" width="11" height="8" rx="4" fill="none" stroke="{color}" stroke-width="2.4" '
        f'transform="rotate(-30)"/>'
        f'<rect x="-1" y="-4" width="11" height="8" rx="4" fill="none" stroke="{color}" stroke-width="2.4" '
        f'transform="rotate(-30)"/>'
    )


# Keyed by every lowercased keyword a diagram spec's `node.icon` may use -
# several synonyms per concept (see DIAGRAM_SYSTEM's own icon guidance)
# since the model picking "people" vs "team" vs "users" for the same idea
# is exactly the kind of near-miss a strict single-keyword match would
# silently drop (drawing no icon at all, never an error - see
# `_draw_icon`'s own docstring).
_ICON_DRAWERS: dict[str, Callable[[str], str]] = {
    "person": _icon_person, "user": _icon_person, "individual": _icon_person,
    "people": _icon_people, "team": _icon_people, "users": _icon_people, "group": _icon_people,
    "gear": _icon_gear, "settings": _icon_gear, "process": _icon_gear, "config": _icon_gear, "configuration": _icon_gear,
    "lock": _icon_lock, "security": _icon_lock, "password": _icon_lock, "privacy": _icon_lock,
    "shield": _icon_shield, "protection": _icon_shield, "defense": _icon_shield, "safety": _icon_shield,
    "chart": _icon_chart_bar, "chart_bar": _icon_chart_bar, "analytics": _icon_chart_bar, "data": _icon_chart_bar, "metrics": _icon_chart_bar,
    "chart_line": _icon_chart_line, "growth": _icon_chart_line, "trend": _icon_chart_line, "progress": _icon_chart_line,
    "idea": _icon_idea, "lightbulb": _icon_idea, "insight": _icon_idea, "innovation": _icon_idea,
    "check": _icon_check, "success": _icon_check, "complete": _icon_check, "done": _icon_check, "quality": _icon_check,
    "warning": _icon_warning, "alert": _icon_warning, "risk": _icon_warning, "caution": _icon_warning, "issue": _icon_warning,
    "clock": _icon_clock, "time": _icon_clock, "schedule": _icon_clock, "speed": _icon_clock,
    "target": _icon_target, "goal": _icon_target, "objective": _icon_target, "focus": _icon_target,
    "arrow": _icon_arrow, "direction": _icon_arrow, "next": _icon_arrow,
    "document": _icon_document, "file": _icon_document, "report": _icon_document, "policy": _icon_document,
    "database": _icon_database, "storage": _icon_database, "data_store": _icon_database,
    "cloud": _icon_cloud, "network": _icon_cloud, "infrastructure": _icon_cloud,
    "globe": _icon_globe, "world": _icon_globe, "global": _icon_globe,
    "mail": _icon_mail, "email": _icon_mail, "message": _icon_mail, "communication": _icon_mail,
    "search": _icon_search, "research": _icon_search, "discover": _icon_search, "analysis": _icon_search,
    "flag": _icon_flag, "milestone": _icon_flag, "start": _icon_flag,
    "star": _icon_star, "quality_star": _icon_star, "premium": _icon_star, "featured": _icon_star,
    "money": _icon_money, "cost": _icon_money, "budget": _icon_money, "finance": _icon_money, "revenue": _icon_money,
    "calendar": _icon_calendar, "date": _icon_calendar, "plan": _icon_calendar, "planning": _icon_calendar,
    "link": _icon_link, "connection": _icon_link, "integration": _icon_link,
}


def _draw_icon(icon: str, *, cx: float, cy: float, r: float, color: str = "#ffffff") -> str:
    """One pictogram, centred at (cx, cy), scaled to roughly fit radius
    `r`. `icon` is matched case/whitespace-insensitively against
    `_ICON_DRAWERS` - an unrecognised or blank value draws nothing (""),
    never an error and never a placeholder glyph that would misrepresent
    what the model actually asked for."""
    drawer = _ICON_DRAWERS.get(icon.strip().lower().replace(" ", "_").replace("-", "_"))
    if drawer is None:
        return ""
    scale = r / 14.0
    # class="diagram-icon" marks this subtree as decorative glyph artwork
    # in small LOCAL coordinates - app.services.diagram_render_qa's edge/
    # node geometry scan (which has no notion of SVG `transform`) relies on
    # this exact class name to skip it, never misreading an icon's own
    # path/line coordinates as absolute canvas geometry.
    return (
        f'<g class="diagram-icon" transform="translate({cx:.1f},{cy:.1f}) scale({scale:.3f})">'
        f"{drawer(color)}</g>"
    )


# A node's icon badge, when present - the SAME visual language across every
# kind that calls this (smart_art, chevron_process, funnel, honeycomb,
# puzzle_pieces): a solid accent-coloured circle with the pictogram drawn
# in white inside it, same proportions a real presentation-software
# SmartArt "icon in a circle" layout uses.
_ICON_BADGE_RADIUS = 22.0


def _icon_badge(node: DiagramNode, *, cx: float, cy: float, theme: TemplateTheme, color: str | None = None) -> str:
    icon_svg = _draw_icon(node.icon, cx=cx, cy=cy, r=_ICON_BADGE_RADIUS * 0.56, color="#ffffff")
    if not icon_svg:
        return ""
    fill = color or theme.accent_color
    return (
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{_ICON_BADGE_RADIUS:.1f}" fill="{fill}"/>'
        f"{icon_svg}"
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
    icon_badge: bool = False,
    icon_color: str | None = None,
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
    "pill" - a fully-rounded stadium shape (flow_chart's start/end nodes).

    `icon_badge`, when true AND `node.icon` resolves to a known pictogram
    (see `_ICON_DRAWERS`), draws that pictogram in a bigger accent-coloured
    circle in place of the small numbered badge - the "icon in a circle"
    look real presentation-software SmartArt uses (smart_art/
    chevron_process callers only; every other kind leaves this False and
    keeps today's numbered-badge/no-badge look unchanged). Falls back to
    the ordinary numbered `badge` when the node's icon is blank or
    unrecognised, so a diagram never silently loses its step numbering
    just because the model left `icon` empty on one node."""
    label_size = _scaled_font_size(LABEL_SIZE)
    detail_size = _scaled_font_size(DETAIL_SIZE)
    has_icon = icon_badge and bool(_ICON_DRAWERS.get(node.icon.strip().lower().replace(" ", "_").replace("-", "_")))
    reserved_w = (2 * _ICON_BADGE_RADIUS + 14.0) if has_icon else (28.0 if badge else 0.0)
    inner_w = width - 2 * BOX_PADDING - reserved_w
    text_x = x + BOX_PADDING + reserved_w
    # Unbounded (999, not a real cap): the box below sizes itself from
    # however many lines come back, so capping here only ever threw away
    # real content rather than preventing overflow - a confirmed bug that
    # every other `_wrap` caller in this file (e.g. _layout_balance_comparison)
    # already avoids the same way. See this function's _layout_comparison
    # caller for the symptom (a comparison box's detail text cut off mid-
    # sentence with a trailing "…" even though the box had room to grow).
    label_lines = _wrap(node.label, font_size=label_size, width=inner_w, max_lines=999, bold=True) or ["—"]
    detail_lines = _wrap(node.detail, font_size=detail_size, width=inner_w, max_lines=999)

    # Short content stuck to the left edge of a box far wider than it needs
    # reads as broken, not as a paragraph - a real, confirmed complaint on
    # a wide data_flow_diagram entity box ("Prerequisites"), and again on a
    # data_flow_diagram STORE box carrying both a label and a short one-
    # line detail ("Velocity gradient" / "2x2 matrix icon (...)") inside a
    # box spanning the full canvas width, which the original "only when
    # there's no detail line at all" rule left stuck left with most of the
    # box empty. The real distinguishing test is how much of the box the
    # content actually NEEDS, not whether a detail line exists - a short
    # few-line block that fills well under half the available width still
    # reads as a header/caption, however many of those lines there are; a
    # genuinely long wrapped paragraph (where most of the width IS used)
    # still reads better left-aligned, exactly as before.
    total_lines = len(label_lines) + len(detail_lines)
    widest_line_w = max(
        [len(line) * label_size * _BOLD_RATIO for line in label_lines]
        + [len(line) * detail_size * _SANS_RATIO for line in detail_lines],
        default=0.0,
    )
    center_block = reserved_w == 0.0 and total_lines <= 3 and widest_line_w < inner_w * 0.55
    label_anchor = "middle" if center_block else "start"
    label_x = (x + width / 2) if center_block else text_x
    content_top = y + BOX_PADDING
    label_svg, label_h = _text(
        label_x,
        content_top + label_size,
        label_lines,
        font_size=label_size,
        weight=600,
        color=theme.text_color,
        font_family=theme.font_family,
        anchor=label_anchor,
    )
    bottom = content_top + label_h
    detail_svg = ""
    if detail_lines:
        bottom += 4
        detail_svg, detail_h = _text(
            label_x if center_block else text_x,
            bottom + detail_size * 0.9,
            detail_lines,
            font_size=detail_size,
            weight=400,
            color=theme.muted_color,
            font_family=theme.font_family,
            anchor=label_anchor,
        )
        bottom += detail_h

    height = max(bottom - y + BOX_PADDING, 2 * BOX_PADDING + label_size + 6)

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
    if has_icon:
        icon_cx = x + BOX_PADDING + _ICON_BADGE_RADIUS
        icon_cy = y + height / 2
        parts.append(_icon_badge(node, cx=icon_cx, cy=icon_cy, theme=theme, color=icon_color))
    elif badge is not None:
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


def _edge_label_pill_width(text: str, *, max_width: float = EDGE_LABEL_MAX_WIDTH) -> float:
    """The real pixel width `_edge_label` will draw this exact text's pill
    at - extracted so a caller can reserve layout space based on what a
    label ACTUALLY needs (a real, confirmed case: `_layout_concept_wheel`
    reserving a flat worst-case gap on every spoke, regardless of that
    diagram's own label lengths, pushed several ordinary short-label
    diagrams' canvases wide enough to fail the readability floor that a
    tighter, label-driven gap would have cleared easily). Returns 0.0 for
    blank text - the same "nothing to draw" contract `_edge_label` itself
    uses, so a caller can tell "no label, use the plain default gap" from
    "a real but short label, a small gap suffices" without duplicating
    this function's own blank-check."""
    text = text.strip()
    if not text:
        return 0.0
    font_size = _scaled_font_size(EDGE_LABEL_FONT_SIZE)
    inner_w = max_width - 16.0
    lines = _wrap(text, font_size=font_size, width=inner_w, max_lines=2)
    if not lines:
        return 0.0
    longest = max(len(line) for line in lines)
    return min(max(longest * font_size * _SANS_RATIO + 14, 40.0), max_width)


def _edge_label(x: float, y: float, text: str, theme: TemplateTheme, *, max_width: float = EDGE_LABEL_MAX_WIDTH) -> str:
    """A small pill sitting on an edge, naming the relationship it
    represents. Wraps to at most 2 lines instead of overflowing its pill -
    same visual style as the single-line version when text is short, just
    taller when it isn't."""
    text = text.strip()
    if not text:
        return ""
    font_size = _scaled_font_size(EDGE_LABEL_FONT_SIZE)
    line_gap = _scaled_font_size(EDGE_LABEL_LINE_GAP)
    base_height = _scaled_font_size(18.0)
    inner_w = max_width - 16.0
    lines = _wrap(text, font_size=font_size, width=inner_w, max_lines=2)
    if not lines:
        return ""
    # Same calibration _wrap already uses for every node box (confirmed
    # against real browser rendering - see _wrap's own docstring), not an
    # independent estimate: a mismatched ratio here left label pills ~27%
    # narrower than their real text, so the text overflowed the pill and
    # got clipped by whichever node box painted over it afterward (a real,
    # confirmed case: "Charge <-> Queue item" rendered as "harge <-> Queue
    # item" in an exported PDF).
    width = _edge_label_pill_width(text, max_width=max_width)
    height = base_height if len(lines) == 1 else base_height + line_gap
    rect = (
        f'<rect class="diagram-edge-label" x="{x - width / 2:.1f}" y="{y - height / 2:.1f}" '
        f'width="{width:.1f}" height="{height:.1f}" rx="9" fill="{theme.page_background}" '
        f'stroke="{theme.border_color}"/>'
    )
    start_y = y - (len(lines) - 1) * line_gap / 2 + 4
    spans = [
        f'<tspan x="{x:.1f}" dy="{0.0 if i == 0 else line_gap:.1f}">{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    ]
    text_svg = (
        f'<text x="{x:.1f}" y="{start_y:.1f}" font-size="{font_size:.1f}" fill="{theme.muted_color}" '
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
    icon_badge: bool = False,
) -> tuple[list[str], float]:
    """Stacked boxes, numbered. Connected kinds get an arrow + optional edge
    label. `icon_badge` (smart_art only - see render_diagram_svg's own
    dispatch) draws each node's own `node.icon` pictogram, cycling through
    the theme's own palette per node (the same "each item gets its own
    accent colour" look a real icon-badge SmartArt list uses) instead of
    the plain numbered badge every other caller of this function keeps."""
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
    palette = _cycling_palette(theme)

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
            node, x=x, y=y, width=box_w, theme=theme, badge=str(index + 1),
            icon_badge=icon_badge, icon_color=palette[index % len(palette)][1] if icon_badge else None,
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
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float,
    gap_scale: float = 1.0,
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
        # with two paths forward IS a decision, whatever it's labelled) and
        # takes priority over any explicit override below: the branch-layout
        # code that follows assumes a 2-out-edge node is drawn as a decision,
        # so letting an override turn that off would silently break it.
        if out_degree.get(nid, 0) >= 2:
            return "decision"
        # Edit Visuals (flowchart editor): an explicit per-node `role`
        # override (set by a person in the editor, e.g. a node literally
        # labelled "Done" that the auto-detection below wouldn't catch)
        # takes the next priority, before falling back to the original
        # structural/label-based auto-detection.
        override = by_id[nid].role.strip().lower()
        if override in ("start", "process", "end"):
            return override
        # Start/end deliberately is NOT structural, unlike an earlier version
        # of this function: a node with no incoming edge isn't necessarily a
        # procedure's "Start" - it's just as often the first of two plain
        # things being related (an eye's "Cornea" pointing to its "Lens"),
        # which still needs its own visible `detail` line, not a pill that
        # can only show a short centred label. Matching the label itself
        # (as the reference flowcharts this mirrors always explicitly do)
        # only fires the pill styling when the content actually calls for it.
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

    # gap_scale (see render_diagram_svg_fitting) compacts ONLY this vertical
    # spine gap when a generated diagram needs to fit a specific page slot -
    # box heights, font sizes and horizontal spacing (col_gap, lane gaps)
    # are deliberately untouched, so node text and chart proportions never
    # shrink, only the breathing room between consecutive steps does.
    gap = 50.0 * gap_scale
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
                    colors=_node_colors(node, "decision"), base_size=_DIAMOND_LABEL_SIZE, min_size=_DIAMOND_LABEL_SIZE,
                )
            )
            positions[nid] = (x_center, y, y + dh, dw)
            return dh
        if role in ("start", "end"):
            elements.append(_draw_pill(node, cx=x_center, cy=y + _PILL_H / 2, theme=theme, colors=_node_colors(node, role)))
            positions[nid] = (x_center, y, y + _PILL_H, _PILL_W)
            return _PILL_H
        svg, height = _node_block(
            node, x=x_center - process_w / 2, y=y, width=process_w, theme=theme, colors=_node_colors(node, "process")
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
                    _draw_pill(hop_node, cx=side_cx, cy=by_y + _PILL_H / 2, theme=theme, colors=_node_colors(hop_node, hop_role))
                )
                hop_h, hop_w = _PILL_H, _PILL_W
            else:
                svg, hop_h = _node_block(
                    hop_node, x=side_left_x, y=by_y, width=side_w, theme=theme, colors=_node_colors(hop_node, "process")
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
                    _draw_pill(target, cx=col_cx, cy=branch_y + _PILL_H / 2, theme=theme, colors=_node_colors(target, target_role))
                )
                target_h, target_w = _PILL_H, _PILL_W
            else:
                svg, target_h = _node_block(
                    target, x=col_x, y=branch_y, width=col_w, theme=theme, colors=_node_colors(target, "process")
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
# Bigger than RING_LABEL_SIZE (15.0, the bare floor every other ring/mesh
# kind shares) - a real, confirmed complaint: DFD's own canvas is always
# the fixed CANVAS_WIDTH (it stacks vertically, never grows wider the way
# network_mesh's shared-ring canvas can), so once the page scales it down
# to CONTENT_WIDTH, 15.0px sits right AT the 11px readability floor
# (15.0 * 666/880 ≈ 11.4px) - technically passing, but genuinely hard to
# read at normal viewing size, not just barely-legible-if-you-squint. DFD
# has no canvas-growth risk the way a shared-ring kind does (raising its
# own label size can never cascade into an oversized canvas the way it
# could for network_mesh - see that kind's own font-shrink loop), so it
# can safely sit well above the floor instead of right on top of it.
_DFD_LABEL_SIZE = 18.0


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
            # A process circle used to be drawn at a FIXED `_DFD_PROCESS_R`
            # via `_fit_boxed_text`, which shrank the font to its floor and
            # then still had to character-break mid-word and ellipsis-
            # truncate any label longer than two or three short words (a
            # real, confirmed bug on a generated course: "Student
            # submission (minimal, CI-runnable)" rendered as "Student /
            # submissio / n / (minimal,…"). `_circle_node` is the same
            # "never truncates, grows past its minimum radius instead"
            # shape every ring/mesh node already uses - sizing is computed
            # first so `cy` can be placed from the REAL (possibly grown)
            # radius, not the fixed minimum. `_DFD_LABEL_SIZE` (see its own
            # comment on why this kind can safely sit well above the bare
            # RING_LABEL_SIZE floor other ring kinds share), not the old
            # 13.0 `_fit_boxed_text` base_size - a real, confirmed
            # regression that fix itself introduced: labels stopped
            # truncating but came out too small to read.
            sizing = _circle_node_size(node.label, min_radius=_DFD_PROCESS_R, font_size=_DFD_LABEL_SIZE)
            radius = sizing[1]
            cy = y + radius
            fill, stroke = colors
            svg, radius = _circle_node(
                node.label, cx=x + box_w / 2, cy=cy, theme=theme, number="", fill=fill, stroke=stroke,
                min_radius=_DFD_PROCESS_R, font_size=_DFD_LABEL_SIZE, detail=node.detail, sizing=sizing,
            )
            elements.append(svg)
            height = radius * 2
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

    lane_x: dict[str, float] = {}
    x = MARGIN
    for lane in lanes:
        lane_x[lane] = x
        x += lane_w + lane_gap

    # Node/edge drawing happens FIRST (into its own list) specifically so
    # the lane BACKGROUND bands below can be sized to the real final
    # content height - a real, confirmed gap: this renderer used to draw
    # only a coloured HEADER band at the top of each lane, with nothing
    # marking a lane's own boundary any further down. A reader had no way
    # to tell which lane a node belonged to once they looked below the
    # header row (only inferable from x position) - the defining visual
    # feature of a real swimlane diagram (BPMN-style full-height lane
    # bands) was simply missing.
    positions: dict[str, tuple[float, float, float, float]] = {}
    node_w = lane_w - 20.0
    rows = _swimlane_rows(nodes, edges)
    rows_grouped: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        if node.id:
            rows_grouped.setdefault(rows[node.id], []).append(node)

    node_elements: list[str] = []
    y = body_top
    for row_index in sorted(rows_grouped):
        row_height = 0.0
        for node in rows_grouped[row_index]:
            lane = node.lane.strip() or "General"
            node_x = lane_x[lane] + 10.0
            svg, height = _node_block(node, x=node_x, y=y, width=node_w, theme=theme)
            node_elements.append(svg)
            positions[node.id] = (node_x + node_w / 2, y + height / 2, node_w, height)
            row_height = max(row_height, height)
        y += row_height + 24.0

    edge_elements: list[str] = []
    for edge in edges:
        a = positions.get(edge.source)
        b = positions.get(edge.target)
        if not a or not b:
            continue
        start, end = _shorten_to_box_edge((a[0], a[1]), (b[0], b[1]), w1=a[2], h1=a[3], w2=b[2], h2=b[3])
        edge_elements.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
        )
        if edge.label.strip():
            mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            edge_elements.append(_edge_label(mid[0], mid[1] - 10, edge.label, theme, max_width=110.0))

    max_bottom = y - 24.0 if rows_grouped else body_top
    lane_bottom = max_bottom + 16.0

    # Lane backgrounds drawn BEFORE (so they sit visually behind) the
    # header/nodes/edges - a full-height, very light tint of the same
    # lane colour, with a solid divider line between lanes, so a lane's
    # own extent is obvious at a glance no matter how far down the
    # diagram a reader is looking.
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    for index, lane in enumerate(lanes):
        fill, stroke = _LANE_COLORS[index % len(_LANE_COLORS)]
        lx = lane_x[lane]
        elements.append(
            f'<rect x="{lx:.1f}" y="{top:.1f}" width="{lane_w:.1f}" height="{lane_bottom - top:.1f}" '
            f'fill="{fill}" fill-opacity="0.25" stroke="{stroke}" stroke-opacity="0.5" stroke-width="1"/>'
        )
    for index, lane in enumerate(lanes):
        fill, stroke = _LANE_COLORS[index % len(_LANE_COLORS)]
        lx = lane_x[lane]
        elements.append(
            f'<rect x="{lx:.1f}" y="{top:.1f}" width="{lane_w:.1f}" height="{_LANE_HEADER_H:.1f}" '
            f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        elements.append(
            _fit_boxed_text(
                lx + lane_w / 2, top + _LANE_HEADER_H / 2, lane, width=lane_w * 0.85,
                height=_LANE_HEADER_H * 0.65, color=stroke, theme=theme, base_size=14.5,
            )
        )
    elements.extend(node_elements)
    elements.extend(edge_elements)

    return elements, lane_bottom - top


# --- textbook-style ring/wheel (flow_chart/process/cycle, concept_map) ----
# Replaces the old box-and-arrow look for exactly these kinds (see
# render_diagram_svg's own dispatch) with a clean, numbered-circle diagram
# plus a separate Key table for full explanations - no text ever sits on a
# connector line, and a node label never truncates to "…" (see
# _circle_node's own docstring). hierarchy/comparison/er_diagram/
# data_flow_diagram/swimlane/schematic/graph/smart_art are untouched.

# Strips a "Fig. N: " / "Fig. N.M: " caption prefix (see
# app.services.diagram_service._with_figure_caption) before reusing a
# diagram's own title as the ring layout's CENTER circle label - the
# caption itself is already drawn above the canvas; repeating the "Fig. "
# prefix a second time, cramped into a small circle, is just noise.
_FIGURE_PREFIX_RE = re.compile(r"^Fig\.\s*\d+(?:\.\d+)?:\s*")

_WHEEL_SEGMENT_COLORS = [
    ("#bfdbfe", "#1d4ed8"),  # blue
    ("#bbf7d0", "#16a34a"),  # green
    ("#fde68a", "#b45309"),  # amber
    ("#fbcfe8", "#be185d"),  # pink
    ("#ddd6fe", "#6d28d9"),  # purple
    ("#99f6e4", "#0f766e"),  # teal
]

# --- style-aware cycling palette ------------------------------------------
# One shared (fill, stroke) colour cycle for every kind that just needs "a
# different colour per item, no inherent meaning per colour" (a pyramid's
# layers, a matrix's cells, a wheel/mindmap/network's nodes, a chevron/
# staircase/timeline's steps) - as opposed to a kind whose colours carry
# real MEANING (flow_chart's start/process/decision/end, hierarchy's depth,
# balance_comparison's cost/value red-green) which stay their own fixed
# palette regardless of style, unaffected by any of this.
#
# "default" is _WHEEL_SEGMENT_COLORS above, unchanged - every course
# generated before this system existed keeps producing byte-identical
# colours. "vibrant"/"muted" are new, selectable via
# TemplateTheme.diagram_palette. The vibrant strokes are the validated,
# colourblind-safe 8-hue categorical order from this workspace's own
# dataviz skill (references/palette.md) - picked for real, checked hue
# separation, not eyeballed - paired with a lighter tint of each hue as
# the fill, matching this module's existing "pastel fill + saturated
# stroke" convention; "muted" is a softer, lower-saturation professional
# family in the same fill/stroke-pair shape, for formal/corporate content
# that wants less visual energy than the default or vibrant sets.
_CYCLING_PALETTES: dict[str, list[tuple[str, str]]] = {
    "default": _WHEEL_SEGMENT_COLORS,
    "vibrant": [
        ("#cde2fb", "#2a78d6"),  # blue
        ("#fbdbc9", "#eb6834"),  # orange
        ("#c3f0dc", "#1baf7a"),  # aqua
        ("#fbe6b3", "#c98500"),  # yellow (darker stroke step for text contrast)
        ("#fad7e4", "#e87ba4"),  # magenta
        ("#c2e8c2", "#008300"),  # green
        ("#e1dcf8", "#6250d6"),  # violet
        ("#fbd6d6", "#e34948"),  # red
    ],
    "muted": [
        ("#e2e9f0", "#5b7a9d"),  # dusty blue
        ("#e3ece4", "#6b8f71"),  # sage green
        ("#f0e6d8", "#a67c52"),  # warm taupe
        ("#f2e2d8", "#b76e49"),  # terracotta
        ("#e8e4f2", "#7c6f9e"),  # muted lavender
        ("#dcebeb", "#4f7c7c"),  # slate teal
        ("#f2e0e4", "#a65d6e"),  # dusty rose
    ],
    # The reference deck's own navy/orange/gray/gold/blue family (Infographics_
    # template.pptx - confirmed against its own ppt/theme/theme1.xml AND sampled
    # directly off the rendered slides, both agree: 4472C4 blue, ED7D31 orange,
    # A5A5A5 gray, FFC000 gold, 5B9BD5 light blue, with a darker navy 1F3864 for
    # the deck's own header/anchor colour). Darker "stroke" shades are each
    # colour's own standard ~25-35% PowerPoint shade-down of the theme hue, not
    # guessed - same pastel-fill + saturated-stroke pairing convention every
    # other entry here already uses, so contrast/QA stay correct for free.
    "corporate": [
        ("#d6dde6", "#1f3864"),  # navy
        ("#fbe0cc", "#c55a11"),  # orange
        ("#e7e6e6", "#595959"),  # gray
        ("#ffe699", "#bf8f00"),  # gold
        ("#d9e2f3", "#2e5395"),  # blue
        ("#dce6f2", "#2e75b6"),  # light blue
    ],
}


def _cycling_palette(theme: TemplateTheme) -> list[tuple[str, str]]:
    """The (fill, stroke) colour cycle this course's theme selected for
    generically-coloured diagram shapes - see TemplateTheme.diagram_palette
    and _CYCLING_PALETTES' own module docstring. An unset or unrecognised
    value is "default", never an error - this is a cosmetic preference,
    not a required field."""
    return _CYCLING_PALETTES.get(theme.diagram_palette.strip().lower(), _WHEEL_SEGMENT_COLORS)


def _circle_node_size(
    label: str, *, min_radius: float, font_size: float
) -> tuple[list[str], float]:
    """The wrapped label lines and the radius a circle needs to hold them -
    split out from `_circle_node` so a caller can learn a node's REAL size
    (label text can grow it past `min_radius` - see `_circle_node`'s own
    docstring) before positions are chosen, e.g. to space nodes around a
    ring far enough apart that two full-sized circles never touch (see
    `_layout_ring_process`'s own neighbour-clearance math, the same
    chord-length approach `_layout_concept_map` already uses for its own
    satellites)."""
    font_size = _scaled_font_size(font_size)
    width_budget = min_radius * 1.55
    # max_lines is deliberately generous (not a small cap like _node_block's
    # low ones, the actual source of the banned "…" cutoff) - wrapped
    # first, UNBOUNDED, so the real line count this label needs is known
    # before the circle is ever sized; a label the writer prompt's own
    # 2-4-word guidance didn't catch (unusually long) widens the wrap
    # budget once and re-wraps, so it grows into a wider, shorter block
    # instead of a tall, narrow column of many short lines - never a
    # truncation, in either pass.
    lines = _wrap(label, font_size=font_size, width=width_budget, max_lines=999, bold=True)
    # Widens and re-wraps once a label needs MORE than 3 lines (4+), not
    # only past 4 lines of the OLD ">4" cap - a real, confirmed visual bug:
    # a label that wrapped into exactly 4 cramped, narrow lines (e.g.
    # "Probes (Ch1 blue, Ch2 red)" at the old `min_radius*1.55` width
    # budget) never lost any text, but read as uncomfortably squeezed.
    # Deliberately NOT lowered to ">2" (tried and reverted): a genuinely
    # common 3-line label (e.g. "Market research and competitive
    # analysis") widened unnecessarily under that threshold, inflating a
    # ring/mesh layout's overall canvas width enough to trip the SEPARATE
    # "text becomes unreadably small once the page scales a too-wide
    # diagram down to its column width" check (diagram_render_qa's
    # text_too_small_after_scaling) - confirmed by three real regression
    # tests (radial_mindmap/network_mesh) failing under ">2" that pass
    # under ">3". 3 clean lines is an acceptable, non-cramped wrap; 4+ is
    # the actual cramped case this threshold exists to catch.
    # A label force-broken mid-word (no hyphen, e.g. "Interpolati" / "on")
    # is ALWAYS wrong, however few lines it produced - a real, confirmed
    # case: a 2-3 word circle label whose one long word ("Interpolation",
    # "differentiation") didn't fit `width_budget` wrapped into exactly 3
    # lines (one a broken word fragment), which sailed straight past the
    # `len(lines) > 3` check above as "not cramped" even though it visibly
    # read as a typo. Checking the LONGEST WORD against this budget's own
    # `chars_per_line` (the same formula `_wrap` uses internally) catches
    # this independently of line count and widens before a break like that
    # can happen, rather than only reacting to how many lines came back.
    chars_per_line = max(int(width_budget / (font_size * _BOLD_RATIO)), 6)
    longest_word = max((len(w) for w in label.split()), default=0)
    too_many_lines = len(lines) > 3
    forced_break = longest_word > chars_per_line
    if too_many_lines or forced_break:
        # The force-break case only needs JUST enough width for its one
        # long word, not the broader `sqrt(len(label))` growth below (tuned
        # for the OTHER case - a label with many short words cramped onto
        # too many lines) - a real, confirmed case: applying that broader
        # formula to a single long word in an otherwise normal 2-3 word
        # label ("Notification Service" on a 6-node network_mesh, where
        # every node shares ONE ring radius sized by the single largest
        # node) over-widened the circle enough to blow the mesh's overall
        # canvas past its own safe-readability width, even though barely
        # more width than the word itself needed would have been enough.
        width_budget = max(width_budget, longest_word * font_size * _BOLD_RATIO + 6.0)
        if too_many_lines:
            width_budget = max(width_budget, (len(label) * font_size * _BOLD_RATIO) ** 0.5 * 9.0)
        lines = _wrap(label, font_size=font_size, width=width_budget, max_lines=999, bold=True)
    text_h = len(lines) * font_size * LINE_HEIGHT
    widest_chars = max((len(line) for line in lines), default=0)
    text_w = widest_chars * font_size * _BOLD_RATIO
    radius = max(min_radius, text_h / 1.15 + 24.0, text_w / 1.55 + 18.0)
    return lines, radius


def _circle_node(
    label: str,
    *,
    cx: float,
    cy: float,
    theme: TemplateTheme,
    number: str,
    fill: str,
    stroke: str,
    min_radius: float = 64.0,
    font_size: float = RING_LABEL_SIZE,
    detail: str = "",
    sizing: tuple[list[str], float] | None = None,
) -> tuple[str, float]:
    """A filled circle with a number badge (or none, for a centre/focal
    node - pass `number=""`) and a short label wrapped inside. Never
    truncates: `_wrap` is called with a generous `max_lines` (unlike
    `_node_block`'s low caps, the actual source of the banned "…" cutoff),
    and the circle's own radius grows past `min_radius` to fit however
    many lines of real label text came back, rather than cutting them -
    the writer prompt already asks for a short (2-4 word) label, so this
    almost never needs to grow in practice, but never silently loses text
    when a label runs long anyway. Returns (svg, the radius actually
    used) so a caller positioning other elements around this node (an
    arrow, a neighbour) works from its real size, not just `min_radius`.

    Wrapped in the same `<g class="diagram-node" tabindex="0" role="group">`
    + `<title>` tooltip + `.diagram-box` inner-element convention every
    other node shape in this module already uses (see `_node_block`) - so
    the existing global hover/focus CSS and keyboard-interactivity actually
    apply to a circle too, not just the old rectangle shapes. `detail`
    (full explanation, also shown unabridged in the Key table below) feeds
    the tooltip even though it's never drawn inside the circle itself.

    `sizing`: a caller that already called `_circle_node_size` itself (to
    plan ring/wheel geometry around this node's REAL size before any
    position was chosen - see `_layout_ring_process`/`_layout_concept_wheel`)
    must pass that exact `(lines, radius)` back in here rather than let
    this call re-derive it from `min_radius`. `_circle_node_size`'s wrap
    width budget scales with `min_radius`, so feeding a GROWN radius back
    in as `min_radius` widens the budget again and can wrap into a
    DIFFERENT, even larger circle than the one geometry was planned
    around - a real, measured bug (a focal/centre label long enough to
    grow past its base radius came out larger when actually drawn than
    the size the surrounding ring had been spaced for, overlapping it).
    """
    if sizing is not None:
        # `sizing` came from a caller's own `_circle_node_size(..., font_size=
        # font_size)` call, which scales that SAME nominal `font_size`
        # internally - read `_scaled_font_size` here too (applying the scale
        # to the original nominal value a second time, not re-scaling an
        # already-scaled one) so this function's own label-position math
        # (`label_center_y`, `badge_y` below) agrees with the font the
        # `lines`/`radius` were actually computed at. Passing the already-
        # scaled value into `_circle_node_size` below instead would scale it
        # TWICE.
        lines, radius = sizing
        font_size = _scaled_font_size(font_size)
    else:
        lines, radius = _circle_node_size(label, min_radius=min_radius, font_size=font_size)
        font_size = _scaled_font_size(font_size)

    tooltip = label.strip()
    if detail.strip():
        tooltip = f"{tooltip} — {detail.strip()}" if tooltip else detail.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>'
    )
    label_center_y = cy + font_size * 0.32
    if number:
        label_center_y = cy - font_size * 0.2
        # The badge's gap above the label used to be a fixed `cy -
        # radius*0.46` regardless of how many lines the label actually
        # wrapped onto - a real, confirmed bug: a longer label wrapped
        # onto 3-4 lines pushes its own first line's baseline UP (see
        # `label_top_y` below), while the badge stayed put, so the two
        # crowded together with almost no visible gap. Deriving `badge_y`
        # from the label's own top edge (never above the fixed fraction,
        # only lower when the label needs more room) keeps a constant,
        # real gap regardless of line count.
        label_top_y = label_center_y - (len(lines) - 1) * font_size * LINE_HEIGHT / 2 - font_size * 0.8
        badge_y = min(cy - radius * 0.46, label_top_y - 14.0)
        parts.append(
            f'<text x="{cx:.1f}" y="{badge_y:.1f}" font-size="14" font-weight="800" '
            f'fill="{stroke}" text-anchor="middle" font-family="{escape(theme.font_family)}">'
            f"{escape(number)}</text>"
        )
    label_svg, label_h = _text(
        cx,
        label_center_y - (len(lines) - 1) * font_size * LINE_HEIGHT / 2,
        lines,
        font_size=font_size,
        weight=700,
        color=theme.text_color,
        font_family=theme.font_family,
        anchor="middle",
    )
    parts.append(label_svg)
    group = f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'
    return group, radius


def _curved_connector(
    x1: float, y1: float, x2: float, y2: float, *, cx: float, cy: float, theme: TemplateTheme
) -> str:
    """An arrow from one ring/wheel node to the next, bowed gently outward
    from the ring's own centre (cx, cy) - never a straight line (reads as
    flatter, more "textbook" than `_layout_cycle`'s old straight
    connectors) and never carries a label (the "no text on arrows" rule -
    an edge's own `label`, if the model set one, isn't drawn here at all;
    the Key table below is where any such context belongs instead)."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = mx - cx, my - cy
    dist = max(math.hypot(dx, dy), 1.0)
    # Scales down for a short chord (nodes placed close together, a dense
    # ring) - a fixed 22px bow could swing the curve's midpoint back
    # toward a tightly-packed neighbour it was never meant to approach;
    # capped at 22px so a normal, spacious ring looks exactly as before.
    chord = math.hypot(x2 - x1, y2 - y1)
    bow = min(22.0, chord * 0.18)
    ctrl = (mx + dx / dist * bow, my + dy / dist * bow)
    return (
        f'<path d="M{x1:.1f},{y1:.1f} Q{ctrl[0]:.1f},{ctrl[1]:.1f} {x2:.1f},{y2:.1f}" '
        f'fill="none" stroke="{theme.accent_color}" stroke-width="2.5" marker-end="url(#diagram-arrow)"/>'
    )


def _key_table_rows(
    entries: list[tuple[str, str, str]],
    *,
    top: float,
    width: float,
    theme: TemplateTheme,
    row_gap_scale: float = 1.0,
) -> tuple[list[str], float]:
    """The full explanation for each ring/wheel node, moved OUT of the
    diagram itself into a plain, textbook-style key below it - one row per
    entry, `(number-or-"" , label, detail)`. Every line wraps generously
    (`_wrap`'s own `max_lines` set high) rather than ever truncating -
    the single reason this table exists at all is so a node's full
    explanation has somewhere to go that ISN'T a cramped circle.
    `row_gap_scale` (see _layout_ring_process's own docstring) only
    compacts the whitespace BETWEEN rows - text size and content are
    exactly as legible compacted as not."""
    row_font, detail_font = _scaled_font_size(KEY_TABLE_LABEL_SIZE), _scaled_font_size(12.0)
    elements: list[str] = [
        f'<line x1="{MARGIN:.1f}" y1="{top:.1f}" x2="{MARGIN + width:.1f}" y2="{top:.1f}" '
        f'stroke="{theme.border_color}" stroke-width="1"/>'
    ]
    y = top + 24.0 * row_gap_scale
    for number, label, detail in entries:
        if not label.strip():
            continue
        prefix = f"{number}. " if number else "• "
        label_svg, label_h = _text(
            MARGIN,
            y,
            [f"{prefix}{label.strip()}"],
            font_size=row_font,
            weight=700,
            color=theme.text_color,
            font_family=theme.font_family,
            anchor="start",
        )
        elements.append(label_svg)
        y += label_h + 3.0 * row_gap_scale
        if detail.strip():
            detail_lines = _wrap(detail, font_size=detail_font, width=width - 20.0, max_lines=999)
            detail_svg, detail_h = _text(
                MARGIN + 16.0,
                y,
                detail_lines,
                font_size=detail_font,
                weight=400,
                color=theme.muted_color,
                font_family=theme.font_family,
                anchor="start",
            )
            elements.append(detail_svg)
            y += detail_h + 4.0 * row_gap_scale
        y += 12.0 * row_gap_scale
        elements.append(
            f'<line x1="{MARGIN:.1f}" y1="{y - 6.0:.1f}" x2="{MARGIN + width:.1f}" y2="{y - 6.0:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="0.75" opacity="0.5"/>'
        )
    return elements, y - top


_RING_START_WORDS = {"start", "begin", "initial"}
_RING_END_WORDS = {"end", "stop", "finish", "done"}


def _ring_role_of(node: DiagramNode, *, is_first: bool, is_last: bool, closes_loop: bool) -> str:
    """Which of _FLOW_ROLE_COLORS' fixed roles a ring node is coloured as -
    reused via _node_colors (same function flow_chart's old box renderer
    used), so a node's own explicit `role`/`color` override (Edit Visuals:
    the flowchart editor) still has a real, visible effect on the new ring
    rendering, exactly as it did before. Structural decision-branch
    detection doesn't apply here any more (a ring has no branches - see
    _layout_ring_process's own docstring), so only an explicit override, a
    literal start/end label, or first/last ring position (for a ring that
    doesn't close) ever produce anything other than "process"."""
    override = node.role.strip().lower()
    if override in ("start", "process", "decision", "end"):
        return override
    label = node.label.strip().lower()
    if label in _RING_START_WORDS:
        return "start"
    if label in _RING_END_WORDS:
        return "end"
    if not closes_loop:
        if is_first:
            return "start"
        if is_last:
            return "end"
    return "process"


def _layout_ring_process(
    nodes: list[DiagramNode],
    edges: list[DiagramEdge],
    *,
    theme: TemplateTheme,
    top: float,
    title: str = "",
    gap_scale: float = 1.0,
) -> tuple[list[str], float, float]:
    """Textbook-style numbered ring for flow_chart/process/cycle: each step
    a numbered circle, joined by curved, unlabelled arrows in order. The
    ring CLOSES (an arrow from the last step back to the first) only when
    the spec's own edges already contain that exact connection - a real,
    repeating cycle - and stays open otherwise, which is what correctly
    draws an ordinary non-cyclic numbered sequence with the SAME layout:
    a textbook draws both of these as a ring of numbered steps, the only
    difference being whether the last arrow closes the loop.

    `gap_scale` (see render_diagram_svg_fitting /
    app.course.document.diagram_compaction) only compacts the Key table's
    own row spacing below the ring - the ring itself (node size, spacing,
    font) is never touched by it, same "whitespace only" contract
    `_key_table_rows` documents."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0

    node_radius = 62.0
    # The ring places every node at the SAME radius, so the one real node
    # size that matters for spacing is the LARGEST one any label actually
    # grows to (_circle_node_size, not just min_radius) - sized up front,
    # before any position is chosen. Same chord-length "keep neighbours
    # from touching" math _layout_concept_map already uses for its own
    # satellites: a real, measured bug at high node counts (14, the
    # documented max for a sequential kind) - the old fixed `count * 42.0`
    # formula didn't account for labels growing nodes past min_radius, so
    # neighbouring circles (and the curved connectors between them)
    # crowded into each other.
    max_node_radius = max(
        (_circle_node_size(node.label, min_radius=node_radius, font_size=RING_LABEL_SIZE)[1] for node in nodes),
        default=node_radius,
    )
    neighbour_clearance_radius = (
        (2 * max_node_radius + 80.0) / (2 * math.sin(math.pi / count)) if count >= 2 else 0.0
    )
    # The ring must also clear the CENTRE title circle, not just its own
    # neighbours - a real, measured bug: a long title (e.g. "Induction
    # problem workflow") grows that centre circle well past its 54.0
    # min_radius (see _circle_node_size), and with no term for it here the
    # ring radius stayed fixed at 165.0, letting every surrounding node
    # overlap the oversized centre. _layout_concept_wheel already does the
    # equivalent for its own focal node; this mirrors that.
    center_sizing = _circle_node_size(title.strip(), min_radius=54.0, font_size=13.5) if title.strip() else ([], 0.0)
    ring_radius = max(165.0, neighbour_clearance_radius, center_sizing[1] + max_node_radius + 40.0)
    cx = ring_radius + max_node_radius + 50.0
    cy = top + ring_radius + max_node_radius + 50.0
    canvas_w = max(CANVAS_WIDTH, 2 * cx)

    has_closing_edge = count > 1 and any(
        edge.source == nodes[-1].id and edge.target == nodes[0].id and nodes[-1].id and nodes[0].id
        for edge in edges
    )

    centers: list[tuple[float, float, float]] = []
    circle_svgs: list[str] = []
    # Flower/pinwheel "petal" shapes (reference infographic template's own
    # overlapping-petal cycle style) drawn BEHIND the circles/arrows below -
    # a soft, pale ellipse running from near the centre out to each node,
    # rotated to point along that node's own radius. Deliberately additive
    # and purely decorative: it never touches a single coordinate the
    # circle/arrow placement below already uses (the real, heavily-tuned
    # neighbour-clearance and label-growth sizing math above this loop), so
    # it carries zero risk to that proven geometry - only ADDS a background
    # layer under it. No `class="diagram-box"` (so it's correctly invisible
    # to the overlap/geometry QA, same reasoning _layout_pyramid's own
    # shadow duplicate uses) and no `class="diagram-icon"` either (it's
    # drawn directly in absolute canvas coordinates, not a local icon-glyph
    # box, so the edge/geometry scan already reads it correctly either way).
    petal_svgs: list[str] = []
    for index, node in enumerate(nodes):
        angle = (2 * math.pi * index / count) - math.pi / 2
        nx = cx + ring_radius * math.cos(angle)
        ny = cy + ring_radius * math.sin(angle)
        petal_fill, _petal_stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        mid_x, mid_y = (cx + nx) / 2, (cy + ny) / 2
        petal_rx = ring_radius * 0.62
        petal_ry = max(max_node_radius * 0.78, 36.0)
        angle_deg = math.degrees(angle)
        petal_svgs.append(
            f'<ellipse cx="{mid_x:.1f}" cy="{mid_y:.1f}" rx="{petal_rx:.1f}" ry="{petal_ry:.1f}" '
            f'fill="{petal_fill}" opacity="0.7" transform="rotate({angle_deg:.1f} {mid_x:.1f} {mid_y:.1f})"/>'
        )
        role = _ring_role_of(
            node, is_first=index == 0, is_last=index == count - 1, closes_loop=has_closing_edge
        )
        fill, stroke = _node_colors(node, role)
        svg, used_radius = _circle_node(
            node.label, cx=nx, cy=ny, theme=theme, number=str(index + 1),
            fill=fill, stroke=stroke, min_radius=node_radius, detail=node.detail,
        )
        centers.append((nx, ny, used_radius))
        circle_svgs.append(svg)

    elements = petal_svgs + [_arrow_marker("diagram-arrow", theme.accent_color)]
    arrow_count = count if has_closing_edge else count - 1
    for index in range(max(arrow_count, 0)):
        x1, y1, r1 = centers[index]
        x2, y2, r2 = centers[(index + 1) % count]
        dx, dy = x2 - x1, y2 - y1
        dist = max(math.hypot(dx, dy), 1.0)
        ux, uy = dx / dist, dy / dist
        # A small extra gap (not just the exact node radius) before the
        # curve starts bowing - a real, measured bug: starting exactly ON
        # a (possibly label-grown) node's own boundary let the curve's
        # early bow swing back across that same boundary before it had
        # moved far enough away, clipping the node it had JUST left.
        clearance = 12.0
        elements.append(
            _curved_connector(
                x1 + ux * (r1 + clearance), y1 + uy * (r1 + clearance),
                x2 - ux * (r2 + clearance), y2 - uy * (r2 + clearance),
                cx=cx, cy=cy, theme=theme,
            )
        )
    elements.extend(circle_svgs)

    center_label = title.strip()
    if center_label:
        center_svg, _ = _circle_node(
            center_label, cx=cx, cy=cy, theme=theme, number="",
            fill=theme.surface_color, stroke=theme.border_color, min_radius=54.0, font_size=13.5,
            sizing=center_sizing,
        )
        elements.append(center_svg)

    ring_bottom = cy + ring_radius + max_node_radius + 40.0
    key_gap = 30.0 * gap_scale
    key_elements, key_h = _key_table_rows(
        [(str(i + 1), n.label, n.detail) for i, n in enumerate(nodes)],
        top=ring_bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme, row_gap_scale=gap_scale,
    )
    elements.extend(key_elements)
    return elements, canvas_w, (ring_bottom + key_gap + key_h - top)


def _shorten_to_circle_edge(
    p1: tuple[float, float], p2: tuple[float, float], *, r1: float, r2: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Circle-radius equivalent of `_shorten_to_box_edge` - pulls a
    connecting line back from two circles' CENTRES to their actual edges,
    along the straight line between them, so an arrow (and any label
    placed on the line/curve) touches the circles instead of crossing into
    them. A bowed satellite-to-satellite curve (see `_layout_concept_wheel`)
    shortens along this same straight chord rather than its own curved
    tangent - the same deliberate simplification `_layout_concept_map`
    already used for its box version, carried over unchanged."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist = max(math.hypot(dx, dy), 1.0)
    ux, uy = dx / dist, dy / dist
    start = (p1[0] + ux * r1, p1[1] + uy * r1)
    end = (p2[0] - ux * r2, p2[1] - uy * r2)
    return start, end


def _layout_concept_wheel(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """Textbook-style concept wheel for concept_map: a focal subject at the
    centre, its parts/properties/components as numbered circles around it
    in a fixed, cycling colour palette.

    Root-cause fix (production course audit - a real course had 5 separate
    concept_map diagrams, several with genuine, specifically-labelled
    causal relationships BETWEEN SATELLITES, e.g. "z --increases--> zeff",
    "zeff --increases--> I1" - rendered as a set of isolated circles with
    NO lines/arrows at all, discarding every edge the spec actually
    carried): an earlier version of this function drew no edges
    whatsoever, reasoning that "these belong to X" is conveyed by
    proximity/colour alone and a relationship line is never needed - true
    ONLY for a pure "parts of X" concept map where every edge is a plain,
    undifferentiated focal-to-satellite membership. It breaks completely
    for a satellite-to-satellite edge (no proximity relationship exists
    between two ring positions at all - the information is simply lost)
    and for any edge whose LABEL carries real, specific meaning ("reduces"
    vs "increases" vs "moderates" - indistinguishable by position alone).
    Edges are drawn again now, reusing the exact same straight-spoke /
    outward-bowed-satellite-curve approach the dead `_layout_concept_map`
    (box-node version, nothing currently calls it) already proved - adapted
    to circles via `_shorten_to_circle_edge` instead of `_shorten_to_box_edge`.
    The wheel's own circular layout, colours and key table are unchanged.
    """
    if not nodes:
        return [], CANVAS_WIDTH, 0.0
    focal = _focal_node(nodes, edges)
    satellites = [n for n in nodes if n is not focal]
    count = max(len(satellites), 1)

    sat_radius, focal_radius = 60.0, 76.0
    # Each satellite's OWN real sizing is kept (not just the single overall
    # max) - needed below to shorten that satellite's edges to its actual
    # drawn radius, and passed back into `_circle_node` as `sizing` so it's
    # never re-derived a second time (see that function's own docstring on
    # why re-deriving from a different `min_radius` can grow a mismatched,
    # larger circle than the one geometry was planned around).
    sat_sizings = [_circle_node_size(sat.label, min_radius=sat_radius, font_size=RING_LABEL_SIZE) for sat in satellites]
    max_sat_radius = max((s[1] for s in sat_sizings), default=sat_radius)
    focal_sizing = _circle_node_size(focal.label, min_radius=focal_radius, font_size=14.5)
    focal_radius = focal_sizing[1]
    neighbour_clearance_radius = (
        (2 * max_sat_radius + 50.0) / (2 * math.sin(math.pi / count)) if count >= 2 else 0.0
    )
    focal_id = focal.id or "__focal__"
    ring_radius = max(190.0, focal_radius + max_sat_radius + 50.0, neighbour_clearance_radius)
    # The focal-to-satellite gap must fit whatever edge LABEL actually gets
    # drawn on that spoke, not a flat buffer - a real, confirmed bug: a flat
    # 50.0px gap let a long label pill visibly overlap INTO both the focal
    # and satellite circles whenever either circle grew past the default
    # radius. Checked and grown ONLY when actually needed, never
    # unconditionally added to the formula above - a real, confirmed
    # regression a first attempt at this fix introduced: forcing every
    # diagram's gap up to fit its worst-case label (even a short, ordinary
    # one like "affects") widened `ring_radius` past what `neighbour_
    # clearance_radius` alone already provided for a diagram with several
    # satellites, uselessly inflating canvas width (and so shrinking
    # on-page font) for a diagram whose real spacing was already generous
    # enough. Only focal<->satellite spokes matter here - a satellite-to-
    # satellite edge bows outward on its own bulge instead (see the
    # edge-drawing loop below), never through this radial gap.
    max_spoke_label_width = max(
        (
            _edge_label_pill_width(edge.label, max_width=CONCEPT_MAP_SPOKE_LABEL_MAX_WIDTH)
            for edge in edges
            if focal_id in (edge.source, edge.target)
        ),
        default=0.0,
    )
    if max_spoke_label_width > 0:
        # +20.0 margin, not the box-layout's +40.0 (see CONCEPT_MAP_LABEL_GAP) -
        # a circle's edge curves away from the spoke on both sides of the
        # label pill, unlike a box's flat edge, so a label sitting on a
        # circle-to-circle spoke needs less extra clearance than the same
        # label would on a box-to-box one to read as clearly not touching
        # either shape; confirmed by direct visual check at this margin.
        needed_gap = max_spoke_label_width + 20.0
        available_gap = ring_radius - focal_radius - max_sat_radius
        if available_gap < needed_gap:
            ring_radius = focal_radius + max_sat_radius + needed_gap
    cx = ring_radius + max_sat_radius + 50.0
    cy = top + ring_radius + max_sat_radius + 50.0
    canvas_w = max(CANVAS_WIDTH, 2 * cx)

    sat_ids: list[str] = []
    # id -> (x, y, radius) for every node, and each satellite's own ring
    # angle - both needed by the edge-drawing pass below, computed up front
    # (before anything is drawn) exactly as `_layout_concept_map` does for
    # its box version.
    positions: dict[str, tuple[float, float, float]] = {focal_id: (cx, cy, focal_radius)}
    sat_angles: dict[str, float] = {}
    for index, sat in enumerate(satellites):
        angle = (2 * math.pi * index / count) - math.pi / 2
        sx = cx + ring_radius * math.cos(angle)
        sy = cy + ring_radius * math.sin(angle)
        sat_id = sat.id or f"__sat{index}__"
        sat_ids.append(sat_id)
        positions[sat_id] = (sx, sy, sat_sizings[index][1])
        sat_angles[sat_id] = angle

    # Relationship lines first, so the node circles sit cleanly on top of
    # their stub ends (same ordering/reasoning as `_layout_concept_map`) -
    # edge LABELS are collected separately and painted last, after every
    # circle, so a label near a circle's edge is never clipped underneath
    # one painted on top of it.
    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    edge_labels: list[str] = []

    # --- convergence fan-out for satellite-to-satellite edges -----------
    # Root-cause fix (production course audit): several real course
    # diagrams have 2+ satellite-to-satellite edges converging on (or
    # diverging from) the SAME one node - e.g. "Shells"/"Anomalies" both
    # relating to BOTH "I1" and "EA"; "orbital_4s"/"orbital_3d" both
    # modifying "orbital_baseline". Their natural bisecting angles land
    # close together (both are "pointing roughly toward the same hub"), so
    # the bounded collision-retry below - which only grows ONE edge's own
    # bulge AWAY from labels/curves already placed earlier in this same
    # loop, a greedy, order-dependent pass - was observed leaving a later
    # edge's label sitting almost entirely on top of the shared target's
    # own circle, confirmed by direct render (not caught by `validate_
    # rendered_svg`, which has no "label overlaps a node" check at all
    # until this fix - see `_label_clears_circles` below). Fixed by
    # construction instead of by luck: every satellite-to-satellite edge
    # that shares its "hub" (the more-converged-upon of its two endpoints)
    # with at least one sibling edge gets its bulge angle spread apart
    # from those siblings BEFORE the retry loop runs at all - the retry
    # loop remains a secondary safety net for whatever this fan-out alone
    # doesn't resolve, not the only thing standing between a dense wheel
    # and overlapping labels.
    sat_sat_edges = [e for e in edges if focal_id not in (e.source, e.target)]
    hub_degree: dict[str, int] = {}
    for e in sat_sat_edges:
        hub_degree[e.source] = hub_degree.get(e.source, 0) + 1
        hub_degree[e.target] = hub_degree.get(e.target, 0) + 1

    def _hub_of(e: DiagramEdge) -> str:
        # Whichever endpoint is converged-upon by more edges - a tie
        # (equal degree, including the common "neither is shared" case of
        # degree 1 each) favours the target, matching the "several
        # relationships converge on one target" shape this fixes.
        return e.target if hub_degree.get(e.target, 0) >= hub_degree.get(e.source, 0) else e.source

    hub_groups: dict[str, list[DiagramEdge]] = {}
    for e in sat_sat_edges:
        hub_groups.setdefault(_hub_of(e), []).append(e)
    # Each edge's (fan_index, fan_count) within its own hub group - a hub
    # with only one edge touching it gets (0, 1), spreading by exactly
    # zero, so the common, non-converging case renders identically to
    # before this fix.
    fan_slot: dict[int, tuple[int, int]] = {}
    for group in hub_groups.values():
        for i, e in enumerate(group):
            fan_slot[id(e)] = (i, len(group))
    _FAN_ANGLE_STEP = math.radians(34.0)

    def _label_clears_circles(mid: tuple[float, float], label: str, max_width: float) -> bool:
        """Does this label's OWN rendered pill - not just the thin curve/
        spoke line it sits on, which `_curve_clears_nodes` already checks -
        clear every node circle in the diagram? A real, confirmed gap: a
        curve sampled at 7 points can clear a node circle by its own
        required 6px margin while the much WIDER label pill drawn at that
        same midpoint (up to 150px wide, vs. the curve's few-px stroke)
        still overlaps that circle significantly. Uses the same width/
        line-wrapping `_edge_label` itself computes (`_edge_label_pill_
        width`/`_wrap`), so this checks against the label's real drawn
        size, never a guess. Checked against EVERY node (focal and every
        satellite), not only the two this edge connects - a label can sit
        clear of its own curve while still landing on an uninvolved third
        satellite's circle in a crowded wheel."""
        text = label.strip()
        if not text:
            return True
        width = _edge_label_pill_width(text, max_width=max_width)
        font_size = _scaled_font_size(EDGE_LABEL_FONT_SIZE)
        lines = _wrap(text, font_size=font_size, width=max_width - 16.0, max_lines=2)
        base_height = _scaled_font_size(18.0)
        height = base_height if len(lines) <= 1 else base_height + _scaled_font_size(EDGE_LABEL_LINE_GAP)
        half_w, half_h = width / 2.0, height / 2.0
        for nx, ny, nr in positions.values():
            closest_x = min(max(nx, mid[0] - half_w), mid[0] + half_w)
            closest_y = min(max(ny, mid[1] - half_h), mid[1] + half_h)
            if math.hypot(nx - closest_x, ny - closest_y) < nr + 4.0:
                return False
        return True
    # Where each already-placed label's pill actually landed - a dense
    # concept map (several satellites all pointing into the same one or two
    # targets - a real, confirmed case: "shells"/"anoms" both relating to
    # BOTH "I1" and "EA") bows more than one satellite-satellite curve
    # through nearly the same lower-middle region, piling their labels on
    # top of each other into illegible overlapping text. Checked below so a
    # genuinely colliding curve's bulge grows until its label clears every
    # label already placed - a non-colliding diagram (the common case)
    # never grows past its normal, original bulge at all.
    placed_label_mids: list[tuple[float, float]] = []
    # Full typical pill width/height (NOT half) - a real, confirmed bug: two
    # pill CENTRES only need to be farther apart than the SUM of both
    # pills' own half-extents to avoid overlapping, but this used a single
    # pill's half-extent as the whole threshold - roughly half of what two
    # genuinely wide, two-line-wrapped pills (confirmed: several real
    # course labels wrap to the full CONCEPT_MAP_LABEL_MAX_WIDTH at 2 lines,
    # ~32-40px tall) actually need, so the in-process retry below judged a
    # position "clear" that the real drawn pills still visibly overlapped
    # at (closed by `label_overlaps_label_issues` in diagram_render_qa.py,
    # the independent final check this exact case exposed as missing).
    label_half_w, label_half_h = CONCEPT_MAP_LABEL_MAX_WIDTH + 12.0, 40.0

    def _label_collides(mid: tuple[float, float]) -> bool:
        return any(
            abs(mid[0] - other[0]) < label_half_w and abs(mid[1] - other[1]) < label_half_h
            for other in placed_label_mids
        )

    def _curve_clears_nodes(
        p0: tuple[float, float], ctrl: tuple[float, float], p2: tuple[float, float],
        *, skip_ids: tuple[str, str],
    ) -> bool:
        """A bulge big enough to clear every placed LABEL can still swing
        the curve itself straight through an uninvolved node's circle in a
        dense wheel (a real, confirmed case: several satellite-to-satellite
        curves all converging on the same two targets - I1/EA - bowed right
        through each other's target circles, 70 separate `edge_crosses_
        node` findings from this module's own render-QA checker on one
        real diagram). Sampled at several points along the ACTUAL drawn
        quadratic curve (not just estimated from its endpoints/midpoint the
        way the label-collision check above is) against every OTHER node's
        real position/radius - the two nodes this edge actually connects
        are deliberately excluded, since the curve legitimately touches
        both of those at its own ends."""
        # Sampled far more densely than the original 7 fixed points - a
        # real, confirmed gap: the final deterministic render-QA check
        # (`edge_crosses_node` in app.services.diagram_render_qa) samples
        # the actual drawn path every ~4px of arc length, fine enough to
        # catch a brief dip into a node between two of this function's old
        # sparse t-values; this in-process check must sample at least that
        # densely too, or it can approve a curve the final QA check still
        # rejects (confirmed: the convergence fan-out fix above moved a
        # curve close enough to a node that exactly this happened).
        for i in range(1, 40):
            t = i / 40.0
            mt = 1.0 - t
            x = mt * mt * p0[0] + 2 * mt * t * ctrl[0] + t * t * p2[0]
            y = mt * mt * p0[1] + 2 * mt * t * ctrl[1] + t * t * p2[1]
            for node_id, (nx, ny, nr) in positions.items():
                if node_id in skip_ids:
                    continue
                if math.hypot(x - nx, y - ny) < nr + 6.0:
                    return False
        return True

    for edge in edges:
        p1 = positions.get(edge.source)
        p2 = positions.get(edge.target)
        if not p1 or not p2:
            continue
        start, end = _shorten_to_circle_edge((p1[0], p1[1]), (p2[0], p2[1]), r1=p1[2], r2=p2[2])
        if focal_id in (edge.source, edge.target):
            # A spoke to/from the focal node: straight - these never cross
            # anything else since every satellite sits on the same ring
            # around the one shared centre.
            elements.append(
                f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
                f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
            mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        else:
            # A relationship between two satellites, not through the focal
            # node - a straight chord would cut across the focal circle and
            # any satellite in between, so it bows outward instead, along
            # the direction that bisects the two satellites' own ring
            # angles (never their averaged position, which collapses
            # toward the centre for near-opposite satellites and gives no
            # usable outward direction).
            theta = _bisecting_angle(sat_angles[edge.source], sat_angles[edge.target])
            fan_i, fan_n = fan_slot.get(id(edge), (0, 1))
            if fan_n > 1:
                theta += (fan_i - (fan_n - 1) / 2.0) * _FAN_ANGLE_STEP
            base_bulge = ring_radius * 0.55 + 30.0
            # Grows the bulge in bounded steps while EITHER this curve
            # itself clips another node, or its label would collide with
            # one already placed, or its label's own pill overlaps a node
            # circle - most satellite-satellite edges never iterate past
            # the first, original bulge at all. Keeps the LAST attempt
            # even if nothing ever fully clears (an unusual topology
            # genuinely has no clean bulge) rather than looping forever -
            # the same bounded-retry-then-ship-best-effort contract this
            # codebase already uses elsewhere.
            #
            # Also nudges the ANGLE, not only the distance - a real,
            # confirmed gap the convergence fan-out above exposed: a fixed
            # fan-shifted angle can point straight at an uninvolved node
            # regardless of bulge distance, so growing distance alone along
            # that same bad angle never clears it (every attempt kept
            # landing on the identical crossing). Cycles a small set of
            # angle offsets together with the growing distance so a bad
            # fixed angle has real alternatives to try, not just "further
            # away in the same wrong direction".
            _ANGLE_JITTERS = (0.0, 10.0, -10.0, 18.0, -18.0)
            for attempt in range(10):
                bulge = base_bulge + attempt * 42.0
                this_theta = theta + math.radians(_ANGLE_JITTERS[attempt % len(_ANGLE_JITTERS)])
                ctrl = (cx + bulge * math.cos(this_theta), cy + bulge * math.sin(this_theta))
                mid = (
                    0.25 * start[0] + 0.5 * ctrl[0] + 0.25 * end[0],
                    0.25 * start[1] + 0.5 * ctrl[1] + 0.25 * end[1],
                )
                if (
                    _curve_clears_nodes(start, ctrl, end, skip_ids=(edge.source, edge.target))
                    and not _label_collides(mid)
                    and _label_clears_circles(mid, edge.label, CONCEPT_MAP_LABEL_MAX_WIDTH)
                ):
                    break
            elements.append(
                f'<path d="M{start[0]:.1f},{start[1]:.1f} Q{ctrl[0]:.1f},{ctrl[1]:.1f} '
                f'{end[0]:.1f},{end[1]:.1f}" fill="none" stroke="{theme.accent_color}" '
                f'stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
        placed_label_mids.append(mid)
        label_width_budget = (
            CONCEPT_MAP_SPOKE_LABEL_MAX_WIDTH if focal_id in (edge.source, edge.target) else CONCEPT_MAP_LABEL_MAX_WIDTH
        )
        edge_labels.append(_edge_label(mid[0], mid[1], edge.label, theme, max_width=label_width_budget))

    focal_svg, _ = _circle_node(
        focal.label, cx=cx, cy=cy, theme=theme, number="",
        fill=theme.accent_soft, stroke=theme.accent_color, min_radius=focal_radius, font_size=14.5,
        detail=focal.detail, sizing=focal_sizing,
    )
    elements.append(focal_svg)

    for index, sat in enumerate(satellites):
        sx, sy, _ = positions[sat_ids[index]]
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        svg, _ = _circle_node(
            sat.label, cx=sx, cy=sy, theme=theme, number=str(index + 1),
            fill=fill, stroke=stroke, min_radius=sat_radius, detail=sat.detail,
            sizing=sat_sizings[index],
        )
        elements.append(svg)
    elements.extend(edge_labels)

    wheel_bottom = cy + ring_radius + max_sat_radius + 40.0
    key_entries = [("", focal.label, focal.detail)] + [
        (str(i + 1), n.label, n.detail) for i, n in enumerate(satellites)
    ]
    key_elements, key_h = _key_table_rows(
        key_entries, top=wheel_bottom + 30.0, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (wheel_bottom + 30.0 + key_h - top)


# --- textbook-style standard flowchart (flow_chart/process only - cycle
# stays on the ring above, a true repeating process reads better as a ring
# than forced into a straight line that has to loop back to its own top) --
# Conventional notation - oval start/end, rectangle process, diamond
# decision - in a single straight column, the way a textbook/whiteboard
# actually draws a plain procedure. Reuses the already-proven _draw_pill/
# _draw_diamond/_fit_boxed_text shapes (er_diagram, and the old box-based
# flow_chart renderer kept below for backward compatibility) rather than
# inventing new ones - only the LAYOUT (straight column, no side-branch
# columns) and the "no detail text inside a node, no text on an arrow"
# rules are new.

# Public (no leading underscore) like RING_LABEL_SIZE - app.services.
# diagram_render_qa imports this to know the REAL smallest label size this
# kind renders at (the pill's own _PILL_LABEL_SIZE is larger, so this -
# process/decision's shared size - is the worst-case readability floor).
STANDARD_FLOWCHART_LABEL_SIZE = 15.0
_MIN_PROCESS_BOX_W = 220.0
_MAX_PROCESS_BOX_W = 380.0
_MIN_PROCESS_BOX_H = 70.0
_MIN_DECISION_BOX_W = 220.0
_MAX_DECISION_BOX_W = 380.0
_MIN_DECISION_BOX_H = 130.0


def _draw_process_box(
    node: DiagramNode, *, cx: float, cy: float, width: float, height: float, theme: TemplateTheme,
    colors: tuple[str, str], base_size: float = STANDARD_FLOWCHART_LABEL_SIZE,
    min_size: float = STANDARD_FLOWCHART_LABEL_SIZE,
) -> str:
    """An ordinary process step - a plain rounded rectangle, label only.
    Never `node.detail` inside the box - a node's full explanation belongs
    in the Key table below (_key_table_rows), the same rule the ring/wheel
    rendering already follows; `detail`, when set, still reaches the hover
    tooltip. `base_size`/`min_size` default to the fixed flowchart label
    size every pre-existing caller already relied on - only
    `_layout_decision_tree`'s own proactive shrink-to-fit (see its own
    comment) passes a smaller, already-computed pair, so every other
    caller is unaffected."""
    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
    fill, stroke = colors
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<rect class="diagram-box" x="{cx - width / 2:.1f}" y="{cy - height / 2:.1f}" '
        f'width="{width:.1f}" height="{height:.1f}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=width * 0.85, height=height * 0.7,
            color=theme.text_color, theme=theme,
            base_size=base_size, min_size=min_size,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _ideal_process_box_size(node: DiagramNode, *, font_size: float | None = None) -> tuple[float, float]:
    """Sizes the box to its own label - the box grows to fit the text
    (never the other way around), the same "enlarge the shape, don't
    shrink or truncate the text" rule every shape in this module follows;
    `_fit_boxed_text`'s own shrink-to-fit inside `_draw_process_box` is
    only ever a defensive last resort once this already gave it generous
    room. `font_size`, when given, overrides the usual `_scaled_font_size
    (STANDARD_FLOWCHART_LABEL_SIZE)` - only `_layout_decision_tree`'s own
    proactive shrink-to-fit loop passes one, to size a box at the SAME
    smaller font it will actually be drawn at. The wrap-width budget and
    the min/max box width both scale down WITH it (not just the font) -
    a real, confirmed bug in an earlier version of this shrink-to-fit:
    `_wrap`'s own 300px budget was a fixed pixel value, so a long,
    already-wrapping label just packed more characters per line at the
    smaller font and kept the SAME ~390px box width regardless of how far
    the font shrank, making the whole shrink loop a no-op for exactly the
    multi-word labels it most needed to help."""
    label = node.label.strip() or "—"
    requested = font_size if font_size is not None else STANDARD_FLOWCHART_LABEL_SIZE
    box_scale = min(1.0, requested / STANDARD_FLOWCHART_LABEL_SIZE)
    font_size = _scaled_font_size(requested)
    # max_lines deliberately unbounded (999, not a real cap) - a real,
    # confirmed bug: an earlier version capped this at 3, so _wrap ITSELF
    # silently truncated an unusually long label with "…" right here,
    # during SIZING, before _draw_process_box's own _fit_boxed_text ever
    # got a chance to give it room - the box was being sized to an
    # already-mutilated line list. Mirrors _circle_node's own fix for the
    # identical class of bug.
    lines = _wrap(label, font_size=font_size, width=300.0 * box_scale, max_lines=999, bold=True) or [label]
    longest = max(len(line) for line in lines)
    line_w = longest * font_size * _BOLD_RATIO
    text_h = len(lines) * font_size * LINE_HEIGHT
    width = max(_MIN_PROCESS_BOX_W * box_scale, min(_MAX_PROCESS_BOX_W * box_scale, (line_w + 32.0) / 0.85))
    height = max(_MIN_PROCESS_BOX_H * box_scale, (text_h + 24.0) / 0.7)
    return width, height


def _ideal_decision_box_size(node: DiagramNode, *, font_size: float | None = None) -> tuple[float, float]:
    """Mirrors `_layout_flow_chart`'s own (still kept, still used for
    backward compatibility) `_ideal_decision_size` - reimplemented
    standalone here, deliberately not extracted/shared, so this change can
    never put that already-stable function's own behaviour at risk.
    `_draw_diamond`'s own usable text area is (width*0.5, height*0.5) - a
    rhombus is only that wide/tall at its own midline - so the diamond is
    sized well past the label's own raw footprint. max_lines deliberately
    unbounded - see _ideal_process_box_size's own comment on the identical
    truncate-during-sizing bug this avoids. `font_size`, when given,
    overrides the usual `_scaled_font_size(STANDARD_FLOWCHART_LABEL_SIZE)` -
    see _ideal_process_box_size's matching parameter, and its own comment
    on why the wrap budget and min/max width below scale down WITH it
    (not just the font), for why."""
    label = node.label.strip() or "?"
    requested = font_size if font_size is not None else STANDARD_FLOWCHART_LABEL_SIZE
    box_scale = min(1.0, requested / STANDARD_FLOWCHART_LABEL_SIZE)
    font_size = _scaled_font_size(requested)
    wrap_budget = 260.0 * box_scale
    lines = _wrap(label, font_size=font_size, width=wrap_budget, max_lines=999, bold=True) or [label]
    longest = max(len(line) for line in lines)
    line_w = longest * font_size * _BOLD_RATIO
    width = max(_MIN_DECISION_BOX_W * box_scale, min(_MAX_DECISION_BOX_W * box_scale, (line_w + 24.0) / 0.5))
    # A real, confirmed bug (pre-dating this specific function - it was
    # already live in flow_chart's own decision diamonds, just never hit
    # by a long-enough label in that kind's own tests): the wrap above
    # assumes a FIXED 260px budget no matter where `width` actually lands.
    # Once _MAX_DECISION_BOX_W caps it, the diamond's own usable text area
    # (width*0.5, per this function's own docstring) can end up NARROWER
    # than that 260px assumption - under-counting how many lines the
    # label really needs and truncating it at draw time. Re-wrap at the
    # REAL usable width once it's known; height has no cap, so it simply
    # grows to fit however many lines that turns out to need, same "grow,
    # never truncate" rule every shape in this module already follows.
    #
    # The 12.0 subtracted here is _fit_boxed_text's own 2*padding (default
    # padding=6.0) - a real, confirmed bug: `_draw_diamond` hands
    # `_fit_boxed_text` a usable area of (width*0.5, height*0.5), but
    # `_fit_boxed_text` itself then shrinks THAT by another 2*padding
    # before wrapping. Comparing/re-wrapping against the bare `width*0.5`
    # here (without that same 12px) let this function believe a label
    # would fit in fewer lines than `_fit_boxed_text` actually had room
    # for at draw time - sizing the box exactly one line too short and
    # triggering `_fit_boxed_text`'s own last-resort ellipsis truncation
    # even though simply growing the box one line taller (which this
    # function is free to do - height has no cap) would have avoided it
    # entirely. Confirmed via a real decision_tree case: a 65-character
    # root-decision label truncated mid-sentence at EVERY font size this
    # function was asked to size at, always exactly one line short.
    usable_w = max(width * 0.5 - 12.0, 10.0)
    if usable_w < wrap_budget:
        lines = _wrap(label, font_size=font_size, width=usable_w, max_lines=999, bold=True) or [label]
    text_h = len(lines) * font_size * LINE_HEIGHT
    height = max(_MIN_DECISION_BOX_H * box_scale, (text_h + 24.0) / 0.5)
    return width, height


def _flowchart_role(node: DiagramNode, *, out_degree: int) -> str:
    """start/process/decision/end for one step of a standard flowchart -
    an explicit override (Edit Visuals: the flowchart editor) wins first,
    then real structure (2+ outgoing edges IS a decision, by construction,
    same rule _layout_flow_chart's own role_of already uses), then a
    literal start/end label match.

    Deliberately NO first/last-position fallback (unlike the ring's own
    _ring_role_of, or the old _layout_flow_chart's role_of): a real,
    confirmed bug - `_draw_pill`'s oval is a fixed, non-growing size
    (correct for a genuinely short "Start"/"End" label, the only thing a
    real flowchart ever puts there), so misclassifying an ordinary,
    possibly LONG first/last process step as "start"/"end" just because
    of its position forced its label into a shape that can't grow to fit
    it, truncating with "…" - the exact bug this whole rendering change
    exists to remove. A node that isn't explicitly marked start/end
    (override or label) is simply an ordinary process step by default,
    which CAN grow (see _ideal_process_box_size) - the safe default."""
    override = node.role.strip().lower()
    if override in ("start", "process", "decision", "end"):
        return override
    if out_degree >= 2:
        return "decision"
    label = node.label.strip().lower()
    if label in _RING_START_WORDS:
        return "start"
    if label in _RING_END_WORDS:
        return "end"
    return "process"


def _flowchart_side_connector(
    x1: float, y1: float, x2: float, y2: float, *, rail_x: float, theme: TemplateTheme
) -> str:
    """A secondary connector (a decision's alternate branch, a loop back
    to an earlier step) - out the source's own right edge, straight down
    (or up) a vertical "rail" placed far enough right to clear every node
    it passes, then left into the target's own right edge - the standard
    flowchart convention for a skip-ahead/loop-back branch, drawn as
    straight right-angle segments (`stroke-linejoin="round"` only softens
    the two corners, it never curves the segments themselves). Replaces an
    earlier free-floating curved bow: a real, confirmed complaint (with a
    screenshot) that it read as a stray circular arc hanging in empty
    space, not a connector - it started/ended at an arbitrary offset from
    each node's own CENTRE rather than touching its actual edge, so it
    never visibly touched either shape. `x1`/`y1`/`x2`/`y2` here are each
    node's own real edge point, so this one now visibly starts and ends on
    the shapes it connects. Never carries a label, same as every other
    connector this module's textbook-style kinds draw."""
    return (
        f'<path d="M{x1:.1f},{y1:.1f} L{rail_x:.1f},{y1:.1f} L{rail_x:.1f},{y2:.1f} L{x2:.1f},{y2:.1f}" '
        f'fill="none" stroke="{theme.accent_color}" stroke-width="2" stroke-linejoin="round" '
        f'marker-end="url(#diagram-arrow)"/>'
    )


# A 2-line _edge_label pill is 18.0 + EDGE_LABEL_LINE_GAP(14.0) = 32px
# tall; this adds real clearance above/below it (never just the pill's own
# bare height) so it reads as a distinct element floating clear of both
# neighbouring shapes, not touching either one.
_EDGE_LABEL_MIN_GAP = 56.0


def _layout_standard_flowchart(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float,
    gap_scale: float = 1.0,
) -> tuple[list[str], float, float]:
    """Textbook-style standard flowchart for flow_chart/process (cycle
    stays on the ring - see _layout_ring_process): a single, clean
    vertical column of conventional symbols - oval for start/end,
    rectangle for an ordinary step, diamond for a decision - connected
    top to bottom by arrows. A decision's own branch meaning ("Yes"/"No")
    sits directly on its outgoing arrow as a small pill (see
    _edge_label) - the same convention every other kind in this module
    already uses for a labelled edge - rather than a separate explanatory
    block underneath the diagram: a real textbook flowchart is read
    entirely from the shapes and arrows themselves.

    Deliberately a SINGLE straight column (max ~7 steps, per the brief)
    rather than the old _layout_flow_chart's side-branching-column design:
    any edge that doesn't connect two consecutive nodes in the given
    order (a decision's secondary branch, a loop back) is drawn as a
    right-angle connector routed out to the side and back (see
    _flowchart_side_connector) instead of a persistent second column.

    `gap_scale` (see render_diagram_svg_fitting /
    app.course.document.diagram_compaction) only compacts the vertical
    gap between consecutive nodes - node/box sizes and font are never
    touched by it."""
    if not nodes:
        return [], CANVAS_WIDTH, 0.0

    _, out_edges = _flow_degrees(nodes, edges)
    index_by_id = {n.id: i for i, n in enumerate(nodes) if n.id}
    edge_by_pair = {(e.source, e.target): e for e in edges}

    cx = CANVAS_WIDTH / 2
    gap = 50.0 * gap_scale
    y = top
    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    positions: list[tuple[float, float, float]] = []  # (cx, top_y, bottom_y)
    widths: list[float] = []  # each node's own drawn width, for side-connector clearance

    for index, node in enumerate(nodes):
        out_degree = len(out_edges.get(node.id, []))
        role = _flowchart_role(node, out_degree=out_degree)
        colors = _node_colors(node, role)
        if role == "decision":
            width, height = _ideal_decision_box_size(node)
            svg = _draw_diamond(
                node, cx=cx, cy=y + height / 2, width=width, height=height, theme=theme, colors=colors,
                base_size=STANDARD_FLOWCHART_LABEL_SIZE, min_size=STANDARD_FLOWCHART_LABEL_SIZE,
            )
        elif role in ("start", "end"):
            width, height = _PILL_W, _PILL_H
            svg = _draw_pill(node, cx=cx, cy=y + height / 2, theme=theme, colors=colors)
        else:
            width, height = _ideal_process_box_size(node)
            svg = _draw_process_box(node, cx=cx, cy=y + height / 2, width=width, height=height, theme=theme, colors=colors)
        elements.append(svg)
        positions.append((cx, y, y + height))
        widths.append(width)
        segment_gap = gap
        if index < len(nodes) - 1:
            next_edge = edge_by_pair.get((node.id, nodes[index + 1].id))
            if next_edge and next_edge.label.strip():
                # Reserved separately from `gap` (and never shrunk by
                # gap_scale/diagram compaction) - a real, confirmed case: a
                # compacted diagram's edge-label pill (_edge_label, up to 2
                # lines tall - see its own docstring) overlapped the node
                # below it once the general inter-node gap shrank below
                # the pill's own height, since this segment's own spacing
                # never grew to compensate.
                segment_gap = max(gap, _EDGE_LABEL_MIN_GAP)
        y += height + segment_gap

    for i in range(len(nodes) - 1):
        bottom = positions[i][2]
        top2 = positions[i + 1][1]
        elements.append(
            f'<line x1="{cx:.1f}" y1="{bottom:.1f}" x2="{cx:.1f}" y2="{top2:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2.5" marker-end="url(#diagram-arrow)"/>'
        )
        straight_edge = edge_by_pair.get((nodes[i].id, nodes[i + 1].id))
        if straight_edge and straight_edge.label.strip():
            elements.append(_edge_label(cx, (bottom + top2) / 2, straight_edge.label, theme, max_width=120.0))

    # Rails already claimed by an earlier side connector, as (y_lo, y_hi,
    # rail_x) - a second connector whose own y-range overlaps an earlier
    # one's is pushed further out past it (never onto the same rail_x), so
    # two independent side connectors can never run down the exact same
    # vertical line even when they both skip over the same stretch of
    # nodes (a real, confirmed case: a "Route" decision's two branches
    # reconverging on a shared "End" node needs one connector for each
    # branch, and their y-ranges overlap).
    placed_rails: list[tuple[float, float, float]] = []
    for edge in edges:
        si, ti = index_by_id.get(edge.source), index_by_id.get(edge.target)
        if si is None or ti is None or ti == si + 1:
            continue  # already drawn as the straight consecutive arrow above
        lo, hi = min(si, ti), max(si, ti)
        # The vertical rail must clear every node's own right edge from
        # source to target INCLUSIVE (the rail starts/ends level with
        # those two nodes too, not just the ones strictly between them) -
        # a wide box anywhere in that range (e.g. a long-labelled process
        # step) would otherwise sit right under the connector, the real
        # bug this clearance math fixes (see _flowchart_side_connector's
        # own docstring for the other real bug this geometry also fixes -
        # the connector visibly touching each node's actual edge now,
        # not a point floating off its centre).
        rail_x = max(positions[i][0] + widths[i] / 2.0 for i in range(lo, hi + 1)) + 40.0
        x1, y1 = positions[si][0] + widths[si] / 2.0, (positions[si][1] + positions[si][2]) / 2
        x2, y2 = positions[ti][0] + widths[ti] / 2.0, (positions[ti][1] + positions[ti][2]) / 2
        y_lo, y_hi = min(y1, y2), max(y1, y2)
        for p_lo, p_hi, p_rail in placed_rails:
            if y_lo < p_hi and p_lo < y_hi:  # overlapping y-ranges
                rail_x = max(rail_x, p_rail + 40.0)
        placed_rails.append((y_lo, y_hi, rail_x))
        elements.append(_flowchart_side_connector(x1, y1, x2, y2, rail_x=rail_x, theme=theme))
        if edge.label.strip():
            # Offset past the rail itself (same "clear of the line" spirit
            # as the other diagonal-connector label offsets in this module,
            # e.g. _LABEL_OFFSET at this file's own swimlane layout) so the
            # pill sits beside the corner it labels, not on top of the node
            # column it runs past.
            elements.append(_edge_label(rail_x + 20.0, y1, edge.label, theme, max_width=110.0))

    bottom = positions[-1][2]
    return elements, CANVAS_WIDTH, (bottom - top)


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
) -> tuple[list[str], float, float]:
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

    # Canvas width (and therefore each row's own centred starting x) needs
    # the WIDEST row's width known up front - a real, confirmed bug: every
    # row used to start flush at x=MARGIN regardless of its own width, so
    # only the single widest row ever happened to span the canvas
    # edge-to-edge; any NARROWER row (a lone root node, a shorter branch)
    # sat stuck to the left instead of centred under the ones around it.
    row_widths = {level: len(rows[level]) * box_w + (len(rows[level]) - 1) * 24.0 for level in levels}
    max_row_width = max(row_widths.values(), default=0.0)
    canvas_w = max(max_row_width + 2 * MARGIN, CANVAS_WIDTH)

    positions: dict[str, tuple[float, float, float]] = {}  # id -> (cx, top_y, bottom_y)
    elements: list[str] = []
    elements.append(_arrow_marker("diagram-arrow", theme.accent_color))

    y = top
    for level in levels:
        row_nodes = rows[level]
        row_width = row_widths[level]
        x = (canvas_w - row_width) / 2
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
# A straight focal<->satellite SPOKE (unlike a bowed satellite-satellite
# curve, which has the whole outward bulge to spread into) sits directly on
# the radial gap this same label forces `_layout_concept_wheel` to reserve
# - so a wide label pill there has a DIRECT, immediate cost: every extra
# pixel of width widens that gap (and so the whole canvas) by the same
# amount, which a scaled-down display then has to re-shrink, risking the
# readability floor (a real, confirmed regression: widening the gap to the
# full `CONCEPT_MAP_LABEL_MAX_WIDTH` budget to stop a prior overlap bug
# pushed even short, ordinary spoke labels like "relates to" past that
# floor). Wrapping the same text to 2 narrower lines (`_edge_label` already
# supports this) instead of 1 wide line keeps it just as legible while
# costing far less width - used ONLY for a spoke's own gap/label sizing,
# never for a satellite-satellite curve's label (that one keeps the wider
# budget, since its bulge already has room to spare).
CONCEPT_MAP_SPOKE_LABEL_MAX_WIDTH = 108.0
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
    plus _fit_boxed_text's own shrink-to-fit underneath.

    `base_size` is RING_LABEL_SIZE, not the old hardcoded 11.5 - a real,
    confirmed readability bug: 11.5px scales to only ~8.7px once the page
    shrinks this canvas to its real column width, under the 11px
    readability floor (the same `diagram_render_qa.readability_issues`
    floor every ring/mesh node is already sized to clear) - and this kind
    wasn't even in that check's own per-kind lookup table, so nothing
    caught it. RING_LABEL_SIZE is the established safe floor for small-
    shape text everywhere else in this module."""
    tooltip = node.label.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<ellipse class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" rx="{width / 2:.1f}" ry="{height / 2:.1f}" '
        f'fill="{theme.surface_color}" stroke="{theme.border_color}" stroke-width="1.5"/>'
    )
    parts.append(
        _fit_boxed_text(
            cx, cy, node.label, width=width * 0.7, height=height * 0.6,
            color=theme.text_color, theme=theme, base_size=RING_LABEL_SIZE, min_size=10.0, weight=500,
        )
    )
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _ordered_er_main_nodes(main_nodes: list[DiagramNode], edges: list[DiagramEdge]) -> list[DiagramNode]:
    """Lay entities/relationships out left-to-right as a CHAIN following the
    edges between them (entity -> relationship -> entity -> ...) - the real
    ER convention - regardless of what order the spec's own `nodes` list
    happens to list them in. A real, confirmed bug: a spec that lists all
    its entities first and its relationship node last (e.g. Customer,
    Order, Places, in that order) placed the relationship in the FAR
    column, so its connector to the first entity became a straight line
    cutting straight through the unrelated entity physically sitting
    between them - exactly the "Order" box and "Places" diamond reading as
    overlapping that a real generated diagram showed. The writer prompt
    never promised the model would order `nodes` with every relationship
    adjacent to its own two entities, so the renderer has to make that
    true itself rather than trust the input order - a breadth-first walk
    along each connected component's own edges is the only ordering where
    a relationship always lands next to the entities it actually
    connects to, so the straight-line connectors this layout draws never
    need to cross another node's box."""
    by_id = {n.id: n for n in main_nodes if n.id}
    adjacency: dict[str, list[str]] = {nid: [] for nid in by_id}
    for edge in edges:
        if edge.source in by_id and edge.target in by_id:
            adjacency[edge.source].append(edge.target)
            adjacency[edge.target].append(edge.source)

    visited: set[str] = set()
    ordered: list[DiagramNode] = []
    for node in main_nodes:  # preserves original first-appearance order across components
        if not node.id or node.id in visited:
            continue
        queue = [node.id]
        visited.add(node.id)
        while queue:
            nid = queue.pop(0)
            ordered.append(by_id[nid])
            for neighbour in adjacency.get(nid, []):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)
    ordered.extend(n for n in main_nodes if not n.id)
    return ordered


def _layout_er(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    is_attribute = lambda n: n.shape_role.strip().lower() == "attribute"  # noqa: E731
    main_nodes = _ordered_er_main_nodes([n for n in nodes if not is_attribute(n)], edges)
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
                # A real, confirmed bug: every attribute's connector used to
                # end at the SAME point (cx, main_top) - the entity's own
                # centre - regardless of which attribute it was, so 2+
                # attributes' lines converged on one identical spot instead
                # of fanning out (duplicate_arrowhead_side correctly flagged
                # this: two lines landing on the same point of an entity's
                # edge read as one ambiguous connection, not two). Each
                # attribute's own x position (a_cx) is where its connector
                # should land instead - a proper fan, the standard ER
                # attribute-oval convention.
                elements.append(
                    f'<line x1="{a_cx:.1f}" y1="{a_cy + _ER_ATTR_H / 2:.1f}" x2="{a_cx:.1f}" y2="{main_top:.1f}" '
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
    # Only actually grows the drawn text when the caller's own box was
    # ALSO sized for the scaled font (e.g. _draw_process_box/_draw_diamond,
    # fed a box from the now-scale-aware _ideal_process_box_size/_ideal_
    # decision_box_size) - a caller that hands this a genuinely FIXED,
    # already-tight box (a schematic shape, a step-number badge) just
    # shrinks straight back down through the loop below, which is
    # harmless (not a regression - that text was already as big as its
    # fixed box could hold).
    size = _scaled_font_size(base_size)
    min_size = _scaled_font_size(min_size)
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
    size = _scaled_font_size(size)
    lines = _wrap(text, font_size=size, width=max(width, 70.0), max_lines=max_lines, bold=weight >= 600)
    if not lines:
        return ""
    svg, _ = _text(cx, top_y, lines, font_size=size, weight=weight, color=color, font_family=theme.font_family)
    return svg


def _anchored_label(
    x: float, y: float, text: str, *, width: float, color: str, theme: TemplateTheme,
    size: float = 13.0, weight: int = 600, max_lines: int = 2, anchor: str = "middle",
) -> str:
    """Same job as `_caption_text` (a free-standing label beside a shape,
    no hard height ceiling) but with a controllable `text-anchor` -
    needed for an external leader-line label that must sit clearly to one
    SIDE of its own leader (donut_segments/orbit_diagram) rather than
    centred on it, which is the only thing `_caption_text` itself offers."""
    size = _scaled_font_size(size)
    lines = _wrap(text, font_size=size, width=max(width, 70.0), max_lines=max_lines, bold=weight >= 600)
    if not lines:
        return ""
    svg, _ = _text(x, y, lines, font_size=size, weight=weight, color=color, font_family=theme.font_family, anchor=anchor)
    return svg


def _polar(cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
    """A point at `radius` from (cx, cy), `degrees` CLOCKWISE from
    straight up (0deg = 12 o'clock, 90deg = 3 o'clock) - the conventional
    pie/donut-chart angle convention, shared by every arc-based kind below
    (donut_segments/pie_wedge/orbit_diagram/progress_rings/bridge_process)."""
    rad = math.radians(degrees - 90.0)
    return cx + radius * math.cos(rad), cy + radius * math.sin(rad)


def _donut_wedge_path(cx: float, cy: float, r_inner: float, r_outer: float, start_deg: float, end_deg: float) -> str:
    """One filled annulus sector (a donut "slice") from `start_deg` to
    `end_deg` - the one new path primitive donut_segments/pie_wedge/
    bridge_process need (an actual filled arc REGION; every existing kind
    only ever positions discrete node shapes around a ring, never draws
    the ring itself as a filled sector). `r_inner=0` degenerates cleanly
    to a true pie wedge (a zero-radius inner "arc" is just the centre
    point), so pie_wedge reuses this unchanged rather than needing its
    own, near-identical path builder."""
    sweep = end_deg - start_deg
    large_arc = 1 if abs(sweep) > 180 else 0
    x1o, y1o = _polar(cx, cy, r_outer, start_deg)
    x2o, y2o = _polar(cx, cy, r_outer, end_deg)
    x1i, y1i = _polar(cx, cy, r_inner, end_deg)
    x2i, y2i = _polar(cx, cy, r_inner, start_deg)
    return (
        f"M{x1o:.2f},{y1o:.2f} "
        f"A{r_outer:.2f},{r_outer:.2f} 0 {large_arc} 1 {x2o:.2f},{y2o:.2f} "
        f"L{x1i:.2f},{y1i:.2f} "
        f"A{r_inner:.2f},{r_inner:.2f} 0 {large_arc} 0 {x2i:.2f},{y2i:.2f} Z"
    )


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


_COMPARISON_MIN_COL_W = 170.0
_COMPARISON_GUTTER = 20.0
_COMPARISON_ROW_GAP = 20.0


def _layout_comparison(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """Side-by-side columns, one per thing being compared - wraps into
    additional ROWS once more columns than the canvas can hold at
    `_COMPARISON_MIN_COL_W` would otherwise squeeze every column narrower
    than that floor. A real, confirmed case: 7 things compared in a single
    row left each column ~95px wide on this kind's old fixed-column-count
    math - well under what even one 8-character word ("Reynolds") needs,
    so `_wrap`'s last-resort mid-word fallback fired on nearly every
    label, producing visibly broken text ("Reynol" / "ds"). A real
    textbook table of many items reads as a multi-row grid (e.g. 4 across,
    then the rest on the next row), not an ever-narrower single line, so
    this grows DOWN instead of shrinking each column - the same
    "never truncate/cram, grow instead" rule every other kind in this
    module already follows."""
    nodes = nodes or []
    if not nodes:
        return [], 0.0
    available = CANVAS_WIDTH - 2 * MARGIN
    cols_per_row = max(
        1, int((available + _COMPARISON_GUTTER) / (_COMPARISON_MIN_COL_W + _COMPARISON_GUTTER))
    )
    cols_per_row = min(cols_per_row, len(nodes))
    col_w = (available - (cols_per_row - 1) * _COMPARISON_GUTTER) / cols_per_row

    elements: list[str] = []
    y = top
    for row_start in range(0, len(nodes), cols_per_row):
        row_nodes = nodes[row_start : row_start + cols_per_row]
        x = MARGIN
        row_height = 0.0
        for node in row_nodes:
            svg, height = _node_block(node, x=x, y=y, width=col_w, theme=theme)
            elements.append(svg)
            row_height = max(row_height, height)
            x += col_w + _COMPARISON_GUTTER
        y += row_height + _COMPARISON_ROW_GAP

    return elements, y - _COMPARISON_ROW_GAP - top


# --- circuit_schematic (kind == "circuit_schematic") ----------------------
#
# Real electrical topology - junction nodes connected by wires, some of which
# carry a component symbol (resistor/capacitor/inductor/source/switch) -
# built on the same DiagramNode/DiagramEdge graph structure every other
# relationship kind already uses (see DiagramNode.lane/shape_role and
# DiagramEdge.label's own docstrings for the exact reuse convention), NOT an
# extension of `schematic`'s anchor/orbit system (see DiagramSpec.is_usable's
# own docstring: that illustrated-scene system is permanently retired after
# real, confirmed overlap/unreadability failures - wholly unsuited to
# precise wire-to-terminal topology anyway, which needs exact connection
# points, not "orbit near this other shape").
#
# Root cause this closes: a circuit-topology comparison (e.g. "Series vs
# Parallel - Valid and Invalid Reductions") used to get authored as
# `diagram_kind: "comparison"` - a generic card grid of text boxes - because
# no kind in this module could draw real wires/nodes/component symbols at
# all. See app.agents.prompts for the authoring-guidance half of this fix.

_CIRCUIT_COMPONENT_PREFIXES = {
    "R": "resistor", "C": "capacitor", "L": "inductor",
    "V": "source", "B": "source", "SW": "switch", "S": "switch",
}


def _parse_circuit_component(label: str) -> tuple[str, str]:
    """"R:220Ω" -> ("resistor", "220Ω"); "SW" -> ("switch", ""); "" ->
    ("wire", ""); "node A tap" (no recognised tag) -> ("wire", "node A tap")
    - an unrecognised tag degrades to a plain wire carrying that text as its
    own label, never a crash on a malformed/unexpected edge label. See
    DiagramEdge.label's own docstring for the full convention."""
    label = (label or "").strip()
    if not label:
        return "wire", ""
    if ":" in label:
        prefix, _, value = label.partition(":")
        kind = _CIRCUIT_COMPONENT_PREFIXES.get(prefix.strip().upper())
        if kind:
            return kind, value.strip()
        return "wire", label
    kind = _CIRCUIT_COMPONENT_PREFIXES.get(label.upper())
    if kind:
        return kind, ""
    return "wire", label


def _circuit_symbol_svg(kind: str, *, color: str) -> str:
    """A component glyph in LOCAL coordinates, centred on the origin,
    drawn along the local +x axis (the caller wraps this in a `translate
    (cx,cy) rotate(deg)` group - see `_draw_circuit_component` - so this
    never needs to know its own final position or orientation). Every
    primitive here is a plain line/path, the same "no font-dependent glyph,
    ever" rule every other shape in this module already follows (see
    the icon-badge section's own comment above)."""
    sw = 2.2
    if kind == "resistor":
        pts = [(-17, 0), (-11, -7), (-5.5, 7), (0, -7), (5.5, 7), (11, -7), (17, 0)]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        return (
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    if kind == "capacitor":
        return (
            f'<line x1="-3" y1="-11" x2="-3" y2="11" stroke="{color}" stroke-width="{sw + 0.8:.1f}" stroke-linecap="round"/>'
            f'<line x1="3" y1="-11" x2="3" y2="11" stroke="{color}" stroke-width="{sw + 0.8:.1f}" stroke-linecap="round"/>'
        )
    if kind == "inductor":
        arcs = []
        for i in range(4):
            cx = -15.0 + i * 10.0
            arcs.append(
                f'<path d="M{cx - 5:.1f},0 A5,6 0 0 1 {cx + 5:.1f},0" fill="none" '
                f'stroke="{color}" stroke-width="{sw}"/>'
            )
        return "".join(arcs)
    if kind == "source":
        # Standard long-bar/short-bar cell symbol - long bar is the positive
        # terminal, by convention the one facing the edge's own direction of
        # travel (source -> target), so a model's "V:9V" edge consistently
        # reads with + toward its declared target.
        return (
            f'<line x1="-5" y1="-12" x2="-5" y2="12" stroke="{color}" stroke-width="{sw + 1.4:.1f}" stroke-linecap="round"/>'
            f'<line x1="5" y1="-6" x2="5" y2="6" stroke="{color}" stroke-width="{sw + 0.4:.1f}" stroke-linecap="round"/>'
        )
    if kind == "switch":
        return (
            f'<circle cx="-11" cy="0" r="2.4" fill="{color}"/>'
            f'<line x1="-11" y1="0" x2="10" y2="-9" stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<circle cx="11" cy="0" r="2.4" fill="{color}"/>'
        )
    return ""


def _draw_circuit_component(
    cx: float, cy: float, angle_deg: float, kind: str, value: str, *, theme: TemplateTheme, side: float = 1.0,
) -> str:
    glyph = _circuit_symbol_svg(kind, color=theme.text_color)
    if not glyph:
        return ""
    # class="diagram-icon" - the SAME marker _draw_icon already uses for its
    # own small-local-coordinate glyphs (see its own comment): this
    # component symbol is drawn in a small LOCAL box (roughly -20..20)
    # around the origin, meant to be read only through this transform.
    # Without this exact class, app.services.diagram_render_qa's geometry
    # scan (which has no notion of SVG `transform`) misreads the glyph's
    # own small negative local coordinates as absolute canvas points - a
    # real, confirmed false `out_of_canvas` finding this closes.
    svg = f'<g class="diagram-icon" transform="translate({cx:.1f},{cy:.1f}) rotate({angle_deg:.1f})">{glyph}</g>'
    if value.strip():
        # The value label is drawn upright in PANEL space (never rotated
        # with the glyph - a 9V battery drawn vertically must not print its
        # label sideways), offset perpendicular to the wire so it never
        # sits on top of the glyph itself.
        label_size = _scaled_font_size(_CIRCUIT_LABEL_SIZE)
        rad = math.radians(angle_deg)
        # `side` (-1.0/+1.0, see the fan-out loop in _draw_one_circuit_panel)
        # pushes the label AWAY from the shared centerline a fanned-out rail
        # was offset from, not always in one fixed direction - a real,
        # confirmed bug otherwise: with 2 parallel rails only _CIRCUIT_FAN_
        # STEP (22px) apart, a fixed "always below/right" offset put the TOP
        # rail's own label almost exactly on top of the BOTTOM rail's wire
        # (label baseline ~0.7px from the second wire, measured directly from
        # a real rendered SVG). A single, non-fanned component (side=1.0,
        # the default) keeps the original always-below/right placement.
        offset = 16.0 * side
        lx = cx - math.sin(rad) * offset
        ly = cy + math.cos(rad) * offset
        label_svg, _ = _text(
            lx, ly + label_size * 0.35, [value.strip()],
            font_size=label_size, weight=600, color=theme.muted_color, font_family=theme.font_family,
        )
        svg += label_svg
    return svg


def _circuit_panel_positions(
    node_ids: list[str], edges: list[tuple[str, str]], *, box_x: float, box_y: float, box_w: float, box_h: float
) -> dict[str, tuple[float, float]]:
    """Deterministic layered (Sugiyama-style) layout for one small circuit's
    junction nodes: BFS rank = hop count from the best-connected node, nodes
    sharing a rank stack in one vertical column. This is what makes a series
    chain read left-to-right (A, B, C each one hop further) AND a parallel
    pair read as two columns (both components tie the SAME two nodes, so
    both land at rank 0/rank 1 regardless of which one the model listed
    first) AND an "invalid - different node" variant read as a visible
    mismatch (two SEPARATE nodes that both happen to be one hop from the
    source land in the same column, side by side, rather than being forced
    to overlap at one shared position) - without any kind-specific series-
    vs-parallel branching: the same one algorithm produces a sensible
    picture for all of them, and for any future topology (e.g. a bridge/
    Wheatstone network) nobody has hand-cased here yet."""
    if not node_ids:
        return {}
    adjacency: dict[str, list[str]] = {n: [] for n in node_ids}
    for a, b in edges:
        if a in adjacency and b in adjacency and a != b:
            adjacency[a].append(b)
            adjacency[b].append(a)

    degree = {n: len(adjacency[n]) for n in node_ids}
    # A true endpoint (degree 1, first declared) makes a plain series chain
    # read as a clean straight line left-to-right (A -> B -> C) instead of
    # starting from the middle node and splaying both neighbours out beside
    # it - which is still topologically correct but reads as a "V" rather
    # than a line. Only falls back to "most-connected node" (the old rule,
    # still right for a branching/hub shape - e.g. the invalid pseudo-series
    # variant's extra tap - or a pure parallel pair where every node has
    # degree >= 2) when no true endpoint exists.
    endpoints = [n for n in node_ids if degree[n] == 1]
    root = endpoints[0] if endpoints else max(node_ids, key=lambda n: (degree[n], -node_ids.index(n)))

    rank: dict[str, int] = {root: 0}
    frontier = [root]
    while frontier:
        nxt: list[str] = []
        for n in frontier:
            for neighbour in adjacency[n]:
                if neighbour not in rank:
                    rank[neighbour] = rank[n] + 1
                    nxt.append(neighbour)
        frontier = nxt
    # Any node unreachable from root (a disconnected sub-circuit - not
    # expected for a well-formed panel, but never a crash) gets its own
    # trailing rank so it still renders instead of vanishing.
    next_rank = (max(rank.values()) + 1) if rank else 0
    for n in node_ids:
        if n not in rank:
            rank[n] = next_rank

    ranks_present = sorted(set(rank.values()))
    rank_index = {r: i for i, r in enumerate(ranks_present)}
    num_cols = len(ranks_present)
    columns: dict[int, list[str]] = {}
    for n in node_ids:  # declaration order -> stable within-rank ordering
        columns.setdefault(rank_index[rank[n]], []).append(n)

    positions: dict[str, tuple[float, float]] = {}
    for col, members in columns.items():
        x = box_x + box_w * (col + 1) / (num_cols + 1)
        count = len(members)
        for i, n in enumerate(members):
            y = box_y + box_h * (i + 1) / (count + 1)
            positions[n] = (x, y)
    return positions


# 15.0, not a smaller "fits more on a small panel" size that first looked
# tempting - the same MIN_READABLE_PX floor every other kind's own label
# size constant is calibrated against (see diagram_render_qa.readability_
# issues) would reject anything much smaller once scaled down to the
# document's real on-page width; RING_LABEL_SIZE already proves 15.0 clears
# it comfortably for small circular/compact shapes like these.
_CIRCUIT_LABEL_SIZE = 15.0
_CIRCUIT_HEADER_SIZE = 16.5
_CIRCUIT_CAPTION_SIZE = 15.0
_CIRCUIT_MIN_PANEL_W = 240.0
_CIRCUIT_PANEL_GUTTER = 22.0
_CIRCUIT_ROW_GAP = 20.0
_CIRCUIT_DRAW_HEIGHT = 190.0
# 36.0, not the original 22.0: a fanned rail's own y also has to clear the
# shared junction node's OWN label, which sits a fixed ~10px
# (_CIRCUIT_DOT_R + 6) above/below the node's unfanned y - at 22.0 (±11.0
# half-step) the nearer rail came within ~1px of that node label, a real,
# confirmed near-overlap measured directly from a rendered SVG's own
# coordinates alongside the label-collision bug the `side` parameter above
# fixes.
_CIRCUIT_FAN_STEP = 36.0
_CIRCUIT_DOT_R = 4.0


def _draw_one_circuit_panel(
    *, junctions: list[DiagramNode], caption: DiagramNode | None, edges: list[DiagramEdge],
    lane: str, x: float, y: float, width: float, theme: TemplateTheme,
) -> tuple[str, float]:
    elements: list[str] = []
    cursor_y = y

    if lane.strip():
        header_svg, header_h = _text(
            x + width / 2, cursor_y + _CIRCUIT_HEADER_SIZE, [lane.strip()],
            font_size=_scaled_font_size(_CIRCUIT_HEADER_SIZE), weight=700,
            color=theme.text_color, font_family=theme.font_family,
        )
        elements.append(header_svg)
        cursor_y += header_h + 10.0

    box_x, box_y, box_w, box_h = x + 14.0, cursor_y, width - 28.0, _CIRCUIT_DRAW_HEIGHT
    node_ids = [n.id for n in junctions if n.id]
    positions = _circuit_panel_positions(
        node_ids, [(e.source, e.target) for e in edges], box_x=box_x, box_y=box_y, box_w=box_w, box_h=box_h,
    )
    by_id = {n.id: n for n in junctions if n.id}

    # Group parallel edges (same unordered node pair) so 2+ components tied
    # to the identical two nodes fan out instead of drawing on top of each
    # other - the exact geometry a VALID parallel pair needs to read clearly
    # as "two separate branches between the same two nodes."
    groups: dict[frozenset, list[DiagramEdge]] = {}
    order: list[frozenset] = []
    for e in edges:
        if e.source not in positions or e.target not in positions:
            continue
        key = frozenset((e.source, e.target))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    for key in order:
        group = groups[key]
        ax, ay = positions[group[0].source]
        bx, by = positions[group[0].target]
        # Right-angle (Manhattan) routing via a shared "rail" - not a direct
        # diagonal/curved line - THE visual convention that makes this read
        # as an actual wired circuit rather than a generic node graph.
        # `horizontal` picks which axis the rail runs along: nodes almost
        # always differ more in x (different BFS-rank columns - see
        # _circuit_panel_positions), so the rail is a horizontal run at some
        # y between the two nodes' own y, reached by a short vertical stub
        # off each one. The component sits on the RAIL itself (guaranteed
        # real length = the dominant-axis separation, never zero), not on
        # the stub - which is what keeps this correct even when both nodes
        # happen to share the exact same y (a plain series chain: A, B, C
        # all sit on one centred row - see _circuit_panel_positions - so
        # naive "bend at the midpoint column" routing would put the
        # component on a zero-length vertical stub there instead). A fan
        # offset (multiple components between the SAME two nodes - a
        # parallel pair) shifts the rail itself away from that baseline,
        # which is exactly the classic parallel-resistor "ladder rung"
        # picture: several rails at different offsets, each with its own
        # component, between two shared stub points.
        horizontal = abs(bx - ax) >= abs(by - ay)
        n = len(group)
        for i, e in enumerate(group):
            offset = (i - (n - 1) / 2.0) * _CIRCUIT_FAN_STEP
            if horizontal:
                rail_y = (ay + by) / 2 + offset
                path_d = f"M{ax:.1f},{ay:.1f} L{ax:.1f},{rail_y:.1f} L{bx:.1f},{rail_y:.1f} L{bx:.1f},{by:.1f}"
                mx, my = (ax + bx) / 2, rail_y
                angle_deg = 0.0
                label_dx, label_dy = (0.0, 1.0)
            else:
                rail_x = (ax + bx) / 2 + offset
                path_d = f"M{ax:.1f},{ay:.1f} L{rail_x:.1f},{ay:.1f} L{rail_x:.1f},{by:.1f} L{bx:.1f},{by:.1f}"
                mx, my = rail_x, (ay + by) / 2
                angle_deg = 90.0
                label_dx, label_dy = (1.0, 0.0)
            elements.append(f'<path d="{path_d}" fill="none" stroke="{theme.text_color}" stroke-width="1.8"/>')
            # Which side of the shared centerline THIS rail fanned to (see
            # _draw_circuit_component's own comment) - a single, non-fanned
            # edge (offset == 0, n == 1) keeps the original default (1.0).
            side = -1.0 if offset < -0.01 else 1.0
            component_kind, value = _parse_circuit_component(e.label)
            if component_kind != "wire":
                elements.append(
                    _draw_circuit_component(mx, my, angle_deg, component_kind, value, theme=theme, side=side)
                )
            elif value:
                # A plain (uncomponented) wire's own text, e.g. a bare node-
                # tap annotation - offset to the side of its own rung (and,
                # same reasoning as the component label above, AWAY from a
                # fanned sibling's own side) so it never collides with it.
                label_size = _scaled_font_size(_CIRCUIT_LABEL_SIZE)
                offset_label = 14.0 * side
                lx = mx + label_dx * offset_label
                ly = my + label_dy * offset_label
                label_svg, _ = _text(
                    lx, ly + label_size * 0.35, [value], font_size=label_size, weight=500,
                    color=theme.muted_color, font_family=theme.font_family,
                )
                elements.append(label_svg)

    for node_id, (nx, ny) in positions.items():
        node = by_id.get(node_id)
        # Deliberately NOT wrapped in `class="diagram-node"`/"diagram-box"`
        # (the convention `_node_boxes`/`text_overflow_issues` key off of,
        # see app.services.diagram_render_qa's own module docstring): that
        # pairing assumes the label is drawn INSIDE its own box, the same
        # way `_node_block` always has - a junction's label is deliberately
        # drawn just OUTSIDE its tiny dot instead (see below), which would
        # read as a false "text overflow" against that assumption. Same
        # reasoning the existing external-label primitives (`_anchored_
        # label`/`_caption_text`, used by donut_segments/orbit_diagram) are
        # already exempt for.
        elements.append(f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="{_CIRCUIT_DOT_R:.1f}" fill="{theme.text_color}"/>')
        label = (node.label.strip() if node else "") or node_id
        label_size = _scaled_font_size(_CIRCUIT_LABEL_SIZE)
        below = ny < box_y + box_h / 2
        label_y = ny + (label_size + 8.0 if below else -(_CIRCUIT_DOT_R + 6.0))
        label_svg, _ = _text(
            nx, label_y, [label], font_size=label_size, weight=600,
            color=theme.text_color, font_family=theme.font_family,
        )
        elements.append(label_svg)

    cursor_y = box_y + box_h + 10.0

    if caption is not None and (caption.label.strip() or caption.detail.strip()):
        verdict = caption.label.strip().lower()
        # Checked in this order deliberately: "invalid" contains "valid" as
        # a literal substring, so testing for it second (only once the
        # "invalid"/"✗" case has already been ruled out) is what keeps
        # "✗ Invalid" from ever being misread as the valid/green case.
        is_invalid = "invalid" in verdict or caption.label.strip().startswith("✗")
        is_valid = not is_invalid and ("valid" in verdict or caption.label.strip().startswith("✓"))
        badge_color = "#dc2626" if is_invalid else ("#16a34a" if is_valid else theme.muted_color)
        if caption.label.strip():
            badge_svg, badge_h = _text(
                x + width / 2, cursor_y + _CIRCUIT_CAPTION_SIZE, [caption.label.strip()],
                font_size=_scaled_font_size(_CIRCUIT_CAPTION_SIZE), weight=700,
                color=badge_color, font_family=theme.font_family,
            )
            elements.append(badge_svg)
            cursor_y += badge_h + 4.0
        if caption.detail.strip():
            detail_lines = _wrap(caption.detail, font_size=_CIRCUIT_CAPTION_SIZE, width=width - 16.0, max_lines=3)
            detail_svg, detail_h = _text(
                x + width / 2, cursor_y + _CIRCUIT_CAPTION_SIZE, detail_lines,
                font_size=_scaled_font_size(_CIRCUIT_CAPTION_SIZE), weight=400,
                color=theme.muted_color, font_family=theme.font_family,
            )
            elements.append(detail_svg)
            cursor_y += detail_h

    panel_border = (
        f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="{width:.1f}" height="{(cursor_y - y + 20):.1f}" '
        f'rx="10" fill="none" stroke="{theme.border_color}" stroke-width="1.2"/>'
    )
    elements.insert(0, panel_border)
    return "".join(elements), cursor_y - y + 20


def _layout_circuit_schematic(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """One circuit, or (when `DiagramNode.lane` varies) several small
    captioned circuit panels side by side - the multi-scenario comparison
    shape "Series vs Parallel - Valid and Invalid Reductions" needs, reusing
    the exact same "grow down into another row, never shrink past a
    readable floor" wrapping rule `_layout_comparison` already established
    for its own card grid."""
    panels: dict[str, list[DiagramNode]] = {}
    panel_order: list[str] = []
    for node in nodes:
        lane = node.lane.strip()
        if lane not in panels:
            panels[lane] = []
            panel_order.append(lane)
        panels[lane].append(node)

    if not panel_order:
        return [], 0.0

    available = CANVAS_WIDTH - 2 * MARGIN
    cols_per_row = max(
        1, int((available + _CIRCUIT_PANEL_GUTTER) / (_CIRCUIT_MIN_PANEL_W + _CIRCUIT_PANEL_GUTTER))
    )
    cols_per_row = min(cols_per_row, len(panel_order))
    panel_w = (available - (cols_per_row - 1) * _CIRCUIT_PANEL_GUTTER) / cols_per_row

    elements: list[str] = []
    y = top
    for row_start in range(0, len(panel_order), cols_per_row):
        row_lanes = panel_order[row_start : row_start + cols_per_row]
        x = MARGIN
        row_height = 0.0
        for lane in row_lanes:
            panel_nodes = panels[lane]
            caption = next((n for n in panel_nodes if n.shape_role == "caption"), None)
            junctions = [n for n in panel_nodes if n.shape_role != "caption" and n.id]
            junction_ids = {n.id for n in junctions}
            panel_edges = [e for e in edges if e.source in junction_ids and e.target in junction_ids]
            svg, height = _draw_one_circuit_panel(
                junctions=junctions, caption=caption, edges=panel_edges,
                lane=lane, x=x, y=y, width=panel_w, theme=theme,
            )
            elements.append(svg)
            row_height = max(row_height, height)
            x += panel_w + _CIRCUIT_PANEL_GUTTER
        y += row_height + _CIRCUIT_ROW_GAP

    return elements, y - _CIRCUIT_ROW_GAP - top


# --- periodic_table: the real, fixed 118-element grid -----------------------
# (app.render.periodic_table_data) - see DIAGRAM_KINDS' own comment in
# app.schemas.diagram for why this is a hardcoded reference table, never
# authored by a model, the same way `toc` is a deterministic page nothing
# ever asks the writer agent to invent.

# 16.0, not a smaller value that first looked tempting - the readability
# floor this whole module's render-QA checker enforces (MIN_READABLE_PX,
# app.services.diagram_render_qa) is checked AFTER scaling this diagram's
# own CANVAS_WIDTH (880) down to the page's real display width
# (CONTENT_WIDTH, 666) - a real, confirmed case: 14.0 scaled to ~10.6px,
# just under the 11.0px floor, failing every real generation attempt.
# 16.0 scales to ~12.1px, comfortably clear.
# 43.0, not 46.0 (first tried, before 16.0's extra width pushed the 18th
# column past CANVAS_WIDTH - a real, confirmed `out_of_canvas` finding):
# (CANVAS_WIDTH - 2*MARGIN - _PT_GUTTER) / 18 columns, exactly - leaves the
# same right-hand margin every other kind's canvas already keeps.
_PT_CELL_W = 43.0
_PT_CELL_H = 50.0
_PT_GUTTER = 26.0  # narrow left column for period numbers
_PT_SYMBOL_SIZE = 16.0
_PT_NUMBER_SIZE = 9.0
_PT_STRIP_GAP = 18.0  # blank row between the main grid and the f-block strips


def _pt_cell(
    x: float, y: float, *, symbol: str, atomic_number: int, fill: str, stroke: str, theme: TemplateTheme,
    highlight_detail: str = "",
) -> str:
    tooltip = f"{symbol} ({atomic_number})"
    if highlight_detail:
        tooltip = f"{tooltip} — {highlight_detail}"
    parts = [
        f"<title>{escape(tooltip)}</title>",
        f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{_PT_CELL_W - 3:.1f}" '
        f'height="{_PT_CELL_H - 3:.1f}" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>',
        f'<text x="{x + 5:.1f}" y="{y + _PT_NUMBER_SIZE + 4:.1f}" font-size="{_PT_NUMBER_SIZE:.1f}" '
        f'fill="{theme.muted_color}" text-anchor="start" font-family="{escape(theme.font_family)}">'
        f"{atomic_number}</text>",
        f'<text x="{x + (_PT_CELL_W - 3) / 2:.1f}" y="{y + _PT_CELL_H / 2 + 10:.1f}" '
        f'font-size="{_PT_SYMBOL_SIZE:.1f}" font-weight="700" fill="{theme.text_color}" '
        f'text-anchor="middle" font-family="{escape(theme.font_family)}">{escape(symbol)}</text>',
    ]
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>'


def _layout_periodic_table(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """The real 118-element grid, always drawn complete and scientifically
    correct from `app.render.periodic_table_data.ELEMENTS` - `nodes` never
    supplies the grid itself (see DiagramSpec.is_usable's own periodic_table
    case), only an OPTIONAL small set of highlighted cells: a node whose
    `id` matches a real element symbol (case-insensitive) gets its own
    `color` as a fill override and, if `detail` is set, a line in the small
    legend below the grid - exactly the "which cells to annotate, never the
    grid itself" contract DIAGRAM_KINDS' own periodic_table comment
    describes."""
    from app.render.periodic_table_data import ACTINIDES, BY_SYMBOL, LANTHANIDES, MAIN_GRID, block_fill

    highlights = {
        n.id.strip().lower(): n for n in nodes if n.id and n.id.strip().lower() in BY_SYMBOL
    }

    grid_x0 = MARGIN + _PT_GUTTER
    elements: list[str] = []

    def _draw_row(items: list, *, row_top: float, col_of) -> None:
        for el in items:
            col = col_of(el)
            x = grid_x0 + (col - 1) * _PT_CELL_W
            highlight = highlights.get(el.symbol.lower())
            fill = (highlight.color.strip() if highlight and highlight.color.strip() else block_fill(el.block))
            stroke = theme.border_color if not highlight else theme.accent_color
            elements.append(
                _pt_cell(
                    x, row_top, symbol=el.symbol, atomic_number=el.atomic_number,
                    fill=fill, stroke=stroke, theme=theme,
                    highlight_detail=highlight.detail if highlight else "",
                )
            )

    for period in range(1, 8):
        row_top = top + (period - 1) * _PT_CELL_H
        _draw_row([e for e in MAIN_GRID if e.period == period], row_top=row_top, col_of=lambda e: e.group)

    grid_bottom = top + 7 * _PT_CELL_H
    lant_top = grid_bottom + _PT_STRIP_GAP
    act_top = lant_top + _PT_CELL_H
    _draw_row(LANTHANIDES, row_top=lant_top, col_of=lambda e: (e.f_index or 0) + 2)
    _draw_row(ACTINIDES, row_top=act_top, col_of=lambda e: (e.f_index or 0) + 2)

    cursor_y = act_top + _PT_CELL_H + 10.0

    if any(n.detail.strip() for n in highlights.values()):
        cursor_y += 10.0
        for symbol_id, node in [(sid, n) for sid, n in highlights.items() if n.detail.strip()]:
            element = BY_SYMBOL[symbol_id]
            swatch_color = node.color.strip() if node.color.strip() else block_fill(element.block)
            elements.append(
                f'<rect x="{MARGIN:.1f}" y="{cursor_y - 11:.1f}" width="14" height="14" rx="3" '
                f'fill="{swatch_color}" stroke="{theme.border_color}" stroke-width="1"/>'
            )
            label_svg, label_h = _text(
                MARGIN + 22, cursor_y, [f"{element.symbol} ({element.name}) — {node.detail.strip()}"],
                font_size=_scaled_font_size(12.5), weight=500, color=theme.text_color,
                font_family=theme.font_family, anchor="start",
            )
            elements.append(label_svg)
            cursor_y += max(label_h, 18.0) + 6.0

    return elements, cursor_y - top


# --- graph/chart (kind == "graph") -------------------------------------------

# Fixed, not theme-derived - same "universal convention, not brand colour"
# reasoning as _FLOW_ROLE_COLORS/_HIERARCHY_LEVEL_COLORS above. Cycles so any
# count of functions/series still gets a distinct, readable colour.
_GRAPH_SERIES_COLORS = ("#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2")

_GRAPH_PLOT_HEIGHT = 320.0
_GRAPH_AXIS_LABEL_SIZE = 13.0
_GRAPH_TICK_LABEL_SIZE = 12.0
_GRAPH_LEGEND_ROW_HEIGHT = 20.0


def _fmt_tick(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _layout_graph(spec: DiagramSpec, *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """Mathematical function plots and categorical bar/line charts -
    deterministic, never a raster image: axes, gridlines and labels are
    drawn directly, and plotted content comes from real data (a function
    safely sampled via app.render.graph_math, or the spec's own series
    values) - never an invented scale or dataset."""
    elements: list[str] = []
    plot_left = MARGIN + 56.0
    plot_right = CANVAS_WIDTH - MARGIN - 16.0
    plot_width = plot_right - plot_left
    plot_top = top + 8.0
    plot_bottom = plot_top + _GRAPH_PLOT_HEIGHT

    is_function = spec.graph_type == "function"
    series_labels: list[str]
    if is_function:
        x_min = spec.x_min if spec.x_min is not None else -10.0
        x_max = spec.x_max if spec.x_max is not None else 10.0
        curves = [
            (fn.label or fn.expression, sample_function(fn.expression, x_min, x_max))
            for fn in spec.functions
            if fn.expression.strip()
        ]
        curves = [(label, points) for label, points in curves if points]
        series_labels = [label for label, _ in curves]
        all_y = [y for _, points in curves for _, y in points]
        y_lo, y_hi = (min(all_y), max(all_y)) if all_y else (-1.0, 1.0)
        pad = (y_hi - y_lo) * 0.1 or 1.0
        y_lo, y_hi = y_lo - pad, y_hi + pad

        def scale_x(value: float) -> float:
            return plot_left + (value - x_min) / (x_max - x_min) * plot_width

    else:
        categories = spec.categories
        series_labels = [s.label or f"Series {i + 1}" for i, s in enumerate(spec.series)]
        all_values = [v for s in spec.series for v in s.values]
        y_lo, y_hi = (min(all_values), max(all_values)) if all_values else (0.0, 1.0)
        if spec.graph_type == "bar":
            # Bar height is a visual proportion of the value - starting
            # anywhere but zero would misrepresent every bar's real size,
            # the standard bar-chart convention. A line chart has no such
            # convention, so it keeps the padded-around-the-data range below.
            y_lo = min(0.0, y_lo)
            y_hi = max(0.0, y_hi) + (y_hi - y_lo) * 0.1 or 1.0
        else:
            pad = (y_hi - y_lo) * 0.1 or 1.0
            y_lo, y_hi = y_lo - pad, y_hi + pad
        slot_width = plot_width / max(len(categories), 1)

        def scale_x(index: float) -> float:  # noqa: ANN001 - index is a category position here
            return plot_left + index * slot_width

    # Snap the drawn axis extent to bracket the computed "nice" ticks (the
    # same convention most charting libraries use) - without this, a padded
    # range that doesn't land on a round number (e.g. 0..97) leaves the top
    # of the plot with no gridline at all, which reads as unfinished rather
    # than textbook-clean.
    y_ticks = nice_ticks(y_lo, y_hi)
    y_lo, y_hi = min(y_lo, y_ticks[0]), max(y_hi, y_ticks[-1])

    def scale_y(value: float) -> float:
        if y_hi <= y_lo:
            return plot_bottom
        return plot_bottom - (value - y_lo) / (y_hi - y_lo) * _GRAPH_PLOT_HEIGHT

    # Plot area background + border, gridlines and y-axis ticks.
    elements.append(
        f'<rect x="{plot_left:.1f}" y="{plot_top:.1f}" width="{plot_width:.1f}" '
        f'height="{_GRAPH_PLOT_HEIGHT:.1f}" fill="{theme.surface_color}" '
        f'stroke="{theme.border_color}" stroke-width="1"/>'
    )
    for tick in y_ticks:
        ty = scale_y(tick)
        elements.append(
            f'<line x1="{plot_left:.1f}" y1="{ty:.1f}" x2="{plot_right:.1f}" y2="{ty:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        label_svg, _ = _text(
            plot_left - 8, ty + _GRAPH_TICK_LABEL_SIZE / 3, [_fmt_tick(tick)],
            font_size=_GRAPH_TICK_LABEL_SIZE, weight=400, color=theme.muted_color,
            font_family=theme.font_family, anchor="end",
        )
        elements.append(label_svg)

    if is_function:
        for tick in nice_ticks(x_min, x_max):
            tx = scale_x(tick)
            label_svg, _ = _text(
                tx, plot_bottom + _GRAPH_TICK_LABEL_SIZE + 4, [_fmt_tick(tick)],
                font_size=_GRAPH_TICK_LABEL_SIZE, weight=400, color=theme.muted_color,
                font_family=theme.font_family,
            )
            elements.append(label_svg)
        # x and y axes themselves, where they cross zero (or the plot's
        # own edge when zero is outside the visible range).
        zero_y = scale_y(0.0) if y_lo <= 0.0 <= y_hi else plot_bottom
        elements.append(
            f'<line x1="{plot_left:.1f}" y1="{zero_y:.1f}" x2="{plot_right:.1f}" y2="{zero_y:.1f}" '
            f'stroke="{theme.text_color}" stroke-width="1.5"/>'
        )
        zero_x = scale_x(0.0) if x_min <= 0.0 <= x_max else plot_left
        elements.append(
            f'<line x1="{zero_x:.1f}" y1="{plot_top:.1f}" x2="{zero_x:.1f}" y2="{plot_bottom:.1f}" '
            f'stroke="{theme.text_color}" stroke-width="1.5"/>'
        )
        for index, (_, points) in enumerate(curves):
            color = _GRAPH_SERIES_COLORS[index % len(_GRAPH_SERIES_COLORS)]
            path = " ".join(
                f"{'M' if i == 0 else 'L'}{scale_x(x):.1f},{scale_y(y):.1f}" for i, (x, y) in enumerate(points)
            )
            elements.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
    else:
        for index, category in enumerate(categories):
            cx = scale_x(index) + slot_width / 2
            label_svg, _ = _text(
                cx, plot_bottom + _GRAPH_TICK_LABEL_SIZE + 4,
                _wrap(category, font_size=_GRAPH_TICK_LABEL_SIZE, width=slot_width),
                font_size=_GRAPH_TICK_LABEL_SIZE, weight=400, color=theme.muted_color,
                font_family=theme.font_family,
            )
            elements.append(label_svg)
        zero_y = scale_y(0.0)
        elements.append(
            f'<line x1="{plot_left:.1f}" y1="{zero_y:.1f}" x2="{plot_right:.1f}" y2="{zero_y:.1f}" '
            f'stroke="{theme.text_color}" stroke-width="1.5"/>'
        )
        if spec.graph_type == "bar":
            series_count = max(len(spec.series), 1)
            bar_gutter = slot_width * 0.15
            group_width = slot_width - bar_gutter
            bar_width = group_width / series_count
            for s_index, s in enumerate(spec.series):
                color = _GRAPH_SERIES_COLORS[s_index % len(_GRAPH_SERIES_COLORS)]
                for c_index, value in enumerate(s.values):
                    bx = scale_x(c_index) + bar_gutter / 2 + s_index * bar_width
                    by = scale_y(max(value, 0.0))
                    bh = abs(scale_y(value) - scale_y(0.0))
                    elements.append(
                        f'<rect x="{bx:.1f}" y="{by:.1f}" width="{(bar_width * 0.85):.1f}" '
                        f'height="{bh:.1f}" fill="{color}" rx="2"/>'
                    )
        else:  # line
            for s_index, s in enumerate(spec.series):
                color = _GRAPH_SERIES_COLORS[s_index % len(_GRAPH_SERIES_COLORS)]
                points = [(scale_x(i) + slot_width / 2, scale_y(v)) for i, v in enumerate(s.values)]
                path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
                elements.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
                for x, y in points:
                    elements.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')

    bottom = plot_bottom + _GRAPH_TICK_LABEL_SIZE + 18.0

    if spec.x_label.strip():
        label_svg, label_h = _text(
            (plot_left + plot_right) / 2, bottom + _GRAPH_AXIS_LABEL_SIZE, [spec.x_label],
            font_size=_GRAPH_AXIS_LABEL_SIZE, weight=600, color=theme.text_color, font_family=theme.font_family,
        )
        elements.append(label_svg)
        bottom += label_h + 10.0

    if spec.y_label.strip():
        mid_y = (plot_top + plot_bottom) / 2
        elements.append(
            f'<text x="{(MARGIN + 14):.1f}" y="{mid_y:.1f}" font-size="{_GRAPH_AXIS_LABEL_SIZE:.1f}" '
            f'font-weight="600" fill="{theme.text_color}" text-anchor="middle" '
            f'font-family="{escape(theme.font_family)}" '
            f'transform="rotate(-90 {(MARGIN + 14):.1f} {mid_y:.1f})">{escape(spec.y_label)}</text>'
        )

    if len(series_labels) > 1 or not is_function:
        legend_y = bottom + _GRAPH_LEGEND_ROW_HEIGHT / 2
        for index, label in enumerate(series_labels):
            color = _GRAPH_SERIES_COLORS[index % len(_GRAPH_SERIES_COLORS)]
            row_y = legend_y + index * _GRAPH_LEGEND_ROW_HEIGHT
            elements.append(f'<rect x="{plot_left:.1f}" y="{row_y - 9:.1f}" width="14" height="14" fill="{color}" rx="2"/>')
            label_svg, _ = _text(
                plot_left + 20, row_y + 4, [label],
                font_size=_GRAPH_TICK_LABEL_SIZE, weight=500, color=theme.text_color,
                font_family=theme.font_family, anchor="start",
            )
            elements.append(label_svg)
        bottom = legend_y + len(series_labels) * _GRAPH_LEGEND_ROW_HEIGHT

    return elements, bottom - top


# ---------------------------------------------------------------------------
# pyramid / funnel, matrix, radial mind map, venn - new SmartArt-style kinds.
# All four follow the exact same discipline every kind above already does:
# a shape only ever holds a short label (never truncated - grow/shrink-to-
# fit, never `_wrap`'s own ellipsis), the full explanation lives in the
# shared Key table below instead, and every drawn shape/arrow carries the
# common `class="diagram-box"`/`class="diagram-node"` markers so the
# existing render-QA geometry checks and hover/focus CSS apply unchanged.
# ---------------------------------------------------------------------------

_PYRAMID_LABEL_SIZE = 15.0
_PYRAMID_MIN_LAYER_H = 56.0
_PYRAMID_APEX_W = 140.0
_PYRAMID_BASE_W = 640.0


def _layout_pyramid(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float, inverted: bool,
) -> tuple[list[str], float]:
    """Stacked trapezoid layers narrowing toward one end - a classic
    pyramid (inverted=False: `nodes[0]` is the narrow APEX/top layer,
    `nodes[-1]` the wide BASE/bottom layer - "levels of importance/
    foundation" content) or a funnel (inverted=True: the same node order,
    just drawn wide-top-narrow-bottom instead - "narrowing stages"
    content). One fixed semantic colour per layer (not theme-derived - the
    same "fixed palette" convention _WHEEL_SEGMENT_COLORS already uses),
    cycling if there are more layers than colours. A layer's own short
    label is centred and shrink-to-fit (_fit_boxed_text) - never
    `node.detail`, which has almost no room in a narrow apex layer; detail
    is a Key table row below instead, same rule as every ring/wheel/
    flowchart node above.

    Each layer draws a small OFFSET duplicate of its own trapezoid behind
    the real one (same points, shifted down-right, filled with that
    layer's own `stroke` tone) - a flat-design "stacked block" depth cue
    (the reference infographic template's own 3D pyramid style), zero new
    geometry math since it reuses the exact same `points` string already
    computed for the real shape, so it can never drift out of alignment
    with it or introduce a new text-fit/overlap risk."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    cx = CANVAS_WIDTH / 2
    layer_h = max(_PYRAMID_MIN_LAYER_H, min(110.0, 420.0 / count))
    if inverted:
        edge_widths = [_PYRAMID_BASE_W - (_PYRAMID_BASE_W - _PYRAMID_APEX_W) * k / count for k in range(count + 1)]
    else:
        edge_widths = [_PYRAMID_APEX_W + (_PYRAMID_BASE_W - _PYRAMID_APEX_W) * k / count for k in range(count + 1)]

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    y = top
    depth = 7.0
    for index, node in enumerate(nodes):
        w_top, w_bottom = edge_widths[index], edge_widths[index + 1]
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        points = (
            f"{cx - w_top / 2:.1f},{y:.1f} {cx + w_top / 2:.1f},{y:.1f} "
            f"{cx + w_bottom / 2:.1f},{y + layer_h:.1f} {cx - w_bottom / 2:.1f},{y + layer_h:.1f}"
        )
        shadow_points = (
            f"{cx - w_top / 2 + depth:.1f},{y + depth:.1f} {cx + w_top / 2 + depth:.1f},{y + depth:.1f} "
            f"{cx + w_bottom / 2 + depth:.1f},{y + layer_h + depth:.1f} {cx - w_bottom / 2 + depth:.1f},{y + layer_h + depth:.1f}"
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(f'<polygon points="{shadow_points}" fill="{stroke}" opacity="0.35"/>')
        parts.append(
            f'<polygon class="diagram-box" points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        fit_w = max(min(w_top, w_bottom) * 0.9, 60.0)
        parts.append(
            _fit_boxed_text(
                cx, y + layer_h / 2, node.label, width=fit_w, height=layer_h * 0.75,
                color=theme.text_color, theme=theme, base_size=_PYRAMID_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))
        y += layer_h

    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=y + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (y + key_gap + key_h - top)


# Same post-scale reasoning as RING_LABEL_SIZE's own comment: this kind's
# canvas stays a fixed CANVAS_WIDTH regardless of node count (more nodes
# only add rows, never width - see _layout_honeycomb), so the display
# scale is always ~0.757x; 15.0 clears MIN_READABLE_PX
# (app.services.diagram_render_qa) with the same real margin RING_
# LABEL_SIZE does.
_HONEYCOMB_LABEL_SIZE = 15.0


def _layout_honeycomb(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A cluster of pointy-top hexagons tessellated in horizontal rows,
    alternate rows offset by half a hexagon's width so they interlock -
    the classic "honeycomb" SmartArt look for a SET of related categories/
    components with no inherent order (as opposed to chevron_process's
    row of arrows, which IS an ordered sequence - see RELATIONSHIP_KINDS'
    own docstring on that distinction). Each cell's own short label is
    centred and shrink-to-fit (_fit_boxed_text) with an optional icon
    badge above it; the full explanation is a Key table row below, the
    same rule every kind in this module already follows. Row width is
    chosen to keep the cluster roughly as wide as it is tall rather than
    either a single long row or a single tall column, for the same
    "reads as one cohesive cluster" reason _layout_matrix picks its own
    `ceil(sqrt(n))` column count."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    cols = max(math.ceil(math.sqrt(count * 1.3)), 2)
    hex_r = max(min(70.0, (CANVAS_WIDTH - 2 * MARGIN) / (cols * 1.73 + 0.87) ), 46.0)
    hex_w = hex_r * math.sqrt(3)
    row_step = hex_r * 1.5
    rows = math.ceil(count / cols)
    cluster_w = cols * hex_w + hex_w / 2
    start_x = (CANVAS_WIDTH - cluster_w) / 2 + hex_w / 2

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    palette = _cycling_palette(theme)
    index = 0
    max_cy = top
    for row in range(rows):
        row_offset = (hex_w / 2) if row % 2 == 1 else 0.0
        row_count = min(cols, count - index)
        if row_count <= 0:
            break
        for col in range(row_count):
            node = nodes[index]
            cx = start_x + col * hex_w + row_offset
            cy = top + hex_r + row * row_step
            fill, stroke = palette[index % len(palette)]
            points = " ".join(
                f"{cx + hex_r * math.cos(math.radians(-90 + 60 * i)):.1f},"
                f"{cy + hex_r * math.sin(math.radians(-90 + 60 * i)):.1f}"
                for i in range(6)
            )
            tooltip = node.label.strip()
            if node.detail.strip():
                tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
            parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
            parts.append(f'<polygon class="diagram-box" points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            icon_svg = _icon_badge(node, cx=cx, cy=cy - hex_r * 0.42, theme=theme, color=stroke)
            label_cy = cy + (hex_r * 0.28 if icon_svg else 0.0)
            if icon_svg:
                parts.append(icon_svg)
            parts.append(
                # min_size pinned close to base_size (never _fit_boxed_text's
                # usual lenient default) - shrinking below ~14.5 here would
                # cross the same readability floor base_size is already
                # sized to clear; an exceptionally long label truncates
                # instead (see _wrap's own ellipsis fallback), never renders
                # unreadably small.
                _fit_boxed_text(
                    cx, label_cy, node.label, width=hex_w * 0.8, height=hex_r * (0.7 if icon_svg else 1.1),
                    color=theme.text_color, theme=theme, base_size=_HONEYCOMB_LABEL_SIZE, min_size=14.5,
                )
            )
            elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
            key_entries.append((str(index + 1), node.label, node.detail))
            max_cy = max(max_cy, cy + hex_r)
            index += 1

    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=max_cy + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (max_cy + key_gap + key_h - top)


# Same reasoning as _HONEYCOMB_LABEL_SIZE above, but this kind's canvas DOES
# grow wider with more pieces (a horizontal row, like chevron_process) - see
# max_nodes_for's own puzzle_pieces case for the node-count cap that keeps
# that growth from ever crossing the readability floor at this base size.
_PUZZLE_LABEL_SIZE = 15.0
_PUZZLE_MIN_SIZE = 130.0
_PUZZLE_TAB_R = 16.0


def _layout_puzzle_pieces(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A horizontal row of square puzzle pieces, each interlocking with
    its neighbour via a circular tab (bulging right, into the next piece)
    and a matching notch (cut into the left edge, where the previous
    piece's tab fits) - the classic "components of one whole/strategy"
    SmartArt look. The first piece's left edge and the last piece's right
    edge stay flat (no tab/notch), the same "first/last edge stays plain"
    convention _layout_chevron_process already uses for its own strip.
    Canvas grows wider to fit more pieces rather than wrapping, same as
    chevron_process/timeline/staircase_process."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    size = _PUZZLE_MIN_SIZE
    total_w = count * size
    canvas_w = max(CANVAS_WIDTH, total_w + 2 * MARGIN)
    start_x = (canvas_w - total_w) / 2
    y = top
    tab_r = _PUZZLE_TAB_R
    mid = size / 2

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    palette = _cycling_palette(theme)
    for index, node in enumerate(nodes):
        x = start_x + index * size
        fill, stroke = palette[index % len(palette)]
        has_right_tab = index < count - 1
        has_left_notch = index > 0
        d = [f"M{x:.1f},{y:.1f}", f"L{x + size:.1f},{y:.1f}"]
        if has_right_tab:
            d.append(f"L{x + size:.1f},{y + mid - tab_r:.1f}")
            d.append(f"A{tab_r:.1f},{tab_r:.1f} 0 0 1 {x + size:.1f},{y + mid + tab_r:.1f}")
        d.append(f"L{x + size:.1f},{y + size:.1f}")
        d.append(f"L{x:.1f},{y + size:.1f}")
        if has_left_notch:
            d.append(f"L{x:.1f},{y + mid + tab_r:.1f}")
            d.append(f"A{tab_r:.1f},{tab_r:.1f} 0 0 1 {x:.1f},{y + mid - tab_r:.1f}")
        d.append("Z")
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(f'<path class="diagram-box" d="{" ".join(d)}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        icon_svg = _icon_badge(node, cx=x + mid, cy=y + mid - 28.0, theme=theme, color=stroke)
        label_cy = y + mid + (14.0 if icon_svg else 0.0)
        if icon_svg:
            parts.append(icon_svg)
        parts.append(
            _fit_boxed_text(
                x + mid, label_cy, node.label, width=size - 2 * tab_r - 16.0, height=size * (0.45 if icon_svg else 0.75),
                color=theme.text_color, theme=theme, base_size=_PUZZLE_LABEL_SIZE, min_size=14.5,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))

    bottom = y + size
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_GEAR_RADIUS = 58.0
_GEAR_TEETH = 10
_GEAR_GAP = 46.0
# Same readability-floor reasoning as _HONEYCOMB_LABEL_SIZE/_PUZZLE_LABEL_SIZE
# above - this kind's canvas also grows wider with more gears (a single
# row, like puzzle_pieces/chevron_process); see max_nodes_for's own
# gears_process case for the node-count cap that keeps that growth from
# ever crossing MIN_READABLE_PX at this base size.
_GEAR_LABEL_SIZE = 15.0


def _gear_points(cx: float, cy: float, r: float, *, teeth: int = _GEAR_TEETH) -> str:
    """A simple cogwheel outline: `teeth` alternating outer/inner radius
    points - the same geometry `_icon_gear` draws as a small badge glyph,
    scaled up and drawn as a real filled/stroked node shape here instead
    of a decorative icon."""
    inner_r = r * 0.78
    n = teeth * 2
    points = []
    for i in range(n):
        angle = 2 * math.pi * i / n
        radius = r if i % 2 == 0 else inner_r
        points.append(f"{cx + radius * math.cos(angle):.1f},{cy + radius * math.sin(angle):.1f}")
    return " ".join(points)


def _layout_gears_process(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A horizontal row of cogwheel shapes, each with a numbered hub and
    its label below - the reference infographic template's own "gears"
    SmartArt look for a short mechanism/process whose steps genuinely
    work together as one system (as opposed to chevron_process's arrow
    strip, which is for an ordinary linear sequence with no "these parts
    mesh together" framing). Evenly spaced with a real gap (never
    overlapping - unlike honeycomb's hexagons, two gears don't need to
    touch to read as "this mechanism," and a real tooth-mesh render would
    need exactly-matching tooth phase between neighbours, not worth the
    complexity for a decorative diagram). Canvas grows wider to fit more
    gears, same convention as chevron_process/timeline."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    r = _GEAR_RADIUS
    slot_w = 2 * r + _GEAR_GAP
    total_w = count * slot_w - _GEAR_GAP
    canvas_w = max(CANVAS_WIDTH, total_w + 2 * MARGIN)
    start_x = (canvas_w - total_w) / 2 + r
    cy = top + r

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        cx = start_x + index * slot_w
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<polygon class="diagram-box" points="{_gear_points(cx, cy, r)}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        hub_r = r * 0.46
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{hub_r:.1f}" fill="{theme.page_background}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 6.5:.1f}" font-size="18" font-weight="700" '
            f'fill="{stroke}" text-anchor="middle" font-family="{escape(theme.font_family)}">{index + 1}</text>'
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        elements.append(
            _fit_boxed_text(
                cx, cy + r + 34.0, node.label, width=slot_w - 14.0, height=60.0,
                color=theme.text_color, theme=theme, base_size=_GEAR_LABEL_SIZE, min_size=14.5,
            )
        )
        key_entries.append((str(index + 1), node.label, node.detail))

    bottom = cy + r + 64.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_MATRIX_LABEL_SIZE = 15.0
_MATRIX_CELL_H = 150.0


def _layout_matrix(spec: DiagramSpec, *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """A quadrant/classification grid - one cell per node, reading order
    (row-major: top-left, top-right, ..., same order as `nodes`). Exactly
    4 labelled nodes PLUS at least one of the `matrix_x_*`/`matrix_y_*`
    fields set draws the classic 2-axis quadrant cross (axis titles,
    endpoint labels, a dividing "+") - a SWOT-style grid is just this with
    no axis fields set at all, which renders as a plain 2x2 grid instead
    (see DiagramSpec's own field docstrings - opt-in, never inferred). Any
    other node count (a 3x3 priority grid, a simple classification list)
    renders as a plain `ceil(sqrt(n))`-column grid, no axis cross. A cell's
    own short label is shrink-to-fit centred (_fit_boxed_text); the full
    explanation is a Key table row below, same rule as every kind above."""
    nodes = spec.nodes
    count = len(nodes)
    if count == 0:
        return [], 0.0
    cols = max(math.ceil(math.sqrt(count)), 1)
    rows = math.ceil(count / cols)
    draw_axis_cross = count == 4 and any(
        s.strip() for s in (
            spec.matrix_x_label, spec.matrix_y_label,
            spec.matrix_x_low, spec.matrix_x_high, spec.matrix_y_low, spec.matrix_y_high,
        )
    )
    axis_margin = 52.0 if draw_axis_cross else 0.0
    grid_left = MARGIN + axis_margin
    # A real, confirmed bug: reserving the y-axis label's gutter ONLY on
    # the left (subtracting axis_margin just once here) left the grid's
    # own right edge flush with the canvas margin while its left edge sat
    # inset by axis_margin - the grid itself (the dominant visual element)
    # read as shifted right of centre, even though "axis label + grid"
    # together technically filled the margin-to-margin width. Reserving a
    # MATCHING empty gutter on the right (subtracting axis_margin twice)
    # keeps the grid itself truly centred on the canvas, same as its own
    # title above it.
    grid_w = CANVAS_WIDTH - 2 * MARGIN - 2 * axis_margin
    cell_w = grid_w / cols
    grid_top = top

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        r, c = divmod(index, cols)
        x = grid_left + c * cell_w
        y = grid_top + r * _MATRIX_CELL_H
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<rect class="diagram-box" x="{x + 4:.1f}" y="{y + 4:.1f}" '
            f'width="{cell_w - 8:.1f}" height="{_MATRIX_CELL_H - 8:.1f}" rx="10" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        parts.append(
            _fit_boxed_text(
                x + cell_w / 2, y + _MATRIX_CELL_H / 2, node.label,
                width=cell_w - 32.0, height=_MATRIX_CELL_H - 32.0,
                color=theme.text_color, theme=theme, base_size=_MATRIX_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))

    grid_bottom = grid_top + rows * _MATRIX_CELL_H
    axis_label_h = 0.0
    if draw_axis_cross:
        mid_x = grid_left + grid_w / 2
        mid_y = grid_top + (grid_bottom - grid_top) / 2
        elements.append(
            f'<line x1="{mid_x:.1f}" y1="{grid_top:.1f}" x2="{mid_x:.1f}" y2="{grid_bottom:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2"/>'
        )
        elements.append(
            f'<line x1="{grid_left:.1f}" y1="{mid_y:.1f}" x2="{grid_left + grid_w:.1f}" y2="{mid_y:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2"/>'
        )
        if spec.matrix_x_low.strip():
            elements.append(_centered_text(
                grid_left + 10, grid_bottom + 16, spec.matrix_x_low,
                color=theme.muted_color, theme=theme, size=11.0, weight=500,
            ))
        if spec.matrix_x_high.strip():
            elements.append(_centered_text(
                grid_left + grid_w - 10, grid_bottom + 16, spec.matrix_x_high,
                color=theme.muted_color, theme=theme, size=11.0, weight=500,
            ))
        if spec.matrix_x_label.strip():
            elements.append(_centered_text(
                mid_x, grid_bottom + 36, spec.matrix_x_label,
                color=theme.text_color, theme=theme, size=13.0, weight=700,
            ))
            axis_label_h = 48.0
        elif spec.matrix_x_low.strip() or spec.matrix_x_high.strip():
            axis_label_h = 24.0
        if spec.matrix_y_label.strip():
            elements.append(
                f'<text x="{(grid_left - 34):.1f}" y="{mid_y:.1f}" font-size="13" font-weight="700" '
                f'fill="{theme.text_color}" text-anchor="middle" font-family="{escape(theme.font_family)}" '
                f'transform="rotate(-90 {(grid_left - 34):.1f} {mid_y:.1f})">{escape(spec.matrix_y_label)}</text>'
            )
        if spec.matrix_y_low.strip():
            elements.append(_centered_text(
                grid_left - 34, grid_bottom - 6, spec.matrix_y_low,
                color=theme.muted_color, theme=theme, size=11.0, weight=500,
            ))
        if spec.matrix_y_high.strip():
            elements.append(_centered_text(
                grid_left - 34, grid_top + 6, spec.matrix_y_high,
                color=theme.muted_color, theme=theme, size=11.0, weight=500,
            ))

    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=grid_bottom + axis_label_h + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (grid_bottom + axis_label_h + key_gap + key_h - top)


def _layout_radial_mindmap(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A central topic with branching sub-ideas radiating outward, each
    branch connected to the centre by a visible curved line - what makes
    this genuinely different from the concept wheel (_layout_concept_wheel,
    kind concept_map/conceptual): a wheel deliberately draws NO connecting
    lines at all ("parts/properties of X", shown by proximity/colour only -
    see its own docstring); a mind map's whole point IS the visible
    branching structure, so every branch is connected to the centre by a
    real line. Two levels deep in this first pass: the centre (the node
    with `level == 0`, or `nodes[0]`) and its direct branches (every other
    node) - a branch's own further `edges` to a child node are deliberately
    not drawn as a third ring here (documented simplification, not silently
    dropped); that child node still appears, as a flat spoke off the
    centre, so no content is ever lost, just not yet nested visually.
    Canvas grows to fit (like _layout_cycle/_layout_concept_wheel) rather
    than capping branch spacing, so a dense mind map never crowds."""
    if not nodes:
        return [], CANVAS_WIDTH, 0.0
    center = next((n for n in nodes if n.level == 0), nodes[0])
    branches = [n for n in nodes if n is not center]
    branch_count = len(branches)

    center_sizing = _circle_node_size(center.label, min_radius=70.0, font_size=RING_LABEL_SIZE)
    # min_radius=75 (not a smaller, visually-"branch-sized" value) is
    # deliberate: _circle_node_size's own wrap-width budget scales with
    # min_radius, so a too-small one forces a long label into very few,
    # very wide lines, actually growing the circle MUCH larger than a
    # wider starting budget does - confirmed empirically (the same label
    # measured 121.8px at min_radius=50 but only 87.4px at min_radius=75,
    # wrapping into one more, better-balanced line instead of a few
    # overly-wide ones).
    branch_sizings = [_circle_node_size(b.label, min_radius=75.0, font_size=13.5) for b in branches]
    max_branch_r = max((r for _, r in branch_sizings), default=0.0)

    # Same chord-length "keep neighbours from touching" math
    # _layout_ring_process/_layout_concept_wheel already use for their own
    # evenly-spaced circles (see _layout_ring_process's own comment on the
    # exact bug this avoids) - a fixed `branch_count * 34.0` heuristic
    # doesn't account for a branch label long enough to grow its circle
    # well past `min_radius`, which a real 6-branch/long-label mind map
    # confirmed: adjacent branch circles overlapped at that fixed spacing.
    # The extra sqrt(2) factor is for a second, DIFFERENT real bug the
    # chord formula alone doesn't cover: render_qa's own node_overlap check
    # compares each node's axis-aligned bounding SQUARE, not true circle-
    # to-circle distance (see diagram_render_qa._node_shapes's own
    # docstring on this exact gap) - at exactly 4 branches (90 degrees
    # apart, starting from the top), two adjacent branch circles sit at a
    # relative angle that makes their bounding squares touch even though
    # the circles themselves clear each other with room to spare (a real,
    # measured case: true centre distance 334.5 vs a radius sum of 238.1 -
    # comfortably clear - while the bounding squares still overlapped).
    # sqrt(2) is the worst-case ratio between the two measures (exactly at
    # 90 degrees of separation - every other branch count needs less
    # extra room, so this one factor covers all of them safely).
    # The bounding-box-vs-true-circle-distance gap this margin exists to
    # cover is NOT a flat sqrt(2) at every branch count - it is a function
    # of the angle between adjacent branches, exact formula below, and
    # sqrt(2) is only its value at EXACTLY 90 degrees (4 branches) - the
    # genuine worst case, confirmed by checking every branch count from 2
    # to 12: the ratio strictly decreases on both sides of 4 (1.0 at 2,
    # 1.1547 at 3, 1.4142 at 4, 1.2361 at 5, 1.1547 at 6, ... -> 1.0 as
    # branch_count grows). Using the flat worst-case value for every count
    # (a real, confirmed bug: it pushed a 5-branch mind map's canvas wide
    # enough that its 15px label text scaled down to 10.7px once shown at
    # the page's own content width, below the 11px readability floor, even
    # though 5 branches never needed anywhere near that much margin) wastes
    # real canvas width exactly where it's least needed; this exact,
    # per-count value keeps the SAME protection at 4 branches (bit-for-bit
    # the same sqrt(2) margin, so the original overlap bug stays fixed)
    # while giving every other count only the margin it actually needs.
    half_angle = math.pi / branch_count if branch_count >= 2 else 0.0
    dx, dy = 1 - math.cos(2 * half_angle), -math.sin(2 * half_angle)
    chebyshev = max(abs(dx), abs(dy))
    bbox_safety_ratio = math.hypot(dx, dy) / chebyshev if chebyshev > 1e-9 else 1.0
    neighbour_clearance_radius = (
        (2 * max_branch_r + 24.0) / (2 * math.sin(math.pi / branch_count)) * bbox_safety_ratio
        if branch_count >= 2 else 0.0
    )
    r1 = max(150.0, neighbour_clearance_radius, center_sizing[1] + max_branch_r + 30.0)
    cx = max(CANVAS_WIDTH / 2, r1 + max_branch_r + MARGIN)
    canvas_w = 2 * cx
    cy = top + r1 + max(center_sizing[1], max_branch_r) + 40.0

    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    center_svg, center_r = _circle_node(
        center.label, cx=cx, cy=cy, theme=theme, number="", fill=theme.accent_soft,
        stroke=theme.accent_color, min_radius=70.0, font_size=RING_LABEL_SIZE,
        detail=center.detail, sizing=center_sizing,
    )
    elements.append(center_svg)
    key_entries: list[tuple[str, str, str]] = [("", center.label, center.detail)]

    for index, branch in enumerate(branches):
        angle = (2 * math.pi * index / max(branch_count, 1)) - math.pi / 2
        ux, uy = math.cos(angle), math.sin(angle)
        bx, by = cx + r1 * ux, cy + r1 * uy
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        branch_svg, branch_r = _circle_node(
            branch.label, cx=bx, cy=by, theme=theme, number="", fill=fill, stroke=stroke,
            min_radius=75.0, font_size=13.5, detail=branch.detail, sizing=branch_sizings[index],
        )
        start = (cx + center_r * ux, cy + center_r * uy)
        end = (bx - branch_r * ux, by - branch_r * uy)
        elements.append(_curved_connector(start[0], start[1], end[0], end[1], cx=cx, cy=cy, theme=theme))
        elements.append(branch_svg)
        key_entries.append((str(index + 1), branch.label, branch.detail))

    bottom = cy + r1 + max_branch_r + 40.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_VENN_SET_COLORS = [
    ("#bfdbfe", "#1d4ed8", 0.55),  # blue
    ("#fde68a", "#b45309", 0.55),  # amber
    ("#bbf7d0", "#16a34a", 0.55),  # green
]
_VENN_LABEL_SIZE = 14.0
_VENN_RADIUS = 150.0


def _layout_venn(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """2 or 3 overlapping circles (DiagramSpec.is_usable() rejects any
    other count for this kind - Venn geometry only reads cleanly at 2-3
    sets). Each circle gets only its own short set name, pulled toward its
    own non-overlapping outer edge - never the WHY (what's unique to a set,
    or what two sets share): a lens-shaped overlap region has no reliable
    room to wrap real sentences into (unlike a rectangle or even a wedge),
    so - the same rule every other kind in this module already follows for
    its own full explanation - the Key table below carries it instead: one
    row per set (`node.detail` - what's unique to it) plus one row per edge
    whose both ends are real node ids and whose `label` is set (what the
    two sets share)."""
    count = len(nodes)
    if count < 2:
        return [], 0.0
    r = _VENN_RADIUS
    cx = CANVAS_WIDTH / 2
    if count == 2:
        cy = top + r
        centers = [(cx - r * 0.55, cy), (cx + r * 0.55, cy)]
        bottom = cy + r
    else:
        cy = top + r * 1.52
        centers = [
            (cx, cy - r * 0.52),
            (cx - r * 0.45, cy + r * 0.26),
            (cx + r * 0.45, cy + r * 0.26),
        ]
        bottom = cy + r * 1.26

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for node, (nx, ny) in zip(nodes, centers):
        index = len(key_entries)
        fill, stroke, opacity = _VENN_SET_COLORS[index % len(_VENN_SET_COLORS)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<circle class="diagram-box" cx="{nx:.1f}" cy="{ny:.1f}" r="{r:.1f}" '
            f'fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" stroke-width="2.5"/></g>'
        )
        key_entries.append((str(index + 1), node.label, node.detail))

    # Labels drawn AFTER every circle (so none sits under a later circle's
    # own semi-transparent fill), pulled from each circle's own centre
    # toward its outer edge, away from the shared hub point - exactly where
    # a 3-circle Venn's triple-overlap region sits, which text must avoid.
    hub = (
        sum(c[0] for c in centers) / len(centers),
        sum(c[1] for c in centers) / len(centers),
    )
    for node, (nx, ny) in zip(nodes, centers):
        dx, dy = nx - hub[0], ny - hub[1]
        dist = math.hypot(dx, dy)
        ux, uy = (dx / dist, dy / dist) if dist > 1e-6 else (0.0, -1.0)
        label_x, label_y = nx + ux * r * 0.45, ny + uy * r * 0.45
        elements.append(
            _fit_boxed_text(
                label_x, label_y, node.label, width=r * 0.85, height=r * 0.6,
                color=theme.text_color, theme=theme, base_size=_VENN_LABEL_SIZE, min_size=10.0,
            )
        )

    by_id = {n.id: n.label for n in nodes if n.id}
    for edge in edges:
        if edge.source in by_id and edge.target in by_id and edge.label.strip():
            key_entries.append(("", f"{by_id[edge.source]} ∩ {by_id[edge.target]}", edge.label))

    key_gap = 20.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (bottom + key_gap + key_h - top)


CHEVRON_LABEL_SIZE = 15.0
_CHEVRON_H = 90.0
_CHEVRON_NOTCH = 22.0
_CHEVRON_GAP = 14.0
_CHEVRON_MIN_W = 150.0


def _layout_chevron_process(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float, float]:
    """A horizontal row of arrow-shaped "chevron" steps pointing right -
    the classic PowerPoint SmartArt process strip, visually distinct from
    the vertical oval/rectangle/diamond column _layout_standard_flowchart
    draws - best for a SHORT linear sequence shown as a strip (a product-
    launch phase strip, a short onboarding flow) rather than a long
    procedure with decisions, which the standard flowchart already covers.
    Every chevron is drawn the SAME width (the widest one actually needs -
    sized up front, never truncated), matching the classic uniform-chevron
    look; a gap (not an overlapping/nested notch) separates consecutive
    chevrons, so their own bounding boxes never register as a false
    node_overlap. Only the short label lives inside a chevron - the full
    explanation is a Key table row below, the same rule every kind in this
    module already follows. Canvas grows wider to fit more steps (like the
    ring/wheel) rather than wrapping to a second row."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    # _CHEVRON_H is fixed (every chevron is the same height), so sizing
    # chevron_w from the WIDEST wrapped line alone isn't enough - a real,
    # confirmed bug: a very long label wrapped at a narrow budget breaks
    # into many short, narrow lines (a small `longest`, so a narrow
    # chevron_w), but those many lines then don't fit the fixed height,
    # silently truncating with "…" inside _fit_boxed_text's own shrink-
    # then-truncate fallback at draw time - the box was sized to the wrong
    # dimension. Widening the wrap budget (same technique
    # _circle_node_size already uses) until the line count actually fits
    # the available height is what makes this never truncate.
    max_fit_lines = max(int((_CHEVRON_H * 0.7) // (CHEVRON_LABEL_SIZE * LINE_HEIGHT)), 1)
    chevron_w = _CHEVRON_MIN_W
    for node in nodes:
        label = node.label.strip() or "—"
        width_budget = _CHEVRON_MIN_W - 40.0
        lines = _wrap(label, font_size=CHEVRON_LABEL_SIZE, width=width_budget, max_lines=999, bold=True)
        while len(lines) > max_fit_lines and width_budget < 2000.0:
            width_budget += 60.0
            lines = _wrap(label, font_size=CHEVRON_LABEL_SIZE, width=width_budget, max_lines=999, bold=True)
        longest = max((len(line) for line in lines), default=0)
        chevron_w = max(chevron_w, longest * CHEVRON_LABEL_SIZE * _BOLD_RATIO + 60.0)

    total_w = count * chevron_w + (count - 1) * _CHEVRON_GAP
    canvas_w = max(CANVAS_WIDTH, total_w + 2 * MARGIN)
    start_x = (canvas_w - total_w) / 2
    # An icon badge floats ABOVE each chevron (the reference "icon strip"
    # look) only when at least one node actually set a recognised `icon` -
    # a diagram with no icons at all keeps today's exact layout (no wasted
    # top margin), same "additive, never a forced look" contract every
    # other icon_badge caller in this module follows.
    normalize = lambda value: value.strip().lower().replace(" ", "_").replace("-", "_")  # noqa: E731
    has_any_icon = any(_ICON_DRAWERS.get(normalize(n.icon)) for n in nodes)
    icon_offset = 2 * _ICON_BADGE_RADIUS + 18.0 if has_any_icon else 0.0
    y = top + icon_offset

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        x = start_x + index * (chevron_w + _CHEVRON_GAP)
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        nd = _CHEVRON_NOTCH if index > 0 else 0.0  # the first chevron's left edge stays flat
        points = (
            f"{x:.1f},{y:.1f} {x + chevron_w - _CHEVRON_NOTCH:.1f},{y:.1f} "
            f"{x + chevron_w:.1f},{y + _CHEVRON_H / 2:.1f} "
            f"{x + chevron_w - _CHEVRON_NOTCH:.1f},{y + _CHEVRON_H:.1f} {x:.1f},{y + _CHEVRON_H:.1f} "
            f"{x + nd:.1f},{y + _CHEVRON_H / 2:.1f}"
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        if has_any_icon:
            parts.append(
                _icon_badge(
                    node, cx=x + (chevron_w - _CHEVRON_NOTCH) / 2, cy=y - icon_offset / 2,
                    theme=theme, color=stroke,
                )
            )
        parts.append(f'<polygon class="diagram-box" points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(
            _fit_boxed_text(
                x + chevron_w / 2, y + _CHEVRON_H / 2, node.label,
                width=chevron_w - _CHEVRON_NOTCH - 20.0, height=_CHEVRON_H * 0.7,
                color=theme.text_color, theme=theme, base_size=CHEVRON_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))

    bottom = y + _CHEVRON_H
    key_gap = 30.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


TIMELINE_LABEL_SIZE = 15.0
_TIMELINE_CARD_W = 118.0
_TIMELINE_CARD_H = 70.0
_TIMELINE_STEM = 40.0
_TIMELINE_MARKER_R = 10.0


def _layout_timeline(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float, float]:
    """A horizontal roadmap: an axis line with one evenly-spaced marker per
    node, each connected by a short leader stem to a label card - cards
    alternate above/below the axis (a zigzag) so consecutive cards never
    crowd each other regardless of label length. The classic PowerPoint
    SmartArt timeline/roadmap look, for genuinely chronological content
    (dates/eras/milestones) - distinct from the vertical standard
    flowchart, which already covers an ordinary non-chronological
    procedure. Only the short label lives on a card - the full explanation
    is a Key table row below, same rule as every kind in this module.
    Canvas grows wider to fit more milestones (like the ring/wheel)."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    card_w = _TIMELINE_CARD_W
    card_h = _TIMELINE_CARD_H
    for node in nodes:
        lines = _wrap(
            node.label.strip() or "—", font_size=TIMELINE_LABEL_SIZE, width=_TIMELINE_CARD_W - 24.0,
            max_lines=999, bold=True,
        )
        longest = max((len(line) for line in lines), default=0)
        card_w = max(card_w, longest * TIMELINE_LABEL_SIZE * _BOLD_RATIO + 24.0)
        card_h = max(card_h, len(lines) * TIMELINE_LABEL_SIZE * LINE_HEIGHT + 30.0)

    gap = 20.0
    slot_w = card_w + gap
    total_w = count * slot_w
    canvas_w = max(CANVAS_WIDTH, total_w + 2 * MARGIN)
    start_x = (canvas_w - total_w) / 2 + slot_w / 2
    axis_y = top + card_h + _TIMELINE_STEM + _TIMELINE_MARKER_R + 10.0

    elements = [_arrow_marker("diagram-arrow", theme.accent_color)]
    elements.append(
        f'<line x1="{MARGIN:.1f}" y1="{axis_y:.1f}" x2="{canvas_w - MARGIN:.1f}" y2="{axis_y:.1f}" '
        f'stroke="{theme.border_color}" stroke-width="3" marker-end="url(#diagram-arrow)"/>'
    )
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        cx = start_x + index * slot_w
        above = index % 2 == 0
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        if above:
            card_bottom = axis_y - _TIMELINE_STEM
            card_top = card_bottom - card_h
            stem_y1, stem_y2 = card_bottom, axis_y - _TIMELINE_MARKER_R
        else:
            card_top = axis_y + _TIMELINE_STEM
            card_bottom = card_top + card_h
            stem_y1, stem_y2 = axis_y + _TIMELINE_MARKER_R, card_top
        elements.append(
            f'<line x1="{cx:.1f}" y1="{stem_y1:.1f}" x2="{cx:.1f}" y2="{stem_y2:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2"/>'
        )
        elements.append(
            f'<circle cx="{cx:.1f}" cy="{axis_y:.1f}" r="{_TIMELINE_MARKER_R:.1f}" '
            f'fill="{stroke}" stroke="{theme.page_background}" stroke-width="2"/>'
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<rect class="diagram-box" x="{cx - card_w / 2:.1f}" y="{card_top:.1f}" '
            f'width="{card_w:.1f}" height="{card_h:.1f}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        parts.append(
            _fit_boxed_text(
                cx, (card_top + card_bottom) / 2, node.label, width=card_w - 20.0, height=card_h - 16.0,
                color=theme.text_color, theme=theme, base_size=TIMELINE_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))

    content_bottom = axis_y + _TIMELINE_STEM + card_h  # the lowest a "below" card ever reaches
    key_gap = 30.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=content_bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (content_bottom + key_gap + key_h - top)


STAIRCASE_LABEL_SIZE = 15.0
_STAIRCASE_BAR_H = 56.0
_STAIRCASE_GAP = 16.0
_STAIRCASE_BADGE_R = 22.0
_STAIRCASE_BASE_W = 220.0
_STAIRCASE_STEP_W = 90.0


def _layout_staircase_process(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A left-aligned column of numbered, ascending bars - each step's own
    bar is visibly LONGER than the one above it, the classic PowerPoint
    SmartArt "ascending/staircase process" look for content that genuinely
    grows or builds in scope/scale step by step (a maturity model, a
    skill-building progression) - distinct from both chevron_process
    (equal-width horizontal arrows) and pyramid (a centred, symmetric
    stack). A bar's own length is the LARGER of its "ascending" target
    width and whatever its own label actually needs (never shrinks text to
    force a shorter bar, never truncates a long label to keep the
    staircase effect) - only the short label lives on a bar; the full
    explanation is a Key table row below, same rule as every kind in this
    module."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    x0 = MARGIN + _STAIRCASE_BADGE_R * 2 + 20.0
    y = top
    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    max_right = x0

    for index, node in enumerate(nodes):
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        cy = y + _STAIRCASE_BAR_H / 2
        badge_cx = MARGIN + _STAIRCASE_BADGE_R
        elements.append(
            f'<circle cx="{badge_cx:.1f}" cy="{cy:.1f}" r="{_STAIRCASE_BADGE_R:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        elements.append(_centered_text(badge_cx, cy, str(index + 1), color=stroke, theme=theme, size=15.0, weight=800))

        # _STAIRCASE_BAR_H is fixed, so - the same real bug already found
        # and fixed in _layout_chevron_process - sizing bar_w from the
        # widest wrapped line alone isn't enough - a long label measured at
        # a narrow fixed budget can wrap into more lines than the fixed
        # bar height actually fits, truncating at draw time. Widen the
        # wrap budget (same technique _circle_node_size/
        # _layout_chevron_process already use) until the line count fits.
        label = node.label.strip() or "—"
        max_fit_lines = max(int((_STAIRCASE_BAR_H * 0.75) // (STAIRCASE_LABEL_SIZE * LINE_HEIGHT)), 1)
        width_budget = _STAIRCASE_BASE_W - 30.0
        lines = _wrap(label, font_size=STAIRCASE_LABEL_SIZE, width=width_budget, max_lines=999, bold=True)
        while len(lines) > max_fit_lines and width_budget < 2000.0:
            width_budget += 60.0
            lines = _wrap(label, font_size=STAIRCASE_LABEL_SIZE, width=width_budget, max_lines=999, bold=True)
        longest = max((len(line) for line in lines), default=0)
        text_needed_w = longest * STAIRCASE_LABEL_SIZE * _BOLD_RATIO + 30.0
        bar_w = max(_STAIRCASE_BASE_W + index * _STAIRCASE_STEP_W, text_needed_w)

        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<rect class="diagram-box" x="{x0:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{_STAIRCASE_BAR_H:.1f}" '
            f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        parts.append(
            _fit_boxed_text(
                x0 + bar_w / 2, cy, node.label, width=bar_w - 20.0, height=_STAIRCASE_BAR_H * 0.75,
                color=theme.text_color, theme=theme, base_size=STAIRCASE_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))
        max_right = max(max_right, x0 + bar_w)
        y += _STAIRCASE_BAR_H + _STAIRCASE_GAP

    bottom = y - _STAIRCASE_GAP
    canvas_w = max(CANVAS_WIDTH, max_right + MARGIN)
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


BALANCE_LABEL_SIZE = 15.0
_BALANCE_DETAIL_SIZE = 12.5
_BALANCE_ROW_PAD = 14.0
_BALANCE_GAP = 10.0
_BALANCE_COL_W = 340.0
_BALANCE_GUTTER = 50.0
# Fixed semantic colours (not theme-derived, same convention every other
# fixed palette in this module uses) - red/rose reads as "cost"/"con"
# universally, green as "value"/"pro", regardless of which literal labels
# a particular brief uses for the two columns.
_BALANCE_LEFT_COLORS = ("#fecaca", "#b91c1c")
_BALANCE_RIGHT_COLORS = ("#bbf7d0", "#16a34a")


def _balance_row(
    node: DiagramNode, *, x: float, y: float, width: float, fill: str, stroke: str, theme: TemplateTheme
) -> tuple[str, float]:
    """One row of a balance_comparison column: label (bold) + detail
    (muted), both wrapped with NO max_lines cap - the box grows to fit
    however much text is there (never `_node_block`'s fixed 2/3-line caps,
    which truncate with "…" - the exact bug class this whole session has
    repeatedly found and fixed elsewhere; this kind's own rows show their
    real explanatory text directly, not deferred to a Key table, since a
    growable left-aligned row already has the room a circle/diamond
    doesn't)."""
    inner_w = width - 2 * _BALANCE_ROW_PAD
    label_lines = _wrap(node.label.strip() or "—", font_size=BALANCE_LABEL_SIZE, width=inner_w, max_lines=999, bold=True)
    detail_lines = (
        _wrap(node.detail, font_size=_BALANCE_DETAIL_SIZE, width=inner_w, max_lines=999) if node.detail.strip() else []
    )
    label_h = len(label_lines) * BALANCE_LABEL_SIZE * LINE_HEIGHT
    detail_h = (len(detail_lines) * _BALANCE_DETAIL_SIZE * LINE_HEIGHT + 4.0) if detail_lines else 0.0
    box_h = 2 * _BALANCE_ROW_PAD + label_h + detail_h

    tooltip = node.label.strip()
    if node.detail.strip():
        tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
    parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
    parts.append(
        f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{box_h:.1f}" '
        f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    label_svg, _label_h = _text(
        x + _BALANCE_ROW_PAD, y + _BALANCE_ROW_PAD + BALANCE_LABEL_SIZE * 0.85, label_lines,
        font_size=BALANCE_LABEL_SIZE, weight=700, color=theme.text_color, font_family=theme.font_family,
        anchor="start",
    )
    parts.append(label_svg)
    if detail_lines:
        detail_svg, _detail_h = _text(
            x + _BALANCE_ROW_PAD, y + _BALANCE_ROW_PAD + label_h + _BALANCE_DETAIL_SIZE * 0.85 + 4.0, detail_lines,
            font_size=_BALANCE_DETAIL_SIZE, weight=400, color=theme.muted_color, font_family=theme.font_family,
            anchor="start",
        )
        parts.append(detail_svg)
    return f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>', box_h


def _layout_balance_comparison(spec: DiagramSpec, *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """Two colour-coded columns (fixed red/rose "cost"/"con" side, fixed
    green "value"/"pro" side), split by `node.shape_role` ("left"/"right")
    when the model set it, otherwise by position (first half left, rest
    right) - the classic PowerPoint SmartArt "cost vs. value"/pros-and-cons
    comparison. Each row grows to fit its own real text (see
    _balance_row's own docstring) rather than deferring to a Key table -
    distinct from `comparison` (plain side-by-side columns, one per thing
    being compared, no colour framing) in both visual treatment and intent
    (a two-SIDED judgement, not a flat N-way comparison)."""
    nodes = spec.nodes
    if not nodes:
        return [], 0.0
    has_explicit = any(n.shape_role.strip().lower() in ("left", "right") for n in nodes)
    if has_explicit:
        left = [n for n in nodes if n.shape_role.strip().lower() != "right"]
        right = [n for n in nodes if n.shape_role.strip().lower() == "right"]
    else:
        split = math.ceil(len(nodes) / 2)
        left, right = nodes[:split], nodes[split:]

    col_w = _BALANCE_COL_W
    left_x = CANVAS_WIDTH / 2 - _BALANCE_GUTTER / 2 - col_w
    right_x = CANVAS_WIDTH / 2 + _BALANCE_GUTTER / 2
    y = top
    elements: list[str] = []
    if spec.balance_left_label.strip() or spec.balance_right_label.strip():
        if spec.balance_left_label.strip():
            elements.append(_centered_text(
                left_x + col_w / 2, y + 18, spec.balance_left_label,
                color=_BALANCE_LEFT_COLORS[1], theme=theme, size=16.0, weight=800,
            ))
        if spec.balance_right_label.strip():
            elements.append(_centered_text(
                right_x + col_w / 2, y + 18, spec.balance_right_label,
                color=_BALANCE_RIGHT_COLORS[1], theme=theme, size=16.0, weight=800,
            ))
        y += 34.0

    rows_top = y
    ly = y
    for node in left:
        svg, h = _balance_row(
            node, x=left_x, y=ly, width=col_w, fill=_BALANCE_LEFT_COLORS[0], stroke=_BALANCE_LEFT_COLORS[1],
            theme=theme,
        )
        elements.append(svg)
        ly += h + _BALANCE_GAP

    ry = y
    for node in right:
        svg, h = _balance_row(
            node, x=right_x, y=ry, width=col_w, fill=_BALANCE_RIGHT_COLORS[0], stroke=_BALANCE_RIGHT_COLORS[1],
            theme=theme,
        )
        elements.append(svg)
        ry += h + _BALANCE_GAP

    bottom = max(ly, ry, rows_top) - _BALANCE_GAP
    if left and right:
        elements.append(
            f'<line x1="{CANVAS_WIDTH / 2:.1f}" y1="{rows_top:.1f}" x2="{CANVAS_WIDTH / 2:.1f}" y2="{bottom:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2" stroke-dasharray="4 4"/>'
        )
    return elements, (bottom - top)


NESTED_CIRCLES_LABEL_SIZE = 15.0
_NESTED_MIN_RADIUS = 50.0
_NESTED_RADIUS_STEP = 60.0


def _layout_nested_circles(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """Concentric rings, outermost first - `nodes[0]` is the broadest/
    outermost ring, `nodes[-1]` the narrowest/innermost - the classic
    PowerPoint SmartArt "scope/containment" look (Vision > Mission >
    Strategy > Tactics; Global > Regional > Local). Each ring is drawn
    outermost-to-innermost so inner circles paint on top, automatically
    showing every outer ring as a coloured band around it - no manual
    annulus/donut math needed. A ring's own short label sits near the top
    of its own band; the full explanation is a Key table row below, same
    rule as every kind in this module. Deliberately NOT exempt from the
    rest of this module's geometry conventions except node_overlap (every
    ring legitimately contains the next - see validate_rendered_svg's own
    `if kind != "venn"` guard, extended to this kind too)."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    cx = CANVAS_WIDTH / 2
    outer_r = _NESTED_MIN_RADIUS + (count - 1) * _NESTED_RADIUS_STEP
    cy = top + outer_r + 10.0

    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        r = outer_r - index * _NESTED_RADIUS_STEP
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
            f'fill="{fill}" fill-opacity="0.88" stroke="{stroke}" stroke-width="2"/>'
        )
        # A ring's own RADIUS is fixed by its position in the nesting
        # order (never grown to fit text - the whole point of this kind is
        # a monotonically decreasing sequence of radii), so unlike every
        # size-from-content shape elsewhere in this module, the label here
        # can't grow its own container. _fit_boxed_text's shrink-then-
        # truncate fallback was a real, confirmed bug for exactly this
        # reason (a long label on a modest-radius ring hit the truncate
        # path) - wrapping directly with an UNBOUNDED max_lines instead
        # (same philosophy _circle_node/_key_table_rows already use) and
        # just letting the label run an extra line down into the ring's
        # own band is what makes this never truncate; the band widens
        # once, modestly, only if that still isn't enough room.
        label = node.label.strip() or "—"
        band_w = max(min(2 * r * 0.75, 260.0), 70.0)
        label_lines = _wrap(label, font_size=NESTED_CIRCLES_LABEL_SIZE, width=band_w, max_lines=999, bold=True)
        if len(label_lines) > 3:
            band_w = min(band_w * 1.4, 2 * r * 0.92)
            label_lines = _wrap(label, font_size=NESTED_CIRCLES_LABEL_SIZE, width=band_w, max_lines=999, bold=True)
        label_top_y = cy - r + 22.0
        label_svg, _label_h = _text(
            cx, label_top_y + NESTED_CIRCLES_LABEL_SIZE * 0.8, label_lines,
            font_size=NESTED_CIRCLES_LABEL_SIZE, weight=700, color=theme.text_color, font_family=theme.font_family,
            anchor="middle",
        )
        parts.append(label_svg)
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))

    bottom = cy + outer_r + 20.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (bottom + key_gap + key_h - top)


NETWORK_LABEL_SIZE = 15.0
# Mirrors app.services.diagram_render_qa.MIN_READABLE_PX - that module
# already imports several label-size constants FROM this one (RING_LABEL_
# SIZE, _SPECTRUM_LABEL_SIZE, ...), so this module importing its
# MIN_READABLE_PX back would be circular; kept in lockstep here
# deliberately instead.
_MIN_READABLE_PX = 11.0
_RING_MIN_LABEL_SIZE = 10.0


def _layout_network_mesh(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A genuine many-to-many network - nodes evenly spaced on a circle
    (same trigonometry _layout_cycle already uses), with a straight,
    unlabelled line for every real edge, however many a node actually
    has - distinct from radial_mindmap (exactly one hub, every other node
    a leaf) and from cycle (only ADJACENT ring nodes ever connect). Best
    for content whose whole point is that many things connect to many
    others (a microservice topology, a social/organisational network) -
    not a hub-and-spoke or a strict hierarchy, which have their own,
    clearer dedicated kinds. Edge lines are drawn BEFORE the node circles
    (so a line's own endpoint is cleanly covered by the circle it connects
    to, never visibly poking past it) - only the short label lives on a
    node; the full explanation is a Key table row below, same rule as
    every kind in this module."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0

    def _size_at(font_size: float) -> tuple[list[tuple[list[str], float]], float, float, float]:
        sizings = [_circle_node_size(n.label, min_radius=55.0, font_size=font_size) for n in nodes]
        max_r = max((r for _, r in sizings), default=55.0)
        radius = (
            (2 * max_r + 60.0) / (2 * math.sin(math.pi / count)) * math.sqrt(2) if count >= 2 else 0.0
        )
        radius = max(170.0, radius)
        cx = max(CANVAS_WIDTH / 2, radius + max_r + MARGIN)
        return sizings, max_r, radius, 2 * cx

    # Every node shares ONE ring radius sized by whichever node's own label
    # is widest (unlike a non-ring kind like data_flow_diagram, where one
    # node's own growth never affects any other) - so a single outlier
    # label (e.g. "Notification Service" among five shorter peers) can
    # inflate the WHOLE canvas just to keep its own longest word from
    # force-breaking (see `_circle_node_size`'s own force-break-avoidance
    # comment), a real, confirmed case where that pushed a 6-node mesh's
    # canvas past its own safe-readability width. Shrinking the shared
    # label font a little, for every node (not just the outlier), brings
    # the ring - and so the whole canvas - back down without re-introducing
    # a mid-word break anywhere: `_circle_node_size`'s own radius is driven
    # by `text_w`, which scales directly with `font_size`, so a smaller
    # font needs a smaller radius to hold the exact same (still unbroken)
    # wrapped lines. The stopping condition is the SAME inequality
    # diagram_render_qa.readability_issues checks after the fact
    # (`font_size * CONTENT_WIDTH / canvas_w >= MIN_READABLE_PX`, rearranged
    # to avoid a division), evaluated against whatever font_size THIS loop
    # iteration actually ends up using - not the original fixed
    # NETWORK_LABEL_SIZE - so it can never converge on a canvas that's
    # "safe" only for a bigger font than what actually got drawn.
    font_size = NETWORK_LABEL_SIZE
    sizings, max_r, radius, canvas_w = _size_at(font_size)
    while canvas_w * _MIN_READABLE_PX > font_size * CONTENT_WIDTH and font_size > _RING_MIN_LABEL_SIZE:
        font_size = max(_RING_MIN_LABEL_SIZE, font_size * 0.92)
        sizings, max_r, radius, canvas_w = _size_at(font_size)
    cx = canvas_w / 2
    cy = top + radius + max_r + 20.0

    positions: list[tuple[float, float]] = []
    for index in range(count):
        angle = 2 * math.pi * index / count - math.pi / 2
        positions.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))

    def _pulled_back(px: float, py: float, toward_x: float, toward_y: float, pull: float) -> tuple[float, float]:
        """A point `pull` px from (px, py) along the direction toward
        (toward_x, toward_y) - used to start/end an edge at a node's own
        BOUNDARY instead of its centre. A real, confirmed bug: every edge
        here used to start/end at `positions[i]` directly (the node's own
        CENTRE) - the first several pixels of any edge were then, by
        construction, inside its own source/target node's circle (a
        56px-radius node starting a line from its exact centre has no
        chance of escaping a 6px endpoint-trim margin), which
        edge_crosses_node correctly flagged as crossing that very node."""
        dx, dy = toward_x - px, toward_y - py
        dist = max(math.hypot(dx, dy), 1.0)
        return px + dx / dist * pull, py + dy / dist * pull

    elements: list[str] = []
    by_id = {n.id: i for i, n in enumerate(nodes) if n.id}
    for edge in edges:
        si, ti = by_id.get(edge.source), by_id.get(edge.target)
        if si is None or ti is None or si == ti:
            continue
        cx1, cy1 = positions[si]
        cx2, cy2 = positions[ti]
        r1, r2 = sizings[si][1], sizings[ti][1]
        hop = min(abs(si - ti), count - abs(si - ti))
        if hop <= 1:
            # Directly adjacent on the circle - a straight chord between
            # them never passes near any OTHER node (there's nothing
            # positioned between two neighbours), so this is always safe.
            x1, y1 = _pulled_back(cx1, cy1, cx2, cy2, r1)
            x2, y2 = _pulled_back(cx2, cy2, cx1, cy1, r2)
            elements.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{theme.border_color}" stroke-width="1.5" opacity="0.65"/>'
            )
        else:
            # A real, confirmed bug: a straight chord between two NON-
            # adjacent nodes on the circle cuts across the interior and
            # can pass directly through one or more nodes positioned
            # between them (a dense real topology measured 200+
            # edge_crosses_node hits). Bowing the chord's own midpoint
            # radially OUTWARD, past the whole node ring, routes it around
            # the OUTSIDE of whatever sits between the two endpoints
            # instead - the curve only ever touches the ring's own radius
            # at its two connected endpoints, nowhere else, so it can
            # never cross a third node regardless of hop distance.
            mx, my = (cx1 + cx2) / 2, (cy1 + cy2) / 2
            dx, dy = mx - cx, my - cy
            dist = max(math.hypot(dx, dy), 1.0)
            bow = max_r + 50.0
            ctrl = (mx + dx / dist * bow, my + dy / dist * bow)
            x1, y1 = _pulled_back(cx1, cy1, ctrl[0], ctrl[1], r1)
            x2, y2 = _pulled_back(cx2, cy2, ctrl[0], ctrl[1], r2)
            elements.append(
                f'<path d="M{x1:.1f},{y1:.1f} Q{ctrl[0]:.1f},{ctrl[1]:.1f} {x2:.1f},{y2:.1f}" '
                f'fill="none" stroke="{theme.border_color}" stroke-width="1.5" opacity="0.65"/>'
            )

    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        nx, ny = positions[index]
        fill, stroke = _cycling_palette(theme)[index % len(_cycling_palette(theme))]
        svg, _r = _circle_node(
            node.label, cx=nx, cy=ny, theme=theme, number=str(index + 1), fill=fill, stroke=stroke,
            min_radius=55.0, font_size=font_size, detail=node.detail, sizing=sizings[index],
        )
        elements.append(svg)
        key_entries.append((str(index + 1), node.label, node.detail))

    bottom = cy + radius + max_r + 30.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_DECISION_TREE_ROW_GAP = 70.0
_DECISION_TREE_COL_GAP = 24.0
# Clearly more than diagram_render_qa's own _DUPLICATE_LANDING_TOLERANCE
# (20px) - two convergent landings spaced this far apart read as visually
# distinct, never as one ambiguous overlapping arrowhead.
_DUPLICATE_LANDING_SPACING = 24.0


def _layout_decision_tree(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A cascading branching tree of decisions - each DECISION node (has
    outgoing edges) a diamond, each OUTCOME/leaf node (no outgoing edges) a
    rounded box - distinct from both `hierarchy` (a flat parent/child
    CATEGORISATION with no decision semantics, every node the same shape)
    and flow_chart's own single inline decision diamond (one branch point
    inside an otherwise linear procedure, not a whole cascading TREE of
    them). Reuses the exact same diamond/box drawing and sizing
    (_draw_diamond/_draw_process_box/_ideal_decision_box_size/
    _ideal_process_box_size) flow_chart already relies on - same "grow to
    fit, never truncate" guarantee, already proven. Rows are grouped by
    `level` (0 = root decision) and centred with the same two-pass
    "measure every row's real width first, THEN centre each row" fix
    _layout_hierarchy needed (a real, confirmed bug there: rows used to
    start flush-left instead of centred - this kind is built with that fix
    already in place, not needing it added later). A branch's own
    condition (edge.label - "Yes"/"No", a threshold) is never drawn on the
    connector line itself (this module's "no text on arrows" rule, kept
    consistent here) - it's a Key table row below instead: "If {label}:
    leads to {child}"."""
    if not nodes:
        return [], CANVAS_WIDTH, 0.0
    _, out_edges = _flow_degrees(nodes, edges)
    by_id = {n.id: n for n in nodes if n.id}

    def role_of(node: DiagramNode) -> str:
        override = node.role.strip().lower()
        if override in ("decision", "outcome"):
            return override
        return "decision" if out_edges.get(node.id) else "outcome"

    def size_of(node: DiagramNode, font_size: float) -> tuple[float, float]:
        return (
            _ideal_decision_box_size(node, font_size=font_size)
            if role_of(node) == "decision"
            else _ideal_process_box_size(node, font_size=font_size)
        )

    rows: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        rows.setdefault(max(node.level, 0), []).append(node)
    levels = sorted(rows)

    # Group every row's nodes so each parent's own children sit in
    # CONTIGUOUS columns, ordered to match that parent's own left-to-right
    # position in the row above - a real, confirmed bug (PDF/visual-pipeline
    # audit, doc_81d0b1d44cae): `node.level` is author-supplied and this
    # function used to position a row purely in `spec.nodes`' own list
    # order, with no regard for which parent a node actually belongs to. An
    # authored spec that lists a level's nodes in narrative order rather
    # than per-subtree order (e.g. two sibling decisions' "first" outcomes
    # listed together, then both "second" outcomes) interleaves unrelated
    # hubs' children into the same row. The elbow-routing below draws one
    # shared horizontal "bus" per hub spanning from its own leftmost to
    # rightmost CHILD COLUMN - a bus that spans across a different hub's
    # children (sitting between them purely because of list order) either
    # overlaps that other hub's own bus (`shared_edge_segment`) or visually
    # crosses its trunk/drop line - reproduced directly: two sibling hubs
    # each with 2 children, listed interleaved, draws two buses that run
    # collinear and overlap for a real stretch. Sorting every level by
    # (parent's own column index in the level above, original relative
    # order among true siblings - a stable sort preserves this) before any
    # sizing/position math runs guarantees every hub's children are
    # adjacent columns, so its bus only ever spans its own family.
    parent_of: dict[str, str] = {}
    for edge in edges:
        if edge.target not in parent_of:
            parent_of[edge.target] = edge.source
    for level in levels[1:]:
        prior_index = {n.id: i for i, n in enumerate(rows[level - 1]) if n.id}
        fallback = len(prior_index)
        rows[level] = sorted(
            rows[level],
            key=lambda n: prior_index.get(parent_of.get(n.id, ""), fallback),
        )

    def _size_at(font_size: float) -> tuple[dict[int, list[tuple[float, float]]], dict[int, float], float]:
        row_sizes = {level: [size_of(n, font_size) for n in rows[level]] for level in levels}
        row_widths = {
            level: sum(w for w, _h in sizes) + _DECISION_TREE_COL_GAP * (len(sizes) - 1)
            for level, sizes in row_sizes.items()
        }
        max_row_width = max(row_widths.values(), default=0.0)
        canvas_w = max(max_row_width + 2 * MARGIN, CANVAS_WIDTH)
        return row_sizes, row_widths, canvas_w

    # Proactive shrink-to-fit, same stopping condition and loop shape as
    # _layout_network_mesh's own font-size loop (see its comment for the
    # full reasoning): an unbounded branching row (several sibling
    # decisions/outcomes at the same level, each already capped at
    # _MAX_DECISION_BOX_W/_MAX_PROCESS_BOX_W individually) can still sum to
    # a canvas wider than what stays readable once the page scales it down
    # to CONTENT_WIDTH - a real, confirmed case: a 3-way physics method
    # split pushed canvas_w to ~1005-1038px, scaling its 15px label text
    # down to ~9.6-9.9px, under the 11px floor, and this kind had no
    # mitigation at all (every OTHER wide-growing kind in this module -
    # network_mesh, hierarchy's row fan-out, concept_map's spoke placement -
    # already does this; decision_tree was the one gap). Shrinking every
    # row's font together (not just the offending row) keeps every level
    # visually consistent, exactly as network_mesh shrinks every node's
    # label together rather than just the outlier.
    font_size = STANDARD_FLOWCHART_LABEL_SIZE
    row_sizes, row_widths, canvas_w = _size_at(font_size)
    while canvas_w * _MIN_READABLE_PX > font_size * CONTENT_WIDTH and font_size > _RING_MIN_LABEL_SIZE:
        font_size = max(_RING_MIN_LABEL_SIZE, font_size * 0.92)
        row_sizes, row_widths, canvas_w = _size_at(font_size)

    outcome_palette = _cycling_palette(theme)
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    # id -> (cx, top_y, bottom_y, own_box_width)
    positions: dict[str, tuple[float, float, float, float]] = {}
    key_entries: list[tuple[str, str, str]] = []
    outcome_index = 0
    y = top
    for level in levels:
        row_nodes = rows[level]
        sizes = row_sizes[level]
        row_w = row_widths[level]
        x = (canvas_w - row_w) / 2
        row_height = max((h for _w, h in sizes), default=0.0)
        for node, (w, h) in zip(row_nodes, sizes):
            role = role_of(node)
            node_cx = x + w / 2
            node_cy = y + h / 2
            if role == "decision":
                colors = _node_colors(node, "decision")
                svg = _draw_diamond(
                    node, cx=node_cx, cy=node_cy, width=w, height=h, theme=theme, colors=colors,
                    base_size=font_size, min_size=font_size,
                )
            else:
                if _HEX_RE.match(node.color.strip()):
                    stroke = node.color.strip()
                    colors = (_tint(stroke), stroke)
                else:
                    colors = outcome_palette[outcome_index % len(outcome_palette)]
                    outcome_index += 1
                svg = _draw_process_box(
                    node, cx=node_cx, cy=node_cy, width=w, height=h, theme=theme, colors=colors,
                    base_size=font_size, min_size=font_size,
                )
            elements.append(svg)
            if node.id:
                positions[node.id] = (node_cx, y, y + h, w)
            key_entries.append((str(len(key_entries) + 1), node.label, node.detail))
            x += w + _DECISION_TREE_COL_GAP
        y += row_height + _DECISION_TREE_ROW_GAP

    # Straight elbow routing (trunk down, bus across, drop down into each
    # child) - the standard decision-tree/org-chart connector. A curved
    # Bezier per edge used to bow into a wavy S-curve whenever a child sat
    # off-centre from its parent. Drawing one trunk+drop PER EDGE (rather
    # than grouped per parent) also used to draw the exact same vertical
    # segment twice whenever a decision had more than one child (both
    # edges share the same parent-bottom-to-mid_y run) - a real, confirmed
    # shared_edge_segment regression. Grouping by source draws that shared
    # trunk once, then a single horizontal bus to each child's own column,
    # then one unique drop per child - nothing ever retraced.
    edges_by_source: dict[str, list[DiagramEdge]] = {}
    for edge in edges:
        if edge.source in positions and edge.target in positions:
            edges_by_source.setdefault(edge.source, []).append(edge)

    # Two DIFFERENT decisions converging on the same shared outcome (a
    # legitimate, common pattern - "otherwise, see the fallback") is grouped
    # by SOURCE above, so each converging edge is drawn by a separate loop
    # iteration that has no idea another source ALSO lands on this exact
    # target - both ending up with an identical `child[0]` x, they drew the
    # exact same final drop segment (`shared_edge_segment`) landing at the
    # exact same point (`duplicate_arrowhead_side`, confirmed 0px apart).
    # Counting incoming edges per target up front and giving each one past
    # the first a distinct, symmetric x-offset along the target's own top
    # border - capped so it can never land outside the box - keeps every
    # convergent arrow visually distinct while still clearly entering the
    # same shared node, the standard flowchart convention for a merge.
    landing_total: dict[str, int] = {}
    for child_edges in edges_by_source.values():
        for edge in child_edges:
            landing_total[edge.target] = landing_total.get(edge.target, 0) + 1
    landing_seen: dict[str, int] = {}

    for source_id, child_edges in edges_by_source.items():
        parent = positions[source_id]
        children = [(edge, positions[edge.target]) for edge in child_edges]
        mid_y = (parent[2] + min(child[1] for _, child in children)) / 2
        trunk_x = parent[0]
        elements.append(
            f'<line x1="{trunk_x:.1f}" y1="{parent[2]:.1f}" x2="{trunk_x:.1f}" y2="{mid_y:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2"/>'
        )
        if len(children) > 1:
            xs = [trunk_x] + [child[0] for _, child in children]
            elements.append(
                f'<line x1="{min(xs):.1f}" y1="{mid_y:.1f}" x2="{max(xs):.1f}" y2="{mid_y:.1f}" '
                f'stroke="{theme.accent_color}" stroke-width="2"/>'
            )
        for edge, child in children:
            total = landing_total.get(edge.target, 1)
            land_x = child[0]
            if total > 1:
                idx = landing_seen.get(edge.target, 0)
                landing_seen[edge.target] = idx + 1
                spacing = min(_DUPLICATE_LANDING_SPACING, child[3] / total)
                land_x += (idx - (total - 1) / 2) * spacing
            elements.append(
                f'<path d="M{child[0]:.1f},{mid_y:.1f} L{land_x:.1f},{mid_y:.1f} '
                f'L{land_x:.1f},{child[1] - 2:.1f}" '
                f'fill="none" stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )
            if edge.label.strip() and edge.target in by_id:
                child_label = by_id[edge.target].label.strip()
                key_entries.append(("", f"If {edge.label.strip()}", f"leads to “{child_label}”"))

    bottom = y - _DECISION_TREE_ROW_GAP
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_FISHBONE_BONE_RISE = 85.0
_FISHBONE_BONE_LEAN = 45.0
_FISHBONE_MIN_CAUSE_W = 140.0
_FISHBONE_MIN_CAUSE_H = 60.0


def _ideal_fishbone_cause_size(node: DiagramNode) -> tuple[float, float]:
    """Same grow-to-fit, never-truncate wrap logic as
    _ideal_process_box_size, but with a SMALLER minimum width - a cause
    CATEGORY name ("People", "Process", "Equipment") is conventionally
    much shorter than a flow_chart action step's own label, and a
    fishbone's own canvas grows wider per category (more categories need
    more spine length - see _layout_fishbone's own docstring on why), so
    reusing the generic 220px floor tuned for longer step phrases made a
    realistic 4-6 category "6M" fishbone (the standard template - Man,
    Machine, Material, Method, Measurement, Environment) shrink past the
    page's own readability floor sooner than it needed to."""
    label = node.label.strip() or "—"
    font_size = _scaled_font_size(STANDARD_FLOWCHART_LABEL_SIZE)
    lines = _wrap(label, font_size=font_size, width=220.0, max_lines=999, bold=True) or [label]
    longest = max(len(line) for line in lines)
    line_w = longest * font_size * _BOLD_RATIO
    text_h = len(lines) * font_size * LINE_HEIGHT
    width = max(_FISHBONE_MIN_CAUSE_W, (line_w + 28.0) / 0.85)
    height = max(_FISHBONE_MIN_CAUSE_H, (text_h + 20.0) / 0.7)
    return width, height


def _layout_fishbone(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float, float]:
    """A root-cause-analysis ("Ishikawa"/fishbone) diagram: a horizontal
    spine pointing at the EFFECT box on the right (`nodes[0]`), with one
    diagonal "bone" per CAUSE CATEGORY (every other node), alternating
    above/below the spine, each leaning toward the tail (the classic
    fishbone silhouette). A category's own sub-causes are never crammed
    into its small label box - they're `node.detail` (a comma/semicolon-
    separated list reads well), shown in full in the Key table below,
    same rule as every kind in this module. Reuses _draw_process_box/
    _ideal_process_box_size - the exact same grow-to-fit, never-truncate
    box flow_chart/decision_tree already rely on - for both the head and
    every category box.

    Consecutive SAME-ROW category boxes (index i and i+2, both above or
    both below) are only 2 attach-slots apart, not 1 - a real, confirmed
    bug: evenly dividing a FIXED spine length by node count let two
    same-row boxes' real widths (which grow past the 220px minimum for a
    longer category name) overlap each other, since the fixed division
    never checked against the boxes' own actual size. The spine's own
    length now grows (same "grow the canvas, don't shrink the content"
    rule every other kind in this module follows) to guarantee each
    slot's own width clears the widest category box, however many
    categories or however long their names are."""
    if len(nodes) < 2:
        return [], CANVAS_WIDTH, 0.0
    head, causes = nodes[0], nodes[1:]
    count = len(causes)

    head_w, head_h = _ideal_process_box_size(head)
    cause_sizes = [_ideal_fishbone_cause_size(n) for n in causes]
    max_cause_w = max((w for w, _h in cause_sizes), default=0.0)
    max_cause_h = max((h for _, h in cause_sizes), default=0.0)

    # Two same-row boxes sit 2 slots apart - for their real widths to
    # clear each other (plus a visible gap), each slot needs at least
    # half that combined width.
    slot_spacing = max_cause_w / 2 + 30.0
    spine_span = slot_spacing * (count + 1)
    canvas_w = max(CANVAS_WIDTH, spine_span + head_w + 2 * MARGIN + 40.0)

    spine_y = top + _FISHBONE_BONE_RISE + max_cause_h / 2 + 10.0
    head_cx = canvas_w - MARGIN - head_w / 2
    spine_right = head_cx - head_w / 2
    spine_left = spine_right - spine_span
    attach_xs = [spine_left + slot_spacing * (i + 1) for i in range(count)]

    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    elements.append(
        f'<line x1="{spine_left:.1f}" y1="{spine_y:.1f}" x2="{spine_right:.1f}" y2="{spine_y:.1f}" '
        f'stroke="{theme.accent_color}" stroke-width="2.5" marker-end="url(#diagram-arrow)"/>'
    )

    palette = _cycling_palette(theme)
    key_entries: list[tuple[str, str, str]] = [("", head.label, head.detail)]
    for index, (node, (w, h)) in enumerate(zip(causes, cause_sizes)):
        above = index % 2 == 0
        attach_x = attach_xs[index]
        box_cx = max(attach_x - _FISHBONE_BONE_LEAN, MARGIN + w / 2)
        box_cy = spine_y - _FISHBONE_BONE_RISE if above else spine_y + _FISHBONE_BONE_RISE
        # A real, confirmed bug (the same class already found and fixed in
        # _layout_network_mesh): the bone line used to start at the box's
        # own CENTRE, not its edge - its first several sample points were
        # then, by construction, inside the box it was drawn from.
        # _ray_box_exit finds the box's real boundary along the direction
        # toward the attach point instead.
        dx, dy = attach_x - box_cx, spine_y - box_cy
        dist = max(math.hypot(dx, dy), 1.0)
        start_x, start_y = _ray_box_exit(box_cx, box_cy, dx / dist, dy / dist, w / 2, h / 2)
        elements.append(
            f'<line x1="{start_x:.1f}" y1="{start_y:.1f}" x2="{attach_x:.1f}" y2="{spine_y:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2"/>'
        )
        fill, stroke = palette[index % len(palette)]
        elements.append(_draw_process_box(node, cx=box_cx, cy=box_cy, width=w, height=h, theme=theme, colors=(fill, stroke)))
        key_entries.append((str(index + 1), node.label, node.detail))

    elements.append(_draw_process_box(head, cx=head_cx, cy=spine_y, width=head_w, height=head_h, theme=theme, colors=_FLOW_ROLE_COLORS["end"]))

    bottom = spine_y + _FISHBONE_BONE_RISE + max_cause_h / 2 + 20.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_LAYERED_BAND_MIN_H = 60.0
_LAYERED_BAND_GAP = 6.0


def _layout_layered_bands(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """A flat stack of EQUAL-width horizontal layers (the OSI networking
    model, geological strata, "layers of a system architecture") - deliberately
    distinct from `pyramid`, which TAPERS (narrow apex to wide base,
    showing a hierarchy of IMPORTANCE/scale). Every band here is the same
    full width - the only thing that matters is stacking ORDER, not size.
    Each band's own height grows to fit its label (never truncates, same
    rule as every kind in this module); `node.detail`, when set, is a Key
    table row below rather than crammed into the band itself."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    band_w = CANVAS_WIDTH - 2 * MARGIN
    palette = _cycling_palette(theme)
    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    y = top
    for index, node in enumerate(nodes):
        label = node.label.strip() or "—"
        lines = _wrap(label, font_size=STANDARD_FLOWCHART_LABEL_SIZE, width=band_w * 0.85, max_lines=999, bold=True)
        text_h = len(lines) * STANDARD_FLOWCHART_LABEL_SIZE * LINE_HEIGHT
        band_h = max(_LAYERED_BAND_MIN_H, (text_h + 24.0) / 0.7)
        fill, stroke = palette[index % len(palette)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        parts = [f"<title>{escape(tooltip)}</title>"] if tooltip else []
        parts.append(
            f'<rect class="diagram-box" x="{MARGIN:.1f}" y="{y:.1f}" width="{band_w:.1f}" height="{band_h:.1f}" '
            f'rx="6" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        parts.append(
            _fit_boxed_text(
                CANVAS_WIDTH / 2, y + band_h / 2, node.label, width=band_w * 0.85, height=band_h * 0.7,
                color=theme.text_color, theme=theme, base_size=STANDARD_FLOWCHART_LABEL_SIZE, min_size=10.0,
            )
        )
        elements.append(f'<g class="diagram-node" tabindex="0" role="group">{"".join(parts)}</g>')
        key_entries.append((str(index + 1), node.label, node.detail))
        y += band_h + _LAYERED_BAND_GAP

    bottom = y - _LAYERED_BAND_GAP
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(key_entries, top=bottom + key_gap, width=band_w, theme=theme)
    elements.extend(key_elements)
    return elements, (bottom + key_gap + key_h - top)


_GANTT_LABEL_COL_W = 190.0
_GANTT_ROW_H = 44.0
_GANTT_ROW_GAP = 10.0
GANTT_LABEL_SIZE = 15.0
_GANTT_AXIS_LABEL_SIZE = 11.0


def _layout_gantt_chart(nodes: list[DiagramNode], *, theme: TemplateTheme, top: float) -> tuple[list[str], float]:
    """A project-schedule chart - one row per task, a task NAME in the
    left column and a DURATION BAR in the chart area to the right,
    positioned and sized by `node.gantt_start`/`node.gantt_duration` (a
    relative time unit - day/week/sprint - never a literal calendar date).
    Distinct from `timeline` (single POINT-IN-TIME milestones on an axis)
    - this kind's whole point is showing tasks that span a RANGE, often
    overlapping with each other, the standard project-management view.
    Tick gridlines mark every whole time unit across the full
    start..(start+duration) range actually used, auto-scaled to fit."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    starts = [max(n.gantt_start, 0.0) for n in nodes]
    durations = [max(n.gantt_duration, 0.1) for n in nodes]
    ends = [s + d for s, d in zip(starts, durations)]
    span = max(max(ends, default=1.0), 1.0)

    chart_left = MARGIN + _GANTT_LABEL_COL_W + 16.0
    chart_right = CANVAS_WIDTH - MARGIN
    chart_w = max(chart_right - chart_left, 100.0)
    unit_w = chart_w / span

    # Each row's own height grows to fit its task label (never a fixed
    # height a long name could overflow into the row below - the same
    # "measure every real line count before laying anything out" rule
    # fishbone/chevron/staircase already needed after a confirmed bug) -
    # the bar itself stays a fixed, modest height regardless, vertically
    # centred within whatever its own row ends up being.
    label_lines_per_row: list[list[str]] = []
    row_heights: list[float] = []
    for node in nodes:
        lines = _wrap(
            node.label.strip() or "—", font_size=GANTT_LABEL_SIZE, width=_GANTT_LABEL_COL_W, max_lines=999,
            bold=True,
        )
        label_lines_per_row.append(lines)
        text_h = len(lines) * GANTT_LABEL_SIZE * LINE_HEIGHT
        row_heights.append(max(_GANTT_ROW_H, text_h + 16.0))

    axis_top = top + 6.0
    rows_top = axis_top + _GANTT_AXIS_LABEL_SIZE + 14.0
    rows_bottom = rows_top + sum(row_heights) + _GANTT_ROW_GAP * (count - 1)
    elements: list[str] = []
    # One gridline per whole time unit - a plain, recessive reference grid,
    # the standard Gantt convention, never a label-bearing element itself.
    tick_count = math.ceil(span) + 1
    for tick in range(tick_count):
        tx = chart_left + tick * unit_w
        if tx > chart_right + 1.0:
            break
        elements.append(
            f'<line x1="{tx:.1f}" y1="{rows_top:.1f}" x2="{tx:.1f}" y2="{rows_bottom:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="1" opacity="0.6"/>'
        )
        elements.append(_centered_text(
            tx, axis_top + _GANTT_AXIS_LABEL_SIZE, str(tick), color=theme.muted_color, theme=theme,
            size=_GANTT_AXIS_LABEL_SIZE, weight=500,
        ))

    palette = _cycling_palette(theme)
    key_entries: list[tuple[str, str, str]] = []
    y = rows_top
    for index, node in enumerate(nodes):
        row_h = row_heights[index]
        row_cy = y + row_h / 2
        label_lines = label_lines_per_row[index]
        label_svg, _h = _text(
            MARGIN, row_cy - (len(label_lines) - 1) * GANTT_LABEL_SIZE * LINE_HEIGHT / 2 + GANTT_LABEL_SIZE * 0.3,
            label_lines, font_size=GANTT_LABEL_SIZE, weight=600, color=theme.text_color,
            font_family=theme.font_family, anchor="start",
        )
        elements.append(label_svg)

        bar_x = chart_left + starts[index] * unit_w
        bar_w = max(durations[index] * unit_w, 6.0)
        bar_h = min(_GANTT_ROW_H - 12.0, row_h - 12.0)
        fill, stroke = palette[index % len(palette)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<rect class="diagram-box" x="{bar_x:.1f}" y="{row_cy - bar_h / 2:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="2"/></g>'
        )
        key_entries.append((str(index + 1), node.label, node.detail))
        y += row_h + _GANTT_ROW_GAP

    bottom = y - _GANTT_ROW_GAP
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, (bottom + key_gap + key_h - top)


# ---------------------------------------------------------------------------
# 10 new kinds (reference-deck additions) - each confirmed as a recurring,
# structurally distinct pattern in Infographics_template.pptx with no
# existing-kind duplicate; see the approved plan's own evidence table.
# ---------------------------------------------------------------------------

_DONUT_LABEL_SIZE = 15.0


def _layout_donut_segments(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A thick ring cut into one equal-width coloured arc band per node,
    with the label on a short leader line outside the ring - categories
    that together make up one whole (a framework's pillars, a budget's
    categories), never a sequence (see RELATIONSHIP_KINDS). Unlike `cycle`
    (nodes-on-a-ring connected by arrows), this fills the ring itself as a
    proportional band - the deck's own recurring "doughnut" motif."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    radius_outer = 190.0
    radius_inner = 112.0
    cx = CANVAS_WIDTH / 2
    cy = top + radius_outer + 20.0
    palette = _cycling_palette(theme)
    elements: list[str] = []
    sweep = 360.0 / count
    for index, node in enumerate(nodes):
        start = index * sweep
        end = start + sweep
        fill, stroke = palette[index % len(palette)]
        path = _donut_wedge_path(cx, cy, radius_inner, radius_outer, start, end)
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<path class="diagram-box" d="{path}" fill="{fill}" stroke="{stroke}" stroke-width="2"/></g>'
        )
        mid_angle = start + sweep / 2
        lx, ly = _polar(cx, cy, radius_outer + 32.0, mid_angle)
        ex, ey = _polar(cx, cy, radius_outer + 2.0, mid_angle)
        elements.append(
            f'<line x1="{ex:.1f}" y1="{ey:.1f}" x2="{lx:.1f}" y2="{ly:.1f}" stroke="{stroke}" stroke-width="1.5"/>'
        )
        anchor = "start" if math.sin(math.radians(mid_angle)) >= 0 else "end"
        label_x = lx + (8.0 if anchor == "start" else -8.0)
        elements.append(
            _anchored_label(
                label_x, ly + 5.0, node.label, width=170.0, color=theme.text_color, theme=theme,
                size=_DONUT_LABEL_SIZE, weight=700, max_lines=2, anchor=anchor,
            )
        )
    bottom = cy + radius_outer + 60.0
    return elements, bottom - top


_PIE_LABEL_SIZE = 15.0


def _layout_pie_wedge(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A full circle cut into one wedge per node - the classic "parts of a
    whole" pie chart, with no existing equivalent (`cycle` is nodes on a
    ring, not a filled sector; `donut_segments` keeps an open centre, this
    doesn't). Reuses `_donut_wedge_path` with `r_inner=0`."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    radius = 190.0
    cx = CANVAS_WIDTH / 2
    cy = top + radius + 20.0
    palette = _cycling_palette(theme)
    elements: list[str] = []
    sweep = 360.0 / count
    for index, node in enumerate(nodes):
        start = index * sweep
        end = start + sweep
        fill, stroke = palette[index % len(palette)]
        path = _donut_wedge_path(cx, cy, 0.0, radius, start, end)
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<path class="diagram-box" d="{path}" fill="{fill}" stroke="{stroke}" stroke-width="2"/></g>'
        )
        mid_angle = start + sweep / 2
        lx, ly = _polar(cx, cy, radius * 0.62, mid_angle)
        chord = 2 * radius * 0.62 * math.sin(math.radians(min(sweep, 180.0) / 2))
        elements.append(
            _fit_boxed_text(
                lx, ly, node.label, width=max(chord * 0.92, 50.0), height=90.0,
                color=theme.text_color, theme=theme, base_size=_PIE_LABEL_SIZE, min_size=10.5, weight=700,
            )
        )
    bottom = cy + radius + 40.0
    return elements, bottom - top


_ORBIT_LABEL_SIZE = 15.0


def _layout_orbit_diagram(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """Concentric ring outlines around a central hub, one node "docked" on
    each ring - a central concept with layers of increasingly distant
    related ideas/categories. Unlike `radial_mindmap` (plain spokes, no
    ring/distance meaning), the ring itself carries the "how far from the
    centre" meaning here."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    hub_radius = 70.0
    ring_gap = 55.0
    node_r = 27.0
    cx = CANVAS_WIDTH / 2
    max_ring = hub_radius + ring_gap * count
    cy = top + max_ring + node_r + 20.0
    elements: list[str] = []
    for i in range(1, count + 1):
        r = hub_radius + ring_gap * i
        elements.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
            f'stroke="{theme.border_color}" stroke-width="1.5" stroke-dasharray="4 5"/>'
        )
    elements.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{hub_radius:.1f}" fill="{theme.accent_soft}" '
        f'stroke="{theme.accent_color}" stroke-width="2.5"/>'
    )
    palette = _cycling_palette(theme)
    golden_angle = 137.5  # spreads nodes around their own rings so no two share an angle
    for index, node in enumerate(nodes):
        r = hub_radius + ring_gap * (index + 1)
        angle = (index * golden_angle) % 360
        nx, ny = _polar(cx, cy, r, angle)
        fill, stroke = palette[index % len(palette)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        # Stopped at the hub's own edge and the node's own edge (radius r -
        # node_r, along the same angle) rather than running all the way to
        # either centre - a spoke ending AT a node's centre necessarily
        # samples points INSIDE that node's own circle, which app.services.
        # diagram_render_qa's edge_crosses_node check (correctly) reads as
        # a connector straying into a node, not as "reaching" it.
        spoke_x1, spoke_y1 = _polar(cx, cy, hub_radius, angle)
        spoke_x2, spoke_y2 = _polar(cx, cy, r - node_r, angle)
        elements.append(
            f'<line x1="{spoke_x1:.1f}" y1="{spoke_y1:.1f}" x2="{spoke_x2:.1f}" y2="{spoke_y2:.1f}" '
            f'stroke="{stroke}" stroke-width="1.5" stroke-dasharray="2 4" opacity="0.6"/>'
        )
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<circle class="diagram-box" cx="{nx:.1f}" cy="{ny:.1f}" r="{node_r:.1f}" fill="{stroke}" '
            f'stroke="#ffffff" stroke-width="2.5"/>'
            + _fit_boxed_text(
                nx, ny, str(index + 1), width=node_r * 1.3, height=node_r * 1.3,
                color="#ffffff", theme=theme, base_size=15.0, min_size=11.0, weight=800,
            )
            + "</g>"
        )
        label_r = r + node_r + 16.0
        lx, ly = _polar(cx, cy, label_r, angle)
        anchor = "start" if math.sin(math.radians(angle)) >= -0.05 else "end"
        elements.append(
            _anchored_label(
                lx, ly + 5.0, node.label, width=150.0, color=theme.text_color, theme=theme,
                size=_ORBIT_LABEL_SIZE, weight=700, max_lines=2, anchor=anchor,
            )
        )
    bottom = cy + max_ring + 50.0
    return elements, bottom - top


_PROGRESS_RING_LABEL_SIZE = 15.0
_PROGRESS_RING_RADIUS = 56.0


def _layout_progress_rings(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """A row of partial-arc "gauge" rings, one per node, each showing its
    own `value_pct` (0-100) - the one kind in this module that visualises
    a genuine per-item STAT rather than a qualitative relationship. A node
    with no `value_pct` draws an EMPTY ring and an em-dash rather than a
    guessed number - never a confident-looking fabricated stat."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    gap = 46.0
    cell_w = 2 * _PROGRESS_RING_RADIUS + gap
    total_w = count * cell_w - gap
    start_x = CANVAS_WIDTH / 2 - total_w / 2
    cy = top + _PROGRESS_RING_RADIUS + 10.0
    palette = _cycling_palette(theme)
    stroke_w = 14.0
    r = _PROGRESS_RING_RADIUS - stroke_w / 2
    circumference = 2 * math.pi * r
    elements: list[str] = []
    label_bottom = cy + _PROGRESS_RING_RADIUS
    for index, node in enumerate(nodes):
        cx = start_x + index * cell_w + _PROGRESS_RING_RADIUS
        fill, stroke = palette[index % len(palette)]
        has_value = node.value_pct is not None
        pct = max(0.0, min(100.0, node.value_pct if has_value else 0.0))
        dash = circumference * pct / 100.0
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<circle class="diagram-box" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
            f'stroke="{theme.border_color}" stroke-width="{stroke_w:.1f}"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" stroke="{stroke}" '
            f'stroke-width="{stroke_w:.1f}" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.1f} {circumference:.1f}" '
            f'transform="rotate(-90 {cx:.1f} {cy:.1f})"/>'
            + _fit_boxed_text(
                cx, cy, (f"{pct:.0f}%" if has_value else "—"), width=_PROGRESS_RING_RADIUS * 1.3,
                height=_PROGRESS_RING_RADIUS * 1.3, color=theme.text_color, theme=theme,
                base_size=20.0, min_size=13.0, weight=800,
            )
            + "</g>"
        )
        elements.append(
            _anchored_label(
                cx, label_bottom + 26.0, node.label, width=cell_w - 10.0, color=theme.text_color, theme=theme,
                size=_PROGRESS_RING_LABEL_SIZE, weight=700, max_lines=2, anchor="middle",
            )
        )
    bottom = label_bottom + 60.0
    return elements, bottom - top


_BRIDGE_LABEL_SIZE = 15.0


def _layout_bridge_process(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A short bounded sequence framed as "bridging a gap" - numbered step
    circles seated along an arched band over the top of a circle, with a
    pillar support at each end (the deck's own "bridge" motif). Unlike
    chevron_process/staircase_process (both straight), this is for content
    explicitly framed as crossing FROM one state TO another. Angles are
    measured from the LEFT end of the arch (-90deg in `_polar` terms)
    through the top (0deg) to the RIGHT end (+90deg) - the one kind here
    that needs angles on both sides of `_polar`'s own 0/360 wraparound, so
    it parametrises directly in that [-90, 90] range rather than reusing
    a 0-360 sweep the way donut_segments/pie_wedge do."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    step_r = 34.0
    gap_deg = 16.0
    half_span = 90.0 - gap_deg
    radius = max(230.0, (count - 1) * 75.0)
    cx = CANVAS_WIDTH / 2
    cy = top + radius + step_r + 10.0
    band_outer = radius + 22.0
    band_inner = radius - 22.0
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    band_path = _donut_wedge_path(cx, cy, band_inner, band_outer, -half_span, half_span)
    elements.append(
        f'<path d="{band_path}" fill="{theme.accent_soft}" stroke="{theme.accent_color}" stroke-width="2"/>'
    )

    palette = _cycling_palette(theme)
    key_entries: list[tuple[str, str, str]] = []
    end_positions: list[tuple[float, float, str]] = []
    for index, node in enumerate(nodes):
        angle = (-half_span + (2 * half_span) * index / (count - 1)) if count > 1 else 0.0
        nx, ny = _polar(cx, cy, radius, angle)
        fill, stroke = palette[index % len(palette)]
        if index == 0 or index == count - 1:
            end_positions.append((nx, ny, stroke))
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<circle class="diagram-box" cx="{nx:.1f}" cy="{ny:.1f}" r="{step_r:.1f}" fill="{stroke}" '
            f'stroke="#ffffff" stroke-width="2.5"/>'
            + _fit_boxed_text(
                nx, ny, str(index + 1), width=step_r * 1.3, height=step_r * 1.3,
                color="#ffffff", theme=theme, base_size=18.0, min_size=12.0, weight=800,
            )
            + "</g>"
        )
        elements.append(
            _anchored_label(
                nx, ny - step_r - 16.0, node.label, width=150.0, color=theme.text_color, theme=theme,
                size=_BRIDGE_LABEL_SIZE, weight=700, max_lines=2, anchor="middle",
            )
        )
        key_entries.append((str(index + 1), node.label, node.detail))

    # Pillars hang straight down from the FIRST and LAST step's own
    # position (clear of that step's own circle, not the band's outer
    # radius) - anchoring to band_outer put a pillar's top edge only
    # ~22px past a step centred at `radius`, well inside that step's own
    # 34px-radius circle, so it visibly clipped through it instead of
    # standing clear below it (a real, confirmed case caught by screenshot
    # review, not something the geometry QA checks for). A plain
    # surface-coloured rect with no visible link to the step above it (a
    # real, confirmed complaint: it read as a stray empty box, not a
    # bridge support) now gets a connector line down from the step circle,
    # a light tint of that same step's own colour, and a simple X-brace -
    # the standard truss-pillar silhouette - so it unmistakably reads as
    # "this step's own support", plus a ground line joining both pillars'
    # feet so the whole arch visibly stands on something.
    pillar_w, pillar_h = 56.0, 46.0
    pillar_gap = 14.0
    pillar_y = max(y for _x, y, _stroke in end_positions) + step_r + pillar_gap
    for px, py, stroke in end_positions:
        elements.append(
            f'<line x1="{px:.1f}" y1="{py + step_r:.1f}" x2="{px:.1f}" y2="{pillar_y:.1f}" '
            f'stroke="{stroke}" stroke-width="2.5"/>'
            f'<rect x="{px - pillar_w / 2:.1f}" y="{pillar_y:.1f}" width="{pillar_w:.1f}" height="{pillar_h:.1f}" '
            f'fill="{_tint(stroke)}" stroke="{stroke}" stroke-width="2"/>'
            f'<line x1="{px - pillar_w / 2 + 6:.1f}" y1="{pillar_y + 6:.1f}" '
            f'x2="{px + pillar_w / 2 - 6:.1f}" y2="{pillar_y + pillar_h - 6:.1f}" '
            f'stroke="{stroke}" stroke-width="1.5" opacity="0.5"/>'
            f'<line x1="{px + pillar_w / 2 - 6:.1f}" y1="{pillar_y + 6:.1f}" '
            f'x2="{px - pillar_w / 2 + 6:.1f}" y2="{pillar_y + pillar_h - 6:.1f}" '
            f'stroke="{stroke}" stroke-width="1.5" opacity="0.5"/>'
        )
    if len(end_positions) >= 2:
        left_x = min(px for px, _py, _s in end_positions) - pillar_w / 2
        right_x = max(px for px, _py, _s in end_positions) + pillar_w / 2
        ground_y = pillar_y + pillar_h
        elements.append(
            f'<line x1="{left_x:.1f}" y1="{ground_y:.1f}" x2="{right_x:.1f}" y2="{ground_y:.1f}" '
            f'stroke="{theme.border_color}" stroke-width="2"/>'
        )
    bottom = pillar_y + pillar_h + 20.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(
        key_entries, top=bottom + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme
    )
    elements.extend(key_elements)
    return elements, CANVAS_WIDTH, (bottom + key_gap + key_h - top)


_CHAIN_LABEL_SIZE = 15.0


def _layout_chain_links(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A row of overlapping, interlocking oval "link" shapes - a sequence
    where each step depends on/follows the one before it (unlike
    puzzle_pieces, which interlocks via a tab/notch and carries no inherent
    order). Simple left-to-right paint order already gives each later link
    the "on top of" look a real chain link has at its own overlap point -
    no extra z-order bookkeeping needed."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    link_w, link_h = 190.0, 110.0
    overlap = 46.0
    step = link_w - overlap
    total_w = link_w + step * (count - 1)
    canvas_w = max(total_w + 2 * MARGIN, CANVAS_WIDTH)
    start_x = (canvas_w - total_w) / 2
    cy = top + link_h / 2 + 10.0
    palette = _cycling_palette(theme)
    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []
    for index, node in enumerate(nodes):
        cx = start_x + link_w / 2 + step * index
        fill, stroke = palette[index % len(palette)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<rect class="diagram-box" x="{cx - link_w / 2:.1f}" y="{cy - link_h / 2:.1f}" '
            f'width="{link_w:.1f}" height="{link_h:.1f}" rx="{link_h / 2:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="3"/>'
            + _fit_boxed_text(
                cx, cy, node.label, width=link_w - overlap - 24.0, height=link_h - 24.0,
                color=theme.text_color, theme=theme, base_size=_CHAIN_LABEL_SIZE, min_size=10.5, weight=700,
            )
            + "</g>"
        )
        key_entries.append((str(index + 1), node.label, node.detail))
    bottom = cy + link_h / 2 + 20.0
    key_gap = 28.0
    key_elements, key_h = _key_table_rows(key_entries, top=bottom + key_gap, width=canvas_w - 2 * MARGIN, theme=theme)
    elements.extend(key_elements)
    return elements, canvas_w, (bottom + key_gap + key_h - top)


_PETAL_LABEL_SIZE = 15.0


def _layout_flower_petal(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """5-7 teardrop petal shapes radiating from a small central hub - a
    more decorative alternative to `radial_mindmap` for a set of related
    sub-topics orbiting one central theme. Each petal is a `<path>` of two
    symmetric cubic-bezier arcs from the hub out to a tip, with its control
    points computed directly in ABSOLUTE canvas coordinates (plain
    rotation trigonometry) - deliberately NOT an SVG `transform` (an
    earlier version used `rotate()` on a local-space `<ellipse>` and hit
    the exact class of bug `_edge_segments`'s own docstring already warns
    about for icons: every geometry check in app.services.diagram_render_
    qa reads raw element coordinates with no notion of `transform`, so a
    rotated local-space shape is read as if its LOCAL coordinates were
    absolute ones - confirmed here as `excessive_whitespace` plus several
    identical-looking `node_overlap` findings, all five petals' raw local
    bounding boxes being the same rectangle before rotation). Every other
    layout in this module draws directly in absolute coordinates for
    exactly this reason; this rewrite brings flower_petal into line with
    that same rule instead of adding a new exemption for it."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    hub_r = 54.0
    petal_len = 150.0
    petal_w = 86.0
    cx = CANVAS_WIDTH / 2
    cy = top + hub_r + petal_len + 30.0
    palette = _cycling_palette(theme)
    elements: list[str] = []
    for index, node in enumerate(nodes):
        angle_deg = index * 360.0 / count
        theta = math.radians(angle_deg)
        ux, uy = math.cos(theta), math.sin(theta)  # along the petal's own axis
        px, py = -math.sin(theta), math.cos(theta)  # perpendicular to it
        fill, stroke = palette[index % len(palette)]
        tip_x, tip_y = cx + petal_len * ux, cy + petal_len * uy
        bulge = petal_w / 2
        c1 = (cx + 0.35 * petal_len * ux + bulge * px, cy + 0.35 * petal_len * uy + bulge * py)
        c2 = (cx + 0.80 * petal_len * ux + bulge * 0.35 * px, cy + 0.80 * petal_len * uy + bulge * 0.35 * py)
        c3 = (cx + 0.80 * petal_len * ux - bulge * 0.35 * px, cy + 0.80 * petal_len * uy - bulge * 0.35 * py)
        c4 = (cx + 0.35 * petal_len * ux - bulge * px, cy + 0.35 * petal_len * uy - bulge * py)
        path = (
            f"M{cx:.1f},{cy:.1f} "
            f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {tip_x:.1f},{tip_y:.1f} "
            f"C{c3[0]:.1f},{c3[1]:.1f} {c4[0]:.1f},{c4[1]:.1f} {cx:.1f},{cy:.1f} Z"
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<path class="diagram-box" d="{path}" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/></g>'
        )
        label_r = hub_r + petal_len + 14.0
        lx = cx + label_r * ux
        ly = cy + label_r * uy
        elements.append(
            _anchored_label(
                lx, ly, node.label, width=130.0, color=theme.text_color, theme=theme,
                size=_PETAL_LABEL_SIZE, weight=700, max_lines=2, anchor="middle",
            )
        )
    elements.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{hub_r:.1f}" fill="{theme.accent_soft}" '
        f'stroke="{theme.accent_color}" stroke-width="2.5"/>'
    )
    bottom = cy + hub_r + petal_len + 40.0
    return elements, bottom - top


_HOURGLASS_LABEL_SIZE = 15.0
_HOURGLASS_WIDE_W = 520.0
_HOURGLASS_WAIST_W = 60.0


def _layout_hourglass_diagram(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float]:
    """Two mirrored trapezoid stacks meeting at a narrow waist - framing
    TWO OPPOSING categorisations (cost vs value, before vs after), unlike
    `funnel` (one-directional) or `pyramid` (one direction, importance not
    opposition). Reuses the exact trapezoid-polygon technique
    `_layout_pyramid` already draws, just computed twice (once narrowing,
    once widening) and stacked. The node list splits into a top half
    (narrowing DOWN to the waist) and a bottom half (widening DOWN from
    it) by position - the first ceil(n/2) nodes are the top half, matching
    the order the writer guidance for this kind asks for."""
    count = len(nodes)
    if count == 0:
        return [], 0.0
    top_n = (count + 1) // 2
    top_nodes = nodes[:top_n]
    bottom_nodes = nodes[top_n:]
    cx = CANVAS_WIDTH / 2
    layer_h = 70.0
    palette = _cycling_palette(theme)
    elements: list[str] = []
    key_entries: list[tuple[str, str, str]] = []

    def _draw_half(half_nodes: list[DiagramNode], *, widening: bool, y_start: float, palette_offset: int) -> float:
        n = len(half_nodes)
        yy = y_start
        for index, node in enumerate(half_nodes):
            if widening:
                w_top = _HOURGLASS_WAIST_W + (_HOURGLASS_WIDE_W - _HOURGLASS_WAIST_W) * index / n
                w_bottom = _HOURGLASS_WAIST_W + (_HOURGLASS_WIDE_W - _HOURGLASS_WAIST_W) * (index + 1) / n
            else:
                w_top = _HOURGLASS_WIDE_W - (_HOURGLASS_WIDE_W - _HOURGLASS_WAIST_W) * index / n
                w_bottom = _HOURGLASS_WIDE_W - (_HOURGLASS_WIDE_W - _HOURGLASS_WAIST_W) * (index + 1) / n
            fill, stroke = palette[(palette_offset + index) % len(palette)]
            points = (
                f"{cx - w_top / 2:.1f},{yy:.1f} {cx + w_top / 2:.1f},{yy:.1f} "
                f"{cx + w_bottom / 2:.1f},{yy + layer_h:.1f} {cx - w_bottom / 2:.1f},{yy + layer_h:.1f}"
            )
            tooltip = node.label.strip()
            if node.detail.strip():
                tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
            title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
            elements.append(
                f'<g class="diagram-node" tabindex="0" role="group">{title}'
                f'<polygon class="diagram-box" points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
                + _fit_boxed_text(
                    cx, yy + layer_h / 2, node.label, width=max(min(w_top, w_bottom) * 0.9, 60.0),
                    height=layer_h * 0.75, color=theme.text_color, theme=theme,
                    base_size=_HOURGLASS_LABEL_SIZE, min_size=10.0,
                )
                + "</g>"
            )
            key_entries.append((str(len(key_entries) + 1), node.label, node.detail))
            yy += layer_h
        return yy

    y = top
    if top_nodes:
        y = _draw_half(top_nodes, widening=False, y_start=y, palette_offset=0)
    if bottom_nodes:
        y = _draw_half(bottom_nodes, widening=True, y_start=y, palette_offset=len(top_nodes))

    key_gap = 28.0
    key_elements, key_h = _key_table_rows(key_entries, top=y + key_gap, width=CANVAS_WIDTH - 2 * MARGIN, theme=theme)
    elements.extend(key_elements)
    return elements, (y + key_gap + key_h - top)


_ORG_CHART_LABEL_SIZE = 15.0


def _layout_org_chart(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A top-down organisational tree - role/title card nodes (a bold
    title line + a lighter role/detail line, divided by a hairline) joined
    by orthogonal (right-angle) connectors, the literal "org chart"
    convention - distinct from the generic labelled-box `hierarchy` kind
    this reuses the same row-by-`level` positioning technique from (see
    `_layout_hierarchy`'s own docstring)."""
    rows: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        rows.setdefault(max(node.level, 0), []).append(node)
    levels = sorted(rows)

    box_w = 190.0
    box_h = 72.0
    row_gap = 50.0
    gap_x = 24.0

    row_widths = {level: len(rows[level]) * box_w + (len(rows[level]) - 1) * gap_x for level in levels}
    max_row_width = max(row_widths.values(), default=0.0)
    canvas_w = max(max_row_width + 2 * MARGIN, CANVAS_WIDTH)

    positions: dict[str, tuple[float, float, float]] = {}
    elements: list[str] = []
    y = top
    for level in levels:
        row_nodes = rows[level]
        row_width = row_widths[level]
        x = (canvas_w - row_width) / 2
        for node in row_nodes:
            cx = x + box_w / 2
            title_svg = _fit_boxed_text(
                cx, y + box_h * 0.36, node.label, width=box_w - 20.0, height=box_h * 0.5,
                color=theme.text_color, theme=theme, base_size=_ORG_CHART_LABEL_SIZE, min_size=11.0, weight=700,
            )
            role_svg = ""
            if node.detail.strip():
                role_svg = _fit_boxed_text(
                    cx, y + box_h * 0.76, node.detail, width=box_w - 20.0, height=box_h * 0.38,
                    color=theme.muted_color, theme=theme, base_size=12.0, min_size=10.0, weight=500,
                )
            tooltip = node.label.strip()
            if node.detail.strip():
                tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
            title_tag = f"<title>{escape(tooltip)}</title>" if tooltip else ""
            elements.append(
                # No inner divider line between the title/role lines -
                # app.services.diagram_render_qa's _edge_segments treats
                # EVERY <line> in the tree as a potential connector with no
                # class check at all (same "not every line/path is a real
                # edge" gap _layout_bridge_process's own band hits) - a
                # full-width divider sitting inside this very box would
                # read as an edge crossing its own node on every sample.
                # The bold/muted weight difference between the two text
                # lines already separates them visually without it.
                f'<g class="diagram-node" tabindex="0" role="group">{title_tag}'
                f'<rect class="diagram-box" x="{x:.1f}" y="{y:.1f}" width="{box_w:.1f}" height="{box_h:.1f}" '
                f'rx="8" fill="{theme.surface_color}" stroke="{theme.accent_color}" stroke-width="2"/>'
                f'{title_svg}{role_svg}</g>'
            )
            positions[node.id or node.label] = (cx, y, y + box_h)
            x += box_w + gap_x
        y += box_h + row_gap

    for edge in edges:
        parent = positions.get(edge.source)
        child = positions.get(edge.target)
        if not parent or not child:
            continue
        mid_y = (parent[2] + child[1]) / 2
        elements.append(
            f'<path d="M{parent[0]:.1f},{parent[2]:.1f} L{parent[0]:.1f},{mid_y:.1f} '
            f'L{child[0]:.1f},{mid_y:.1f} L{child[0]:.1f},{child[1]:.1f}" '
            f'fill="none" stroke="{theme.accent_color}" stroke-width="2"/>'
        )

    return elements, (y - row_gap - top), canvas_w


_JOURNEY_LABEL_SIZE = 15.0


def _layout_wavy_journey_path(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """Numbered milestone nodes along a smooth S-curve path, alternating
    above/below the curve's own centreline - a sequential roadmap/journey
    narrative, distinct from `timeline` (a straight line). Consecutive
    nodes are joined by a cubic bezier whose control points sit at each
    node's own y and halfway between the two x's - the standard technique
    for a smooth wave through a series of points with no separate curve-
    fitting step."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    node_r = 32.0
    amplitude = 70.0
    step_x = 190.0
    total_w = max(count - 1, 0) * step_x + 2 * node_r
    canvas_w = max(total_w + 2 * MARGIN, CANVAS_WIDTH)
    start_x = (canvas_w - total_w) / 2 + node_r
    cy = top + amplitude + node_r + 50.0

    points: list[tuple[float, float]] = []
    for index in range(count):
        x = start_x + step_x * index
        y = cy + (amplitude if index % 2 else -amplitude)
        points.append((x, y))

    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    if count > 1:
        path_d = f"M{points[0][0]:.1f},{points[0][1]:.1f} "
        for i in range(1, count):
            x0, y0 = points[i - 1]
            x1, y1 = points[i]
            cx1, cy1 = x0 + step_x / 2, y0
            cx2, cy2 = x1 - step_x / 2, y1
            path_d += f"C{cx1:.1f},{cy1:.1f} {cx2:.1f},{cy2:.1f} {x1:.1f},{y1:.1f} "
        marker = ' marker-end="url(#diagram-arrow)"' if count > 1 else ""
        elements.append(f'<path d="{path_d}" fill="none" stroke="{theme.accent_color}" stroke-width="3"{marker}/>')

    palette = _cycling_palette(theme)
    for index, node in enumerate(nodes):
        x, y = points[index]
        fill, stroke = palette[index % len(palette)]
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title}'
            f'<circle class="diagram-box" cx="{x:.1f}" cy="{y:.1f}" r="{node_r:.1f}" fill="{stroke}" '
            f'stroke="#ffffff" stroke-width="3"/>'
            + _fit_boxed_text(
                x, y, str(index + 1), width=node_r * 1.3, height=node_r * 1.3,
                color="#ffffff", theme=theme, base_size=17.0, min_size=12.0, weight=800,
            )
            + "</g>"
        )
        below = index % 2 == 1
        label_y = y + node_r + 24.0 if below else y - node_r - 16.0
        elements.append(
            _anchored_label(
                x, label_y, node.label, width=step_x - 20.0, color=theme.text_color, theme=theme,
                size=_JOURNEY_LABEL_SIZE, weight=700, max_lines=2, anchor="middle",
            )
        )
    bottom = cy + amplitude + node_r + 50.0
    return elements, canvas_w, (bottom - top)


# ---------------------------------------------------------------------------
# spectrum: a single continuum from matrix_x_low to matrix_x_high, each node
# placed at its own value_pct along it (see DiagramNode.value_pct/DiagramSpec
# .matrix_x_low's own docstrings for the field-reuse reasoning).
# ---------------------------------------------------------------------------

_SPECTRUM_LABEL_SIZE = RING_LABEL_SIZE


def _layout_spectrum(
    spec: DiagramSpec, *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A single horizontal continuum from `matrix_x_low` to `matrix_x_high`
    - a political spectrum, a pH scale, an introvert/extrovert scale - each
    node placed at its own `value_pct` (0-100) along the line. A node with
    no `value_pct` set falls back to an even spread by its own position in
    `nodes` (never guessed from its neighbours' real values, never
    dropped). Labels alternate above/below the line, in the order they
    actually land along it (not `nodes` order), so two positions that
    happen to sit close together never collide label-to-label - the same
    "alternate by drawn position" idea `_layout_wavy_journey_path` already
    uses for its own milestones."""
    nodes = spec.nodes
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0

    canvas_w = CANVAS_WIDTH
    line_x0 = MARGIN + 90.0
    line_x1 = canvas_w - MARGIN - 90.0
    stem_len = 24.0
    card_gap = 8.0
    # The widest a nudge-driven canvas is ever allowed to grow to - past
    # this, RING_LABEL_SIZE text (calibrated for this module's normal
    # 880px canvas) scales down below the 11px readability floor once the
    # page shrinks it back to CONTENT_WIDTH, the exact same class of bug
    # `_layout_radial_mindmap`'s own canvas-width geometry already had to
    # fix. Comfortably under the true ~908px threshold.
    _SPECTRUM_MAX_CANVAS_W = 900.0

    positioned: list[tuple[float, DiagramNode]] = []
    for index, node in enumerate(nodes):
        pct = node.value_pct if node.value_pct is not None else (100.0 * index / max(count - 1, 1))
        pct = min(max(pct, 0.0), 100.0)
        x = line_x0 + (pct / 100.0) * (line_x1 - line_x0)
        positioned.append((x, node))
    positioned.sort(key=lambda item: item[0])

    def _plan(card_w: float):
        """Collision avoidance NUDGES a card sideways (never stacks it
        onto a second row behind an earlier one) - a real, confirmed bug
        with the row-stacking approach tried first: a stem line reaching a
        DEEPER row necessarily passes straight through the shallower
        row's own card along the way, which `edge_crosses_node` correctly
        flags (that stem really does cross that card). A single row per
        side, with each card's centre nudged right of the previous one on
        that side whenever its own x-position would otherwise overlap,
        keeps every stem a single short, unobstructed segment from its
        dot to its own (possibly nudged) card - `positioned` is already
        x-sorted, so nudges only ever push right, never cross an earlier
        card back over."""
        plans: list[tuple[float, float, DiagramNode, bool, float, list[str]]] = []
        above_edge: float | None = None
        below_edge: float | None = None
        max_above_h, max_below_h = 0.0, 0.0
        for draw_index, (x, node) in enumerate(positioned):
            below = draw_index % 2 == 1
            lines = _wrap(node.label, font_size=_SPECTRUM_LABEL_SIZE, width=card_w - 22.0, max_lines=999, bold=True)
            card_h = max(len(lines) * _SPECTRUM_LABEL_SIZE * LINE_HEIGHT + 16.0, 34.0)
            label_x = x
            edge = below_edge if below else above_edge
            # The +3.0 is a deliberate small real gap, not just touching -
            # two cards landing EXACTLY edge-to-edge is one floating-point
            # rounding error away from a sub-pixel overlap that
            # `node_overlap` (correctly) flags, a real, confirmed case at
            # the default card width.
            if edge is not None and label_x - card_w / 2 < edge + 3.0:
                label_x = edge + 3.0 + card_w / 2
            if below:
                below_edge = label_x + card_w / 2
            else:
                above_edge = label_x + card_w / 2
            block_h = stem_len + card_h + card_gap
            if below:
                max_below_h = max(max_below_h, block_h)
            else:
                max_above_h = max(max_above_h, block_h)
            plans.append((x, label_x, node, below, card_h, lines))
        return plans, above_edge, below_edge, max_above_h, max_below_h

    # A card_w that would need more nudging room than the canvas can
    # safely grow to (dense clustering - a real, confirmed case: 8 nodes
    # within a few percentage points of each other) shrinks ONCE to
    # whatever width actually fits that many cards in the safe budget,
    # rather than ever letting the canvas grow past the readable limit -
    # cards sitting a little closer together than the default beats any
    # label shrinking below the readability floor.
    card_w = 150.0
    plans, above_edge, below_edge, max_above_h, max_below_h = _plan(card_w)
    needed = max(above_edge or 0.0, below_edge or 0.0)
    # Shrinks geometrically (never more than a handful of iterations,
    # `max_nodes_for("spectrum")` caps this at 8 nodes total) until the
    # nudge-driven chain fits the safe canvas budget, or hits a floor
    # narrow enough that 4 chained cards (half of the node cap, the worst
    # realistic one-side pile-up) still comfortably fits it.
    while needed + MARGIN > _SPECTRUM_MAX_CANVAS_W and card_w > 85.0:
        card_w = max(card_w * 0.85, 85.0)
        plans, above_edge, below_edge, max_above_h, max_below_h = _plan(card_w)
        needed = max(above_edge or 0.0, below_edge or 0.0)

    canvas_w = min(max(canvas_w, needed + MARGIN), _SPECTRUM_MAX_CANVAS_W)
    # Final safety net, after everything above: a cluster sitting right at
    # the continuum's own far end (value_pct close to 100) has nowhere
    # left to nudge INTO without the shrink loop alone guaranteeing it
    # stays on-canvas, however narrow `card_w` gets. Shifting every card on
    # an overflowing side left by the SAME amount preserves their already-
    # collision-free relative spacing while guaranteeing the rightmost one
    # never draws past `canvas_w`'s own right margin.
    overflow = {
        True: max(0.0, (below_edge or 0.0) - (canvas_w - MARGIN)),
        False: max(0.0, (above_edge or 0.0) - (canvas_w - MARGIN)),
    }
    if overflow[True] or overflow[False]:
        plans = [
            (x, label_x - overflow[below], node, below, card_h, lines)
            for x, label_x, node, below, card_h, lines in plans
        ]

    y = top
    elements: list[str] = []
    title = spec.matrix_x_label.strip()
    if title:
        title_svg, title_h = _text(
            canvas_w / 2, y + 16.0, [title], font_size=16.0, weight=700,
            color=theme.text_color, font_family=theme.font_family,
        )
        elements.append(title_svg)
        y += title_h + 22.0

    # `max_above_h` already accounts for every "above" card's real row
    # depth/height - never a fixed guess that a tall clustered stack could
    # exceed and draw above this layout's own `top`.
    line_y = y + max_above_h + 20.0
    elements.append(
        f'<line x1="{line_x0:.1f}" y1="{line_y:.1f}" x2="{line_x1:.1f}" y2="{line_y:.1f}" '
        f'stroke="{theme.accent_color}" stroke-width="3" stroke-linecap="round"/>'
    )
    for cap_x in (line_x0, line_x1):
        elements.append(
            f'<line x1="{cap_x:.1f}" y1="{line_y - 9.0:.1f}" x2="{cap_x:.1f}" y2="{line_y + 9.0:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="3" stroke-linecap="round"/>'
        )
    low_label, high_label = spec.matrix_x_low.strip(), spec.matrix_x_high.strip()
    if low_label:
        elements.append(
            _anchored_label(
                line_x0, line_y + 32.0, low_label, width=150.0, color=theme.muted_color, theme=theme,
                size=12.5, weight=700, max_lines=1, anchor="start",
            )
        )
    if high_label:
        elements.append(
            _anchored_label(
                line_x1, line_y + 32.0, high_label, width=150.0, color=theme.muted_color, theme=theme,
                size=12.5, weight=700, max_lines=1, anchor="end",
            )
        )

    # Each position's own marker dot is deliberately plain (not
    # `class="diagram-box"`) and its label lives on a small card instead of
    # bare floating text - the exact same "plain axis marker + leader stem
    # + real diagram-box card" split `_layout_timeline` already uses, for
    # the same two reasons: a bare text label has near-zero measured area,
    # which starved this kind's occupancy_issues check (a real, confirmed
    # case: 3 short labels on an otherwise-wide canvas measured as only 3%
    # occupied); and a stem line touching a `diagram-box`-classed dot at
    # its OWN end was being misread as an edge crossing/running collinear
    # with that same dot's border by `edge_crosses_node`/
    # `edge_collinear_with_node_border`, neither of which happens once the
    # dot itself carries no `diagram-box` class for them to match against.
    palette = _cycling_palette(theme)
    max_below_drawn = 0.0
    for draw_index, (x, label_x, node, below, card_h, lines) in enumerate(plans):
        fill, stroke = palette[draw_index % len(palette)]
        elements.append(f'<circle cx="{x:.1f}" cy="{line_y:.1f}" r="7" fill="{stroke}" stroke="none"/>')
        stem_y1 = line_y + (7.0 if below else -7.0)
        card_top = line_y + stem_len if below else line_y - stem_len - card_h
        stem_y2 = card_top if below else card_top + card_h
        elements.append(
            f'<line x1="{x:.1f}" y1="{stem_y1:.1f}" x2="{label_x:.1f}" y2="{stem_y2:.1f}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        title_tag = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        label_svg, _lh = _text(
            label_x,
            card_top + card_h / 2 + _SPECTRUM_LABEL_SIZE * 0.35
            - (len(lines) - 1) * _SPECTRUM_LABEL_SIZE * LINE_HEIGHT / 2,
            lines, font_size=_SPECTRUM_LABEL_SIZE, weight=700, color=theme.text_color,
            font_family=theme.font_family, anchor="middle",
        )
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">{title_tag}'
            f'<rect class="diagram-box" x="{label_x - card_w / 2:.1f}" y="{card_top:.1f}" width="{card_w:.1f}" '
            f'height="{card_h:.1f}" rx="7" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
            f"{label_svg}</g>"
        )
        if below:
            max_below_drawn = max(max_below_drawn, card_top + card_h - line_y)

    bottom = line_y + max_below_drawn + card_gap
    return elements, canvas_w, (bottom - top)


# ---------------------------------------------------------------------------
# iceberg: a visible tip (level 0) above a dashed waterline, a hidden mass
# (level >= 1, each deeper level WIDER) below it.
# ---------------------------------------------------------------------------

_ICEBERG_ROW_GAP = 22.0


def _iceberg_row(
    row_nodes: list[DiagramNode], *, cx: float, y: float, row_w: float, theme: TemplateTheme,
    fill: str, stroke: str,
) -> tuple[str, float]:
    count = len(row_nodes)
    gap = 14.0
    chip_w = max((row_w - (count - 1) * gap) / count, 110.0)
    total_w = count * chip_w + (count - 1) * gap
    x = cx - total_w / 2
    parts: list[str] = []
    max_h = 0.0
    for node in row_nodes:
        svg, h = _node_block(node, x=x, y=y, width=chip_w, theme=theme, colors=(fill, stroke))
        parts.append(svg)
        max_h = max(max_h, h)
        x += chip_w + gap
    return "".join(parts), max_h


def _layout_iceberg(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """The classic "visible tip, hidden mass" shape - level 0 nodes sit
    ABOVE a dashed waterline (the small, visible part of the idea), level
    >= 1 nodes sit below it in rows that grow WIDER with depth (the much
    larger hidden part) - an iceberg of conflict (the visible argument
    above the line, the unspoken needs/fears/history beneath it), a
    culture/competency iceberg (visible skills vs. hidden traits/values).
    Nodes sharing a level share one row, in `nodes` order. `is_usable()`
    already guarantees at least one node at level 0 and at least one at
    level >= 1, so neither zone is ever drawn empty."""
    rows: dict[int, list[DiagramNode]] = {}
    for node in nodes:
        rows.setdefault(max(node.level, 0), []).append(node)
    visible = rows.get(0, [])
    hidden_levels = sorted(lvl for lvl in rows if lvl >= 1)

    canvas_w = CANVAS_WIDTH
    cx = canvas_w / 2
    y = top
    elements: list[str] = []

    if visible:
        row_w = min(len(visible) * 220.0, canvas_w - 2 * MARGIN)
        row_svg, row_h = _iceberg_row(
            visible, cx=cx, y=y, row_w=row_w, theme=theme, fill=theme.surface_color, stroke=theme.accent_color,
        )
        elements.append(row_svg)
        y += row_h + 20.0

    waterline_y = y
    elements.append(
        f'<line x1="{MARGIN:.1f}" y1="{waterline_y:.1f}" x2="{canvas_w - MARGIN:.1f}" y2="{waterline_y:.1f}" '
        f'stroke="{theme.muted_color}" stroke-width="2" stroke-dasharray="6,5"/>'
    )
    elements.append(
        _anchored_label(
            canvas_w - MARGIN, waterline_y - 12.0, "Visible", width=90.0, color=theme.muted_color, theme=theme,
            size=11.0, weight=700, max_lines=1, anchor="end",
        )
    )
    elements.append(
        _anchored_label(
            canvas_w - MARGIN, waterline_y + 24.0, "Hidden", width=90.0, color=theme.muted_color, theme=theme,
            size=11.0, weight=700, max_lines=1, anchor="end",
        )
    )
    y = waterline_y + 30.0

    for depth_index, level in enumerate(hidden_levels):
        row_nodes = rows[level]
        row_w = min(canvas_w - 2 * MARGIN, 260.0 + depth_index * 90.0 + len(row_nodes) * 50.0)
        toward_white = max(0.82 - depth_index * 0.14, 0.15)
        fill = _tint(theme.accent_color, toward_white=toward_white)
        row_svg, row_h = _iceberg_row(
            row_nodes, cx=cx, y=y, row_w=row_w, theme=theme, fill=fill, stroke=theme.accent_color,
        )
        elements.append(row_svg)
        y += row_h + _ICEBERG_ROW_GAP

    bottom = y - (_ICEBERG_ROW_GAP if hidden_levels else 0.0)
    return elements, canvas_w, (bottom - top)


# ---------------------------------------------------------------------------
# feedback_loop_process: a linear chain (like `process`) plus one or more
# explicit backward "revise and go back" edges, drawn as a dashed side rail.
# ---------------------------------------------------------------------------


def _layout_feedback_loop_process(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A linear top-to-bottom chain of steps (same `_node_block` box
    stacking every sequential box-based kind in this module uses) PLUS one
    or more explicit "revise and go back" return edges - the scientific
    method (hypothesize -> test -> [back to hypothesize on failure] ->
    conclude), an Agile sprint (plan -> build -> review -> [back to plan]
    -> ship). Distinct from `cycle` (which always loops back to the very
    FIRST step, every time, by definition, and never also reads as a
    plain finishing sequence) - here the forward chain still reads exactly
    like `process`, and only an edge that explicitly goes backward (its
    target at or before its source in `nodes` order) draws as a dashed
    rail on the LEFT (mirroring `_layout_standard_flowchart`'s own
    right-side rail for a forward skip-ahead branch, so the two read as
    deliberately different conventions - back vs. ahead - at a glance),
    labelled with its own condition (e.g. "if it fails")."""
    if not nodes:
        return [], CANVAS_WIDTH, 0.0
    index_by_id = {n.id: i for i, n in enumerate(nodes) if n.id}
    has_backward = any(
        edge.source in index_by_id and edge.target in index_by_id
        and index_by_id[edge.target] <= index_by_id[edge.source]
        for edge in edges
    )
    box_w = 440.0
    canvas_w = CANVAS_WIDTH + (130.0 if has_backward else 0.0)
    cx = canvas_w / 2 + (65.0 if has_backward else 0.0)
    gap = 60.0
    y = top
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    positions: list[tuple[float, float, float]] = []
    for node in nodes:
        svg, height = _node_block(node, x=cx - box_w / 2, y=y, width=box_w, theme=theme)
        elements.append(svg)
        positions.append((cx, y, y + height))
        y += height + gap

    for i in range(len(nodes) - 1):
        bottom = positions[i][2]
        next_top = positions[i + 1][1]
        elements.append(
            f'<line x1="{cx:.1f}" y1="{bottom:.1f}" x2="{cx:.1f}" y2="{next_top - 4.0:.1f}" '
            f'stroke="{theme.accent_color}" stroke-width="2.5" marker-end="url(#diagram-arrow)"/>'
        )

    base_rail_x = cx - box_w / 2 - 50.0
    rail_bands: list[tuple[float, float, float]] = []
    for edge in edges:
        si, ti = index_by_id.get(edge.source), index_by_id.get(edge.target)
        if si is None or ti is None or ti > si:
            continue  # a forward edge (already drawn above) or an unknown node
        x1, y1 = cx - box_w / 2, (positions[si][1] + positions[si][2]) / 2
        x2, y2 = cx - box_w / 2, (positions[ti][1] + positions[ti][2]) / 2
        y_lo, y_hi = min(y1, y2), max(y1, y2)
        rail_x = base_rail_x
        for lo, hi, used_rail in rail_bands:
            if y_lo < hi and lo < y_hi:
                rail_x = min(rail_x, used_rail - 40.0)
        rail_bands.append((y_lo, y_hi, rail_x))
        elements.append(_flowchart_side_connector(x1, y1, x2, y2, rail_x=rail_x, theme=theme))
        if edge.label.strip():
            # A wider budget than `_layout_standard_flowchart`'s own
            # `max_width=110.0` (a short "Yes"/"No" branch word) - a
            # feedback loop's own label is typically a reason/condition
            # phrase ("if validation fails go back"), long enough that
            # 110px forced `_edge_label`'s existing 2-line cap into its
            # own truncate-with-"…" fallback, a real, confirmed case.
            elements.append(_edge_label(rail_x - 20.0, (y1 + y2) / 2, edge.label, theme, max_width=190.0))

    bottom = positions[-1][2]
    return elements, canvas_w, (bottom - top)


# ---------------------------------------------------------------------------
# causal_loop: factors around a ring, connected by curved, polarity-labelled
# ("+"/"-") arrows - the systems-thinking causal loop diagram notation.
# ---------------------------------------------------------------------------

_CAUSAL_NODE_R = 72.0
_CAUSAL_LABEL_SIZE = RING_LABEL_SIZE


def _parse_polarity(label: str) -> tuple[str, str]:
    """(sign, rest) - `sign` is "+"/"-"/"" (a leading reinforcing/balancing
    marker, see DiagramEdge.label's own docstring), `rest` is the label
    with that leading sign and any following whitespace stripped. A label
    with no leading sign returns ("", the whole trimmed label) - never a
    crash, never a dropped label."""
    text = label.strip()
    if text[:1] in ("+", "-"):
        return text[0], text[1:].strip()
    return "", text


def _layout_causal_loop(
    nodes: list[DiagramNode], edges: list[DiagramEdge], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """Factors arranged around a ring (the same even-angle-spacing
    convention `_layout_cycle`/`_layout_ring_process` already use),
    connected by curved, polarity-labelled arrows - a reinforcing link
    ("+": more of the source causes more of the target) or a balancing one
    ("-": more of the source causes LESS of it) - the standard systems-
    thinking "causal loop diagram" notation. Unlike `cycle`, an edge isn't
    limited to consecutive neighbours - any factor can drive any other,
    which is the whole point of a feedback system; each curve bows OUTWARD
    from the ring's own centre so two edges on opposite sides of the ring
    never cross through the middle the way a straight chord would."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0

    # Each node's circle grows past `_CAUSAL_NODE_R` to fit a long label
    # (see `_circle_node_size`'s own "never truncates" contract) - sizing
    # every node FIRST, before any position is chosen, means the ring's
    # own radius can be derived from the REAL largest circle rather than
    # the fixed minimum. A real, confirmed bug when it wasn't: a ring
    # radius picked from node COUNT alone (`count * 50.0`) stayed far too
    # small once several nodes grew past that estimate (long factor
    # labels), so the curved edges between nodes - sized to bow around
    # circles of the OLD, smaller assumed radius - cut straight through
    # neighbouring nodes that had since grown bigger.
    #
    # Unlike `_layout_radial_mindmap` (branch-to-centre only, so a single
    # adjacent-neighbour chord is the only pair that ever matters), an
    # edge here can connect ANY two nodes on the ring, at any angular
    # separation - a real, confirmed bug when this used the plain
    # adjacent-chord formula alone: two nodes ~103 degrees apart (not the
    # ~51 degree adjacent spacing the formula was solved for) still had
    # axis-aligned bounding SQUARES that overlapped even though their true
    # circle-to-circle distance did not, the same bounding-box-vs-true-
    # distance gap `_layout_radial_mindmap`'s own fix already documents -
    # worse here since it's not just the adjacent pair at risk, every
    # pairwise separation is. The flat `sqrt(2)` margin (the true maximum
    # that gap can ever reach, at any angle) covers every pair at once,
    # not just the adjacent one.
    sizings = [
        _circle_node_size(node.label, min_radius=_CAUSAL_NODE_R, font_size=_CAUSAL_LABEL_SIZE) for node in nodes
    ]
    max_node_r = max((r for _lines, r in sizings), default=_CAUSAL_NODE_R)
    radius = max(
        190.0,
        (2 * max_node_r + 30.0) / (2 * math.sin(math.pi / count)) * math.sqrt(2) if count >= 2 else 0.0,
    )
    cx = radius + max_node_r + 30.0
    cy = top + radius + max_node_r + 30.0
    canvas_w = 2 * cx

    centers: dict[str, tuple[float, float]] = {}
    radii: dict[str, float] = {}
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    palette = _cycling_palette(theme)
    for index, node in enumerate(nodes):
        angle = (2 * math.pi * index / count) - math.pi / 2
        nx = cx + radius * math.cos(angle)
        ny = cy + radius * math.sin(angle)
        if node.id:
            centers[node.id] = (nx, ny)
            radii[node.id] = sizings[index][1]
        fill, stroke = palette[index % len(palette)]
        svg, _radius_used = _circle_node(
            node.label, cx=nx, cy=ny, theme=theme, number="", fill=fill, stroke=stroke,
            min_radius=_CAUSAL_NODE_R, font_size=_CAUSAL_LABEL_SIZE, detail=node.detail, sizing=sizings[index],
        )
        elements.append(svg)

    for edge in edges:
        a, b = centers.get(edge.source), centers.get(edge.target)
        if not a or not b or edge.source == edge.target:
            continue
        # Trimmed by each node's own REAL radius, not the fixed
        # `_CAUSAL_NODE_R` tried first - a real, confirmed bug: a label
        # long enough to grow its circle past that fixed minimum (see
        # `_circle_node_size`'s own "never truncates" contract) left the
        # edge's endpoint still computed from the smaller, stale radius,
        # landing it INSIDE that node's real, bigger circle - precisely
        # the `edge_crosses_node` failure this trim exists to prevent.
        r_source, r_target = radii[edge.source], radii[edge.target]
        sign, rest = _parse_polarity(edge.label)
        x1, y1 = a
        x2, y2 = b
        dist = max(math.hypot(x2 - x1, y2 - y1), 1.0)
        ux, uy = (x2 - x1) / dist, (y2 - y1) / dist
        sx, sy = x1 + ux * r_source, y1 + uy * r_source
        tx, ty = x2 - ux * r_target, y2 - uy * r_target
        mx, my = (sx + tx) / 2, (sy + ty) / 2
        # A bow PERPENDICULAR to the source->target direction, scaled with
        # the chord's own length - not the fixed 34px tried first, and not
        # bowed toward/away from the ring's centre. A real, confirmed bug
        # with both of those: a short, fixed bow cleared the gap between
        # ADJACENT nodes fine, but a longer edge connecting two nodes
        # several positions apart has a chord that cuts much closer to the
        # ring's own middle, where it was passing straight through an
        # UNRELATED node sitting on the arc between the two endpoints - a
        # small, constant bow never grew to route around it. The direction
        # also used to come from (midpoint - centre), which degenerates
        # (near divide-by-noise) for two nearly-diametric nodes, whose
        # midpoint sits almost exactly ON the centre; the chord's own
        # perpendicular is always well-defined instead, from the two real
        # endpoints, however far apart they are.
        perp_x, perp_y = -uy, ux
        side = 1.0 if (perp_x * (mx - cx) + perp_y * (my - cy)) >= 0 else -1.0
        bow = min(max(dist * 0.22, max_node_r * 0.6), radius * 0.9)
        ctrl = (mx + perp_x * side * bow, my + perp_y * side * bow)
        elements.append(
            f'<path d="M{sx:.1f},{sy:.1f} Q{ctrl[0]:.1f},{ctrl[1]:.1f} {tx:.1f},{ty:.1f}" '
            f'fill="none" stroke="{theme.accent_color}" stroke-width="2.5" marker-end="url(#diagram-arrow)"/>'
        )
        label_x, label_y = ctrl
        if sign:
            badge_color = "#16a34a" if sign == "+" else "#dc2626"
            elements.append(
                f'<circle cx="{label_x:.1f}" cy="{label_y:.1f}" r="13" fill="#ffffff" '
                f'stroke="{badge_color}" stroke-width="2.5"/>'
                f'<text x="{label_x:.1f}" y="{label_y + 5.5:.1f}" font-size="16" font-weight="800" '
                f'fill="{badge_color}" text-anchor="middle" '
                f'font-family="{escape(theme.font_family)}">{escape(sign)}</text>'
            )
        if rest:
            text_y = label_y + (28.0 if sign else 6.0)
            elements.append(
                _anchored_label(
                    label_x, text_y, rest, width=140.0, color=theme.muted_color, theme=theme,
                    size=11.5, weight=600, max_lines=2, anchor="middle",
                )
            )

    bottom = cy + radius + max_node_r + 30.0
    return elements, canvas_w, (bottom - top)


# ---------------------------------------------------------------------------
# input_process_output: the classic three-zone "black box" systems diagram.
# ---------------------------------------------------------------------------

_IPO_ZONE_LABELS = {"input": "INPUT", "process": "PROCESS", "output": "OUTPUT"}
_IPO_ZONE_COLORS = {
    "input": ("#dbeafe", "#1d4ed8"),
    "process": ("#fef9c3", "#b45309"),
    "output": ("#dcfce7", "#15803d"),
}


def _layout_ipo(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """Three zones side by side - INPUT, PROCESS, OUTPUT - the classic
    systems/programming "black box" diagram: what goes in, what happens to
    it, what comes out. A node's `shape_role` ("input"/"process"/"output",
    "" defaulting to "process") decides its zone; nodes within a zone
    stack top to bottom in `nodes` order, connected to the next step
    within their own zone, and the LAST node of one zone connects to the
    FIRST node of the next (input -> process -> output) - the one flow
    this kind ever draws between zones. An empty zone (no node used that
    `shape_role`) simply draws its header with nothing under it and no
    connector into/out of it - never a hard failure."""
    zones: dict[str, list[DiagramNode]] = {"input": [], "process": [], "output": []}
    for node in nodes:
        role = node.shape_role.strip().lower()
        zones[role if role in zones else "process"].append(node)

    zone_w = 260.0
    gap_x = 56.0
    canvas_w = max(CANVAS_WIDTH, 3 * zone_w + 2 * gap_x + 2 * MARGIN)
    xs = {
        "input": MARGIN + zone_w / 2,
        "process": canvas_w / 2,
        "output": canvas_w - MARGIN - zone_w / 2,
    }
    header_h = 36.0
    header_gap = 20.0
    node_gap = 26.0
    elements: list[str] = [_arrow_marker("diagram-arrow", theme.accent_color)]
    zone_positions: dict[str, list[tuple[float, float, float]]] = {}
    max_bottom = top + header_h
    for role in ("input", "process", "output"):
        fill, stroke = _IPO_ZONE_COLORS[role]
        zx = xs[role]
        elements.append(
            f'<rect x="{zx - zone_w / 2:.1f}" y="{top:.1f}" width="{zone_w:.1f}" height="{header_h:.1f}" '
            f'rx="8" fill="{stroke}"/>'
            f'<text x="{zx:.1f}" y="{top + header_h / 2 + 5.0:.1f}" font-size="13" font-weight="800" '
            f'fill="#ffffff" text-anchor="middle" letter-spacing="1.5" '
            f'font-family="{escape(theme.font_family)}">{_IPO_ZONE_LABELS[role]}</text>'
        )
        y = top + header_h + header_gap
        positions: list[tuple[float, float, float]] = []
        for node in zones[role]:
            svg, height = _node_block(node, x=zx - zone_w / 2, y=y, width=zone_w, theme=theme, colors=(fill, stroke))
            elements.append(svg)
            positions.append((zx, y, y + height))
            y += height + node_gap
        zone_positions[role] = positions
        max_bottom = max(max_bottom, y - node_gap if positions else top + header_h + header_gap)

    for role in ("input", "process", "output"):
        positions = zone_positions[role]
        for i in range(len(positions) - 1):
            x, _top_i, bottom = positions[i]
            _x2, top2, _bottom2 = positions[i + 1]
            elements.append(
                f'<line x1="{x:.1f}" y1="{bottom:.1f}" x2="{x:.1f}" y2="{top2 - 4.0:.1f}" '
                f'stroke="{theme.accent_color}" stroke-width="2" marker-end="url(#diagram-arrow)"/>'
            )

    order = ["input", "process", "output"]
    for zone_a, zone_b in zip(order, order[1:]):
        pos_a, pos_b = zone_positions[zone_a], zone_positions[zone_b]
        if not pos_a or not pos_b:
            continue
        ax, ay = pos_a[-1][0] + zone_w / 2, (pos_a[-1][1] + pos_a[-1][2]) / 2
        bx, by = pos_b[0][0] - zone_w / 2, (pos_b[0][1] + pos_b[0][2]) / 2
        mid_x1, mid_x2 = ax + gap_x / 2, bx - gap_x / 2
        mid_y = (ay + by) / 2
        elements.append(
            f'<path d="M{ax:.1f},{ay:.1f} L{mid_x1:.1f},{ay:.1f} L{mid_x1:.1f},{mid_y:.1f} '
            f'L{mid_x2:.1f},{mid_y:.1f} L{mid_x2:.1f},{by:.1f} L{bx:.1f},{by:.1f}" '
            f'fill="none" stroke="{theme.accent_color}" stroke-width="2.5" stroke-linejoin="round" '
            f'marker-end="url(#diagram-arrow)"/>'
        )

    return elements, canvas_w, (max_bottom - top)


# ---------------------------------------------------------------------------
# ranked_bars: one horizontal bar per node, sorted by value_pct (highest
# first) - "top factors by importance", survey results, a ranked checklist.
# ---------------------------------------------------------------------------

_RANKED_BAR_LABEL_SIZE = RING_LABEL_SIZE


def _layout_ranked_bars(
    nodes: list[DiagramNode], *, theme: TemplateTheme, top: float
) -> tuple[list[str], float, float]:
    """A horizontal bar per node, sorted by `value_pct` (highest first,
    a node with none set sorts last, drawing a zero-length bar rather than
    guessing a value), bar length proportional to that value - "top
    factors by importance", survey results, a ranked checklist. Distinct
    from `progress_rings` (independent circular gauges with no implied
    ranking between them) and `gantt_chart` (a schedule, not a ranking)."""
    count = len(nodes)
    if count == 0:
        return [], CANVAS_WIDTH, 0.0
    ordered = sorted(nodes, key=lambda n: n.value_pct if n.value_pct is not None else -1.0, reverse=True)
    canvas_w = CANVAS_WIDTH
    label_w = 220.0
    bar_x0 = MARGIN + label_w + 20.0
    bar_max_w = canvas_w - MARGIN - bar_x0 - 50.0
    bar_h = 28.0
    row_gap = 20.0
    max_value = max((n.value_pct or 0.0 for n in ordered), default=0.0) or 100.0

    y = top
    elements: list[str] = []
    palette = _cycling_palette(theme)
    for index, node in enumerate(ordered):
        value = node.value_pct if node.value_pct is not None else 0.0
        bar_w = max((value / max_value) * bar_max_w, 4.0)
        fill, stroke = palette[index % len(palette)]
        # Unbounded (999, not the 2 first tried) - a real, confirmed
        # truncation: a long label ("Network infrastructure failures
        # across data centers") needed 3 lines and hit `_wrap`'s own
        # ellipsis fallback at a 2-line cap even though `row_h` below
        # already grows to fit however many lines come back, the same
        # "the cap, not the growth logic, was the actual bug" pattern
        # this module's other box-based kinds already learned from.
        label_lines = _wrap(node.label, font_size=_RANKED_BAR_LABEL_SIZE, width=label_w, max_lines=999, bold=True)
        label_h = len(label_lines) * _RANKED_BAR_LABEL_SIZE * LINE_HEIGHT
        row_h = max(bar_h, label_h)
        label_svg, _lh = _text(
            MARGIN + label_w,
            y + row_h / 2 + _RANKED_BAR_LABEL_SIZE * 0.35 - (len(label_lines) - 1) * _RANKED_BAR_LABEL_SIZE * LINE_HEIGHT / 2,
            label_lines, font_size=_RANKED_BAR_LABEL_SIZE, weight=700, color=theme.text_color,
            font_family=theme.font_family, anchor="end",
        )
        bar_y = y + (row_h - bar_h) / 2
        value_text = (
            f'<text x="{bar_x0 + bar_w + 10.0:.1f}" y="{y + row_h / 2 + 5.0:.1f}" font-size="13" '
            f'font-weight="700" fill="{theme.muted_color}" '
            f'font-family="{escape(theme.font_family)}">{int(round(value))}</text>'
            if node.value_pct is not None else ""
        )
        tooltip = node.label.strip()
        if node.detail.strip():
            tooltip = f"{tooltip} — {node.detail.strip()}" if tooltip else node.detail.strip()
        elements.append(
            f'<g class="diagram-node" tabindex="0" role="group">'
            f'{f"<title>{escape(tooltip)}</title>" if tooltip else ""}'
            f"{label_svg}"
            f'<rect class="diagram-box" x="{bar_x0:.1f}" y="{bar_y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="6" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
            f"{value_text}</g>"
        )
        y += row_h + row_gap

    return elements, canvas_w, (y - row_gap - top)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def render_diagram_svg(
    spec: DiagramSpec, theme: TemplateTheme, *, gap_scale: float = 1.0
) -> tuple[bytes, int, int]:
    """Render a validated `DiagramSpec` to a standalone SVG document.

    Returns (utf-8 svg bytes, intrinsic width, intrinsic height) so the caller
    can store them exactly like a generated image's dimensions.

    `gap_scale` (1.0 = normal) only affects a flow_chart/process's own
    vertical spine spacing - see _layout_flow_chart's own comment on its
    `gap` variable, and render_diagram_svg_fitting, the caller that actually
    searches for a value that fits a given page slot. Every other kind
    ignores it; this is deliberately a narrow, additive capability, not a
    blanket "shrink everything" knob.
    """
    # DiagramSpec.label_scale (the editor's "increase text size" control -
    # see its own docstring): clamped to [1.0, 1.5] here, not trusted on
    # faith - an unbounded or shrinking value could push a diagram back
    # under the readability floor, or grow it unboundedly. Reset via the
    # token (not a bare re-assignment) so an exception part-way through
    # this render can never leave a stale scale bleeding into the NEXT
    # render on this same context.
    _scale_token = _label_scale_ctx.set(max(1.0, min(1.5, spec.label_scale or 1.0)))
    try:
        return _render_diagram_svg(spec, theme, gap_scale=gap_scale)
    finally:
        _label_scale_ctx.reset(_scale_token)


def _render_diagram_svg(spec: DiagramSpec, theme: TemplateTheme, *, gap_scale: float) -> tuple[bytes, int, int]:
    kind = spec.normalised_kind()
    top = MARGIN
    title_svg = ""
    if spec.title.strip():
        # max_lines was a hard 1 - a title longer than one line at this
        # width (very common: this module's own titles are routinely
        # prefixed "Fig. N: " and often end in a parenthetical, e.g. "Fig.
        # 3: Oscilloscope setup — quick checklist & visible controls")
        # got silently cut off with a trailing "…", the same truncation
        # bug class already fixed for `_node_block` and `_circle_node`.
        # `top += title_h + 20` just below already sizes the rest of the
        # layout from the title's REAL height, so a second line costs
        # nothing but a little vertical space - never a layout bug.
        title_font_size = _scaled_font_size(21)
        title_lines = _wrap(spec.title, font_size=title_font_size, width=CANVAS_WIDTH - 2 * MARGIN, max_lines=2, bold=True)
        title_svg, title_h = _text(
            CANVAS_WIDTH / 2,
            top + title_font_size,
            title_lines,
            font_size=title_font_size,
            weight=700,
            color=theme.text_color,
            font_family=theme.font_family,
        )
        top += title_h + 20

    canvas_w = CANVAS_WIDTH
    if kind == "cycle":
        # The caption drawn ABOVE the canvas (title_svg, just above) already
        # shows the diagram's full title, including the "Fig. N: " prefix
        # app.services.diagram_service._with_figure_caption adds - passing
        # that SAME, already-prefixed string to the ring's own CENTER
        # circle too (see _layout_ring_process) would literally repeat
        # "Fig. 1: ..." a second time, redundant and visually confirmed
        # cramped on a real generated diagram. The center circle gets the
        # bare concept name instead.
        elements, canvas_w, content_h = _layout_ring_process(
            spec.nodes, spec.edges, theme=theme, top=top,
            title=_FIGURE_PREFIX_RE.sub("", spec.title), gap_scale=gap_scale,
        )
    elif kind in ("flow_chart", "process"):
        # A plain sequential procedure - not a repeating cycle - reads as
        # the conventional oval/rectangle/diamond notation instead (see
        # _layout_standard_flowchart's own docstring for why this is a
        # separate kind from the ring above, split out from a single
        # combined dispatch branch).
        elements, canvas_w, content_h = _layout_standard_flowchart(
            spec.nodes, spec.edges, theme=theme, top=top, gap_scale=gap_scale
        )
    elif kind == "data_flow_diagram":
        elements, content_h = _layout_dfd(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "swimlane":
        elements, content_h = _layout_swimlane(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "hierarchy":
        elements, content_h, canvas_w = _layout_hierarchy(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "comparison":
        elements, content_h = _layout_comparison(spec.nodes, theme=theme, top=top)
    elif kind == "circuit_schematic":
        elements, content_h = _layout_circuit_schematic(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "periodic_table":
        elements, content_h = _layout_periodic_table(spec.nodes, theme=theme, top=top)
    elif kind in ("concept_map", "conceptual"):
        elements, canvas_w, content_h = _layout_concept_wheel(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "er_diagram":
        elements, canvas_w, content_h = _layout_er(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "schematic":
        elements, content_h = _layout_schematic(spec, theme=theme, top=top)
    elif kind == "graph":
        elements, content_h = _layout_graph(spec, theme=theme, top=top)
    elif kind == "pyramid":
        elements, content_h = _layout_pyramid(spec.nodes, theme=theme, top=top, inverted=spec.pyramid_inverted)
    elif kind == "funnel":
        # The exact same wide-top/narrow-bottom trapezoid-stack geometry
        # _layout_pyramid's own `inverted=True` path already draws (see its
        # docstring) - "funnel" is its own named kind (rather than asking
        # the model to remember "pyramid + pyramid_inverted=true") purely
        # so a narrowing-stages brief (a sales/conversion/learning funnel)
        # has an obvious, discoverable kind name of its own; always forces
        # inverted regardless of `spec.pyramid_inverted`, since a funnel
        # that happened to be left False would otherwise silently draw as
        # an upright pyramid instead.
        elements, content_h = _layout_pyramid(spec.nodes, theme=theme, top=top, inverted=True)
    elif kind == "honeycomb":
        elements, content_h = _layout_honeycomb(spec.nodes, theme=theme, top=top)
    elif kind == "puzzle_pieces":
        elements, canvas_w, content_h = _layout_puzzle_pieces(spec.nodes, theme=theme, top=top)
    elif kind == "gears_process":
        elements, canvas_w, content_h = _layout_gears_process(spec.nodes, theme=theme, top=top)
    elif kind == "matrix":
        elements, content_h = _layout_matrix(spec, theme=theme, top=top)
    elif kind == "radial_mindmap":
        elements, canvas_w, content_h = _layout_radial_mindmap(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "venn":
        elements, content_h = _layout_venn(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "chevron_process":
        elements, canvas_w, content_h = _layout_chevron_process(spec.nodes, theme=theme, top=top)
    elif kind == "timeline":
        elements, canvas_w, content_h = _layout_timeline(spec.nodes, theme=theme, top=top)
    elif kind == "staircase_process":
        elements, canvas_w, content_h = _layout_staircase_process(spec.nodes, theme=theme, top=top)
    elif kind == "balance_comparison":
        elements, content_h = _layout_balance_comparison(spec, theme=theme, top=top)
    elif kind == "nested_circles":
        elements, content_h = _layout_nested_circles(spec.nodes, theme=theme, top=top)
    elif kind == "network_mesh":
        elements, canvas_w, content_h = _layout_network_mesh(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "decision_tree":
        elements, canvas_w, content_h = _layout_decision_tree(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "fishbone":
        elements, canvas_w, content_h = _layout_fishbone(spec.nodes, theme=theme, top=top)
    elif kind == "layered_diagram":
        elements, content_h = _layout_layered_bands(spec.nodes, theme=theme, top=top)
    elif kind == "gantt_chart":
        elements, content_h = _layout_gantt_chart(spec.nodes, theme=theme, top=top)
    elif kind == "donut_segments":
        elements, content_h = _layout_donut_segments(spec.nodes, theme=theme, top=top)
    elif kind == "pie_wedge":
        elements, content_h = _layout_pie_wedge(spec.nodes, theme=theme, top=top)
    elif kind == "orbit_diagram":
        elements, content_h = _layout_orbit_diagram(spec.nodes, theme=theme, top=top)
    elif kind == "progress_rings":
        elements, content_h = _layout_progress_rings(spec.nodes, theme=theme, top=top)
    elif kind == "flower_petal":
        elements, content_h = _layout_flower_petal(spec.nodes, theme=theme, top=top)
    elif kind == "hourglass_diagram":
        elements, content_h = _layout_hourglass_diagram(spec.nodes, theme=theme, top=top)
    elif kind == "bridge_process":
        elements, canvas_w, content_h = _layout_bridge_process(spec.nodes, theme=theme, top=top)
    elif kind == "chain_links":
        elements, canvas_w, content_h = _layout_chain_links(spec.nodes, theme=theme, top=top)
    elif kind == "wavy_journey_path":
        elements, canvas_w, content_h = _layout_wavy_journey_path(spec.nodes, theme=theme, top=top)
    elif kind == "org_chart":
        elements, content_h, canvas_w = _layout_org_chart(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "spectrum":
        elements, canvas_w, content_h = _layout_spectrum(spec, theme=theme, top=top)
    elif kind == "iceberg":
        elements, canvas_w, content_h = _layout_iceberg(spec.nodes, theme=theme, top=top)
    elif kind == "feedback_loop_process":
        elements, canvas_w, content_h = _layout_feedback_loop_process(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "causal_loop":
        elements, canvas_w, content_h = _layout_causal_loop(spec.nodes, spec.edges, theme=theme, top=top)
    elif kind == "input_process_output":
        elements, canvas_w, content_h = _layout_ipo(spec.nodes, theme=theme, top=top)
    elif kind == "ranked_bars":
        elements, canvas_w, content_h = _layout_ranked_bars(spec.nodes, theme=theme, top=top)
    else:  # smart_art: icon-badge list (falls back to a numbered badge for
        # any node whose own `icon` is blank/unrecognised), no connecting
        # arrows - a real "SmartArt list" look instead of a plain numbered
        # stack, see _layout_vertical's own docstring.
        elements, content_h = _layout_vertical(
            spec.nodes, spec.edges, theme=theme, connected=False, top=top, icon_badge=True,
        )

    canvas_h = max(top + content_h + MARGIN, top + MARGIN)

    # Hover/focus feedback only takes effect where the caller inlines this SVG
    # into the page DOM (not when it's referenced via `<img src>`) - the
    # frontend does that for diagrams specifically, see ImageBlock.tsx.
    # A subtle, consistent drop shadow on every shape (every kind's own
    # nodes already share this one "diagram-box" class, so one rule here
    # lifts the whole library's visual quality at once rather than
    # threading a shadow through ~20 separate _draw_*/_layout_* functions
    # individually) - small blur, low opacity, the same restrained amount
    # of depth real presentation-software SmartArt themes use; never so
    # heavy it reads as a cartoon bevel or risks clipping at the canvas
    # edge (MARGIN=40 comfortably clears a 1-2px offset, 3px blur shadow).
    # Decorative, non-"diagram-box" elements (lane background bands, the
    # page background rect) are deliberately unaffected - a shadow on a
    # flat full-height background band would look wrong, not polished.
    style = (
        "<style>.diagram-node{cursor:pointer}"
        ".diagram-node .diagram-box{transition:stroke .15s ease,fill .15s ease;"
        "filter:drop-shadow(0 1px 2px rgba(15,23,42,.14))}"
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


# How far render_diagram_svg_fitting is willing to compact a flow_chart's
# vertical spine gap, and in how many steps - stops well short of the
# spine gap vanishing entirely (0.4 still leaves a real, visible 20px gap
# at the 50px base), so consecutive steps never look like they're touching.
_FITTING_GAP_SCALES = (1.0, 0.85, 0.7, 0.55, 0.4)


def render_diagram_svg_fitting(
    spec: DiagramSpec, theme: TemplateTheme, *, max_height: float | None
) -> tuple[bytes, int, int]:
    """Like render_diagram_svg, but when the normal render is taller than
    `max_height`, retries with a tighter gap_scale (see _layout_flow_chart's
    own comment on it) until it fits or the compaction floor is reached -
    never touching box sizes or font sizes, so node/edge text is exactly as
    legible as the normal, uncompacted render.

    This is the actual fix for a real, measured bug: a flow_chart diagram
    only a little too tall for the page slot it needed to share with its
    own lead-in text used to always force that ENTIRE slot's page to move
    whole to a fresh page - discarding the ORIGINAL page's own leftover
    space (confirmed via a real generated course, up to 750px/78% of a
    page) - even though the diagram's content didn't actually need that
    much room, just more of it packed a little closer together.

    `max_height=None`, or a kind other than flow_chart/process/cycle (the
    only kinds gap_scale affects - all three now share _layout_ring_process,
    see its own dispatch in render_diagram_svg), is just render_diagram_svg's
    normal, full-size output - this function never makes anything WORSE
    than that baseline. Returns the best-fitting attempt even when nothing
    fits `max_height` - the caller
    (app.course.document.diagram_compaction) decides whether that's still
    worth using over the uncompacted version.
    """
    svg, width, height = render_diagram_svg(spec, theme)
    if (
        max_height is None
        or height <= max_height
        or spec.normalised_kind() not in ("flow_chart", "process", "cycle")
    ):
        return svg, width, height

    best = (svg, width, height)
    for scale in _FITTING_GAP_SCALES[1:]:
        svg, width, height = render_diagram_svg(spec, theme, gap_scale=scale)
        best = (svg, width, height)
        if height <= max_height:
            break
    return best
