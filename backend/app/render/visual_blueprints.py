"""Canonical semantic blueprints for well-known textbook visuals.

A blueprint is a checklist, never a layout - it contains no x/y/pixel
coordinates (positioning stays the exclusive job of
`app.render.schematic_layout`, driven by each shape's own `anchor` field).
It describes, for one named `visual_type`:

- which components a scientifically/structurally correct diagram of this
  subject needs (`required_components`) and which are optional
  (`optional_components`);
- a recommended default role/anchor/size/priority/color_role/shape_type for
  each component, used only to *fill in* whatever the model left blank (see
  `apply_blueprint_defaults`) - the model can always override any of these;
- the semantic relationships expected between components, used only for
  validation (see `app.services.diagram_qa.evaluate_blueprint`), never for
  layout.

Required components are strict; everything else here is a *default*, not a
constraint - "layout = deterministic but adaptive, colors = semantic, not
arbitrary" per the brief this exists to satisfy. An unregistered
`visual_type` (including "", the common case) is always valid input: nothing
here is ever a precondition for the generic semantic schematic system to
work exactly as it already did before this module existed - see
`get_blueprint`'s docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    label: str  # canonical display label - only a fallback if the model leaves `label` blank
    role: str = "secondary"  # "primary" | "secondary"
    color_role: str = "structure"
    anchor: str = ""  # recommended default anchor; "" lets the layout engine's own default (orbit the primary) apply
    size: str = "medium"
    priority: str = "important"  # "critical" | "important" | "optional"
    shape_type: str = "block"


@dataclass(frozen=True)
class RelationshipSpec:
    source: str
    type: str
    target: str


@dataclass(frozen=True)
class VisualBlueprint:
    visual_type: str
    title: str
    educational_purpose: str
    required_components: tuple[str, ...]
    optional_components: tuple[str, ...] = ()
    components: dict[str, ComponentSpec] = field(default_factory=dict)
    relationships: tuple[RelationshipSpec, ...] = ()

    def component(self, component_id: str) -> ComponentSpec | None:
        return self.components.get(component_id)


BLUEPRINT_REGISTRY: dict[str, VisualBlueprint] = {}


def _register(
    visual_type: str,
    title: str,
    purpose: str,
    *,
    components: list[ComponentSpec],
    relationships: list[RelationshipSpec],
    required: list[str],
    optional: list[str] = (),
) -> None:
    BLUEPRINT_REGISTRY[visual_type] = VisualBlueprint(
        visual_type=visual_type,
        title=title,
        educational_purpose=purpose,
        required_components=tuple(required),
        optional_components=tuple(optional),
        components={c.id: c for c in components},
        relationships=tuple(relationships),
    )


# ---------------------------------------------------------------------------
# Physics / engineering
# ---------------------------------------------------------------------------

_register(
    "electric_motor",
    "Simple DC Electric Motor",
    "Explain how a current-carrying coil in a magnetic field experiences a "
    "force that rotates it continuously, reversed each half-turn by the "
    "commutator and brushes.",
    components=[
        ComponentSpec("coil", "Coil", role="primary", color_role="accent", shape_type="coil", size="large", priority="critical"),
        ComponentSpec("magnet", "Magnet", color_role="structure", shape_type="block", anchor="orbit:coil", size="medium", priority="critical"),
        ComponentSpec("axle", "Axle", color_role="structure", shape_type="block", anchor="right_of:coil", size="small", priority="critical"),
        ComponentSpec("commutator", "Commutator", color_role="secondary", shape_type="block", anchor="right_of:axle", size="small", priority="critical"),
        ComponentSpec("brush", "Brush", color_role="structure", shape_type="block", anchor="right_of:commutator", size="small", priority="important"),
        ComponentSpec("battery", "Battery", color_role="positive", shape_type="block", anchor="orbit:coil", size="small", priority="optional"),
        ComponentSpec("magnetic_field", "Magnetic field", color_role="magnetic_field", shape_type="flow", anchor="orbit:coil", size="small", priority="important"),
    ],
    relationships=[
        RelationshipSpec("coil", "inside", "magnet"),
        RelationshipSpec("axle", "passes_through", "coil"),
        RelationshipSpec("coil", "attached_to", "axle"),
        RelationshipSpec("axle", "attached_to", "commutator"),
        RelationshipSpec("brush", "contacts", "commutator"),
        RelationshipSpec("commutator", "connected_to", "coil"),
    ],
    required=["coil", "magnet", "axle", "commutator", "brush"],
    optional=["battery", "magnetic_field"],
)

_register(
    "electromagnetic_induction",
    "Electromagnetic Induction",
    "Show a magnet moving relative to a coil inducing a current, detected by "
    "an ammeter/galvanometer.",
    components=[
        ComponentSpec("coil", "Coil", role="primary", color_role="accent", shape_type="coil", size="large", priority="critical"),
        ComponentSpec("magnet", "Bar magnet", color_role="structure", shape_type="block", anchor="left_of:coil", size="medium", priority="critical"),
        ComponentSpec("field_lines", "Magnetic field lines", color_role="magnetic_field", shape_type="flow", anchor="orbit:coil", size="small", priority="important"),
        ComponentSpec("ammeter", "Ammeter", color_role="secondary", shape_type="gauge", anchor="below:coil", size="small", priority="critical"),
    ],
    relationships=[
        RelationshipSpec("magnet", "flows_into", "coil"),
        RelationshipSpec("coil", "connected_to", "ammeter"),
    ],
    required=["coil", "magnet", "ammeter"],
    optional=["field_lines"],
)

_register(
    "fixed_pulley",
    "Fixed Pulley System",
    "Understand how a fixed pulley redirects the force needed to lift a load.",
    components=[
        ComponentSpec("pulley", "Pulley wheel", role="primary", color_role="structure", shape_type="circle", size="medium", priority="critical"),
        ComponentSpec("load", "Load", color_role="secondary", shape_type="block", anchor="below:pulley", size="medium", priority="critical"),
        ComponentSpec("applied_force", "Applied force", color_role="accent", shape_type="arrow", anchor="right_of:pulley", size="small", priority="important"),
        ComponentSpec("lifting_force", "Lifting force", color_role="accent", shape_type="arrow", anchor="left_of:pulley", size="small", priority="important"),
    ],
    relationships=[
        RelationshipSpec("applied_force", "points_to", "pulley"),
        RelationshipSpec("lifting_force", "points_to", "load"),
        RelationshipSpec("load", "attached_to", "pulley"),
    ],
    required=["pulley", "load"],
    optional=["applied_force", "lifting_force"],
)

_register(
    "simple_electric_circuit",
    "Simple Electric Circuit",
    "Show current flowing from a battery through a switch and a load (e.g. a "
    "bulb) and back.",
    components=[
        ComponentSpec("battery", "Battery", role="primary", color_role="positive", shape_type="block", size="medium", priority="critical"),
        ComponentSpec("bulb", "Bulb", color_role="accent", shape_type="circle", anchor="right_of:battery", size="medium", priority="critical"),
        ComponentSpec("switch", "Switch", color_role="structure", shape_type="block", anchor="above:battery", size="small", priority="important"),
        ComponentSpec("current", "Current", color_role="current", shape_type="arrow", anchor="right_of:battery", size="small", priority="important"),
    ],
    relationships=[
        RelationshipSpec("battery", "connected_to", "switch"),
        RelationshipSpec("switch", "connected_to", "bulb"),
        RelationshipSpec("current", "flows_into", "bulb"),
    ],
    required=["battery", "bulb"],
    optional=["switch", "current"],
)


# ---------------------------------------------------------------------------
# Biology
# ---------------------------------------------------------------------------

_register(
    "animal_cell",
    "Animal Cell Structure",
    "Identify the main organelles of an animal cell and their arrangement "
    "within the cell membrane.",
    components=[
        ComponentSpec("cell_membrane", "Cell membrane", role="primary", color_role="structure", shape_type="circle", size="large", priority="critical"),
        ComponentSpec("nucleus", "Nucleus", color_role="secondary", shape_type="circle", anchor="inside:cell_membrane", size="small", priority="critical"),
        ComponentSpec("mitochondria", "Mitochondria", color_role="accent", shape_type="label", anchor="orbit:cell_membrane", priority="important"),
        ComponentSpec("ribosomes", "Ribosomes", color_role="accent", shape_type="label", anchor="orbit:cell_membrane", priority="important"),
        ComponentSpec("golgi_apparatus", "Golgi apparatus", color_role="fluid", shape_type="label", anchor="orbit:cell_membrane", priority="optional"),
        ComponentSpec("endoplasmic_reticulum", "Endoplasmic reticulum", color_role="fluid", shape_type="label", anchor="orbit:cell_membrane", priority="optional"),
    ],
    relationships=[
        RelationshipSpec("nucleus", "inside", "cell_membrane"),
        RelationshipSpec("mitochondria", "inside", "cell_membrane"),
    ],
    required=["cell_membrane", "nucleus"],
    optional=["mitochondria", "ribosomes", "golgi_apparatus", "endoplasmic_reticulum"],
)

_register(
    "plant_cell",
    "Plant Cell Structure",
    "Identify the main organelles of a plant cell, including the structures "
    "an animal cell doesn't have.",
    components=[
        ComponentSpec("cell_wall", "Cell wall", role="primary", color_role="structure", shape_type="circle", size="large", priority="critical"),
        ComponentSpec("nucleus", "Nucleus", color_role="secondary", shape_type="circle", anchor="inside:cell_wall", size="small", priority="critical"),
        ComponentSpec("chloroplast", "Chloroplast", color_role="magnetic_field", shape_type="label", anchor="orbit:cell_wall", priority="critical"),
        ComponentSpec("vacuole", "Vacuole", color_role="fluid", shape_type="label", anchor="orbit:cell_wall", priority="important"),
        ComponentSpec("mitochondria", "Mitochondria", color_role="accent", shape_type="label", anchor="orbit:cell_wall", priority="optional"),
    ],
    relationships=[
        RelationshipSpec("nucleus", "inside", "cell_wall"),
        RelationshipSpec("chloroplast", "inside", "cell_wall"),
        RelationshipSpec("vacuole", "inside", "cell_wall"),
    ],
    required=["cell_wall", "nucleus", "chloroplast"],
    optional=["vacuole", "mitochondria"],
)

_register(
    "human_heart",
    "Human Heart (External Blood Flow)",
    "Show the four chambers of the heart and the major vessels carrying "
    "blood into and out of it.",
    components=[
        ComponentSpec("heart", "Heart", role="primary", color_role="structure", shape_type="circle", size="large", priority="critical"),
        ComponentSpec("right_atrium", "Right atrium", color_role="negative", shape_type="label", anchor="orbit:heart", priority="critical"),
        ComponentSpec("right_ventricle", "Right ventricle", color_role="negative", shape_type="label", anchor="orbit:heart", priority="critical"),
        ComponentSpec("left_atrium", "Left atrium", color_role="current", shape_type="label", anchor="orbit:heart", priority="critical"),
        ComponentSpec("left_ventricle", "Left ventricle", color_role="current", shape_type="label", anchor="orbit:heart", priority="critical"),
        ComponentSpec("aorta", "Aorta", color_role="fluid", shape_type="label", anchor="orbit:heart", priority="optional"),
    ],
    relationships=[
        RelationshipSpec("right_atrium", "connected_to", "right_ventricle"),
        RelationshipSpec("left_atrium", "connected_to", "left_ventricle"),
        RelationshipSpec("aorta", "connected_to", "left_ventricle"),
    ],
    required=["heart", "right_atrium", "right_ventricle", "left_atrium", "left_ventricle"],
    optional=["aorta"],
)

# ---------------------------------------------------------------------------
# Chemistry
# ---------------------------------------------------------------------------

_register(
    "water_formation",
    "Formation of Water",
    "Show hydrogen and oxygen combining through a chemical reaction to form "
    "water.",
    components=[
        ComponentSpec("hydrogen", "2H₂", role="primary", color_role="primary", shape_type="block", size="medium", priority="critical"),
        ComponentSpec("oxygen", "O₂", color_role="secondary", shape_type="block", anchor="orbit:hydrogen", size="medium", priority="critical"),
        ComponentSpec("reaction_arrow", "Reaction", color_role="accent", shape_type="arrow", anchor="right_of:hydrogen", size="small", priority="important"),
        ComponentSpec("water", "2H₂O", color_role="fluid", shape_type="block", anchor="orbit:hydrogen", size="medium", priority="critical"),
    ],
    relationships=[
        RelationshipSpec("hydrogen", "connected_to", "water"),
        RelationshipSpec("oxygen", "connected_to", "water"),
        RelationshipSpec("reaction_arrow", "points_to", "water"),
    ],
    required=["hydrogen", "oxygen", "water"],
    optional=["reaction_arrow"],
)


def get_blueprint(visual_type: str) -> VisualBlueprint | None:
    """None for "", an unrecognised value, or any input at all that isn't a
    registered key - always a safe, valid outcome. Every caller in this
    codebase (apply_blueprint_defaults, evaluate_blueprint) treats None as
    "fall back to the generic semantic schematic system", never an error."""
    return BLUEPRINT_REGISTRY.get((visual_type or "").strip().lower().replace(" ", "_"))


def apply_blueprint_defaults(spec: "DiagramSpec") -> "DiagramSpec":  # noqa: F821 - see import below
    """Fill in role/anchor/color_role/size/priority/label for any shape whose
    `id` matches a known blueprint component and who left that field blank -
    never overrides a value the model actually set. A no-op when
    `visual_type` doesn't resolve to a registered blueprint, which keeps the
    generic semantic schematic path (no blueprint at all) completely
    unaffected - see get_blueprint."""
    from app.schemas.diagram import SchematicState  # local import - avoids a schema<->render import cycle

    if spec.normalised_kind() != "schematic":
        return spec
    blueprint = get_blueprint(spec.visual_type)
    if blueprint is None:
        return spec

    def fill(shapes):
        filled = []
        for shape in shapes:
            comp = blueprint.component(shape.id)
            if comp is None:
                filled.append(shape)
                continue
            updates = {}
            if not shape.role.strip():
                updates["role"] = comp.role
            if not shape.anchor.strip() and comp.anchor:
                updates["anchor"] = comp.anchor
            if not shape.color_role.strip():
                updates["color_role"] = comp.color_role
            if not shape.size.strip():
                updates["size"] = comp.size
            if not shape.label.strip():
                updates["label"] = comp.label
            filled.append(shape.model_copy(update=updates) if updates else shape)
        return filled

    if spec.states:
        new_states = [
            SchematicState(caption=state.caption, shapes=fill(state.shapes)) for state in spec.states
        ]
        return spec.model_copy(update={"states": new_states})
    if spec.shapes:
        return spec.model_copy(update={"shapes": fill(spec.shapes)})
    return spec
