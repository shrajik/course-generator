"""Centralized, deterministic textbook color system for `schematic` diagrams.

A small set of named semantic *roles* (see COLOR_ROLES) - never raw hex
values scattered through blueprints, prompts or the renderer. Each role
resolves to a (fill, stroke, text) triple:

- `fill`: a light, subtle tint - never the thing text sits directly on top
  of at full saturation (see the module-level requirement this satisfies:
  "color is secondary to clarity").
- `stroke`: a stronger, saturated outline - carries the actual semantic
  color, gives every shape a clean printable boundary.
- `text`: a dark, high-contrast color guaranteed readable against `fill`.

The whole palette is ~6 coordinated hues (blue/violet/amber/red/green/cyan)
reused across roles with matching intent (e.g. "current" and "positive"
share the red family - both read as "active/hot") - restrained on purpose,
never a rainbow. Nothing here is scientifically authoritative; it is a
*consistent* mapping a course author can rely on, not a claim that "current
is always red" in every textbook ever printed.

Role is never the only carrier of meaning: every renderer call site that
uses a role also keeps its existing arrows/labels/shape differences, so the
diagram stays legible in grayscale or for a color-vision-deficient reader -
see app.render.diagram_renderer's `_draw_*` functions, which are unchanged
in *what* they draw, only in which colors they draw it with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColorRole:
    fill: str
    stroke: str
    text: str


# fmt: off
TEXTBOOK_PALETTE: dict[str, ColorRole] = {
    # Structural/focal roles
    "primary":        ColorRole(fill="#dbeafe", stroke="#1d4ed8", text="#1e3a8a"),  # blue
    "secondary":      ColorRole(fill="#ede9fe", stroke="#6d28d9", text="#4c1d95"),  # violet
    "accent":         ColorRole(fill="#fef3c7", stroke="#b45309", text="#78350f"),  # amber
    "structure":      ColorRole(fill="#f1f5f9", stroke="#475569", text="#1e293b"),  # slate

    # Physics / electromagnetism
    "current":        ColorRole(fill="#fee2e2", stroke="#b91c1c", text="#7f1d1d"),  # red
    "magnetic_field": ColorRole(fill="#d1fae5", stroke="#047857", text="#064e3b"),  # green
    "positive":       ColorRole(fill="#fee2e2", stroke="#b91c1c", text="#7f1d1d"),  # red
    "negative":       ColorRole(fill="#dbeafe", stroke="#1d4ed8", text="#1e3a8a"),  # blue

    # Biology / chemistry
    "fluid":          ColorRole(fill="#cffafe", stroke="#0e7490", text="#164e63"),  # cyan

    # Generic emphasis
    "highlight":      ColorRole(fill="#fef3c7", stroke="#b45309", text="#78350f"),  # amber
    "annotation":     ColorRole(fill="#f8fafc", stroke="#94a3b8", text="#334155"),  # light neutral
    "neutral":        ColorRole(fill="#f8fafc", stroke="#cbd5e1", text="#334155"),  # light neutral
}
# fmt: on

COLOR_ROLES: tuple[str, ...] = tuple(TEXTBOOK_PALETTE)

# Stroke width scales with visual hierarchy - the primary/focal shape stays
# dominant, everything else recedes slightly. Mirrors the exact convention
# `diagram_renderer._node_block`'s `emphasis` flag already established for
# concept_map's focal box, so a schematic's primary object gets the same
# visual weight for the same reason.
PRIMARY_STROKE_WIDTH = 2.5
SECONDARY_STROKE_WIDTH = 1.5


def resolve_color_role(color_role: str) -> ColorRole:
    """Unknown/blank roles fall back to "neutral" - never a hard failure.
    Matches every other best-effort fallback in this pipeline: a course
    author typo-ing a color role should degrade to a plain, still-legible
    shape, not break diagram generation."""
    return TEXTBOOK_PALETTE.get((color_role or "").strip().lower(), TEXTBOOK_PALETTE["neutral"])


def stroke_width_for(shape_role: str) -> float:
    return PRIMARY_STROKE_WIDTH if shape_role == "primary" else SECONDARY_STROKE_WIDTH
