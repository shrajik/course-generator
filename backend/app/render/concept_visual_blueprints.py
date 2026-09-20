"""Canonical blueprints for `concept_experience` visuals - the entity/
interaction counterpart to `app.render.visual_blueprints`'s illustrated-
apparatus registry. Domain-agnostic by design (see the approved universal-
architecture plan): this module is pure registry/lookup machinery that never
branches on `domain` or on any subject name - only `apply_concept_blueprint_defaults`
reads a blueprint's own data to fill blanks. Adding a new concept (in any
subject) means adding a dict entry, never touching the functions below.

A blueprint is the *complete* canonical spec for a well-known concept (its
entities/steps/states, its interactions, its learning objectives and
validation criteria) - unlike the schematic registry's `ComponentSpec`
(which only fills in *blanks* the model left empty), a concept blueprint's
content is close to fixed: "Java Class and Object" always teaches the same
car-blueprint metaphor with the same three sample objects, regardless of
course. `apply_concept_blueprint_defaults` still only overrides a field the
model left blank, never something it explicitly set, matching the exact
same "model proposes, blueprint constrains" contract the schematic registry
already uses.

An unregistered `concept` (including "", the common case) is always valid
input - nothing here is ever a precondition for a concept_experience spec to
render; see `get_concept_blueprint`'s docstring.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.diagram import (
    DiagramSpec,
    InteractionSpec,
    VisualEntity,
    VisualState,
    VisualStep,
    VisualTransition,
)


@dataclass(frozen=True)
class ConceptVisualBlueprint:
    concept: str
    title: str
    learner_level: str
    core_message: str
    visual_metaphor: str
    generation_strategy: str
    # Metadata only (subject label for browsing/reuse-grouping) - never read
    # by any branching logic, same contract as DiagramSpec.domain.
    domain: str = ""
    # One of REPRESENTATION_TYPES - which renderer function handles this
    # blueprint. See DiagramSpec.representation's docstring.
    representation: str = "object"
    technical_signature: str = ""
    learning_objectives: tuple[str, ...] = ()
    entities: tuple[VisualEntity, ...] = ()
    steps: tuple[VisualStep, ...] = ()
    concept_states: tuple[VisualState, ...] = ()
    transitions: tuple[VisualTransition, ...] = ()
    interactions: tuple[InteractionSpec, ...] = ()
    validation_criteria: tuple[str, ...] = ()


CONCEPT_BLUEPRINT_REGISTRY: dict[str, ConceptVisualBlueprint] = {}


def _register(blueprint: ConceptVisualBlueprint) -> None:
    CONCEPT_BLUEPRINT_REGISTRY[blueprint.concept] = blueprint


def get_concept_blueprint(concept: str) -> ConceptVisualBlueprint | None:
    """None for "", an unrecognised value, or any input that isn't a
    registered key - always a safe, valid outcome. Every caller in this
    codebase (apply_concept_blueprint_defaults, concept_qa) treats None as
    "fall back to whatever the planner produced", never an error."""
    return CONCEPT_BLUEPRINT_REGISTRY.get((concept or "").strip().lower().replace(" ", "_"))


def apply_concept_blueprint_defaults(spec: DiagramSpec) -> DiagramSpec:
    """Fill in whatever the planner left blank on a concept_experience spec
    from its known blueprint - never overrides a value the model actually
    set. A no-op when `concept` doesn't resolve to a registered blueprint,
    which keeps an unknown/future concept fully usable via whatever the
    planner produced on its own, exactly as `apply_blueprint_defaults` does
    for schematic."""
    if spec.normalised_kind() != "concept_experience":
        return spec
    blueprint = get_concept_blueprint(spec.concept)
    if blueprint is None:
        return spec

    updates: dict[str, object] = {}
    if not spec.title.strip():
        updates["title"] = blueprint.title
    if not spec.domain.strip():
        updates["domain"] = blueprint.domain
    if not spec.representation.strip():
        updates["representation"] = blueprint.representation
    if not spec.learner_level.strip():
        updates["learner_level"] = blueprint.learner_level
    if not spec.core_message.strip():
        updates["core_message"] = blueprint.core_message
    if not spec.visual_metaphor.strip():
        updates["visual_metaphor"] = blueprint.visual_metaphor
    if not spec.technical_signature.strip():
        updates["technical_signature"] = blueprint.technical_signature
    if not spec.generation_strategy.strip():
        updates["generation_strategy"] = blueprint.generation_strategy
    if not spec.learning_objectives:
        updates["learning_objectives"] = list(blueprint.learning_objectives)
    if not spec.entities:
        updates["entities"] = [entity.model_copy() for entity in blueprint.entities]
    if not spec.steps:
        updates["steps"] = [step.model_copy() for step in blueprint.steps]
    if not spec.concept_states:
        updates["concept_states"] = [state.model_copy() for state in blueprint.concept_states]
    if not spec.transitions:
        updates["transitions"] = [transition.model_copy() for transition in blueprint.transitions]
    if not spec.interactions:
        updates["interactions"] = [interaction.model_copy() for interaction in blueprint.interactions]
    if not spec.validation_criteria:
        updates["validation_criteria"] = list(blueprint.validation_criteria)

    return spec.model_copy(update=updates) if updates else spec


# ---------------------------------------------------------------------------
# First POC (see the approved plan, section F) - the only registered concept
# in this phase. Everything above this line is generic; only this entry is
# Java-specific.
# ---------------------------------------------------------------------------

_register(
    ConceptVisualBlueprint(
        concept="java_class_and_object",
        domain="java",
        representation="object",
        title="Class and Object",
        learner_level="beginner",
        core_message="A class is a blueprint; an object is a real thing built from it.",
        visual_metaphor="a car blueprint (the Class) and real cars built from it (Objects)",
        generation_strategy="deterministic_interactive_html",
        technical_signature="class Car { color; model; speed; start(); stop(); drive(); }",
        learning_objectives=(
            "Explain that a class is a blueprint/template, not a real thing by itself",
            "Explain that an object is a real instance created from a class",
            "Identify that different objects of the same class can hold different property values",
            "Connect the informal blueprint/car metaphor to the actual Java syntax (fields, methods, new)",
        ),
        entities=(
            VisualEntity(
                id="car_class", role="template", label="Car (Class)",
                properties={"color": "", "model": "", "speed": ""},
                actions=["start()", "stop()", "drive()"],
                icon="🏭", color_role="primary",
            ),
            VisualEntity(
                id="car1", role="instance", label="car1",
                properties={"color": "Red", "model": "X1", "speed": "80"},
                icon="🚗", color_role="accent",
            ),
            VisualEntity(
                id="car2", role="instance", label="car2",
                properties={"color": "Blue", "model": "X2", "speed": "95"},
                icon="🚙", color_role="secondary",
            ),
            VisualEntity(
                id="car3", role="instance", label="car3",
                properties={"color": "Green", "model": "X3", "speed": "70"},
                icon="🚘", color_role="fluid",
            ),
        ),
        validation_criteria=(
            "The Class is visually distinct from every Object (e.g. blueprint/factory styling vs. real-object styling)",
            "At least two Objects are shown with genuinely different property values",
            "It is visually obvious the Objects were created FROM the Class (a clear direction/flow: class -> objects, matching the new Car(...) cue)",
            "The short technical_signature line is legible and matches the entities' properties/actions - no full sentences, no multi-line code",
            "No paragraph of body text is required to understand that class=blueprint, object=instance",
            "Every property/action label is short (<=3 words) - no full sentences inside entity cards",
            "Clicking the class and clicking an object produce visually distinguishable responses",
        ),
    )
)
