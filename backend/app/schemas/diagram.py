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
    "concept_experience",
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


# The semantic-relationship vocabulary a schematic's `DiagramSpec.relationships`
# entries use - purely for validation (see app.services.diagram_qa) and for
# matching a canonical blueprint's expected structure (see
# app.render.visual_blueprints). Layout is unaffected by these: a shape's
# actual position still comes only from its own `anchor` field. Several of
# these read like anchor keywords (inside/above/below/left_of/right_of)
# because the same spatial relationship is often worth stating twice - once
# as *where* a shape sits (anchor, used by the layout engine) and once as
# *what it means* (relationships, used for validation) - but a relationship
# never has to have a matching anchor, and vice versa.
RELATIONSHIP_TYPES = (
    "inside",
    "contains",
    "connected_to",
    "attached_to",
    "above",
    "below",
    "left_of",
    "right_of",
    "passes_through",
    "surrounds",
    "contacts",
    "points_to",
    "flows_into",
    "rotates_around",
    "between",
)


class SchematicRelationship(BaseModel):
    """One semantic relationship between two schematic components, e.g.
    `brush CONTACTS commutator`. Declarative only - it documents *what is
    true*, not where anything is drawn; see RELATIONSHIP_TYPES."""

    model_config = _AI

    source: str = ""
    type: str = ""
    target: str = ""


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
    # One of app.render.textbook_palette.COLOR_ROLES (e.g. "current",
    # "magnetic_field", "positive") - a semantic role, never a raw hex value;
    # the renderer resolves it against the centralized palette. Empty means
    # "use the theme's default schematic colors", which is exactly today's
    # rendering - this is what keeps every legacy/coordinate-authored shape
    # visually unchanged.
    color_role: str = ""
    # The color_role a nested "inside:<this shape's id>" child shape carried
    # before app.render.schematic_layout._merge_inside_anchors folded it into
    # this shape's own `sublabel` (see that field above) - lets the inner
    # nested circle (a nucleus inside a cell, a core inside an atom) keep its
    # own distinct semantic color instead of inheriting the parent's. Set
    # automatically by the merge step; a model should never set this
    # directly - a directly-authored `sublabel` (e.g. a gauge's caption or a
    # block's second half) has no separate nested shape to carry a color
    # from, so this stays blank and the parent's own `color_role` is used
    # for both, exactly as before this field existed.
    sublabel_color_role: str = ""


class SchematicState(BaseModel):
    """One snapshot of a schematic - e.g. "before" / "after", "at rest" /
    "in motion". A single-state schematic just uses DiagramSpec.shapes and
    never needs this; use `states` (2 or more) for a before/after comparison,
    rendered as stacked, captioned panels."""

    model_config = _AI

    caption: str = ""
    shapes: list[SchematicShape] = Field(default_factory=list)


# --- concept_experience: a beginner-oriented, static visual for a single
# named concept (e.g. "java_class_and_object"), distinct from schematic's
# illustrated-apparatus vocabulary above. See
# app.render.concept_visual_blueprints for the canonical registry and
# app.render.concept_experience_renderer for how a spec becomes markup. Only
# meaningful for kind == "concept_experience"; every field here defaults to
# empty/None, so nothing above this line is affected by its existence.

# How a concept_experience spec gets turned into markup - proposed by the
# planner, but overridden deterministically by a known concept_visual_blueprints
# entry when one exists (same "model proposes, blueprint constrains" pattern
# visual_type/BLUEPRINT_REGISTRY already uses for schematic). Named for what
# each strategy actually produces, not for a specific graphics technology
# assumption: "deterministic_interactive_html" is self-contained HTML+CSS
# (entity cards, connectors) - not SVG shapes; despite the name it renders a
# static picture, no script and no buttons (see
# app.render.concept_experience_renderer's module docstring).
GENERATION_STRATEGIES = (
    "deterministic_svg",
    "deterministic_interactive_html",
    "gpt_image",
    "hybrid",
)

