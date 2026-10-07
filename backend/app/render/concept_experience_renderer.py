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


def _connector_html(rel_type: str) -> str:
    """A real visual connector - an animated flowing line with an arrowhead
    and the relationship type label - placed between two adjacent entity
    cards in the flex row. Not a precise SVG line between exact pixel
    positions (this is flexbox-laid-out HTML, not a coordinate system), but
    a genuine connecting element a reader sees between the two specific
    cards it sits between, which is what a `relationship`/`hierarchy`
    concept (a SQL join, a microservice calling another) needs to read as
    connected rather than two unrelated cards."""
    normalized = rel_type.strip().lower()
    label = "" if normalized in _SPATIAL_ONLY_RELATIONSHIP_TYPES else rel_type.replace("_", " ").strip()
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
    """A genuine chain, rendered as one flowing left-to-right series - a
    plain arrow between each consecutive card, never the per-relationship
    "connected to" connector `_entities_row_html` draws (that phrasing and
    the animated dashed line both make sense for an independent spoke off a
    hub; repeated down an actual straight sequence, it reads as five
    separate little relationships rather than one continuous flow). Reuses
    `_entity_card_html` itself unchanged (still gets its icon/props/
    actions) - only the connector between cards and the class the caller
    wraps this in (`cev-style-flow`, see `_render_object`) differ, so a
    chain gets the same flatter, whiteboard-flowchart treatment
    `_render_process` already uses for an explicit step sequence."""
    parts: list[str] = []
    for index, entity in enumerate(entities_in_order):
        if index > 0:
            parts.append('<div class="cev-step-arrow">&rarr;</div>')
        parts.append(_entity_card_html(entity, extra_class="cev-instance cev-step", index=index))
    return "".join(parts)


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
  border-radius:20px;border:3px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 45%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 68%, #fff));
  color:var(--cev-text,{theme.text_color});
  padding:14px 18px;min-width:104px;text-align:center;
  box-shadow:0 3px 0 color-mix(in srgb, var(--cev-stroke,{theme.border_color}) 55%, transparent), 0 6px 14px rgba(0,0,0,.08);
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
  box-shadow:inset 0 -3px 0 rgba(0,0,0,.15), 0 2px 4px rgba(0,0,0,.12);
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
  box-shadow:0 2px 3px rgba(0,0,0,.15);
}}
.cev-step-desc{{font-size:11px;color:{theme.muted_color};margin-top:5px;}}
.cev-step-arrow{{color:{theme.accent_color};font-size:22px;font-weight:700;padding:0 2px;}}
/* comparison renderer */
.cev-cmp-wrap{{overflow-x:auto;}}
.cev-cmp-table{{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0;}}
.cev-cmp-corner{{background:transparent;width:26%;}}
.cev-cmp-col{{
  padding:12px 10px;border-radius:16px 16px 0 0;text-align:center;color:var(--cev-text,{theme.text_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 45%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 68%, #fff));
  border:3px solid var(--cev-stroke,{theme.border_color});border-bottom:none;
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
  flex:1;border-radius:20px;border:3px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 45%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 68%, #fff));
  color:var(--cev-text,{theme.text_color});padding:18px;text-align:center;
  box-shadow:0 3px 0 color-mix(in srgb, var(--cev-stroke,{theme.border_color}) 55%, transparent), 0 6px 14px rgba(0,0,0,.08);
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
  position:relative;border-radius:20px;border:3px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 26%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 46%, #fff));
  color:var(--cev-text,{theme.text_color});padding:14px 16px 14px 30px;display:flex;align-items:center;gap:10px;
  box-shadow:0 3px 0 color-mix(in srgb, var(--cev-stroke,{theme.border_color}) 55%, transparent), 0 6px 14px rgba(0,0,0,.08);
  animation:cevFadeUp .4s ease both;min-width:0;
}}
.cev-zig-card-solo{{grid-column:1/-1;width:74%;margin:0 auto;}}
.cev-zig-badge{{
  position:absolute;top:-13px;left:-13px;width:36px;height:36px;background:var(--cev-stroke,{theme.accent_color});
  color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;
  box-shadow:0 3px 6px rgba(0,0,0,.22);z-index:1;
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
  content:"";position:absolute;left:50%;top:0;bottom:0;width:3px;
  background:{theme.border_color};transform:translateX(-50%);
}}
.cev-tree-links-fanned{{border-top:3px solid {theme.border_color};}}
.cev-tree-link-label{{
  position:absolute;top:2px;left:50%;transform:translateX(-50%);
  font-size:10px;font-weight:700;color:{theme.muted_color};
  background:{theme.page_background};padding:0 5px;white-space:nowrap;
}}
@media (prefers-reduced-motion: reduce) {{
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line,.cev-timeline-entry,.cev-ba-card,.cev-zig-card {{
    animation:none !important;
  }}
}}
@media print {{
  /* PDF export (Playwright, emulate_media("print")) snapshots the page right
  after load with no extra wait for these wall-clock entrance animations to
  finish - without this, a printed page could freeze mid fade-in (a still-
  faded card, a half-drawn connector). Printing always shows the fully
  settled final state instead, with no added export latency. */
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line,.cev-timeline-entry,.cev-ba-card,.cev-zig-card {{
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
    own fallback when their data isn't a clean tree - see `_render_tree`), a
    row of entities connected by real visual connectors instead - a flat,
    flowing left-to-right SERIES (see `_chain_row_html`) when those relationships form one
    unambiguous straight path through every instance (`_is_linear_chain`),
    the existing hub-style card row (each spoke independently connected,
    still the right shape for e.g. one thing connected to several unrelated
    requirements) otherwise. A static picture - every instance is
    pre-populated in the markup, so the one rendered state is already a
    complete picture with nothing left to reveal."""
    template_entities = [e for e in spec.entities if e.role == "template"]
    instance_entities = [e for e in spec.entities if e.role != "template"]

    chain_order = _is_linear_chain(instance_entities, spec.relationships) if not template_entities else None
    if chain_order:
        by_id = {e.id: e for e in instance_entities}
        instances_html = _chain_row_html([by_id[eid] for eid in chain_order])
        instances_class = "cev-steps"
        root_class = "cev-root cev-style-flow"
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

# A structural relationship type whose label is redundant once two entities
# are already drawn as parent/child in a tree (position alone says "this
# contains/connects to that") - hidden here the same way
# _SPATIAL_ONLY_RELATIONSHIP_TYPES is hidden on the flat connector, so a
# tree's branch label is reserved for when it actually says something a
# reader couldn't already see (a decision's "yes"/"no", a real condition).
_GENERIC_STRUCTURAL_EDGE_TYPES = {"inside", "contains", "connected_to", "attached_to", "surrounds", "passes_through"}


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
    branching/tree shape, distinct from "relationship"'s flat connected row
    even though both read the same entities+relationships input. Falls back
    to that same flat row (no relationships at all, or a shape too tangled
    to call a clean tree) rather than guessing - never a hard failure."""
    built = _build_tree_levels(spec.entities, spec.relationships)
    if built is None:
        instances_html = _entities_row_html(spec.entities, spec.relationships, extra_class="cev-instance")
        body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-instances">{instances_html}</div>
</div>"""
        return body.encode("utf-8")

    levels, level_labels = built
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
# "decision_tree" shares _render_object with "hierarchy"/"relationship"/
# "object" (which must keep the icon-card look) - a renderer-identity check
# couldn't tell those apart.
_FLOW_STYLE_REPRESENTATIONS = {
    "process", "sequence", "pipeline", "state_machine", "cycle", "decision_tree",
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
        if built is None:
            # No relationships, or not a clean tree - falls back to the
            # exact same flat connected-row estimate as the `else` branch
            # below (that's what it actually renders as - see _render_tree).
            busiest = max((_entity_chars(e) for e in spec.entities), default=0)
            slot = _slot_width(busiest, compact=180, wide=340) if spec.relationships else _slot_width(
                busiest, compact=120, wide=212
            )
            per_row = max(1, _INNER_WIDTH // slot)
            if spec.entities:
                height += math.ceil(len(spec.entities) / per_row) * 168.0
        else:
            levels, _labels = built
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
