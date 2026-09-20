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


def _entity_card_html(entity: VisualEntity, *, extra_class: str = "", index: int = 0) -> str:
    role = _resolve_color_role(entity.color_role)
    css_vars = f"--cev-fill:{role.fill};--cev-stroke:{role.stroke};--cev-text:{role.text};"
    classes = f"cev-card {extra_class}".strip()
    # A card is never bare/iconless - if the planner didn't supply an icon,
    # a coloured initial-letter avatar stands in so the card still reads as
    # a deliberate, finished piece of UI. Only for an alphabetic label
    # though ("Car (Class)" -> "C") - truncating a numeric/symbolic label
    # (a BST node "10", a stack value "42") to its first character would
    # sit right next to the real value and silently misstate it ("1" next
    # to "10"), which is worse than no icon at all.
    first_char = (entity.label.strip() or entity.id)[:1]
    icon_char = entity.icon.strip() or (first_char.upper() if first_char.isalpha() else "◆")
    icon_html = f'<div class="cev-icon-badge"><span class="cev-icon">{_esc(icon_char)}</span></div>'
    label_html = f'<div class="cev-label">{_esc(entity.label.strip() or entity.id)}</div>'
    prop_items = "".join(
        f'<span class="cev-prop"><b>{_esc(key)}</b>{": " + _esc(value) if value.strip() else ""}</span>'
        for key, value in entity.properties.items()
        if key.strip()
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


def _connector_html(rel_type: str) -> str:
    """A real visual connector - an animated flowing line with an arrowhead
    and the relationship type label - placed between two adjacent entity
    cards in the flex row. Not a precise SVG line between exact pixel
    positions (this is flexbox-laid-out HTML, not a coordinate system), but
    a genuine connecting element a reader sees between the two specific
    cards it sits between, which is what a `relationship`/`hierarchy`
    concept (a SQL join, a microservice calling another) needs to read as
    connected rather than two unrelated cards."""
    label = rel_type.replace("_", " ").strip()
    label_html = f'<span class="cev-connector-label">{_esc(label)}</span>' if label else ""
    return f'<div class="cev-connector"><span class="cev-connector-line"></span>{label_html}<span class="cev-connector-arrow">&rarr;</span></div>'


def _entities_row_html(entities: list[VisualEntity], relationships, *, extra_class: str) -> str:
    """Renders `entities` in a flex row - reordered around `relationships`
    (source immediately followed by a connector then target) when any are
    declared, so related entities land adjacent with a real connector
    between them; a plain row (today's exact behaviour) when no
    relationships are set. Each card gets a staggered entrance delay by its
    position in the rendered row."""
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
        parts.append(_connector_html(rel.type))
        if target.id not in rendered:
            parts.append(_entity_card_html(target, extra_class=extra_class, index=order))
            rendered.add(target.id)
            order += 1
    for entity in entities:
        if entity.id not in rendered:
            parts.append(_entity_card_html(entity, extra_class=extra_class, index=order))
            order += 1
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
.cev-root *{{box-sizing:border-box;}}
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
.cev-instances{{display:flex;flex-wrap:wrap;gap:16px;justify-content:center;align-items:center;}}
.cev-card{{
  border-radius:20px;border:3px solid var(--cev-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 45%, #fff), color-mix(in srgb, var(--cev-fill,{theme.surface_color}) 68%, #fff));
  color:var(--cev-text,{theme.text_color});
  padding:14px 18px;min-width:104px;max-width:200px;text-align:center;
  box-shadow:0 3px 0 color-mix(in srgb, var(--cev-stroke,{theme.border_color}) 55%, transparent), 0 6px 14px rgba(0,0,0,.08);
  animation:cevFadeUp .4s ease both;
}}
.cev-card.cev-template{{min-width:220px;max-width:280px;padding:20px 26px;}}
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
.cev-steps{{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;align-items:center;}}
.cev-step{{min-width:118px;max-width:190px;}}
.cev-step-number{{
  width:24px;height:24px;border-radius:50%;background:var(--cev-stroke,{theme.accent_color});color:#fff;
  font-size:11.5px;font-weight:800;display:flex;align-items:center;justify-content:center;margin:0 auto 5px;
  box-shadow:0 2px 3px rgba(0,0,0,.15);
}}
.cev-step-desc{{font-size:11px;color:{theme.muted_color};margin-top:5px;}}
.cev-step-arrow{{color:{theme.accent_color};font-size:22px;font-weight:700;padding:0 2px;}}
@media (prefers-reduced-motion: reduce) {{
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line {{
    animation:none !important;
  }}
}}
@media print {{
  /* PDF export (Playwright, emulate_media("print")) snapshots the page right
  after load with no extra wait for these wall-clock entrance animations to
  finish - without this, a printed page could freeze mid fade-in (a still-
  faded card, a half-drawn connector). Printing always shows the fully
  settled final state instead, with no added export latency. */
  .cev-card,.cev-connector,.cev-flow-cue,.cev-metaphor,.cev-connector-line {{
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
    is set (the `relationship`/`hierarchy` fallback case), a row of entities
    connected by real visual connectors instead. A static picture - every
    instance is pre-populated in the markup, so the one rendered state is
    already a complete picture with nothing left to reveal."""
    template_entities = [e for e in spec.entities if e.role == "template"]
    instance_entities = [e for e in spec.entities if e.role != "template"]

    template_html = "".join(_entity_card_html(e, extra_class="cev-template") for e in template_entities)
    instances_html = _entities_row_html(instance_entities, spec.relationships, extra_class="cev-instance")

    flow_cue = ""
    if template_entities and instance_entities:
        type_name = (template_entities[0].label.strip() or template_entities[0].id).split("(")[0].strip()
        flow_cue = f'<div class="cev-flow-cue">&darr;<br>new {_esc(type_name)}(...)<br>&darr;</div>'

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
<div class="cev-template-row">{template_html}</div>
{flow_cue}
<div class="cev-instances">{instances_html}</div>
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


def _render_process(spec: DiagramSpec, theme: TemplateTheme) -> bytes:
    steps = _effective_steps(spec)
    parts: list[str] = []
    for index, step in enumerate(steps):
        if index:
            parts.append('<div class="cev-step-arrow">&rarr;</div>')
        parts.append(_step_card_html(step, index))
    steps_html = "".join(parts)
    # A plain step count gives a non-technical reader an immediate sense of
    # scale before reading the row itself.
    meta_html = f'<div class="cev-step-counter">{len(steps)} steps</div>' if steps else ""

    body = f"""\
<div class="cev-root">
{_style_html(theme)}
{_header_html(spec, theme)}
{meta_html}
<div class="cev-steps">{steps_html}</div>
</div>"""
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# dispatch: every REPRESENTATION_TYPES value maps to one of the 3 real
# renderers above. Adding a 4th real renderer later means adding a function
# + repointing its own keys here, never touching the other renderers or
# anything upstream of this table.
# ---------------------------------------------------------------------------

_REPRESENTATION_RENDERERS = {
    "object": _render_object,
    "code_visualization": _render_object,   # already ties technical_signature to an entity
    "comparison": _render_object,            # a template-less row of entities is already this shape
    "relationship": _render_object,          # + relationship connector lines (see _entities_row_html)
    "hierarchy": _render_object,             # + relationship connector lines (contains/above read as tree edges)
    "spatial": _render_object,               # best-effort; a documented future gap, not a crash
    "data_structure": _render_data_structure,
    "process": _render_process,
    "sequence": _render_process,             # a strict linear walkthrough - same step-chain grammar
    "pipeline": _render_process,             # a staged transformation is an ordered step chain
    "state_machine": _render_process,        # concept_states+transitions adapted into the same grammar
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
    renderer = _REPRESENTATION_RENDERERS.get(spec.representation.strip()) or _render_object
    body = renderer(spec, theme)
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
        steps = _effective_steps(spec)
        if steps:
            height += 34.0  # plain step counter
            busiest = max((_step_chars(s) for s in steps), default=0)
            slot = _slot_width(busiest, compact=150, wide=226)  # card + ~arrow + gaps
            per_row = max(1, _INNER_WIDTH // slot)
            height += math.ceil(len(steps) / per_row) * 168.0
    elif renderer is _render_data_structure:
        if spec.entities:
            busiest = max((_entity_chars(e) for e in spec.entities), default=0)
            if spec.relationships:
                slot = _slot_width(busiest, compact=140, wide=272)  # + connector
                per_row = max(1, _INNER_WIDTH // slot)
                height += math.ceil(len(spec.entities) / per_row) * 175.0
            else:
                slot = _slot_width(busiest, compact=120, wide=212)
                per_row = max(1, _INNER_WIDTH // slot)
                height += math.ceil(len(spec.entities) / per_row) * 168.0
                height += 30.0  # front/top end captions
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
            slot = _slot_width(busiest, compact=140, wide=272)  # + connector
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
