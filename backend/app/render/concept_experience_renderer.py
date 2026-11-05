"""`concept_experience` DiagramSpec -> deterministic, self-contained HTML.

Same philosophy as `app.render.diagram_renderer` (pure, deterministic,
AI-free - the model only supplies structure, every pixel/behaviour here is
produced by code). Deliberately static - no JS, no buttons: this fragment is
consumed the same way a diagram SVG already is (inlined into the page DOM by
the frontend, and by Playwright for PDF export), and both of those contexts
are read-only, so every card/step/connector is pre-populated and every
choice (colours, icons, layout) is baked into one motionless picture. The
default (only) state must already tell the complete story.

Domain-agnostic dispatch: `render_concept_experience_html` picks a renderer
function from `_REPRESENTATION_RENDERERS` keyed by `spec.representation`
(see REPRESENTATION_TYPES in app.schemas.diagram). Three representation
families have a real, dedicated renderer - "object" (`_render_object`),
"data_structure" (`_render_data_structure`) and "process"
(`_render_process`) - chosen by what STRUCTURE the concept needs, never by
subject. Every other representation value renders through a documented
compatibility fallback onto one of those three; a genuinely unrecognised
value falls back to `_render_object`, the universal safe default. Never a
hard failure - the same "never a crash just because a new representation
appears" contract every other fallback in this pipeline uses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from html import escape as _esc

from app.schemas.diagram import DiagramSpec, VisualEntity, VisualStep
from app.schemas.document import CONTENT_HEIGHT
from app.schemas.template import TemplateTheme

# ---------------------------------------------------------------------------
# A deliberately vivid, storybook-bright palette - distinct from
# app.render.textbook_palette's muted, "color is secondary to clarity" set,
# which stays exactly as-is for the schematic/diagram pipeline (physics/bio/
# chem diagrams genuinely want restraint; this project's whole point is the
# opposite - a beginner, even a child, should be able to tell entities apart
# and feel drawn in at a glance). Same role names as TEXTBOOK_PALETTE so a
# planner-supplied `color_role` still resolves consistently, but every value
# here is a real, saturated hue - never a pale tint - because a card's own
# fill IS its main color statement, not just a hint of one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CevColorRole:
    fill: str
    stroke: str
    text: str


_CEV_PALETTE: dict[str, _CevColorRole] = {
    "primary":        _CevColorRole(fill="#60a5fa", stroke="#1d4ed8", text="#0c1e4a"),  # bright blue
    "secondary":      _CevColorRole(fill="#c084fc", stroke="#7e22ce", text="#3b0764"),  # bright purple
    "accent":         _CevColorRole(fill="#fb923c", stroke="#c2410c", text="#431407"),  # bright orange
    "structure":      _CevColorRole(fill="#2dd4bf", stroke="#0f766e", text="#042f2c"),  # bright teal
    "current":        _CevColorRole(fill="#f87171", stroke="#b91c1c", text="#450a0a"),  # bright red
    "magnetic_field": _CevColorRole(fill="#4ade80", stroke="#15803d", text="#052e16"),  # bright green
    "positive":       _CevColorRole(fill="#4ade80", stroke="#15803d", text="#052e16"),  # bright green
    "negative":       _CevColorRole(fill="#f87171", stroke="#b91c1c", text="#450a0a"),  # bright red
    "fluid":          _CevColorRole(fill="#22d3ee", stroke="#0e7490", text="#083344"),  # bright cyan
    "highlight":      _CevColorRole(fill="#facc15", stroke="#a16207", text="#422006"),  # bright yellow
    "annotation":     _CevColorRole(fill="#f9a8d4", stroke="#be185d", text="#500724"),  # bright pink
    "neutral":        _CevColorRole(fill="#cbd5e1", stroke="#475569", text="#1e293b"),  # soft slate
}


def _resolve_color_role(color_role: str) -> _CevColorRole:
    """Unknown/blank roles fall back to "neutral" - never a hard failure."""
    return _CEV_PALETTE.get((color_role or "").strip().lower(), _CEV_PALETTE["neutral"])


# --- shared chrome (title/core message/technical signature) ----------------


def _header_html(spec: DiagramSpec, theme: TemplateTheme) -> str:
    parts = [f'<div class="cev-title">{_esc(spec.title.strip() or "Concept")}</div>']
    if spec.visual_metaphor.strip():
        # The one field on DiagramSpec written specifically to translate the
        # concept into a plain-language, everyday comparison for a
        # non-technical reader - surfaced here as its own friendly callout
        # rather than left unrendered (it previously was).
        parts.append(
            '<div class="cev-metaphor"><span class="cev-metaphor-icon" aria-hidden="true">&#128161;</span>'
            f'<span class="cev-metaphor-text">{_esc(spec.visual_metaphor.strip())}</span></div>'
        )
    if spec.core_message.strip():
        parts.append(f'<div class="cev-core-message">{_esc(spec.core_message.strip())}</div>')
    if spec.technical_signature.strip():
        # A single line, guaranteed by concept_qa's structural layer - still
        # defensively collapsed here so a stray newline can never blow up
        # the layout even if that check is ever bypassed.
        line = " ".join(spec.technical_signature.split())
        parts.append(f'<div class="cev-technical">{_esc(line)}</div>')
    return "".join(parts)


def _entity_icon_char(entity: VisualEntity) -> str:
    """A card/column/panel is never bare/iconless - if the planner didn't
    supply an icon, a coloured initial-letter avatar stands in so it still
    reads as a deliberate, finished piece of UI. Only for an alphabetic
    label though ("Car (Class)" -> "C") - truncating a numeric/symbolic
    label (a BST node "10", a stack value "42") to its first character
    would sit right next to the real value and silently misstate it ("1"
    next to "10"), which is worse than no icon at all."""
    first_char = (entity.label.strip() or entity.id)[:1]
    return entity.icon.strip() or (first_char.upper() if first_char.isalpha() else "◆")


# A property key that describes the card's own PRESENTATION rather than any
# fact about the entity - e.g. a model emitting {"style": "border + drop
# shadow"} (a real, confirmed case). Styling is this renderer's own job
# (`_resolve_color_role`, the CSS below); a "style" property is never
# genuine content, so it's dropped entirely rather than echoed as visible
# text.
_META_PROPERTY_KEYS = {"style", "styling", "css", "format", "formatting"}

# A generic ordinal placeholder name ("bullet1", "point2", "line1") instead
# of a real, descriptive attribute name - another real, confirmed case. The
# VALUE is still genuine content (e.g. "ordered", "mutable"); only the key
# is meaningless noise, so it renders as a plain fact instead of a bold,
# content-free "BULLET1:" label.
_PLACEHOLDER_PROPERTY_KEY_RE = re.compile(r"^(bullet|point|item|line|prop(?:erty)?|note|fact)\s*\d*$", re.IGNORECASE)

# A sentence/formula-shaped property value is caught and retried at the
# source - app.services.concept_qa's structural QA (same length caps) - not
# truncated here: this renderer has an existing, deliberate guarantee that a
# long UNBROKEN token (a function signature, an identifier with no spaces)
# still renders in full and wraps via CSS (`overflow-wrap:anywhere` on
# `.cev-root *`) rather than being cut short, which a blanket length-based
# truncation here would silently break.
def _render_prop(key: str, value: str) -> str:
    if _PLACEHOLDER_PROPERTY_KEY_RE.match(key.strip()):
        return f'<span class="cev-prop">{_esc(value)}</span>' if value.strip() else ""
    return f'<span class="cev-prop"><b>{_esc(key)}</b>{": " + _esc(value) if value.strip() else ""}</span>'


def _entity_card_html(entity: VisualEntity, *, extra_class: str = "", index: int = 0) -> str:
    role = _resolve_color_role(entity.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    classes = f"cev-card {extra_class}".strip()
    icon_char = _entity_icon_char(entity)
    icon_html = f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(icon_char)}</span></div>'
    label_html = f'<div class="cev-label">{_esc(entity.label.strip() or entity.id)}</div>'
    prop_items = "".join(
        _render_prop(key, value)
        for key, value in entity.properties.items()
        if key.strip() and key.strip().lower() not in _META_PROPERTY_KEYS
    )
    props_html = f'<div class="cev-props">{prop_items}</div>' if prop_items else ""
    action_items = "".join(f'<span class="cev-action">{_esc(action)}</span>' for action in entity.actions)
    actions_html = f'<div class="cev-actions">{action_items}</div>' if action_items else ""
    # Cards fade/rise into place staggered by their position on load - a
    # still snapshot looks inert; a few cards arriving in a quick,
    # deliberate sequence reads as alive without needing the reader to
    # click anything (there is nothing to click - this is a static image).
    delay = f"{min(index, 8) * 0.07:.2f}s"
    return (
        f'<div class="{classes}" style="{css_vars}animation-delay:{delay};" '
        f'data-entity-id="{_esc(entity.id)}" data-role="{_esc(entity.role)}">'
        f"{icon_html}{label_html}{props_html}{actions_html}"
        f"</div>"
    )


# `SchematicRelationship.type` is a shared vocabulary (see RELATIONSHIP_TYPES
# in app.schemas.diagram) also used by the `schematic` kind, where these four
# are purely spatial anchoring hints and deliberately never drawn as a label
# (see app.render.diagram_renderer's own _RELATIONSHIP_STRUCTURAL_TYPES
# comment). concept_experience has no separate layout channel for them, so
# without this same exclusion they render as a literal, meaningless connector
# caption ("left of", "above") between two cards whose actual relationship -
# if any - was never named. Mirrors the schematic renderer's own rule so
# both renderers treat this vocabulary consistently.
_SPATIAL_ONLY_RELATIONSHIP_TYPES = {"above", "below", "left_of", "right_of", "between"}

# A structural relationship type that states the obvious once two entities are
# already drawn connected by a real line - "connected to", repeated on every
# spoke of a hub, tells a reader nothing they couldn't already see (a real,
# confirmed case: six distinct facts about ONE concept, each drawn as its own
# satellite entity "connected_to" the concept, every single connector
# captioned the identical word "connected to" - pure visual noise, not a
# relationship worth naming). Reused by `_tree_links_html` below for the same
# reason. A model that actually has something specific to say (a SQL join's
# own type, "manages", "feeds into") still gets to say it - only the bare,
# generic placeholder words are hidden.
_GENERIC_STRUCTURAL_EDGE_TYPES = {"inside", "contains", "connected_to", "attached_to", "surrounds", "passes_through"}
_REDUNDANT_CONNECTOR_LABEL_TYPES = _SPATIAL_ONLY_RELATIONSHIP_TYPES | _GENERIC_STRUCTURAL_EDGE_TYPES


def _connector_label(rel_type: str) -> str:
    """The visible text for a relationship connector/spoke - blank for the
    spatial-anchoring and generic-structural vocabulary that states the
    obvious once two entities are already drawn connected (see
    _REDUNDANT_CONNECTOR_LABEL_TYPES). Shared by `_connector_html` (the
    flat-row connector) and `_render_concept_map_html` (the circular
    hub/ring layout) so both ever apply exactly the same suppression
    rule."""
    normalized = rel_type.strip().lower()
    return "" if normalized in _REDUNDANT_CONNECTOR_LABEL_TYPES else rel_type.replace("_", " ").strip()


def _connector_html(rel_type: str) -> str:
    """A real visual connector - an animated flowing line with an arrowhead
    and the relationship type label - placed between two adjacent entity
    cards in the flex row. Not a precise SVG line between exact pixel
    positions (this is flexbox-laid-out HTML, not a coordinate system), but
    a genuine connecting element a reader sees between the two specific
    cards it sits between, which is what a `relationship`/`hierarchy`
    concept (a SQL join, a microservice calling another) needs to read as
    connected rather than two unrelated cards."""
    label = _connector_label(rel_type)
    label_html = f'<span class="cev-connector-label">{_esc(label)}</span>' if label else ""
    return f'<div class="cev-connector"><span class="cev-connector-line"></span>{label_html}<span class="cev-connector-arrow">&rarr;</span></div>'


def _entities_row_html(entities: list[VisualEntity], relationships, *, extra_class: str) -> str:
    """Renders `entities` in a flex row - reordered around `relationships`
    (source immediately followed by a connector then target) when any are
    declared, so related entities land adjacent with a real connector
    between them; a plain row (today's exact behaviour) when no
    relationships are set. Each card gets a staggered entrance delay by its
    position in the rendered row.

    A connector and its target are always grouped into one atomic flex unit
    (`.cev-rel-link`, see _style_html) - a bare connector can otherwise land
    alone at the start of a wrapped row (nothing stopping a row-wrap from
    falling between a connector and the card it's pointing at), which reads
    as broken. The trade-off: a node that's the target of more than one
    relationship (a shared hub several things point to) renders its card
    once per incoming edge rather than once overall - a little repetition,
    never a dangling connector, which is the one outcome worth avoiding at
    all costs here."""
    if not relationships:
        return "".join(_entity_card_html(e, extra_class=extra_class, index=i) for i, e in enumerate(entities))

    by_id = {e.id: e for e in entities}
    rendered: set[str] = set()
    parts: list[str] = []
    order = 0
    for rel in relationships:
        source, target = by_id.get(rel.source), by_id.get(rel.target)
        if source is None or target is None:
            continue
        if source.id not in rendered:
            parts.append(_entity_card_html(source, extra_class=extra_class, index=order))
            rendered.add(source.id)
            order += 1
        target_html = _entity_card_html(target, extra_class=extra_class, index=order)
        parts.append(f'<div class="cev-rel-link">{_connector_html(rel.type)}{target_html}</div>')
        rendered.add(target.id)
        order += 1
    for entity in entities:
        if entity.id not in rendered:
            parts.append(_entity_card_html(entity, extra_class=extra_class, index=order))
            order += 1
    return "".join(parts)


def _is_linear_chain(entities: list[VisualEntity], relationships) -> list[str] | None:
    """Returns every entity's id, ordered start-to-end, when `relationships`
    describes ONE straight path through EVERY entity - A -> B -> C -> ... -
    rather than a hub (one entity with several independent targets, e.g. a
    template's "Delegation" connected to five separate required elements) or
    any other shape (a branch, a disconnected extra edge, a cycle). A hub
    reads fine as the existing card-row-with-connectors layout (each spoke
    IS independently connected to the one hub, which the current rendering
    already shows); a genuine chain reads badly that way - real, confirmed
    case: five entities wrapped into a multi-row grid with each row's own
    "connected to" connector, which no longer visually reads as ONE
    continuous series the way a plain left-to-right flowchart does. `None`
    for anything that isn't unambiguously a single path, so the caller can
    safely fall back to the existing, already-correct hub rendering rather
    than mis-drawing a shape this can't confidently tell is linear."""
    if not relationships:
        return None
    by_id = {e.id: e for e in entities}
    out_edges: dict[str, str] = {}
    in_degree: dict[str, int] = {}
    for rel in relationships:
        if rel.source not in by_id or rel.target not in by_id:
            return None
        if rel.source in out_edges:
            return None  # more than one outgoing edge - a hub/branch, not a chain
        out_edges[rel.source] = rel.target
        in_degree[rel.target] = in_degree.get(rel.target, 0) + 1
        if in_degree[rel.target] > 1:
            return None  # more than one incoming edge - a merge, not a chain
    involved = set(out_edges) | set(in_degree)
    starts = [eid for eid in involved if in_degree.get(eid, 0) == 0]
    if len(starts) != 1:
        return None
    order = [starts[0]]
    seen = {starts[0]}
    current = starts[0]
    while current in out_edges:
        current = out_edges[current]
        if current in seen:
            return None  # a cycle, not a chain
        order.append(current)
        seen.add(current)
    if seen != involved or len(order) != len(entities):
        return None  # a disconnected component, or an entity outside the chain
    return order


def _chain_row_html(entities_in_order: list[VisualEntity]) -> str:
    """A genuine chain, rendered as one flowing series - a plain arrow
    between each consecutive card, never the per-relationship "connected
    to" connector `_entities_row_html` draws (that phrasing and the
    animated dashed line both make sense for an independent spoke off a
    hub; repeated down an actual straight sequence, it reads as five
    separate little relationships rather than one continuous flow).

    Wrap-aware: a short chain fits one row and just needs an arrow between
    cards, but a longer one (5+ entities - a real, confirmed case: a
    7-layer OSI-style hierarchy rendered this way) wraps onto several rows,
    and a plain flex-wrap row has no way to show that row 2 continues from
    row 1 - it reads as a disconnected grid of cards, not one flow. Reuses
    the exact two-column-grid-plus-curved-connector technique
    `_zigzag_steps_html` already proved out for `_render_process`'s own
    step sequence (`_ZIG_LINK_HTML` between a row's pair, `_zig_curve_svg`
    carrying the line from one row's end into the next row's start) -
    the caller wraps this in `.cev-zig-grid`, not `.cev-steps` (see
    `_render_object`/`_render_tree`/`_render_concept_flow_html`)."""
    parts: list[str] = []
    n = len(entities_in_order)
    last_stroke = "#94a3b8"
    index = 0
    while index < n:
        pair = entities_in_order[index : index + 2]
        if len(pair) == 2:
            parts.append(_entity_card_html(pair[0], extra_class="cev-instance cev-step", index=index))
            parts.append(_ZIG_LINK_HTML)
            parts.append(_entity_card_html(pair[1], extra_class="cev-instance cev-step", index=index + 1))
            last_stroke = _resolve_color_role(pair[1].color_role).stroke
        else:
            parts.append(
                _entity_card_html(pair[0], extra_class="cev-instance cev-step cev-zig-card-solo", index=index)
            )
            last_stroke = _resolve_color_role(pair[0].color_role).stroke
        index += 2
        if index < n:
            parts.append(_zig_curve_svg(last_stroke))
    return "".join(parts)


# ---------------------------------------------------------------------------
# concept map: entities connected by `relationships` that are NOT a clean
# linear chain (_is_linear_chain already owns that shape - see
# `_render_object`) - laid out on a circle instead of the old flat
# flex-wrap card row with a connector threaded between every card
# (`_entities_row_html`'s own relationship branch, which `_render_data_structure`
# still uses unchanged for its own tree/graph-shaped case - see that
# function's docstring for why). A real, confirmed case this replaces: one
# concept explained by six of its own defining facts, rendered as a hub
# with every spoke captioned the same generic word, wrapped into a
# multi-row grid that no longer read as a hub at all - just a grid with
# lines in it. A genuine star (one source, 2+ distinct targets, never
# itself a target - see `_concept_map_hub`) now gets a real centre-hub with
# satellites orbiting it; anything else connected (a cycle, a small web, a
# merge) gets every entity evenly spaced on the ring with a straight chord
# per relationship - still a real circular shape, never a guess at a "hub"
# the data doesn't actually have. Used by `_render_object` (for
# `relationship`/`spatial`, and `object` on the rare occasion it's given
# relationships) and by `_render_tree`'s own "not a clean tree" fallback
# (for `hierarchy`/`decision_tree`).
# ---------------------------------------------------------------------------


def _concept_map_hub(entities: list[VisualEntity], relationships) -> VisualEntity | None:
    """A clean star: ONE entity that is the source of 2+ relationships, is
    never itself a target, and no other entity has any outgoing edge of its
    own - the exact shape the old flat row rendered as "one hub + N
    independent spokes", now given a real centre instead. `None` for
    anything else (a cycle, a merge, a multi-hub web, a lone pair) - the
    caller then arranges every entity on the ring instead, rather than
    guessing which one deserves the centre."""
    if not relationships:
        return None
    by_id = {e.id: e for e in entities}
    out_ids: set[str] = set()
    in_ids: set[str] = set()
    out_count: dict[str, int] = {}
    for rel in relationships:
        if rel.source not in by_id or rel.target not in by_id:
            return None
        out_ids.add(rel.source)
        in_ids.add(rel.target)
        out_count[rel.source] = out_count.get(rel.source, 0) + 1
    if len(out_ids) != 1:
        return None  # more than one entity has an outgoing edge - not a pure star
    hub_id = next(iter(out_ids))
    if hub_id in in_ids or out_count[hub_id] < 2:
        return None  # the "hub" is also a target somewhere, or has <2 spokes - not a star
    return by_id[hub_id]


def _concept_map_satellite_angles(count: int) -> list[float]:
    """The real discrete angles (radians) `count` satellites land at,
    starting at the top (12 o'clock) and going clockwise - the one source
    of truth both `_concept_map_ring_positions` (where a satellite is
    actually drawn) and every geometry calculation below (how much room
    that satellite actually needs) read from, so the two can never
    disagree about what angle a given satellite is at."""
    if count <= 0:
        return []
    if count == 1:
        return [math.radians(-90.0)]
    return [math.radians(-90 + i * (360.0 / count)) for i in range(count)]


def _concept_map_ring_positions(count: int, radius: float) -> list[tuple[float, float]]:
    """`count` points evenly spaced on a circle of `radius` - plain Python
    trigonometry, baked into fixed pixel offsets, not CSS trig functions
    the renderer would have to trust the browser to evaluate consistently."""
    if count == 1:
        return [(0.0, -radius)]
    return [(radius * math.cos(a), radius * math.sin(a)) for a in _concept_map_satellite_angles(count)]


def _concept_map_hub_circle_html(hub: VisualEntity, *, diameter: float) -> str:
    """The hub's own markup for the circular concept-map (hub mode only) -
    a solid-filled CIRCLE (icon + title, an optional short subtitle from
    the hub's own properties) rather than the rounded-rectangle card every
    other entity/renderer in this module uses, matching a real circular
    concept-map's own visual convention (the centre idea reads as the
    "hub" of a wheel, not just another card)."""
    role = _resolve_color_role(hub.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_html = f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(_entity_icon_char(hub))}</span></div>'
    label_html = f'<div class="cev-label">{_esc(hub.label.strip() or hub.id)}</div>'
    sub_items = "".join(
        _render_prop(k, v)
        for k, v in hub.properties.items()
        if k.strip() and k.strip().lower() not in _META_PROPERTY_KEYS
    )
    sub_html = f'<div class="cev-cmap-hub-sub">{sub_items}</div>' if sub_items else ""
    return (
        f'<div class="cev-cmap-hub-circle" style="{css_vars}width:{diameter:.0f}px;height:{diameter:.0f}px;">'
        f"{icon_html}{label_html}{sub_html}</div>"
    )


def _concept_map_node_html(
    entity: VisualEntity, *, dx: float, dy: float, is_hub: bool = False, hub_diameter: float = 0.0
) -> str:
    """The outer wrapper's own `class` is always exactly `"cev-cmap-node"`
    (never a hub-specific variant) - existing callers/tests key off
    `data-cmap-node-id` plus the `data-cmap-is-hub` marker attribute below
    to tell the hub apart, not the class list, so this stays a stable,
    single exact string regardless of which inner markup (a card, or the
    hub's own circle - see _concept_map_hub_circle_html) a given node
    wraps."""
    inner = (
        _concept_map_hub_circle_html(entity, diameter=hub_diameter)
        if is_hub
        else _entity_card_html(entity, extra_class="cev-instance")
    )
    hub_attr = ' data-cmap-is-hub="true"' if is_hub else ""
    return (
        f'<div class="cev-cmap-node" style="transform:translate(-50%,-50%) translate({dx:.1f}px,{dy:.1f}px);" '
        f'data-cmap-node-id="{_esc(entity.id)}"{hub_attr}>{inner}</div>'
    )


def _concept_map_curved_spoke_path(
    *, x1: float, y1: float, x2: float, y2: float
) -> tuple[str, float, float]:
    """A gentle quadratic-bezier curve from (x1,y1) to (x2,y2), bowed
    perpendicular to the straight line between them (a fixed, consistent
    "always bows the same way" curvature, not a randomised wobble) -
    matching a real hand-drawn concept-map's curved connectors instead of
    a plain straight chord. Returns (path `d`, label_x,
    label_y) - the label point is the curve's OWN midpoint (the quadratic
    bezier at t=0.5), not the straight line's midpoint, so a label sits
    visually ON the curve rather than floating off to one side of it."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length  # unit vector perpendicular to the spoke
    bow = min(length * 0.16, 36.0)
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    ctrl_x, ctrl_y = mx + px * bow, my + py * bow
    path_d = f"M{x1:.1f},{y1:.1f} Q{ctrl_x:.1f},{ctrl_y:.1f} {x2:.1f},{y2:.1f}"
    label_x = 0.25 * x1 + 0.5 * ctrl_x + 0.25 * x2
    label_y = 0.25 * y1 + 0.5 * ctrl_y + 0.25 * y2
    return path_d, label_x, label_y


_CONCEPT_MAP_ARROW_MARKER_ID = "cevCmapArrow"


def _concept_map_curved_spokes_svg(
    spokes: list[tuple[float, float, float, float, str]], *, container_w: float, container_h: float
) -> str:
    """One shared SVG overlay for every hub-mode spoke (hub centre to each
    satellite) - a single `<defs><marker>` arrowhead reused by every path,
    rather than one per spoke. `spokes` is a list of (x1, y1, x2, y2,
    label) in the map's own centre-origin coordinate space; stroke/fill
    colours come from the `.cev-cmap-curve-*` CSS classes in `_style_html`
    (the same "colour lives in the <style> block, markup stays
    presentation-agnostic" pattern `.cev-cmap-spoke-line` already uses),
    except the arrowhead's own `fill`, which SVG `<marker>` content can't
    reliably inherit from an external stylesheet - its value is baked in
    directly from the one color this module's style block already commits
    to for this element at the CSS level (`_CEV_PALETTE["neutral"].stroke`
    at `#475569`, exactly what `.cev-cmap-curve-path`'s own stroke resolves
    to), so the two can never visually mismatch."""
    paths: list[str] = []
    labels: list[str] = []
    for x1, y1, x2, y2, label in spokes:
        path_d, lx, ly = _concept_map_curved_spoke_path(x1=x1, y1=y1, x2=x2, y2=y2)
        paths.append(
            f'<path class="cev-cmap-curve-path" d="{path_d}" fill="none" '
            f'marker-end="url(#{_CONCEPT_MAP_ARROW_MARKER_ID})"/>'
        )
        if label:
            labels.append(f'<text class="cev-cmap-curve-label" x="{lx:.1f}" y="{ly:.1f}">{_esc(label)}</text>')
    marker = (
        f'<marker id="{_CONCEPT_MAP_ARROW_MARKER_ID}" markerWidth="9" markerHeight="9" '
        f'refX="6.5" refY="3.5" orient="auto-start-reverse">'
        f'<path d="M0,0 L7,3.5 L0,7 Z" fill="#475569"/></marker>'
    )
    view_box = f"{-container_w / 2:.1f} {-container_h / 2:.1f} {container_w:.1f} {container_h:.1f}"
    return (
        f'<svg class="cev-cmap-curves" viewBox="{view_box}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f"<defs>{marker}</defs>{''.join(paths)}{''.join(labels)}</svg>"
    )


def _concept_map_card_height(entity: VisualEntity) -> float:
    """The same icon-badge+label+properties card-height estimate
    `_render_before_after` already uses (`_entity_chars`/`_slot_width`,
    reused here, not reinvented) - independent of width tier, since a
    card's height tracks how much TEXT it holds, not which max-width cap
    it's rendered at."""
    return float(_slot_width(_entity_chars(entity), compact=170, wide=260))


def _concept_map_required_radius(
    hub_w: float, hub_h: float, sat_w: float, sat_h: float, angles: list[float]
) -> float:
    """Approach C (exact axis-aligned-rectangle separation), not a bounding
    circle: two axis-aligned rectangles don't overlap iff they're separated
    on x OR y, so the minimum radius at which a satellite at angle `theta`
    first clears the hub is `min(A/|cos(theta)|, B/|sin(theta)|)` where
    `A`/`B` are the summed half-widths/half-heights plus clearance - never
    the full diagonal, which earlier measurement proved is needlessly
    conservative at axis-aligned angles (confirmed: a satellite sitting
    directly beside the hub only ever needs horizontal clearance, not its
    full corner-to-corner reach). The same separating-axis logic, applied
    to each pair of ring-adjacent satellites, gives the minimum radius
    satellites need to clear EACH OTHER. Division by a near-zero cos/sin
    (a satellite sitting exactly on that axis) is skipped rather than
    risking a float blow-up - that axis simply contributes nothing to the
    "required" side for that satellite/pair, which is correct: a
    satellite with zero horizontal projection needs zero horizontal
    clearance from the hub."""
    count = len(angles)
    if count == 0:
        return _CONCEPT_MAP_BASE_RADIUS

    hub_sat_a = hub_w / 2.0 + sat_w / 2.0 + _CONCEPT_MAP_CLEARANCE
    hub_sat_b = hub_h / 2.0 + sat_h / 2.0 + _CONCEPT_MAP_CLEARANCE
    hub_sat_needed = 0.0
    for theta in angles:
        terms = []
        c, s = math.cos(theta), math.sin(theta)
        if abs(c) > _CONCEPT_MAP_ANGLE_EPSILON:
            terms.append(hub_sat_a / abs(c))
        if abs(s) > _CONCEPT_MAP_ANGLE_EPSILON:
            terms.append(hub_sat_b / abs(s))
        if terms:
            hub_sat_needed = max(hub_sat_needed, min(terms))

    sat_sat_needed = 0.0
    if count >= 2:
        sat_sat_c = sat_w + _CONCEPT_MAP_CLEARANCE
        sat_sat_d = sat_h + _CONCEPT_MAP_CLEARANCE
        for i in range(count):
            theta_i, theta_j = angles[i], angles[(i + 1) % count]
            kx = math.cos(theta_i) - math.cos(theta_j)
            ky = math.sin(theta_i) - math.sin(theta_j)
            terms = []
            if abs(kx) > _CONCEPT_MAP_ANGLE_EPSILON:
                terms.append(sat_sat_c / abs(kx))
            if abs(ky) > _CONCEPT_MAP_ANGLE_EPSILON:
                terms.append(sat_sat_d / abs(ky))
            if terms:
                sat_sat_needed = max(sat_sat_needed, min(terms))

    return max(_CONCEPT_MAP_BASE_RADIUS, hub_sat_needed, sat_sat_needed)


def _concept_map_max_radius_for_width(sat_w: float, angles: list[float]) -> float:
    """The largest radius whose rendered shape - every satellite's own
    rectangle at its real angle - still fits inside the page's available
    width (`_INNER_WIDTH`), derived directly from the actual horizontal
    extent of the outermost satellite rectangles, never a flat placeholder
    margin (the earlier `(_INNER_WIDTH-220)/2` constant was never actually
    tied to a card's real width - this replaces it). A satellite sitting
    exactly on the vertical axis (zero horizontal projection - e.g. a
    2-satellite map's top/bottom pair) never constrains width at all, so
    width is correctly left unbounded (`math.inf`) when that's the only
    kind of satellite present; the hub's own half-width is never the
    binding term in practice since it's always smaller than
    `_INNER_WIDTH`, so it isn't included as a separate branch."""
    cos_max = max((abs(math.cos(theta)) for theta in angles), default=0.0)
    if cos_max <= _CONCEPT_MAP_ANGLE_EPSILON:
        return math.inf
    return (_INNER_WIDTH / 2.0 - sat_w / 2.0) / cos_max


def _concept_map_container_size(
    hub_w: float, hub_h: float, sat_w: float, sat_h: float, radius: float, angles: list[float]
) -> tuple[float, float]:
    """The real bounding box of everything hub mode actually draws - the
    hub at the centre plus every satellite's own rectangle at its real
    angle and the chosen radius - computed from the same inputs as the
    radius itself, never a flat `+220`-style margin applied the same way
    regardless of the actual shape."""
    max_x = hub_w / 2.0
    max_y = hub_h / 2.0
    for theta in angles:
        max_x = max(max_x, radius * abs(math.cos(theta)) + sat_w / 2.0)
        max_y = max(max_y, radius * abs(math.sin(theta)) + sat_h / 2.0)
    return max_x * 2.0, max_y * 2.0


def _concept_map_hub_layout(
    hub: VisualEntity, satellites: list[VisualEntity]
) -> tuple[float, bool, float, float]:
    """Hub-mode geometry, computed once here and shared by both the
    renderer (which needs the real container box to size `.cev-cmap`) and
    `_raw_pixel_size` (which needs to reserve the same height the renderer
    will actually produce), so the two can never drift apart. Tries the
    normal card-width tier first; only drops to the narrower "compact"
    tier when the normal tier's REQUIRED radius (Approach C, above)
    genuinely exceeds the page-width-AVAILABLE radius for that tier - never
    unconditionally, and never by shrinking clearance or inflating the
    page-width cap to force a fit. If even the compact tier's required
    radius exceeds what's available, the radius is clamped to what's
    actually available at that tier (the same "never silently fail, never
    an open-ended shrink loop" final safety net as before) - a real,
    flagged residual limitation for an extreme case, not a hidden one.
    Returns (radius, used_compact_tier, container_width, container_height).
    """
    angles = _concept_map_satellite_angles(len(satellites))
    tiers = (
        (_CONCEPT_MAP_HUB_WIDTH, _CONCEPT_MAP_SAT_WIDTH, False),
        (_CONCEPT_MAP_HUB_WIDTH_COMPACT, _CONCEPT_MAP_SAT_WIDTH_COMPACT, True),
    )
    radius = _CONCEPT_MAP_BASE_RADIUS
    hub_w, sat_w = _CONCEPT_MAP_HUB_WIDTH, _CONCEPT_MAP_SAT_WIDTH
    hub_h = _concept_map_card_height(hub)
    sat_h = max((_concept_map_card_height(e) for e in satellites), default=170.0)
    compact = False
    for hub_width, sat_width, compact in tiers:
        hub_w, sat_w = hub_width, sat_width
        needed = _concept_map_required_radius(hub_w, hub_h, sat_w, sat_h, angles)
        available = _concept_map_max_radius_for_width(sat_w, angles)
        radius = needed if available == math.inf else min(needed, available)
        if needed <= available:
            break  # this tier satisfies the real geometry AND fits the page - done
    container_w, container_h = _concept_map_container_size(hub_w, hub_h, sat_w, sat_h, radius, angles)
    return radius, compact, container_w, container_h


def _render_concept_map_html(entities: list[VisualEntity], relationships) -> str:
    """See the module section comment above. Falls back to the plain flat
    row for the genuinely degenerate case of 0-1 entities - nothing to
    arrange on a circle."""
    if len(entities) < 2:
        cards = "".join(_entity_card_html(e, extra_class="cev-instance", index=i) for i, e in enumerate(entities))
        return f'<div class="cev-instances">{cards}</div>'

    hub = _concept_map_hub(entities, relationships)

    if hub is not None:
        nodes_html: list[str] = []
        lines_html: list[str] = []
        container_class = "cev-cmap"
        satellites = [e for e in entities if e.id != hub.id]
        radius, compact, container_w, container_h = _concept_map_hub_layout(hub, satellites)
        if compact:
            container_class += " cev-cmap-compact"
        hub_diameter = (_CONCEPT_MAP_HUB_WIDTH_COMPACT if compact else _CONCEPT_MAP_HUB_WIDTH) * 0.85
        angles = _concept_map_satellite_angles(len(satellites))
        ring = _concept_map_ring_positions(len(satellites), radius)
        positions = {satellite.id: pos for satellite, pos in zip(satellites, ring)}
        angle_by_id = {satellite.id: a for satellite, a in zip(satellites, angles)}
        label_by_target: dict[str, str] = {}
        for rel in relationships:
            label_by_target.setdefault(rel.target, _connector_label(rel.type))
        nodes_html.append(_concept_map_node_html(hub, dx=0.0, dy=0.0, is_hub=True, hub_diameter=hub_diameter))
        spokes: list[tuple[float, float, float, float, str]] = []
        for entity in satellites:
            dx, dy = positions[entity.id]
            nodes_html.append(_concept_map_node_html(entity, dx=dx, dy=dy))
            a = angle_by_id[entity.id]
            start_x, start_y = (hub_diameter / 2.0) * math.cos(a), (hub_diameter / 2.0) * math.sin(a)
            end_x, end_y = dx - _CONCEPT_MAP_SPOKE_PULLBACK * math.cos(a), dy - _CONCEPT_MAP_SPOKE_PULLBACK * math.sin(a)
            spokes.append((start_x, start_y, end_x, end_y, label_by_target.get(entity.id, "")))
        lines_html.append(
            _concept_map_curved_spokes_svg(spokes, container_w=container_w, container_h=container_h)
        )
        return (
            f'<div class="cev-cmap-wrap"><div class="{container_class}" '
            f'style="width:{container_w:.0f}px;height:{container_h:.0f}px;">'
            f'{"".join(lines_html)}{"".join(nodes_html)}</div></div>'
        )

    # No single dominant hub - a real top-to-bottom flowchart (see
    # _render_concept_flow_html) instead of a circular "ring" of chords
    # (the ring layout this replaced was confirmed, in practice, to read
    # as a tangle of crossing lines rather than a clear flow - a real,
    # confirmed complaint on exactly this shape, e.g. a microservice call
    # graph).
    return _render_concept_flow_html(entities, relationships)


def _build_flow_levels(
    entities: list[VisualEntity], relationships
) -> tuple[list[list[VisualEntity]], dict[str, int]]:
    """BFS levels from the root(s) (an entity never targeted by a
    relationship) - the same top-down shape `_build_tree_levels` builds,
    but never gives up: a genuine web (branches AND merges), a cycle, or a
    disconnected entity all still get a definite level, never `None`. A
    node is placed at the level of its FIRST arrival (BFS, so the
    shortest/most direct path wins); if literally every entity has an
    incoming edge (a pure cycle with no obvious start), the first entity
    in list order becomes the lone root - a real start is always picked,
    never a hard failure. Any entity relationships don't connect to a root
    at all is appended as its own trailing level rather than silently
    dropped."""
    if not entities:
        return [], {}
    by_id = {e.id: e for e in entities}
    out_edges: dict[str, list] = {}
    has_incoming: set[str] = set()
    for rel in relationships:
        if rel.source not in by_id or rel.target not in by_id:
            continue
        out_edges.setdefault(rel.source, []).append(rel)
        has_incoming.add(rel.target)

    roots = [e.id for e in entities if e.id not in has_incoming] or [entities[0].id]
    level_of: dict[str, int] = {}
    order: list[str] = []
    current: list[str] = []
    for rid in roots:
        if rid not in level_of:
            level_of[rid] = 0
            order.append(rid)
            current.append(rid)
    while current:
        next_ids: list[str] = []
        for nid in current:
            for rel in out_edges.get(nid, []):
                if rel.target not in level_of:
                    level_of[rel.target] = level_of[nid] + 1
                    order.append(rel.target)
                    next_ids.append(rel.target)
        current = next_ids
    trailing = max(level_of.values(), default=-1) + 1
    for entity in entities:
        if entity.id not in level_of:
            level_of[entity.id] = trailing
            order.append(entity.id)

    levels: list[list[VisualEntity]] = [[] for _ in range(max(level_of.values()) + 1)]
    for eid in order:
        levels[level_of[eid]].append(by_id[eid])
    return levels, level_of


def _concept_flow_height_estimate(entities: list[VisualEntity], relationships) -> float:
    """The flow-chart's own reserved-height estimate, used by
    `_raw_pixel_size` so the page layout always matches what
    `_render_concept_flow_html` actually renders - one row per level plus
    one short arrowed-stem row between each pair (the same accounting
    `_render_tree`'s own successful-tree branch already uses for an
    identical level-row shape), plus a little extra for any level that
    shows a back-edge badge underneath it."""
    levels, level_of = _build_flow_levels(entities, relationships)
    if not levels:
        return 0.0
    by_id = {e.id: e for e in entities}
    valid_rels = [r for r in relationships if r.source in by_id and r.target in by_id]
    forward_edges = [r for r in valid_rels if level_of.get(r.target) == level_of.get(r.source, -2) + 1]
    back_edges = [r for r in valid_rels if r not in forward_edges]
    back_edge_sources = {r.source for r in back_edges}
    busiest = max((_entity_chars(e) for e in entities), default=0)

    if len(levels) > 1 and all(len(lvl) == 1 for lvl in levels):
        # A pure chain - renders via _chain_row_html's own two-column
        # zig-grid technique (see _render_concept_flow_html's matching
        # branch and _chain_row_html's own docstring for why), so this
        # needs the exact same accounting _render_process already uses for
        # that identical grid shape - two entities per row, one curved
        # connector row between each pair of rows - never the level-count
        # math below (that's for genuinely vertical level stacking).
        per_row_height = _slot_width(busiest, compact=88, wide=160)
        rows = math.ceil(len(levels) / 2)
        height = rows * per_row_height
        height += max(rows - 1, 0) * 54.0  # curved connector between rows
        if back_edge_sources:
            height += 36.0
        return height

    level_h = _slot_width(busiest, compact=120, wide=190)
    height = len(levels) * level_h
    height += max(len(levels) - 1, 0) * 52.0  # stem+arrowhead row between levels
    for level_entities in levels:
        if any(e.id in back_edge_sources for e in level_entities):
            height += 36.0  # the back-edge badge row under this level
    return height


def _flow_links_html(
    child_entities: list[VisualEntity], labels: dict[str, str]
) -> str:
    """The arrowed connector row between one flow level and the next - the
    same proven flex-sizing trick `_tree_links_html` uses (`.cev-flow-level`/
    `.cev-flow-links` share identical per-card flex-basis, so a stem always
    lines up under its own child with no pixel math needed), but with a
    real arrowhead (this is a flowchart - the direction of flow is the
    whole point, which a tree's plain stem never needed to show)."""
    cells: list[str] = []
    for child in child_entities:
        raw_label = labels.get(child.id, "").strip()
        show_label = bool(raw_label) and raw_label.lower() not in _GENERIC_STRUCTURAL_EDGE_TYPES
        label_html = (
            f'<span class="cev-tree-link-label">{_esc(raw_label.replace("_", " "))}</span>' if show_label else ""
        )
        cells.append(f'<div class="cev-flow-link-cell">{label_html}</div>')
    return f'<div class="cev-flow-links">{"".join(cells)}</div>'


def _flow_backedges_html(
    source_entities: list[VisualEntity], back_edges: list, by_id: dict[str, VisualEntity]
) -> str:
    """A small annotation badge per back/cross edge whose SOURCE is in this
    level - a cycle (B loops back to an earlier step) or a skip/lateral
    link shown as a labelled note rather than a long line crossing back up
    through the whole chart, which would turn a clean top-to-bottom read
    into a tangle - exactly the shape this flowchart replaced the old
    circular "ring" layout to get away from. Mirrors `_render_cycle`'s own
    "then back to ..." badge, generalised to name both ends since a flow
    can have several distinct back-edges, not just one implicit loop."""
    source_ids = {e.id for e in source_entities}
    relevant = [rel for rel in back_edges if rel.source in source_ids]
    if not relevant:
        return ""
    items: list[str] = []
    for rel in relevant:
        source = by_id.get(rel.source)
        target = by_id.get(rel.target)
        if source is None or target is None:
            continue
        label = _connector_label(rel.type)
        label_html = f' <span class="cev-flow-backedge-note">({_esc(label)})</span>' if label else ""
        items.append(
            '<div class="cev-flow-backedge">'
            '<span class="cev-flow-backedge-icon" aria-hidden="true">&#8635;</span>'
            f'<span>back to &ldquo;{_esc(target.label.strip() or target.id)}&rdquo;</span>{label_html}'
            "</div>"
        )
    return "".join(items)


def _render_concept_flow_html(entities: list[VisualEntity], relationships) -> str:
    """Entities connected by `relationships` with no single dominant hub
    (see `_concept_map_hub`) - a real top-to-bottom FLOWCHART: BFS levels
    from the root(s) (`_build_flow_levels`, tolerant of branches, merges,
    cycles and disconnected nodes - never fails), each level its own row,
    connected to the next by arrowed stems (`_flow_links_html` - the exact
    `_tree_links_html` technique, plus a real arrowhead since direction is
    the point of a flowchart). A back/cross edge (a cycle, or a link that
    doesn't advance exactly one level) is never drawn as a long line
    crossing back up through the chart - it's a small labelled badge right
    after the row it starts from (`_flow_backedges_html`), keeping the
    top-to-bottom read clean. Replaces the old circular "ring" layout for
    this exact shape (a web/cycle with no single hub) - confirmed, in
    practice, to read as crossing chords rather than a clear flow."""
    levels, level_of = _build_flow_levels(entities, relationships)
    by_id = {e.id: e for e in entities}
    valid_rels = [r for r in relationships if r.source in by_id and r.target in by_id]
    forward_edges = [r for r in valid_rels if level_of.get(r.target) == level_of.get(r.source, -2) + 1]
    back_edges = [r for r in valid_rels if r not in forward_edges]

    if len(levels) > 1 and all(len(lvl) == 1 for lvl in levels):
        # A pure linear chain - every level has exactly one entity, no
        # branching or merging at all (see _render_tree's own matching
        # branch for why this gets the flowing-horizontal-row treatment
        # instead of a tall vertical stack of single-card "levels"). Any
        # back-edge still shows as its own badge underneath, same as the
        # level-based layout below.
        chain_html = _chain_row_html([lvl[0] for lvl in levels])
        backedges_html = _flow_backedges_html(entities, back_edges, by_id)
        backedges_block = f'<div class="cev-flow-backedges">{backedges_html}</div>' if backedges_html else ""
        return f'<div class="cev-zig-grid">{chain_html}</div>{backedges_block}'

    parts: list[str] = []
    index = 0
    for level_index, level_entities in enumerate(levels):
        cards = "".join(_tree_card_html(e, index + i) for i, e in enumerate(level_entities))
        parts.append(f'<div class="cev-flow-level">{cards}</div>')
        index += len(level_entities)
        backedges_html = _flow_backedges_html(level_entities, back_edges, by_id)
        if backedges_html:
            parts.append(f'<div class="cev-flow-backedges">{backedges_html}</div>')
        if level_index + 1 < len(levels):
            next_level = levels[level_index + 1]
            label_by_child: dict[str, str] = {}
            for rel in forward_edges:
                if rel.target in {e.id for e in next_level}:
                    label_by_child.setdefault(rel.target, rel.type)
            parts.append(_flow_links_html(next_level, label_by_child))
    flow_html = "".join(parts)

    return f'<div class="cev-flow-chart">{flow_html}</div>'


def _style_html(theme: TemplateTheme) -> str:
    return f"""\
<style>
@keyframes cevFadeUp {{
  from {{ opacity:0; transform:translateY(10px) scale(.97); }}
  to   {{ opacity:1; transform:translateY(0) scale(1); }}
}}
@keyframes cevFlow {{
  to {{ background-position:-24px 0; }}
}}
.cev-root {{
  font-family:{theme.font_family};color:{theme.text_color};
  background:linear-gradient(180deg, color-mix(in srgb, {theme.accent_color} 8%, #fffdf7), #fffdf7);
  border:1px solid {theme.border_color};border-radius:22px;padding:28px;box-sizing:border-box;
  box-shadow:0 2px 4px rgba(0,0,0,.05), 0 16px 32px -18px rgba(0,0,0,.22);
  position:relative;overflow:hidden;
}}
.cev-root::before {{
  content:"";position:absolute;inset:0 0 auto 0;height:7px;
  background:linear-gradient(90deg, #f472b6, #fb923c, #facc15, #4ade80, #22d3ee, #60a5fa, #c084fc);
}}
/* Any single unbroken "word" (a long function signature, a path, an
   identifier with no spaces - very common in this domain) must never be
   allowed to sit wider than its box and spill out past the card/cell edge -
   this applies everywhere text renders inside a concept_experience visual,
   not case by case, so a future card type can never reintroduce it. */
.cev-root *{{box-sizing:border-box;overflow-wrap:anywhere;word-break:break-word;}}
.cev-title{{font-size:23px;font-weight:800;text-align:center;margin-bottom:8px;letter-spacing:-.01em;}}
.cev-metaphor{{
  display:flex;align-items:flex-start;gap:9px;justify-content:center;text-align:left;
  background:color-mix(in srgb, {theme.accent_color} 12%, #fffbeb);
  border:2px dashed color-mix(in srgb, {theme.accent_color} 50%, {theme.border_color});
  border-radius:14px;padding:10px 16px;margin:0 auto 14px;max-width:520px;
  animation:cevFadeUp .45s ease both;
}}
.cev-metaphor-icon{{font-size:19px;line-height:1.4;flex:none;}}
.cev-metaphor-text{{font-size:13.5px;font-style:italic;color:{theme.text_color};line-height:1.4;}}
.cev-core-message{{font-size:14px;color:{theme.muted_color};text-align:center;margin-bottom:12px;line-height:1.5;}}
.cev-technical{{
  font-family:{theme.mono_font_family};font-size:12.5px;background:{theme.surface_color};
  border:1px solid {theme.border_color};border-radius:8px;padding:8px 12px;text-align:center;
  margin:0 auto 18px;max-width:fit-content;white-space:nowrap;overflow-x:auto;
}}
.cev-template-row{{display:flex;justify-content:center;margin-bottom:8px;}}
.cev-flow-cue{{text-align:center;color:{theme.muted_color};font-size:12.5px;margin:2px 0 12px;line-height:1.5;animation:cevFadeUp .4s ease .15s both;}}
.cev-instances{{display:flex;flex-wrap:wrap;gap:16px;justify-content:center;align-items:stretch;}}
.cev-card{{
  border-radius:20px;border:2px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 60%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 80%, #fff));
  color:var(--cev-text,{theme.text_color});
  padding:14px 18px;min-width:104px;text-align:center;
  box-shadow:0 2px 8px rgba(0,0,0,.07);
  animation:cevFadeUp .4s ease both;
}}
/* Cards in an entity row (object instances, or a relationship-shaped
   data_structure - both use .cev-instances) grow to fill whatever width
   their row actually has, instead of staying pinned to a narrow fixed size
   with the whole row then centered in a sea of blank margin - a handful of
   cards on a wide page should read as a wide, short picture, not a tall,
   narrow column. A single compact data_structure value (.cev-ds-track,
   never this rule) stays tight on purpose - see that block below. */
.cev-instances .cev-card{{flex:1 1 170px;max-width:240px;}}
.cev-card.cev-template{{min-width:220px;padding:20px 26px;}}
.cev-instances .cev-card.cev-template{{flex-basis:220px;max-width:300px;}}
.cev-icon-badge{{
  width:50px;height:50px;border-radius:50%;margin:0 auto 8px;display:flex;align-items:center;justify-content:center;
  background:var(--cev-stroke,{theme.accent_color});
  box-shadow:0 1px 3px rgba(0,0,0,.1);
}}
.cev-icon{{font-size:26px;line-height:1;font-weight:700;color:#fff;}}
.cev-label{{font-size:15px;font-weight:800;margin-top:2px;}}
.cev-props{{font-size:11.5px;margin-top:7px;display:flex;flex-direction:column;gap:3px;}}
.cev-prop{{display:block;}}
.cev-prop b{{font-weight:700;margin-right:3px;}}
.cev-actions{{font-size:11px;color:{theme.muted_color};margin-top:7px;}}
.cev-action{{
  margin:2px 3px;display:inline-block;background:color-mix(in srgb, var(--cev-stroke,{theme.muted_color}) 16%, #fff);
  border-radius:999px;padding:2px 9px;font-weight:600;
}}
/* relationship connector (object renderer, relationship/hierarchy fallback) */
.cev-connector{{display:flex;align-items:center;gap:4px;color:{theme.muted_color};font-size:11px;font-weight:600;animation:cevFadeUp .4s ease both;}}
.cev-connector-line{{
  display:inline-block;width:28px;height:4px;border-radius:2px;
  background-image:repeating-linear-gradient(90deg, var(--cev-stroke,{theme.accent_color}) 0 6px, transparent 6px 12px);
  background-size:24px 3px;animation:cevFlow 1s linear infinite;
}}
.cev-connector-arrow{{font-size:17px;line-height:1;color:{theme.accent_color};font-weight:700;}}
.cev-connector-label{{white-space:nowrap;font-weight:700;}}
/* A connector plus its target, as one unbreakable flex item (see
   _entities_row_html) - grows to fill its share of the row the same way a
   lone card does, with the connector staying its own natural size and the
   nested card (still matched by ".cev-instances .cev-card" above, since
   flex-grow only ever looks at a DIRECT parent) absorbing the rest. */
.cev-rel-link{{display:flex;align-items:center;gap:8px;flex:1 1 260px;max-width:340px;min-width:0;}}
.cev-rel-link .cev-card{{min-width:0;}}
/* data_structure renderer */
.cev-ds-track{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;align-items:flex-end;}}
.cev-ds-item-wrap{{display:flex;flex-direction:column;align-items:center;gap:4px;position:relative;}}
.cev-ds-index{{font-size:10.5px;font-weight:700;color:{theme.muted_color};}}
.cev-ds-end-badge{{
  position:absolute;top:-10px;right:-10px;font-size:9px;font-weight:800;letter-spacing:.03em;
  background:#f472b6;color:#fff;border-radius:999px;padding:3px 8px;
  box-shadow:0 2px 4px rgba(0,0,0,.18);
}}
.cev-ds-ends{{display:flex;justify-content:space-between;font-size:11.5px;font-weight:600;color:{theme.muted_color};
  margin-top:12px;max-width:420px;margin-left:auto;margin-right:auto;}}
/* process renderer */
.cev-step-counter{{font-size:12px;font-weight:700;color:{theme.muted_color};text-align:center;margin-bottom:14px;}}
.cev-steps{{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;align-items:stretch;}}
.cev-step{{min-width:118px;flex:1 1 150px;max-width:230px;}}
.cev-step-number{{
  width:24px;height:24px;border-radius:50%;background:var(--cev-stroke,{theme.accent_color});color:#fff;
  font-size:11.5px;font-weight:800;display:flex;align-items:center;justify-content:center;margin:0 auto 5px;
  box-shadow:0 1px 3px rgba(0,0,0,.12);
}}
.cev-step-desc{{font-size:11px;color:{theme.muted_color};margin-top:5px;}}
.cev-step-arrow{{color:{theme.accent_color};font-size:22px;font-weight:700;padding:0 2px;}}
/* comparison renderer */
.cev-cmp-wrap{{overflow-x:auto;}}
.cev-cmp-table{{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0;}}
.cev-cmp-corner{{background:transparent;width:26%;}}
.cev-cmp-col{{
  padding:12px 10px;border-radius:16px 16px 0 0;text-align:center;color:var(--cev-text,{theme.text_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 60%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 80%, #fff));
  border:2px solid var(--cev-stroke,{theme.border_color});border-bottom:none;
}}
.cev-cmp-col .cev-icon-badge{{width:38px;height:38px;margin-bottom:6px;}}
.cev-cmp-col .cev-icon{{font-size:18px;}}
.cev-cmp-col-label{{font-size:13px;font-weight:800;word-break:break-word;}}
.cev-cmp-row-label{{
  text-align:left;padding:9px 12px;font-size:11.5px;font-weight:700;color:{theme.muted_color};
}}
.cev-cmp-cell{{
  padding:9px 10px;text-align:center;font-size:12px;font-weight:600;color:{theme.text_color};
  border-top:1px solid {theme.border_color};
}}
.cev-cmp-table tbody tr:first-child .cev-cmp-cell{{border-top:2px solid {theme.border_color};}}
/* timeline renderer */
.cev-timeline-row{{display:flex;flex-wrap:wrap;gap:14px;justify-content:center;align-items:flex-start;}}
.cev-timeline-entry{{display:flex;flex-direction:column;align-items:center;flex:1 1 150px;max-width:220px;
  animation:cevFadeUp .4s ease both;}}
.cev-timeline-dot-wrap{{display:flex;flex-direction:column;align-items:center;}}
.cev-timeline-dot{{
  width:18px;height:18px;border-radius:50%;background:var(--cev-stroke,{theme.accent_color});
  border:3px solid #fff;box-shadow:0 0 0 2px var(--cev-stroke,{theme.accent_color}),0 2px 4px rgba(0,0,0,.15);
}}
.cev-timeline-stem{{width:3px;height:14px;background:var(--cev-stroke,{theme.accent_color});}}
.cev-timeline-card{{margin-top:2px;width:100%;animation:none;}}
/* before_after renderer */
.cev-before-after{{display:flex;flex-wrap:wrap;gap:16px;justify-content:center;align-items:center;}}
.cev-ba-panel{{flex:1 1 200px;max-width:250px;display:flex;}}
.cev-ba-card{{
  flex:1;border-radius:20px;border:2px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 60%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 80%, #fff));
  color:var(--cev-text,{theme.text_color});padding:18px;text-align:center;
  box-shadow:0 2px 8px rgba(0,0,0,.07);
  animation:cevFadeUp .4s ease both;
}}
.cev-ba-tag{{
  display:inline-block;font-size:10.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;
  background:var(--cev-stroke,{theme.accent_color});color:#fff;border-radius:999px;padding:3px 12px;margin-bottom:10px;
}}
.cev-ba-arrow{{flex:none;font-size:30px;color:{theme.accent_color};font-weight:700;}}
/* cycle renderer */
.cev-cycle-loop{{
  display:flex;align-items:center;justify-content:center;gap:8px;margin-top:16px;
  font-size:12.5px;font-weight:700;color:{theme.muted_color};
  background:color-mix(in srgb, {theme.accent_color} 10%, #fff);
  border:2px dashed color-mix(in srgb, {theme.accent_color} 45%, {theme.border_color});
  border-radius:999px;padding:8px 18px;max-width:fit-content;margin-left:auto;margin-right:auto;
}}
.cev-cycle-loop-icon{{font-size:18px;line-height:1;color:{theme.accent_color};}}
/* "flow" style (process/sequence/pipeline/state_machine/cycle/decision_tree):
   a flatter, plainer boxes-and-arrows treatment - closer to a clean
   textbook/whiteboard flowchart than the colourful sticker-card language
   the rest of this engine uses for entities. Purely additive overrides
   scoped under .cev-style-flow (added to the root div only for those
   representations - see _FLOW_STYLE_REPRESENTATIONS), so every other
   representation's card styling above is completely untouched. */
.cev-style-flow::before{{display:none;}}
.cev-style-flow .cev-card,
.cev-style-flow .cev-step{{
  border-radius:14px;border-width:2px;
  background:color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 16%, #fff);
  box-shadow:0 1px 3px rgba(0,0,0,.08);
}}
.cev-style-flow .cev-icon-badge{{
  width:auto;height:auto;border-radius:0;background:none;box-shadow:none;margin:0 0 4px;
}}
.cev-style-flow .cev-icon{{color:var(--cev-stroke,{theme.accent_color});font-size:20px;}}
.cev-style-flow .cev-step-number{{
  background:none;color:var(--cev-stroke,{theme.accent_color});box-shadow:none;
  border:2px solid var(--cev-stroke,{theme.accent_color});width:22px;height:22px;font-size:11px;
}}
.cev-style-flow .cev-step-arrow{{animation:none;}}
.cev-style-flow .cev-connector-line{{
  animation:none;background-image:none;background-color:var(--cev-stroke,{theme.accent_color});height:2px;width:22px;
}}
.cev-style-flow .cev-connector{{color:{theme.text_color};}}
.cev-style-flow .cev-connector-label{{
  text-transform:uppercase;font-size:10px;letter-spacing:.04em;
  background:color-mix(in srgb, var(--cev-stroke,{theme.accent_color}) 14%, #fff);
  border:1px solid var(--cev-stroke,{theme.accent_color});border-radius:999px;padding:2px 8px;
}}
.cev-style-flow .cev-cycle-loop{{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06);}}
/* "process"/"sequence"/"pipeline"/"state_machine" - a top-to-bottom arrow
   flowchart (see _render_process): one wide card per row instead of the
   flex-wrap row every other step-card representation uses, so a step's
   width no longer needs to shrink to share a row with siblings - it always
   gets the full column width up to its own cap. */
.cev-steps-vertical{{flex-direction:column;flex-wrap:nowrap;align-items:center;gap:0;}}
.cev-steps-vertical .cev-step{{width:100%;max-width:420px;flex:none;}}
.cev-steps-vertical .cev-step-arrow{{padding:2px 0;}}
/* "process"/"sequence"/"pipeline"/"state_machine" zigzag grid (see
   _render_process/_zigzag_steps_html): a numbered, two-column snake of
   wide icon+text cards - odd/even pairs share a row with a straight arrow
   between them, and a curved connector (one continuous SVG path per gap,
   stretched via preserveAspectRatio="none" so it always spans whatever
   width/height CSS grid actually gives that row - this is still "not a
   precise coordinate system", same as every other connector in this
   module, just a curved one) carries the flow from the right card of one
   row down into the left card of the next. */
.cev-zig-grid{{display:grid;grid-template-columns:1fr 36px 1fr;align-items:center;row-gap:4px;column-gap:6px;margin-top:6px;}}
.cev-zig-card{{
  position:relative;border-radius:20px;border:2px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 42%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 60%, #fff));
  color:var(--cev-text,{theme.text_color});padding:14px 16px 14px 30px;display:flex;align-items:center;gap:10px;
  box-shadow:0 2px 8px rgba(0,0,0,.07);
  animation:cevFadeUp .4s ease both;min-width:0;
}}
.cev-zig-card-solo{{grid-column:1/-1;width:74%;margin:0 auto;}}
.cev-zig-badge{{
  position:absolute;top:-13px;left:-13px;width:36px;height:36px;background:var(--cev-stroke,{theme.accent_color});
  color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;
  box-shadow:0 1px 4px rgba(0,0,0,.15);z-index:1;
}}
.cev-zig-badge-circle{{border-radius:50%;}}
.cev-zig-badge-diamond{{transform:rotate(45deg);border-radius:20%;}}
.cev-zig-badge-diamond span{{display:block;transform:rotate(-45deg);}}
.cev-zig-badge-hexagon{{clip-path:polygon(25% 3%, 75% 3%, 100% 50%, 75% 97%, 25% 97%, 0% 50%);}}
.cev-zig-blob{{
  flex:none;width:50px;height:50px;border-radius:42% 58% 65% 35%/45% 45% 55% 55%;
  background:color-mix(in srgb, var(--cev-stroke,{theme.accent_color}) 28%, #fff);
  display:flex;align-items:center;justify-content:center;
}}
.cev-zig-icon{{font-size:22px;line-height:1;}}
.cev-zig-text{{min-width:0;display:flex;flex-direction:column;gap:4px;text-align:left;}}
.cev-zig-text .cev-label{{font-size:14px;font-weight:800;margin:0;}}
.cev-zig-desc{{
  font-size:10.5px;color:var(--cev-text,{theme.text_color});background:rgba(255,255,255,.55);
  border-radius:9px;padding:3px 8px;line-height:1.35;display:inline-block;max-width:fit-content;
}}
.cev-zig-link{{display:flex;align-items:center;justify-content:center;}}
.cev-zig-link-arrow{{font-size:19px;font-weight:700;color:{theme.muted_color};}}
.cev-zig-curve-row{{grid-column:1/-1;height:46px;}}
.cev-zig-curve-row svg{{width:100%;height:100%;display:block;}}
/* "hierarchy"/"decision_tree" top-down tree (see _render_tree): a level's
   row and the stem row right below it share the SAME flex sizing on their
   children, so a stem always lines up under its own card purely from CSS
   flex distribution - no pixel coordinates computed anywhere. */
.cev-tree{{display:flex;flex-direction:column;align-items:stretch;}}
.cev-tree-level{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;}}
.cev-tree-level .cev-card{{flex:1 1 140px;max-width:220px;}}
.cev-tree-links{{display:flex;gap:14px;justify-content:center;height:34px;}}
.cev-tree-links .cev-tree-link-cell{{flex:1 1 140px;max-width:220px;position:relative;}}
.cev-tree-link-cell::after{{
  content:"";position:absolute;left:50%;top:0;bottom:0;width:4px;border-radius:2px;
  background:{theme.accent_color};transform:translateX(-50%);
}}
.cev-tree-links-fanned{{border-top:4px solid {theme.accent_color};}}
.cev-tree-link-label{{
  position:absolute;top:2px;left:50%;transform:translateX(-50%);z-index:1;
  font-size:10px;font-weight:700;color:{theme.muted_color};
  background:{theme.page_background};padding:0 5px;white-space:nowrap;
}}
/* concept flow-chart (no single dominant hub - see
   _render_concept_flow_html): the exact same level-row/stem technique
   `.cev-tree*` above uses (`.cev-flow-level`/`.cev-flow-links` share
   identical per-card flex-sizing with their own level row, so a stem
   always lines up under its own child), plus a real arrowhead - a
   flowchart's whole point is the direction of flow, which a tree's plain
   stem never needed to show. */
.cev-flow-chart{{display:flex;flex-direction:column;align-items:stretch;}}
.cev-flow-level{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;}}
.cev-flow-level .cev-card{{flex:1 1 140px;max-width:220px;}}
.cev-flow-links{{display:flex;gap:14px;justify-content:center;height:46px;}}
.cev-flow-links .cev-flow-link-cell{{flex:1 1 140px;max-width:220px;position:relative;}}
.cev-flow-link-cell::after{{
  content:"";position:absolute;left:50%;top:16px;bottom:11px;width:4px;border-radius:2px;
  background:{theme.accent_color};transform:translateX(-50%);
}}
.cev-flow-link-cell::before{{
  content:"";position:absolute;left:50%;bottom:0;width:0;height:0;
  transform:translateX(-50%);
  border-left:8px solid transparent;border-right:8px solid transparent;
  border-top:11px solid {theme.accent_color};
}}
.cev-flow-backedges{{
  display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin:4px 0 10px;
}}
.cev-flow-backedge{{
  display:flex;align-items:center;gap:6px;font-size:11.5px;font-weight:700;color:{theme.muted_color};
  background:color-mix(in srgb, {theme.accent_color} 10%, #fff);
  border:2px dashed color-mix(in srgb, {theme.accent_color} 45%, {theme.border_color});
  border-radius:999px;padding:5px 14px;
}}
.cev-flow-backedge-icon{{font-size:14px;line-height:1;color:{theme.accent_color};}}
.cev-flow-backedge-note{{font-weight:500;text-transform:none;}}
/* concept map (relationship/spatial hub-or-network, and hierarchy/
   decision_tree's own not-a-clean-tree fallback - see
   _render_concept_map_html): entities on a circle - every position is a
   fixed pixel transform computed once in Python (see that function),
   never CSS trigonometry. Hub mode connects them with curved SVG spokes
   (.cev-cmap-curves below, a real hand-drawn-concept-map look); ring mode
   (no single dominant hub) keeps its own plain straight chords
   (.cev-cmap-spoke, further below) - deliberately left alone, see
   _render_concept_map_html's own ring-mode comment for why. */
.cev-cmap-wrap{{display:flex;justify-content:center;}}
.cev-cmap{{position:relative;margin:6px 0;}}
.cev-cmap-node{{position:absolute;top:50%;left:50%;}}
.cev-cmap-node .cev-card{{min-width:0;max-width:176px;position:relative;padding-top:30px;}}
/* The icon badge floats above the card's own top edge (centred), rather
   than sitting inside the card's own padding like every other `.cev-card`
   user in this module - a real concept-map's own visual signature. The
   extra top padding above makes room for it without the label/props
   underneath ever sitting beneath the badge. */
.cev-cmap-node:not([data-cmap-is-hub]) .cev-icon-badge{{
  position:absolute;top:-22px;left:50%;transform:translateX(-50%);margin:0;
}}
.cev-cmap-node .cev-props{{text-align:left;}}
/* A "hanging bullet" via absolute position, not flex - `.cev-prop`'s real
   content is `<b>key</b>: value` as one continuous inline run (see
   _render_prop); making THIS element `display:flex` was tried first and
   reverted - mixing an element (`<b>`) with bare text nodes as flex
   children splits them into separate flex items that each shrink/wrap
   independently (a confirmed real bug: "summary" wrapped one letter per
   line), rather than reading as one wrapped paragraph. Plain block flow
   plus an absolutely-positioned bullet avoids that entirely. */
.cev-cmap-node .cev-prop{{display:block;position:relative;padding-left:13px;}}
.cev-cmap-node .cev-prop::before{{
  content:"\\2022";position:absolute;left:0;top:0;font-weight:800;color:var(--cev-stroke,{theme.accent_color});
}}
.cev-cmap-node[data-cmap-is-hub]{{z-index:1;}}
/* The hub's own circular markup (_concept_map_hub_circle_html) - solid
   fill, icon + title centred, an optional short subtitle beneath. Sized
   via its own inline width/height (see that function), not this tier CSS
   (the hub is never a `.cev-card`, so the tier rules below never touch it). */
.cev-cmap-hub-circle{{
  border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:4px;padding:14px;text-align:center;box-sizing:border-box;
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 65%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 85%, #fff));
  border:2px solid var(--cev-stroke,{theme.border_color});color:var(--cev-text,{theme.text_color});
  box-shadow:0 4px 14px rgba(0,0,0,.1);
  animation:cevFadeUp .4s ease both;
}}
.cev-cmap-hub-circle .cev-icon-badge{{margin:0 0 2px;}}
.cev-cmap-hub-circle .cev-label{{font-size:15.5px;line-height:1.2;}}
.cev-cmap-hub-sub{{font-size:10px;color:var(--cev-text,{theme.text_color});opacity:.85;line-height:1.3;}}
/* Narrower satellite-card tier (see _concept_map_hub_layout) - used when
   the page-width-safe orbit radius at the normal tier still wouldn't give
   the hub and its satellites enough clearance to avoid overlapping. The
   hub's own diameter shrinks the same way, but via the inline width/height
   `_concept_map_hub_circle_html` is called with (computed from the same
   `compact` flag in Python), not a CSS class - it was never a `.cev-card`. */
.cev-cmap-compact .cev-cmap-node .cev-card{{max-width:140px;}}
/* Hub-mode curved spokes (_concept_map_curved_spokes_svg) - one shared SVG
   overlay, positioned/sized to exactly cover `.cev-cmap` and using the
   same centre-origin coordinate space every node's own transform already
   uses (see that function's own viewBox). */
.cev-cmap-curves{{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;}}
.cev-cmap-curve-path{{stroke:#475569;stroke-width:2.5;stroke-linecap:round;}}
.cev-cmap-curve-label{{
  font-size:10.5px;font-weight:700;fill:{theme.muted_color};text-anchor:middle;dominant-baseline:middle;
  paint-order:stroke;stroke:{theme.page_background};stroke-width:4px;stroke-linejoin:round;
}}
@media (prefers-reduced-motion: reduce) {{
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line,.cev-timeline-entry,.cev-ba-card,.cev-zig-card,.cev-cmap-hub-circle {{
    animation:none !important;
  }}
}}
@media print {{
  /* PDF export (Playwright, emulate_media("print")) snapshots the page right
  after load with no extra wait for these wall-clock entrance animations to
  finish - without this, a printed page could freeze mid fade-in (a still-
  faded card, a half-drawn connector). Printing always shows the fully
  settled final state instead, with no added export latency. */
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line,.cev-timeline-entry,.cev-ba-card,.cev-zig-card,.cev-cmap-hub-circle {{
    animation:none !important;
  }}
}}
</style>"""


# ---------------------------------------------------------------------------
# 1. "object" - a template/blueprint entity + real instances, or (when
#    `relationships` is set) a connected row of entities. Handles: object,
#    code_visualization, comparison, relationship, hierarchy, spatial.
# ---------------------------------------------------------------------------


def _render_object(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    """A template/blueprint entity plus its real instances, with an explicit
    flow cue between them (labels, never prose) - or, when `relationships`
    is set (the `relationship`/`spatial` case, and `hierarchy`/`decision_tree`'s
    own fallback when their data isn't a clean tree - see `_render_tree`),
    entities connected by real visual connectors instead of an unconnected
    row: a flat, flowing left-to-right SERIES (see `_chain_row_html`) when
    those relationships form one unambiguous straight path through every
    instance (`_is_linear_chain`), the circular concept-map layout (see
    `_render_concept_map_html`) for anything else connected (a hub, a
    cycle, a small web) - never the old flat flex-wrap card row with a
    connector threaded between every card, which reads as a grid with
    lines in it rather than the hub/network shape it's actually showing. A
    static picture - every instance is pre-populated in the markup, so the
    one rendered state is already a complete picture with nothing left to
    reveal."""
    template_entities = [e for e in spec.entities if e.role == "template"]
    instance_entities = [e for e in spec.entities if e.role != "template"]

    chain_order = _is_linear_chain(instance_entities, spec.relationships) if not template_entities else None
    if chain_order:
        by_id = {e.id: e for e in instance_entities}
        instances_html = _chain_row_html([by_id[eid] for eid in chain_order])
        instances_class = "cev-zig-grid"
        root_class = "cev-root cev-style-flow"
    elif spec.relationships and not template_entities:
        # A hub or small network, not a clean chain - see this function's
        # own docstring and the module-level concept-map section comment.
        instances_html = _render_concept_map_html(instance_entities, spec.relationships)
        instances_class = ""
        # A genuine single-dominant-hub keeps the circular concept-map's
        # own vivid per-entity look (a deliberate design choice elsewhere
        # in this module); the flowchart fallback (no dominant hub - see
        # _render_concept_map_html/_render_concept_flow_html) is visually
        # the exact same shape as process/cycle's own flowchart, so it
        # earns that same toned-down "textbook" treatment instead of the
        # default saturated icon-card palette - a real, confirmed
        # complaint about exactly this shape ("too colourful").
        is_hub = _concept_map_hub(instance_entities, spec.relationships) is not None
        root_class = "cev-root" if is_hub else "cev-root cev-style-flow"
    else:
        instances_html = _entities_row_html(instance_entities, spec.relationships, extra_class="cev-instance")
        instances_class = "cev-instances"
        root_class = "cev-root"

    template_html = "".join(_entity_card_html(e, extra_class="cev-template") for e in template_entities)

    flow_cue = ""
    if template_entities and instance_entities:
        type_name = (template_entities[0].label.strip() or template_entities[0].id).split("(")[0].strip()
        flow_cue = f'<div class="cev-flow-cue">&darr;<br>new {_esc(type_name)}(...)<br>&darr;</div>'

    body = f"""\
<div class="{root_class}">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-template-row">{template_html}</div>
{flow_cue}
<div class="{instances_class}">{instances_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 1.5 "hierarchy"/"decision_tree" - entities laid out in top-down LEVELS (a
#    real branching tree), not the flat connected row "relationship" uses -
#    the whole point of having a dedicated representation for these is that
#    the LAYOUT itself should look different, not just the card content (see
#    the universal content-to-structure mapping this module follows:
#    hierarchy -> tree structure, decision -> branching flow, relationship ->
#    connected-node diagram - three different shapes from the same
#    entities+relationships input).
# ---------------------------------------------------------------------------

def _build_tree_levels(
    entities: list[VisualEntity], relationships
) -> tuple[list[list[VisualEntity]], list[dict[str, str]]] | None:
    """BFS levels from every root (an entity that is never a relationship's
    target) - `None` when there's nothing to build a tree from (no
    relationships) or the data doesn't form one clean forest (a cycle, a
    re-merge where two parents share a child, or an edge to/from an unknown
    id) - callers fall back to the flat connected-row rendering, the same
    "never a hard failure, never fake a shape the data doesn't actually
    have" contract `_is_linear_chain` already keeps for the chain case.

    Returns (levels, level_labels) - `level_labels[i]` maps an entity id in
    `levels[i]` to the label of the edge that reached it (empty for level 0,
    which has no parent)."""
    if not relationships:
        return None
    by_id = {e.id: e for e in entities}
    children: dict[str, list[tuple[str, str]]] = {}
    has_incoming: set[str] = set()
    for rel in relationships:
        if rel.source not in by_id or rel.target not in by_id:
            return None
        children.setdefault(rel.source, []).append((rel.target, rel.type))
        has_incoming.add(rel.target)

    roots = [e.id for e in entities if e.id not in has_incoming]
    if not roots:
        return None  # every entity has a parent - a cycle, not a tree

    levels: list[list[VisualEntity]] = [[by_id[eid] for eid in roots]]
    level_labels: list[dict[str, str]] = [{}]
    visited = set(roots)
    current = roots
    for _ in range(len(entities) + 1):  # bounded - a real DAG can't need more hops than it has entities
        next_ids: list[str] = []
        next_labels: dict[str, str] = {}
        for parent_id in current:
            for child_id, label in children.get(parent_id, []):
                if child_id in visited:
                    return None  # a cycle, or two parents re-merging into one child - not a clean tree
                visited.add(child_id)
                next_ids.append(child_id)
                next_labels[child_id] = label
        if not next_ids:
            break
        levels.append([by_id[eid] for eid in next_ids])
        level_labels.append(next_labels)
        current = next_ids

    if visited != set(by_id):
        return None  # a disconnected entity never reached from any root - not one clean tree
    return levels, level_labels


def _tree_card_html(entity: VisualEntity, index: int) -> str:
    role = _resolve_color_role(entity.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_html = f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(_entity_icon_char(entity))}</span></div>'
    prop_items = "".join(
        _render_prop(key, value)
        for key, value in entity.properties.items()
        if key.strip() and key.strip().lower() not in _META_PROPERTY_KEYS
    )
    props_html = f'<div class="cev-props">{prop_items}</div>' if prop_items else ""
    delay = f"{min(index, 8) * 0.07:.2f}s"
    return (
        f'<div class="cev-card cev-tree-card" style="{css_vars}animation-delay:{delay};" '
        f'data-entity-id="{_esc(entity.id)}" data-role="{_esc(entity.role)}">'
        f"{icon_html}"
        f'<div class="cev-label">{_esc(entity.label.strip() or entity.id)}</div>'
        f"{props_html}"
        f"</div>"
    )


def _tree_links_html(child_entities: list[VisualEntity], labels: dict[str, str]) -> str:
    """The connector row between one tree level and the next: a vertical
    stem per child (CSS, not SVG - `.cev-tree-level`/`.cev-tree-links` share
    the same flex sizing so a stem always lines up under its own child with
    no pixel math needed), joined by one shared rail across the top when
    there's more than one child, a real condition/branch label (never a
    redundant structural one - see _GENERIC_STRUCTURAL_EDGE_TYPES) shown
    just under the rail."""
    cells: list[str] = []
    for child in child_entities:
        raw_label = labels.get(child.id, "").strip()
        show_label = bool(raw_label) and raw_label.lower() not in _GENERIC_STRUCTURAL_EDGE_TYPES
        label_html = (
            f'<span class="cev-tree-link-label">{_esc(raw_label.replace("_", " "))}</span>' if show_label else ""
        )
        cells.append(f'<div class="cev-tree-link-cell">{label_html}</div>')
    fanned = " cev-tree-links-fanned" if len(child_entities) > 1 else ""
    return f'<div class="cev-tree-links{fanned}">{"".join(cells)}</div>'


def _render_tree(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    """hierarchy/decision_tree: entities laid out top-down in BFS levels from
    the root(s) declared by `relationships` (see _build_tree_levels), each
    level its own row, connected by a simple rail+stem - a genuine
    branching/tree shape, distinct from "relationship"'s circular
    concept-map layout even though both read the same entities+relationships
    input. Falls back to the plain flat row when there are no relationships
    at all, or to that same circular concept-map layout (see
    `_render_concept_map_html`) when the data has relationships but isn't a
    clean tree (a cycle, a re-merge, a disconnected node) - never a hard
    failure."""
    built = _build_tree_levels(spec.entities, spec.relationships)
    if built is None:
        if spec.relationships:
            content_html = _render_concept_map_html(spec.entities, spec.relationships)
            wrapper_open, wrapper_close = "", ""
        else:
            content_html = _entities_row_html(spec.entities, spec.relationships, extra_class="cev-instance")
            wrapper_open, wrapper_close = '<div class="cev-instances">', "</div>"
        body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
{wrapper_open}{content_html}{wrapper_close}
</div>"""
        return body.encode("utf-8")

    levels, level_labels = built

    if len(levels) > 1 and all(len(lvl) == 1 for lvl in levels):
        # A pure linear chain dressed up as a hierarchy - every level has
        # exactly one entity, no branching at all (a real, confirmed case:
        # the OSI model's 7 layers, each strictly nested inside the last,
        # with no sibling layers anywhere). Stacking that vertically as
        # "levels" wastes the page's own width (the whole diagram is only
        # ever as wide as one card) and, for a chain long enough, forces
        # the page-fit safety net (`_fit_scale`) to shrink the entire
        # fragment - and its connector lines with it - down to a fraction
        # of its natural size. A flowing horizontal chain (the exact
        # `_chain_row_html` technique a genuine relationship chain already
        # uses) reads exactly as correctly - still top-to-bottom in
        # meaning, left-to-right on the page - while actually using the
        # page's width and wrapping onto more rows instead of shrinking.
        chain_entities = [lvl[0] for lvl in levels]
        tree_html = _chain_row_html(chain_entities)
        body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-zig-grid">{tree_html}</div>
</div>"""
        return body.encode("utf-8")

    parts: list[str] = []
    index = 0
    for level_index, level_entities in enumerate(levels):
        cards = "".join(_tree_card_html(e, index + i) for i, e in enumerate(level_entities))
        parts.append(f'<div class="cev-tree-level">{cards}</div>')
        index += len(level_entities)
        if level_index + 1 < len(levels):
            parts.append(_tree_links_html(levels[level_index + 1], level_labels[level_index + 1]))
    tree_html = "".join(parts)

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-tree">{tree_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 2. "data_structure" - an ordered sequence of entities (their own list
#    order IS the structure's order) + push/pop/enqueue/dequeue operations.
# ---------------------------------------------------------------------------


def _render_data_structure(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    """Entities render as an ordered sequence - a stack/queue/linked-list's
    own front-to-back arrangement, taken directly from `entities`' own list
    order, no separate ordering field needed. A static picture: every
    element is pre-populated in the markup, and the FRONT/TOP end badges
    show at a glance which end push/pop/enqueue/dequeue would act on if the
    learner were doing it themselves, without needing a live button to
    demonstrate it.

    When `relationships` are declared (a tree/graph-shaped structure such as
    a binary search tree - not a plain stack/queue/list, which never sets
    them), a linear front-to-back row would silently flatten and lose the
    parent/child edges. Reuses the same connector technique `_render_object`
    already uses for `relationship`/`hierarchy`: entities are laid out next
    to a real connector for each declared edge, so the structure's actual
    shape stays visible instead of becoming an unconnected row. Front/back
    index badges are stack/queue-specific and don't apply here, so they're
    skipped for this case."""
    if spec.relationships:
        items_html = _entities_row_html(spec.entities, spec.relationships, extra_class="cev-instance cev-ds-item")
        track_class = "cev-instances"
        ends_html = ""
    else:
        last_index = len(spec.entities) - 1
        items_html = "".join(
            f'<div class="cev-ds-item-wrap">'
            f'{_entity_card_html(entity, extra_class="cev-instance cev-ds-item", index=index)}'
            # A badge on the two end elements so a non-technical reader sees
            # at a glance *where* push/pop/enqueue/dequeue would act,
            # without needing to read the "front / top-back" caption text.
            + (f'<span class="cev-ds-end-badge">FRONT</span>' if index == 0 and last_index > 0 else "")
            + (f'<span class="cev-ds-end-badge">TOP</span>' if index == last_index else "")
            + f'<div class="cev-ds-index">{index}</div>'
            f"</div>"
            for index, entity in enumerate(spec.entities)
        )
        track_class = "cev-ds-track"
        # "front" (left) is where dequeue removes from; "top / back" (right) is
        # where push/pop/enqueue conceptually act, matching the FRONT/TOP
        # badges above regardless of whether the spec is stack-flavoured
        # (top) or queue-flavoured (back).
        ends_html = (
            '<div class="cev-ds-ends"><span>&larr; front</span><span>top / back &rarr;</span></div>'
            if spec.entities
            else ""
        )

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="{track_class}">{items_html}</div>
{ends_html}
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 3. "process" - a chain of step cards connected by arrows, numbered in
#    order. Also adapts `concept_states`+`transitions` into the same
#    grammar when `steps` is empty (state_machine's fallback).
# ---------------------------------------------------------------------------


def _step_card_html(step: VisualStep, index: int) -> str:
    role = _resolve_color_role(step.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_html = f'<div class="cev-icon">{_esc(step.icon)}</div>' if step.icon.strip() else ""
    desc_html = f'<div class="cev-step-desc">{_esc(step.description)}</div>' if step.description.strip() else ""
    delay = f"{min(index, 8) * 0.07:.2f}s"
    return (
        f'<div class="cev-card cev-step" style="{css_vars}animation-delay:{delay};" data-step-id="{_esc(step.id)}">'
        f'<div class="cev-step-number">{index + 1}</div>'
        f"{icon_html}"
        f'<div class="cev-label">{_esc(step.label)}</div>'
        f"{desc_html}"
        f"</div>"
    )


def _effective_steps(spec: DiagramSpec) -> list[VisualStep]:
    if spec.steps:
        return sorted(spec.steps, key=lambda s: s.order)
    if spec.concept_states:
        incoming = {t.to_state: t.label for t in spec.transitions}
        return [
            VisualStep(
                id=state.id, label=state.label,
                description=incoming.get(state.id, "") or state.description,
                order=index, icon=state.icon, color_role=state.color_role,
            )
            for index, state in enumerate(spec.concept_states)
        ]
    return []


# Cycled per card index purely for visual variety (a reader never needs to
# decode meaning from WHICH shape a given step got) - three silhouettes so
# consecutive cards in the same column (two rows apart) read as different
# rather than a monotonous column of identical numbered circles.
_ZIG_BADGE_SHAPES = ("diamond", "hexagon", "circle")


def _zig_badge_html(index: int) -> str:
    shape = _ZIG_BADGE_SHAPES[index % len(_ZIG_BADGE_SHAPES)]
    number = f"{index + 1:02d}"
    inner = f"<span>{number}</span>" if shape == "diamond" else number
    return f'<div class="cev-zig-badge cev-zig-badge-{shape}">{inner}</div>'


def _zig_card_html(step: VisualStep, index: int, *, solo: bool = False) -> str:
    role = _resolve_color_role(step.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_html = f'<span class="cev-zig-icon">{_esc(step.icon)}</span>' if step.icon.strip() else ""
    desc_html = f'<div class="cev-zig-desc">{_esc(step.description)}</div>' if step.description.strip() else ""
    classes = "cev-zig-card cev-zig-card-solo" if solo else "cev-zig-card"
    delay = f"{min(index, 8) * 0.07:.2f}s"
    return (
        f'<div class="{classes}" style="{css_vars}animation-delay:{delay};" data-step-id="{_esc(step.id)}">'
        f"{_zig_badge_html(index)}"
        f'<div class="cev-zig-blob">{icon_html}</div>'
        f'<div class="cev-zig-text"><div class="cev-label">{_esc(step.label)}</div>{desc_html}</div>'
        f"</div>"
    )


_ZIG_LINK_HTML = '<div class="cev-zig-link"><span class="cev-zig-link-arrow">&rarr;</span></div>'


def _zig_curve_svg(color: str) -> str:
    """One continuous curved connector carrying the flow from the right
    card of one row down into the left card of the next - the zigzag's own
    row always ends on the right (grid auto-flow always starts each new
    row back at the left column), so this same left-ending/right-starting
    shape is correct for every row boundary, never just alternating ones.
    `preserveAspectRatio="none"` lets this one fixed path stretch to
    whatever width/height CSS grid actually gives its row - approximate,
    not pixel-exact, but this module has never claimed pixel-exact
    connectors (see _connector_html's own docstring)."""
    return (
        '<div class="cev-zig-curve-row">'
        '<svg viewBox="0 0 600 72" preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="M560,2 C560,40 480,40 420,40 L130,40 C70,40 44,40 44,66" '
        f'fill="none" stroke="{color}" stroke-width="5" stroke-linecap="round"/>'
        f'<path d="M35,56 L44,72 L53,56 Z" fill="{color}"/>'
        "</svg></div>"
    )


def _zigzag_steps_html(steps: list[VisualStep]) -> str:
    """Pairs of cards sharing a row (straight arrow between them), a curved
    connector carrying the flow into the next row, a lone trailing card
    (odd step count) centred and spanning the full row with no connector
    after it. CSS grid auto-placement (see .cev-zig-grid) does the actual
    row-wrapping - every element here just states its own column span, so
    this never needs to compute row/column positions itself."""
    parts: list[str] = []
    steps_count = len(steps)
    index = 0
    last_stroke = "#94a3b8"
    while index < steps_count:
        pair = steps[index : index + 2]
        if len(pair) == 2:
            parts.append(_zig_card_html(pair[0], index))
            parts.append(_ZIG_LINK_HTML)
            parts.append(_zig_card_html(pair[1], index + 1))
            last_stroke = _resolve_color_role(pair[1].color_role).stroke
        else:
            parts.append(_zig_card_html(pair[0], index, solo=True))
            last_stroke = _resolve_color_role(pair[0].color_role).stroke
        index += 2
        if index < steps_count:
            parts.append(_zig_curve_svg(last_stroke))
    return "".join(parts)


def _render_process(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    # "process"/"sequence"/"pipeline"/"state_machine" render as a numbered
    # two-column zigzag of icon+text cards (see _zigzag_steps_html) - "cycle"
    # keeps its own separate renderer/layout (a left-to-right row + loop-back
    # badge - see _render_cycle), so this is exactly what every other
    # representation reaching this function wants, with no per-representation
    # branching needed here.
    steps = _effective_steps(spec)
    steps_html = _zigzag_steps_html(steps)
    # A plain step count gives a non-technical reader an immediate sense of
    # scale before reading the grid itself.
    meta_html = f'<div class="cev-step-counter">{len(steps)} steps</div>' if steps else ""

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
{meta_html}
<div class="cev-zig-grid">{steps_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 4. "comparison" - entities side by side as columns of a table, rows are
#    the union of every entity's property keys. A real dedicated shape
#    (previously an alias for _render_object's template-less entity row) -
#    a proper table reads far more clearly as "these things, compared on
#    these criteria" than a plain row of cards ever could.
# ---------------------------------------------------------------------------


def _render_comparison(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    entities = spec.entities
    keys: list[str] = []
    seen: set[str] = set()
    for entity in entities:
        for key in entity.properties:
            if key.strip() and key not in seen:
                seen.add(key)
                keys.append(key)

    header_cells = "".join(
        f'<th class="cev-cmp-col" style="--cev-fill:{_resolve_color_role(e.color_role).fill};'
        f"--cev-stroke:{_resolve_color_role(e.color_role).stroke};"
        f'--cev-text:{_resolve_color_role(e.color_role).text};">'
        f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(_entity_icon_char(e))}</span></div>'
        f'<div class="cev-cmp-col-label">{_esc(e.label.strip() or e.id)}</div>'
        f"</th>"
        for e in entities
    )
    body_rows = "".join(
        f"<tr><th class=\"cev-cmp-row-label\">{_esc(key)}</th>"
        + "".join(
            f'<td class="cev-cmp-cell">{_esc(e.properties.get(key, "").strip() or "—")}</td>'
            for e in entities
        )
        + "</tr>"
        for key in keys
    )
    table_html = (
        f'<table class="cev-cmp-table"><thead><tr><th class="cev-cmp-corner"></th>{header_cells}</tr></thead>'
        f"<tbody>{body_rows}</tbody></table>"
    )

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-cmp-wrap">{table_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 5. "timeline" - a chronological sequence of milestones (reuses `steps`,
#    same grammar as "process" - a milestone's `label` carries the
#    date/era, `description` the detail), each its own dot-and-stem marker
#    over a card so it never depends on siblings' positions - safe to wrap
#    into any number of rows without a shared rail breaking.
# ---------------------------------------------------------------------------


def _timeline_entry_html(step: VisualStep, index: int) -> str:
    role = _resolve_color_role(step.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_html = f'<div class="cev-icon">{_esc(step.icon)}</div>' if step.icon.strip() else ""
    desc_html = f'<div class="cev-step-desc">{_esc(step.description)}</div>' if step.description.strip() else ""
    delay = f"{min(index, 8) * 0.07:.2f}s"
    return (
        f'<div class="cev-timeline-entry" style="animation-delay:{delay};" data-step-id="{_esc(step.id)}">'
        f'<div class="cev-timeline-dot-wrap">'
        f'<div class="cev-timeline-dot" style="{css_vars}"></div>'
        f'<div class="cev-timeline-stem" style="{css_vars}"></div>'
        f"</div>"
        f'<div class="cev-card cev-timeline-card" style="{css_vars}">'
        f"{icon_html}"
        f'<div class="cev-label">{_esc(step.label)}</div>'
        f"{desc_html}"
        f"</div>"
        f"</div>"
    )


def _render_timeline(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    steps = _effective_steps(spec)
    entries_html = "".join(_timeline_entry_html(step, index) for index, step in enumerate(steps))
    meta_html = f'<div class="cev-step-counter">{len(steps)} milestones</div>' if steps else ""

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
{meta_html}
<div class="cev-timeline-row">{entries_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 6.5 "cycle" - a repeating sequence (a training loop, a release cycle) -
#    same step-card grammar as "process" (reuses _step_card_html), but the
#    last step visibly loops back to the first instead of just ending, so
#    it reads as "this repeats" rather than "this finishes". Deliberately
#    NOT a geometric ring layout (N cards positioned in a circle needs real
#    trigonometry and breaks the moment N or a label's length changes) -
#    the loop-back badge conveys the same idea with zero new failure modes,
#    reusing every width-fill/overflow/cap-and-scale guarantee "process"
#    already has.
# ---------------------------------------------------------------------------


def _render_cycle(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    steps = _effective_steps(spec)
    parts: list[str] = []
    for index, step in enumerate(steps):
        if index:
            parts.append('<div class="cev-step-arrow">&rarr;</div>')
        parts.append(_step_card_html(step, index))
    loop_html = ""
    if len(steps) > 1:
        loop_html = (
            '<div class="cev-cycle-loop">'
            '<span class="cev-cycle-loop-icon" aria-hidden="true">&#8635;</span>'
            f'<span>then back to &ldquo;{_esc(steps[0].label)}&rdquo;</span>'
            "</div>"
        )
    steps_html = "".join(parts)
    meta_html = f'<div class="cev-step-counter">{len(steps)}-step cycle</div>' if steps else ""

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
{meta_html}
<div class="cev-steps">{steps_html}</div>
{loop_html}
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# 6. "before_after" - exactly two entities (the first two in `entities`)
#    shown as a "then vs now" pair either side of an arrow - a state
#    transformation (a variable's value, a system's architecture, a
#    process before/after an optimisation), not a sequence or a >2-way
#    comparison.
# ---------------------------------------------------------------------------


def _before_after_panel_html(entity: VisualEntity, tag_label: str) -> str:
    role = _resolve_color_role(entity.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    icon_char = _entity_icon_char(entity)
    prop_items = "".join(
        _render_prop(key, value)
        for key, value in entity.properties.items()
        if key.strip() and key.strip().lower() not in _META_PROPERTY_KEYS
    )
    props_html = f'<div class="cev-props">{prop_items}</div>' if prop_items else ""
    return (
        f'<div class="cev-ba-card" style="{css_vars}">'
        f'<div class="cev-ba-tag">{_esc(tag_label)}</div>'
        f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(icon_char)}</span></div>'
        f'<div class="cev-label">{_esc(entity.label.strip() or entity.id)}</div>'
        f"{props_html}"
        f"</div>"
    )


def _render_before_after(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    entities = spec.entities
    before = entities[0] if len(entities) > 0 else None
    after = entities[1] if len(entities) > 1 else None
    before_html = f'<div class="cev-ba-panel">{_before_after_panel_html(before, "Before")}</div>' if before else ""
    after_html = f'<div class="cev-ba-panel">{_before_after_panel_html(after, "After")}</div>' if after else ""
    arrow_html = '<div class="cev-ba-arrow">&rarr;</div>' if before and after else ""

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-before-after">{before_html}{arrow_html}{after_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# dispatch: every REPRESENTATION_TYPES value maps to one of the 7 real
# renderers above. Adding another real renderer later means adding a
# function + repointing its own keys here, never touching the other
# renderers or anything upstream of this table.
# ---------------------------------------------------------------------------

_REPRESENTATION_RENDERERS = {
    "object": _render_object,
    "code_visualization": _render_object,   # already ties technical_signature to an entity
    "comparison": _render_comparison,
    "relationship": _render_object,          # + relationship connector lines (see _entities_row_html)
    "hierarchy": _render_tree,               # top-down branching levels (see _render_tree) - a real tree
                                              # shape, not just "relationship" with different edge labels
    "decision_tree": _render_tree,           # a branching decision is the exact same tree shape, with its
                                              # Yes/No (or condition) edges shown as branch labels
    "spatial": _render_object,               # best-effort; a documented future gap, not a crash
    "timeline": _render_timeline,
    "before_after": _render_before_after,
    "data_structure": _render_data_structure,
    "process": _render_process,
    "sequence": _render_process,             # a strict linear walkthrough - same step-chain grammar
    "pipeline": _render_process,             # a staged transformation is an ordered step chain
    "state_machine": _render_process,        # concept_states+transitions adapted into the same grammar
    "cycle": _render_cycle,
}

# Representations that get the flatter "flow" card treatment (see
# .cev-style-flow in _style_html) instead of the default colourful icon-card
# look. Keyed by representation, not by renderer function, because
# "decision_tree" and "hierarchy" share _render_tree with each other but
# must look the same regardless (both are the identical top-down branching/
# flowchart shape, just with or without branch labels), while "object" (via
# _render_object, a DIFFERENT function) must keep the icon-card look for its
# own template+instances/flat-row case - a renderer-identity check couldn't
# tell those apart, but representation can. "relationship"/"spatial" are
# deliberately NOT listed here even though they also share _render_object:
# unlike hierarchy/decision_tree, _render_object already decides per-branch
# whether toned-down styling applies (a genuine single-dominant-hub keeps
# its own vivid concept-map look; its flowchart fallback gets toned down -
# see that function's own logic), so a blanket entry here would double up
# with (and fight) that finer-grained, structure-aware decision.
_FLOW_STYLE_REPRESENTATIONS = {
    "process", "sequence", "pipeline", "state_machine", "cycle", "decision_tree", "hierarchy",
}


def render_concept_experience_html(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    """Dispatches on `spec.representation` via `_REPRESENTATION_RENDERERS`;
    an unrecognised or blank value falls back to `_render_object` (the
    universal safe default) rather than raising - same "never a hard
    failure" contract as every other fallback in this pipeline.

    A concept with many entities/steps can need more vertical room than a
    single page has (`.cev-card`/`.cev-step` now cap their own width - see
    `_style_html` - so a long description wraps onto more lines instead of
    stretching the card wide and silently collapsing the row-count this
    module's estimate assumes, but a genuinely long list still won't fit
    one page at natural size). Rather than let that overflow get clipped by
    the fixed-size page box (or overlap the next block, since the page
    layout engine only ever reserves what `estimate_pixel_size` reports),
    the whole fragment is uniformly shrunk - via a CSS transform, not by
    dropping any content - to the exact box `estimate_pixel_size` reserved
    for it, so the two always agree."""
    representation = spec.representation.strip()
    renderer = _REPRESENTATION_RENDERERS.get(representation) or _render_object
    body = renderer(spec, theme)
    if representation in _FLOW_STYLE_REPRESENTATIONS:
        # Every renderer's very first element is literally `<div class="cev-root">`
        # (see each _render_* function) - a single, one-time, start-anchored
        # replace is safe and can't collide with anything later in the markup.
        body = body.replace(b'<div class="cev-root">', b'<div class="cev-root cev-style-flow">', 1)
    width, natural_height, capped_height, scale = _fit_scale(spec)
    if scale >= 1.0:
        return body
    return _wrap_scaled(body, width=width, natural_height=natural_height, capped_height=capped_height, scale=scale)


def _wrap_scaled(body: bytes, *, width: int, natural_height: float, capped_height: int, scale: float) -> bytes:
    # `overflow:visible`, not `hidden`: the estimate this scale factor is
    # built from is still just an estimate (real browser text wrapping can
    # come out a little different from the row-count arithmetic above) - if
    # it undershoots, a slightly-too-tall scaled fragment should spill a few
    # pixels past its reserved box (the same "estimate was a little off"
    # risk every other block height in this codebase already carries)
    # instead of silently clipping content out of view, which is the one
    # outcome worth avoiding at all costs here.
    wrapper = (
        f'<div style="width:{width}px;height:{capped_height}px;'
        f'display:flex;justify-content:center;">'
        f'<div style="width:{width}px;height:{math.ceil(natural_height)}px;flex:none;'
        f'transform:scale({scale:.4f});transform-origin:top center;">'
        f"{body.decode('utf-8')}"
        f"</div></div>"
    )
    return wrapper.encode("utf-8")


# ---------------------------------------------------------------------------
# Layout sizing: unlike a raster/SVG picture (a fixed aspect ratio), this
# fragment's real height varies a lot with its content - the page layout
# engine (app.course.document.layout.estimate_height /
# image_box_height) positions every block that comes AFTER this one using
# ONE absolute Y coordinate computed from this block's estimated height,
# so an underestimate doesn't just look wrong on this block, it makes the
# NEXT block overlap it. `ConceptVisualService` calls this once per
# generated spec and stores the result as content.width/height, reusing the
# exact mechanism a raster image already uses for its own intrinsic size -
# see `estimate_pixel_size`'s own docstring for why the estimate always
# errs tall rather than risking another overlap.
#
# That estimate is also capped at roughly one page's worth of height
# (`_MAX_HEIGHT`) - a page-flow block never gets a second chance to grow
# once it's already alone on a fresh page (see `flow_blocks` in
# app.course.document.layout), so reserving MORE than a page can ever hold
# would just move the overlap into a clip instead. `_fit_scale` reports the
# same cap as a scale factor so `render_concept_experience_html` can shrink
# the actual markup to match exactly what got reserved.
# ---------------------------------------------------------------------------

_REFERENCE_WIDTH = 666  # matches CONTENT_WIDTH in app.schemas.document at the default template
_ESTIMATE_SAFETY = 1.18  # err generously tall - extra whitespace is harmless, overlap is not
# CONTENT_HEIGHT is the absolute most a block starting fresh at the top of a
# page could ever occupy; reserve some of that for the image block's own
# caption/padding overhead (see image_box_height/estimate_height in
# app.course.document.layout, which add those on top of this number) so the
# full block - not just the picture - is guaranteed to fit on one page.
_MAX_HEIGHT = CONTENT_HEIGHT - 100.0


# `.cev-root` has 28px padding on every side (see _style_html), so the flex
# rows that actually wrap entities/steps only ever have this much width to
# work with - using the full _REFERENCE_WIDTH here (as an earlier version of
# this function did) overstated how much fit per row and was a direct cause
# of underestimating height for any card long enough to sit near its
# `.cev-card`/`.cev-step` max-width (see that rule in _style_html).
_INNER_WIDTH = _REFERENCE_WIDTH - 56

# Concept-map hub-mode sizing (see _render_concept_map_html/
# _concept_map_hub_layout) - a floor so even a 1-2-satellite hub doesn't
# look cramped.
_CONCEPT_MAP_BASE_RADIUS = 130.0

# Hub-mode geometry (see _concept_map_hub_layout/_concept_map_required_radius/
# _concept_map_max_radius_for_width) - the real CSS max-width caps for each
# card-width tier (.cev-cmap-node's own rules, and their
# `.cev-cmap-compact` narrower overrides below), the clear gap a spoke line
# needs between a card's edge and its neighbour's, and the float tolerance
# a satellite angle's cos/sin is treated as exactly zero at (avoids a
# division blow-up for a satellite sitting exactly on that axis).
_CONCEPT_MAP_HUB_WIDTH = 200.0
_CONCEPT_MAP_SAT_WIDTH = 176.0
_CONCEPT_MAP_HUB_WIDTH_COMPACT = 160.0
_CONCEPT_MAP_SAT_WIDTH_COMPACT = 140.0
_CONCEPT_MAP_CLEARANCE = 28.0
_CONCEPT_MAP_ANGLE_EPSILON = 1e-6

# How far a curved spoke (_concept_map_curved_spoke_path) pulls its satellite
# end back from the satellite's own centre, toward the hub, so the curve
# visually stops near the card's edge instead of running into its middle -
# an approximation (this module has never claimed pixel-exact connectors),
# not a measurement of any specific card's real edge.
_CONCEPT_MAP_SPOKE_PULLBACK = 26.0

# A card only stretches out toward its CSS max-width when it actually has
# enough text to need it (a description, several properties, a long label) -
# a short label-only card (a BST node's "8", a queue slot's "req-42") sits
# much closer to its min-width and several more of them share a row. Using
# one fixed slot width for every card either wastes a lot of space on the
# common short-label visuals or (the bug this module is fixing) undercounts
# rows for the long-content ones, so the per-row math below interpolates the
# assumed slot width from how much text a card actually carries, between a
# `compact` endpoint (min-width-ish) and a `wide` one (max-width, see the
# `.cev-card`/`.cev-step` rules in _style_html).
_COMPACT_CHARS = 18   # a short label/value alone - card stays near min-width
_WIDE_CHARS = 70      # a real sentence-length description - card hits max-width


def _slot_width(total_chars: int, *, compact: int, wide: int) -> int:
    if total_chars <= _COMPACT_CHARS:
        return compact
    if total_chars >= _WIDE_CHARS:
        return wide
    t = (total_chars - _COMPACT_CHARS) / (_WIDE_CHARS - _COMPACT_CHARS)
    return round(compact + (wide - compact) * t)


def _step_chars(step: VisualStep) -> int:
    return len(step.label.strip()) + len(step.description.strip())


def _entity_chars(entity: VisualEntity) -> int:
    props = sum(len(k) + len(v) + 2 for k, v in entity.properties.items())
    actions = sum(len(a) + 1 for a in entity.actions)
    return len(entity.label.strip()) + props + actions


def _raw_pixel_size(spec: DiagramSpec) -> tuple[int, float]:
    """The uncapped, safety-inflated footprint estimate - see
    `estimate_pixel_size` for the accounting. Shared with `_fit_scale` so the
    page layout's reservation and the renderer's own scale-down always agree
    on the same number."""
    width = _REFERENCE_WIDTH
    height = 52.0  # .cev-root vertical padding (26px top + bottom)
    height += 40.0  # .cev-title
    if spec.visual_metaphor.strip():
        height += 58.0
    if spec.core_message.strip():
        height += 38.0
    if spec.technical_signature.strip():
        height += 46.0

    representation = spec.representation.strip()
    renderer = _REPRESENTATION_RENDERERS.get(representation) or _render_object

    if renderer is _render_process:
        # Two steps per row (see _render_process/_zigzag_steps_html) - height
        # grows with the number of ROWS, not the number of steps, plus one
        # curved connector between each pair of rows. Row height is still
        # interpolated from how much text the busiest step's description
        # wraps onto, the same way _slot_width interpolates a card's WIDTH
        # elsewhere in this module.
        steps = _effective_steps(spec)
        if steps:
            height += 34.0  # plain step counter
            busiest = max((_step_chars(s) for s in steps), default=0)
            per_row = _slot_width(busiest, compact=88, wide=160)  # row height
            rows = math.ceil(len(steps) / 2)
            height += rows * per_row
            height += max(rows - 1, 0) * 54.0  # curved connector between rows
    elif renderer is _render_cycle:
        steps = _effective_steps(spec)
        if steps:
            height += 34.0  # plain step counter
            busiest = max((_step_chars(s) for s in steps), default=0)
            slot = _slot_width(busiest, compact=150, wide=226)  # card + ~arrow + gaps
            per_row = max(1, _INNER_WIDTH // slot)
            height += math.ceil(len(steps) / per_row) * 168.0
            if len(steps) > 1:
                height += 56.0  # the "loops back to ..." badge
    elif renderer is _render_data_structure:
        if spec.entities:
            busiest = max((_entity_chars(e) for e in spec.entities), default=0)
            if spec.relationships:
                slot = _slot_width(busiest, compact=180, wide=340)  # connector + card, one unit
                per_row = max(1, _INNER_WIDTH // slot)
                height += math.ceil(len(spec.entities) / per_row) * 175.0
            else:
                slot = _slot_width(busiest, compact=120, wide=212)
                per_row = max(1, _INNER_WIDTH // slot)
                height += math.ceil(len(spec.entities) / per_row) * 168.0
                height += 30.0  # front/top end captions
    elif renderer is _render_comparison:
        # A fixed-layout table, never more than one "row of columns" - the
        # columns (entities) share the width evenly (table-layout:fixed, see
        # _style_html) instead of wrapping, so height only grows with the
        # number of property-key rows, not the number of things compared.
        if spec.entities:
            keys: set[str] = set()
            for entity in spec.entities:
                keys.update(k for k in entity.properties if k.strip())
            height += 95.0  # column headers: icon badge + label + padding
            height += len(keys) * 40.0
    elif renderer is _render_timeline:
        steps = _effective_steps(spec)
        if steps:
            height += 34.0  # milestone counter
            busiest = max((_step_chars(s) for s in steps), default=0)
            slot = _slot_width(busiest, compact=150, wide=220)  # card + gaps
            per_row = max(1, _INNER_WIDTH // slot)
            height += math.ceil(len(steps) / per_row) * 200.0  # dot + stem + card
    elif renderer is _render_before_after:
        # Always exactly one row of (at most) two panels - .cev-ba-panel's
        # own max-width (see _style_html) guarantees they never wrap onto a
        # second row at the reference width, so this only needs the row's
        # own height, not a row-count calculation.
        entities = spec.entities[:2]
        if entities:
            busiest = max((_entity_chars(e) for e in entities), default=0)
            height += _slot_width(busiest, compact=170, wide=260)
    elif renderer is _render_tree:
        # One row per tree level, plus one short stem+rail row between each
        # pair of levels (see _tree_links_html) - never one row per entity,
        # since siblings on the same level share a row.
        built = _build_tree_levels(spec.entities, spec.relationships)
        if built is None and spec.relationships and len(spec.entities) >= 2:
            # Not a clean tree, but has relationships - falls back to the
            # concept-map's own real footprint (see _render_tree and the
            # matching `_render_object` branch above for why) - hub mode's
            # exact-geometry container height (_concept_map_hub_layout)
            # when there's a genuine hub, the flow-chart's own level-count
            # estimate (_concept_flow_height_estimate) otherwise, so this
            # estimate always matches what actually renders.
            hub = _concept_map_hub(spec.entities, spec.relationships)
            if hub is not None:
                satellites = [e for e in spec.entities if e.id != hub.id]
                _radius, _compact, _container_w, container_h = _concept_map_hub_layout(hub, satellites)
                height += container_h
            else:
                height += _concept_flow_height_estimate(spec.entities, spec.relationships)
        elif built is None:
            # No relationships at all - falls back to the plain flat row
            # (that's what it actually renders as - see _render_tree).
            busiest = max((_entity_chars(e) for e in spec.entities), default=0)
            slot = _slot_width(busiest, compact=120, wide=212)
            per_row = max(1, _INNER_WIDTH // slot)
            if spec.entities:
                height += math.ceil(len(spec.entities) / per_row) * 168.0
        else:
            levels, _labels = built
            if len(levels) > 1 and all(len(lvl) == 1 for lvl in levels):
                # A pure linear chain (see _render_tree's own matching
                # branch) - renders via _chain_row_html's own two-column
                # zig-grid technique, not vertical levels, so this needs
                # the exact same accounting _render_process already uses
                # for that identical grid shape (never the level-count
                # math a few lines down, which is for genuinely vertical
                # level stacking).
                busiest = max((_entity_chars(e) for e in spec.entities), default=0)
                per_row_height = _slot_width(busiest, compact=88, wide=160)
                rows = math.ceil(len(levels) / 2)
                height += rows * per_row_height
                height += max(rows - 1, 0) * 54.0  # curved connector between rows
            else:
                busiest = max((_entity_chars(e) for e in spec.entities), default=0)
                level_h = _slot_width(busiest, compact=120, wide=190)
                height += len(levels) * level_h
                height += max(len(levels) - 1, 0) * 40.0  # stem+rail row between levels
    else:  # _render_object and every representation that falls back to it
        template_count = sum(1 for e in spec.entities if e.role == "template")
        instance_count = len(spec.entities) - template_count
        if template_count:
            height += 155.0
        if template_count and instance_count:
            height += 48.0  # flow cue between template and instances
        instances = [e for e in spec.entities if e.role != "template"]
        chain_order = _is_linear_chain(instances, spec.relationships) if not template_count else None
        if chain_order is None and spec.relationships and not template_count and len(instances) >= 2:
            # A hub/network concept map (see _render_concept_map_html) - not
            # a per-character row-wrap estimate (that math, below, is for
            # the flat card row this shape no longer renders as). Hub
            # mode's real exact-geometry container height
            # (_concept_map_hub_layout) when there's a genuine hub, the
            # flow-chart's own level-count estimate
            # (_concept_flow_height_estimate) otherwise - see the matching
            # `_render_tree` branch above.
            hub = _concept_map_hub(instances, spec.relationships)
            if hub is not None:
                satellites = [e for e in instances if e.id != hub.id]
                _radius, _compact, _container_w, container_h = _concept_map_hub_layout(hub, satellites)
                height += container_h
            else:
                height += _concept_flow_height_estimate(instances, spec.relationships)
        elif chain_order is not None:
            # Renders via _chain_row_html's own two-column zig-grid
            # technique (see _render_tree's matching branch and
            # _chain_row_html's own docstring for why) - the exact
            # _render_process accounting for that identical grid shape,
            # never the flat per-character row-wrap math below (that's for
            # the unconnected/template+instances cases, which still render
            # as a plain flex-wrap row via _entities_row_html, unchanged).
            busiest = max((_entity_chars(e) for e in instances), default=0)
            per_row_height = _slot_width(busiest, compact=88, wide=160)
            rows = math.ceil(instance_count / 2)
            height += rows * per_row_height
            height += max(rows - 1, 0) * 54.0  # curved connector between rows
        else:
            busiest = max((_entity_chars(e) for e in instances), default=0)
            if spec.relationships:
                slot = _slot_width(busiest, compact=180, wide=340)  # connector + card, one unit
            else:
                slot = _slot_width(busiest, compact=120, wide=212)
            per_row = max(1, _INNER_WIDTH // slot)
            if instance_count:
                height += math.ceil(instance_count / per_row) * 168.0

    return width, height * _ESTIMATE_SAFETY


def _fit_scale(spec: DiagramSpec) -> tuple[int, float, int, float]:
    """(width, natural_height, capped_height, scale). `capped_height` is the
    exact same integer `estimate_pixel_size` returns - computed here too
    (not re-derived from `scale`) so the two can never disagree by a
    floating-point rounding hair. scale is 1.0 when the natural estimate
    already fits one page; otherwise the factor `render_concept_experience_html`
    must shrink the actual markup by so it never needs more room than
    `estimate_pixel_size` reserved for it."""
    width, natural_height = _raw_pixel_size(spec)
    capped_height = math.ceil(min(natural_height, _MAX_HEIGHT))
    if natural_height <= _MAX_HEIGHT:
        return width, natural_height, capped_height, 1.0
    # A second, independent safety margin on top of _ESTIMATE_SAFETY: shrink
    # a little further than the bare ratio requires, so a real render that
    # comes out somewhat taller than this estimate (browser text wrapping
    # is never pixel-exact) still lands inside the reserved box instead of
    # spilling past it - see _wrap_scaled's overflow:visible note for why
    # that spill, if it still happens, is deliberately non-clipping.
    return width, natural_height, capped_height, (_MAX_HEIGHT / natural_height) * 0.92


def estimate_pixel_size(spec: DiagramSpec) -> tuple[int, int]:
    """A conservative, err-high estimate of the rendered fragment's pixel
    footprint at the standard content width - not a text-shaping engine,
    just enough arithmetic (entity/step counts, whether each optional
    chrome element is present) to reserve roughly the right amount of page
    space. Deliberately generous: this module's own layout engine already
    treats "extra whitespace" as harmless and "overlap" as the one outcome
    to avoid at all costs (see app.course.document.layout's module
    docstring) - the same principle applies here. Capped at one page's
    worth of height - see `_MAX_HEIGHT` - since `render_concept_experience_html`
    shrinks the actual markup to match whenever the natural estimate would
    have exceeded it."""
    width, natural_height, capped_height, _scale = _fit_scale(spec)
    return width, capped_height