# Which *shape* of visual best teaches this concept - a domain-agnostic
# dispatch key `app.render.concept_experience_renderer` uses to pick a
# renderer function, chosen from what the concept itself needs (an object
# with instances, an ordered structure, a step sequence, a relationship
# graph, ...), never from which subject it belongs to. Distinct from
# `GENERATION_STRATEGIES` (a *technology* choice - svg/html/image/hybrid)
# and from `concept` (an *identity* key - which registered blueprint, if
# any). See app.render.concept_visual_blueprints and the approved plan for
# the full rationale. Three families have a real, dedicated renderer
# ("object", "data_structure", "process") - every other value renders
# through a documented compatibility fallback onto one of those three (see
# `_REPRESENTATION_RENDERERS` in app.render.concept_experience_renderer),
# never a failure.
REPRESENTATION_TYPES = (
    "object",              # a template/blueprint entity + real instances (a class and its objects,
                            # a microservice) - or, for comparison/relationship-style content, a
                            # template-less row of entities (optionally connected by `relationships`)
    "data_structure",      # ordered/structured entities + operations (a stack, a queue, a linked list)
    "process",             # ordered steps, optionally played through (a CI/CD pipeline, an auth flow)
    "sequence",             # a strict linear walkthrough (an algorithm's execution order, a protocol
                            # handshake) - renders the same way as "process"
    "state_machine",       # named states + transitions (TCP states, an order's lifecycle)
    "relationship",        # entities + relationships, no strong sequence (a SQL join, a microservice
                            # topology, an ER diagram) - renders via "object" + relationship connector lines
    "hierarchy",           # parent/child structure (OSI layers, class inheritance, an org chart) -
                            # renders via "object" + relationship connector lines
    "comparison",          # entities compared side by side (stack vs queue, SQL vs NoSQL)
    "pipeline",            # staged data transformation (RAG, ETL, a compiler pipeline) - renders like a process
    "spatial",             # physical/structural layout matters (memory layout, network topology, CPU
                            # architecture) - best-effort via "object", a documented future gap
    "code_visualization",  # a short technical_signature tied directly to the entity it describes
)

# A VisualEntity's role - "template" is the one class/blueprint object,
# "instance" is a real object created from it. Kept as a free string (like
# SchematicShape.role) rather than a strict enum so a future concept (e.g. an
# interface, an abstract class) can introduce its own role without a schema
# change.
ENTITY_ROLES = ("template", "instance")


class VisualEntity(BaseModel):
    """One thing shown in a concept_experience visual - generic enough to
    cover a Java class/object, a stack's frames, two things being compared,
    or a labelled part of an annotated diagram; never subject-specific.
    `properties` holds the actual instance values a learner can inspect
    (e.g. {"color": "Red"}); `actions` is a short, display-only list of
    capabilities/behaviours (e.g. "start()" for a Java method, "Divides" for
    a cell, "Repairs DNA" for an enzyme) - neither is executed, this is a
    visual spec, not code, and `actions` is deliberately not named "methods"
    so it reads naturally for any domain, not just OOP."""

    model_config = _AI

    id: str = ""
    label: str = ""
    role: str = ""  # one of ENTITY_ROLES, informally - see role docstring above
    properties: dict[str, str] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)
    icon: str = ""
    color_role: str = ""


class VisualStep(BaseModel):
    """One step in a `representation="process"/"sequence"/"pipeline"`
    concept_experience visual (e.g. one stage of a CI/CD pipeline, one
    message in a TCP handshake) - see DiagramSpec.steps. `order` is the
    step's position (0-based); ties are broken by list order, so it's only
    needed when steps might be reordered independently of the list
    itself."""

    model_config = _AI

    id: str = ""
    label: str = ""
    description: str = ""
    order: int = 0
    icon: str = ""
    color_role: str = ""


class VisualState(BaseModel):
    """One named state in a `representation="state_machine"`
    concept_experience visual (e.g. "No current" / "Current flows", a TCP
    connection state) - see DiagramSpec.concept_states. `entity_ids` names
    which entities are involved in/visible during this state; empty means
    "not entity-specific"."""

    model_config = _AI

    id: str = ""
    label: str = ""
    description: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    icon: str = ""
    color_role: str = ""


class VisualTransition(BaseModel):
    """One edge between two `VisualState`s - see DiagramSpec.transitions."""

    model_config = _AI

    from_state: str = ""
    to_state: str = ""
    label: str = ""


