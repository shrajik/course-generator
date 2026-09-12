"""Structured diagram specification.

A diagram is authored by the model as *structure* (nodes/edges), never as
pixels or markup - `app.render.diagram_renderer` turns it into SVG
deterministically. This is what makes flow charts, SmartArt-style lists,
hierarchies and conceptual diagrams reliable: labels can never be garbled the
way a diffusion image model garbles text.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")

DIAGRAM_KINDS = (
    "flow_chart",
    "process",
    "cycle",
    "hierarchy",
    "comparison",
    "smart_art",
    "conceptual",
    "concept_map",
    "schematic",
)

# Kinds that show a SEQUENCE (steps happen in this order) - as opposed to
# concept_map, which shows a STRUCTURE (things and how they relate, with no
# implied order). Used to detect when the model picked a sequential kind for
# what was actually requested as a relationship diagram - see
# DiagramService._kind_family_matches.
SEQUENTIAL_KINDS = ("flow_chart", "process", "cycle")
RELATIONSHIP_KINDS = ("concept_map", "conceptual", "hierarchy", "schematic")

# The small, topic-agnostic vocabulary of drawable primitives a "schematic"
# diagram is built from - see SchematicShape. Generic on purpose, and NOT
# physics-specific: "block" is a magnet, a battery, a beaker, a cell wall or
# any two-part rectangular object; "circle" is a cell, an atom/nucleus, a
# planet, a seed or any round object (with an optional smaller labelled
# circle inside it - a nucleus, an embryo, a core); "coil" is a wound coil, a
# spring or a coiled tube/intestine; "gauge" is any dial/meter reading;
# "flow" is field lines, current, airflow, blood flow or a reaction's
# particle/energy movement; "arrow" is a motion/direction/reaction-progress
# indicator; "label" is a standalone annotation.
SHAPE_TYPES = ("block", "circle", "coil", "gauge", "flow", "arrow", "label")


class DiagramNode(BaseModel):
    model_config = _AI

    id: str = ""
    label: str = ""
    detail: str = ""
    # hierarchy: depth (0 = root). concept_map: 0 = the central/focal thing
    # being explained, >=1 = a labelled component/relationship around it.
    # Ignored by other kinds.
    level: int = 0


class DiagramEdge(BaseModel):
    model_config = _AI

    source: str = ""
    target: str = ""
    label: str = ""


ANNOTATION_PRIORITIES = ("critical", "important", "optional")
# Ordering used to decide what survives DiagramSpec.max_annotations when a
# semantically-authored schematic proposes more secondary objects than fit -
# see app.render.schematic_layout. Lower index = kept first.
_PRIORITY_RANK = {name: rank for rank, name in enumerate(ANNOTATION_PRIORITIES)}

SIZE_CLASSES = ("small", "medium", "large")

# The anchor grammar a semantically-authored shape's `anchor` field uses -
# see app.render.schematic_layout.resolve_schematic_layout for how each is
# turned into an actual x/y. "<id>" refers to another shape's `id` in the
# same state.
ANCHOR_KEYWORDS = ("center", "orbit", "left_of", "right_of", "above", "below", "inside")


class SchematicShape(BaseModel):
    """One drawable primitive in a `schematic` diagram - a real illustrated
    component (a coil, a magnet, a meter, field lines) rather than a labelled
    box.

    Two ways to position a shape, kept simultaneously for backward
    compatibility (see app.render.schematic_layout):

    1. Legacy / coordinate-authored: set `x`/`y`/`width`/`height` directly
       (0..1 fraction of the panel). This is what hand-built specs (tests,
       the pre-blueprint mock) already do, and continues to render exactly
       as before - nothing here changes that path.
    2. Semantic / blueprint-authored: leave `x`/`y` at their defaults and set
       `role`/`anchor`/`priority`/`size` instead. A shape is treated as
       semantically-authored the moment ANY shape in the same state sets
       `anchor` or `role` - `resolve_schematic_layout` then computes real
       x/y/width/height for the whole state deterministically, the same way
       the model authors nodes/edges (not pixels) for every other diagram
       kind. This is the path new LLM output uses; see DIAGRAM_SYSTEM.
    """

    model_config = _AI

    type: str = "block"  # one of SHAPE_TYPES
    id: str = ""
    x: float = 0.5  # 0..1, centre position within the panel (legacy path)
    y: float = 0.5
    width: float = 0.22  # 0..1, meaning depends on type (legacy path)
    height: float = 0.22
    label: str = ""
    # block: the second half's label (e.g. a magnet's other pole). circle: a
    # smaller labelled circle drawn inside it (e.g. a cell's nucleus, an
    # atom's core). gauge: the reading shown under the dial. Unused by other
    # types.
    sublabel: str = ""
    rotation: float = 0.0  # degrees: arrow direction / gauge needle angle
    intensity: float = 0.5  # 0..1: flow line count/curvature, gauge deflection magnitude
    # flow/arrow: id of another shape in the same state to point toward -
    # takes priority over `rotation` when it resolves.
    target_id: str = ""

    # --- semantic / blueprint-authored fields (all optional; see docstring) ---
    # "primary" = the one focal subject of the diagram (there should be
    # exactly one per state); "secondary" = everything else. Leave "" on a
    # legacy/coordinate-authored shape.
    role: str = ""
    # One of ANCHOR_KEYWORDS, optionally with ":<other shape's id>" - e.g.
    # "center", "orbit:magnet", "left_of:coil", "above:ammeter". Empty means
    # "not semantically placed" (legacy path) unless another shape in the
    # same state has already opted the state into the semantic path, in
    # which case empty defaults to orbiting the primary shape.
    anchor: str = ""
    # Annotation budget priority - see ANNOTATION_PRIORITIES and
    # DiagramSpec.max_annotations. Only consulted on the semantic path.
    priority: str = "important"
    # Relative size class mapped to a concrete width/height by
    # schematic_layout - see SIZE_CLASSES. Empty means "use width/height as
    # given" (legacy path).
    size: str = ""


class SchematicState(BaseModel):
    """One snapshot of a schematic - e.g. "before" / "after", "at rest" /
    "in motion". A single-state schematic just uses DiagramSpec.shapes and
    never needs this; use `states` (2 or more) for a before/after comparison,
    rendered as stacked, captioned panels."""

    model_config = _AI

    caption: str = ""
    shapes: list[SchematicShape] = Field(default_factory=list)


class DiagramSpec(BaseModel):
    model_config = _AI

    kind: str = "flow_chart"
    title: str = ""
    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)
    # Only used when kind == "schematic": either a flat `shapes` list (one
    # static illustration) or 2+ `states` (a before/after comparison) - never
    # both populated at once.
    shapes: list[SchematicShape] = Field(default_factory=list)
    states: list[SchematicState] = Field(default_factory=list)
    # Only meaningful for a semantically-authored schematic (see
    # SchematicShape) - what a student should take away from this specific
    # visual, and how many secondary shapes it may show before annotation
    # priority starts trimming (see app.render.schematic_layout). Ignored by
    # every other kind and by a legacy/coordinate-authored schematic.
    learning_objective: str = ""
    max_annotations: int = 5

    def normalised_kind(self) -> str:
        value = (self.kind or "").strip().lower().replace(" ", "_")
        return value if value in DIAGRAM_KINDS else "smart_art"

    def is_usable(self) -> bool:
        """A diagram needs at least two labelled things to relate to each
        other, and a relationship-shaped diagram needs at least one labelled
        relationship - a bare list of boxes isn't a concept map. A schematic
        needs at least two recognised shapes, and every state (if any) needs
        its own shapes - an empty "before" panel isn't a comparison."""
        kind = self.normalised_kind()
        if kind == "schematic":
            if self.states:
                if len(self.states) < 2:
                    return False
                return all(len(state.shapes) >= 1 for state in self.states) and (
                    sum(len(state.shapes) for state in self.states) >= 3
                )
            valid = [s for s in self.shapes if s.type in SHAPE_TYPES]
            return len(valid) >= 2

        labelled = [n for n in self.nodes if n.label.strip()]
        if not (2 <= len(labelled) <= 9):
            return False
        if kind == "concept_map":
            has_labelled_edge = any(e.label.strip() for e in self.edges)
            return bool(self.edges) and (has_labelled_edge or len(labelled) >= 3)
        return True