class InteractionSpec(BaseModel):
    """One interaction a concept_experience visual's rendered markup offers -
    e.g. clicking the class entity, or a "Create Object"/"Reset" control.
    Declarative only: this describes *what interaction exists and on what*,
    never how it's implemented - app.render.concept_experience_renderer owns
    the actual click/hover/reset JS. `target_entity_ids` empty means "applies
    broadly" (e.g. every instance for a click-to-inspect interaction, or the
    whole visual for reset) rather than one specific entity."""

    model_config = _AI

    type: str = ""
    target_entity_ids: list[str] = Field(default_factory=list)
    trigger_label: str = ""


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
    # Names a canonical textbook visual (e.g. "electric_motor", "animal_cell")
    # - see app.render.visual_blueprints.BLUEPRINT_REGISTRY. Purely additive:
    # an unset or unrecognised visual_type is always valid and simply skips
    # blueprint defaults/validation, falling back to the generic semantic
    # schematic system exactly as before this field existed. Only meaningful
    # for kind == "schematic".
    visual_type: str = ""
    # Declarative semantic relationships between shape ids - see
    # SchematicRelationship. Only used for validation/blueprint-matching,
    # never for layout (that's `SchematicShape.anchor`'s job). Only
    # meaningful for kind == "schematic".
    relationships: list[SchematicRelationship] = Field(default_factory=list)

    # --- concept_experience fields (all optional; see VisualEntity/
    # InteractionSpec docstrings above) - only meaningful for
    # kind == "concept_experience"; every existing kind is unaffected by
    # their presence. ---
    # Names a canonical concept (e.g. "java_class_and_object") - see
    # app.render.concept_visual_blueprints.CONCEPT_BLUEPRINT_REGISTRY.
    # Purely additive, same fallback contract as `visual_type` above: an
    # unset or unrecognised concept is always valid and just skips blueprint
    # defaults, never a hard failure.
    concept: str = ""
    # Free-text/snake_case subject label (e.g. "java", "physics", "sql",
    # "biology"). METADATA ONLY - used for storage/reuse-grouping and human
    # browsing of the blueprint registry; deliberately never read by any
    # branching logic (VisualPlanner's prompt, apply_concept_blueprint_defaults,
    # concept_qa's checks) - representation/entities/steps decide behaviour,
    # not the subject name. See the approved plan's "universal architecture"
    # rationale.
    domain: str = ""
    # One of REPRESENTATION_TYPES - which shape of visual best teaches this
    # concept. See REPRESENTATION_TYPES' docstring above.
    representation: str = ""
    learner_level: str = ""
    # Planner-generated, specific objectives derived from the source content
    # - deliberately a *separate*, plural field from `learning_objective`
    # above (which stays exactly what it was: a schematic's single takeaway
    # sentence). Concept-level QA (app.services.concept_qa) grades the spec
    # against each entry here.
    learning_objectives: list[str] = Field(default_factory=list)
    # The one-sentence, punchy takeaway (e.g. "A class is a blueprint; an
    # object is a real thing built from it.") - shorter and more quotable
    # than any single `learning_objectives` entry; rendered as an on-visual
    # caption/banner.
    core_message: str = ""
    visual_metaphor: str = ""
    # A short, single-line code-like mapping (e.g. "class Car { color;
    # model; speed; start(); stop(); drive(); }") shown beside the
    # template/blueprint entity - never a multi-line code block or
    # screenshot. concept_qa's structural layer rejects anything containing
    # a newline here.
    technical_signature: str = ""
    # One of GENERATION_STRATEGIES. Empty means "not yet decided" - the
    # planner proposes a value, a known concept_visual_blueprints entry may
    # override it deterministically.
    generation_strategy: str = ""
    entities: list[VisualEntity] = Field(default_factory=list)
    # Only meaningful for representation == "process"/"sequence"/"pipeline".
    steps: list[VisualStep] = Field(default_factory=list)
    # Only meaningful for representation == "state_machine". Named
    # `concept_states` (not `states`) to avoid colliding with the existing
    # `states` field above, which is schematic's own before/after panel list
    # - the two are unrelated and must stay independently addressable.
    concept_states: list[VisualState] = Field(default_factory=list)
    transitions: list[VisualTransition] = Field(default_factory=list)
    interactions: list[InteractionSpec] = Field(default_factory=list)
    # Free-text, planner-written assertions concept-level QA's semantic layer
    # grades the spec against - generated from the concept itself, not a
    # hardcoded per-concept ruleset (mirrors how `relationships` above is
    # declarative-only content, never layout).
    validation_criteria: list[str] = Field(default_factory=list)

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

        if kind == "concept_experience":
            # Not every representation needs entities (a pure step sequence
            # or state machine may have none) - usable as long as SOME
            # content structure is present, whichever the representation
            # actually needs.
            return bool(self.concept.strip()) and (
                len(self.entities) + len(self.steps) + len(self.concept_states) >= 1
            )

        labelled = [n for n in self.nodes if n.label.strip()]
        if not (2 <= len(labelled) <= 9):
            return False
        if kind == "concept_map":
            has_labelled_edge = any(e.label.strip() for e in self.edges)
            return bool(self.edges) and (has_labelled_edge or len(labelled) >= 3)
        return True
