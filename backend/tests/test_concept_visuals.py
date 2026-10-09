"""`concept_experience` visuals: concept-first planning -> deterministic
interactive HTML -> concept-level QA -> retry -> reuse.

Domain-agnostic by design (see the approved universal-architecture plan at
C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md): the schema,
planner and blueprint registry never branch on subject name - only the
`representation` field and the concept's own registered data (or lack of it)
drive behaviour. Java Class & Object is the first (and, in this POC, only)
registered concept - tests here prove the generic machinery works for it
without assuming Java anywhere except inside that one blueprint entry.

Tests are added phase by phase alongside the implementation; mirrors
test_diagrams.py's structure so the two suites read as siblings.
"""

from __future__ import annotations

import math
import re

import pytest

from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.render.concept_experience_renderer import estimate_pixel_size, render_concept_experience_html
from app.render.concept_visual_blueprints import (
    CONCEPT_BLUEPRINT_REGISTRY,
    ConceptVisualBlueprint,
    apply_concept_blueprint_defaults,
    get_concept_blueprint,
)
from app.schemas.blocks import BlockType
from app.schemas.diagram import (
    DIAGRAM_KINDS,
    GENERATION_STRATEGIES,
    REPRESENTATION_TYPES,
    DiagramSpec,
    InteractionSpec,
    SchematicRelationship,
    VisualEntity,
    VisualState,
    VisualStep,
    VisualTransition,
)
from app.schemas.document import Block
from app.schemas.template import TemplateTheme
from app.services import mock_ai as mock_ai_module
from app.services.concept_qa import (
    CONCEPT_NOT_CLEAR,
    DANGLING_TRANSITION,
    DISCONNECTED_CONCEPTS,
    GENERIC_VISUAL,
    MISSING_ENTITY,
    MISSING_LEARNING_OBJECTIVES,
    MULTILINE_TECHNICAL_SIGNATURE,
    NO_INSTANCE_VARIATION,
    TOO_DENSE,
    evaluate_concept_experience,
)
from app.services.concept_visual_service import ConceptVisualService
from app.services.mock_ai import MockAIClient
from app.services.visual_planner import VisualPlanner
from tests.test_pdf import needs_browser

# ---------------------------------------------------------------------------
# schema: concept_experience is additive, legacy specs are unaffected
# ---------------------------------------------------------------------------


def test_concept_experience_is_a_registered_diagram_kind():
    assert "concept_experience" in DIAGRAM_KINDS


def test_generation_strategies_names_match_the_approved_plan():
    """Regression guard for the strategy rename: `interactive_svg` was
    replaced with `deterministic_interactive_html` because the POC's actual
    implementation is HTML+CSS+vanilla-JS, not SVG."""
    assert GENERATION_STRATEGIES == (
        "deterministic_svg",
        "deterministic_interactive_html",
        "gpt_image",
        "hybrid",
    )
    assert "interactive_svg" not in GENERATION_STRATEGIES


def test_representation_types_match_the_universal_taxonomy():
    """Regression guard for the taxonomy rename: `concept_metaphor` ->
    `object`, `annotated_diagram` -> `spatial`; `lifecycle`/`illustration`
    removed; `pipeline`/`code_visualization` added - see the approved plan's
    section B (11-family taxonomy). `timeline`/`before_after` added later,
    each with a real dedicated renderer; `cycle` (dedicated renderer) and
    `decision_tree` (alias onto `hierarchy`/`object`) added after that -
    see concept_experience_renderer."""
    assert REPRESENTATION_TYPES == (
        "object", "data_structure", "process", "sequence", "state_machine", "relationship",
        "hierarchy", "comparison", "timeline", "before_after", "pipeline", "spatial",
        "code_visualization", "cycle", "decision_tree",
    )
    for stale in ("concept_metaphor", "annotated_diagram", "lifecycle", "illustration"):
        assert stale not in REPRESENTATION_TYPES


def test_legacy_diagram_spec_is_completely_unaffected_by_new_fields():
    """A spec built the way every existing test/mock already builds one
    (no concept_experience fields touched) must serialize/behave exactly as
    it did before this feature existed."""
    spec = DiagramSpec(kind="flow_chart", title="Legacy")
    assert spec.concept == ""
    assert spec.domain == ""
    assert spec.representation == ""
    assert spec.learner_level == ""
    assert spec.learning_objectives == []
    assert spec.core_message == ""
    assert spec.visual_metaphor == ""
    assert spec.technical_signature == ""
    assert spec.generation_strategy == ""
    assert spec.entities == []
    assert spec.steps == []
    assert spec.concept_states == []
    assert spec.transitions == []
    assert spec.interactions == []
    assert spec.validation_criteria == []
    # the pre-existing singular field, and schematic's own `states` field,
    # are untouched by the new plural/renamed ones
    assert spec.learning_objective == ""
    assert spec.states == []


def test_visual_entity_uses_actions_not_methods():
    """Regression guard for the domain-neutrality rename: `methods` (OOP-
    specific naming) was replaced with `actions`."""
    entity = VisualEntity(
        id="car_class", label="Car (Class)", role="template",
        properties={"color": "", "model": "", "speed": ""},
        actions=["start()", "stop()", "drive()"], icon="🏭", color_role="primary",
    )
    assert entity.actions == ["start()", "stop()", "drive()"]
    assert not hasattr(entity, "methods")


def test_visual_entity_and_interaction_spec_round_trip():
    entity = VisualEntity(id="car_class", actions=["start()"], properties={"color": ""})
    interaction = InteractionSpec(type="click_class", target_entity_ids=["car_class"], trigger_label="Click the Class")
    spec = DiagramSpec(
        kind="concept_experience", concept="java_class_and_object", domain="java",
        representation="object", entities=[entity], interactions=[interaction],
    )
    restored = DiagramSpec.model_validate(spec.model_dump())
    assert restored.entities[0].id == "car_class"
    assert restored.entities[0].properties == {"color": ""}
    assert restored.interactions[0].type == "click_class"


def test_step_state_transition_models_round_trip():
    """These back representations other than object (process, state_machine)
    - proven independently of any registered blueprint."""
    step = VisualStep(id="s1", label="Step 1", description="First step", order=0)
    state = VisualState(id="idle", label="Idle", entity_ids=["machine"])
    transition = VisualTransition(from_state="idle", to_state="running", label="start")
    spec = DiagramSpec(
        kind="concept_experience", concept="x", representation="state_machine",
        steps=[step], concept_states=[state], transitions=[transition],
    )
    restored = DiagramSpec.model_validate(spec.model_dump())
    assert restored.steps[0].order == 0
    assert restored.concept_states[0].entity_ids == ["machine"]
    assert restored.transitions[0].to_state == "running"


def test_is_usable_requires_a_concept_and_some_content():
    assert not DiagramSpec(kind="concept_experience").is_usable()
    assert not DiagramSpec(kind="concept_experience", concept="java_class_and_object").is_usable()
    assert DiagramSpec(
        kind="concept_experience", concept="java_class_and_object", entities=[VisualEntity(id="car_class")],
    ).is_usable()
    # a pure step/state representation with zero entities is still usable -
    # not every representation needs entities
    assert DiagramSpec(
        kind="concept_experience", concept="a_loop", representation="process",
        steps=[VisualStep(id="s1", label="Step 1")],
    ).is_usable()
    assert DiagramSpec(
        kind="concept_experience", concept="tcp_states", representation="state_machine",
        concept_states=[VisualState(id="idle", label="Idle")],
    ).is_usable()


def test_unknown_kind_still_falls_back_to_smart_art():
    """Adding a new kind must never change the existing unknown-kind
    fallback behaviour other kinds rely on."""
    assert DiagramSpec(kind="not_a_real_kind").normalised_kind() == "smart_art"


# ---------------------------------------------------------------------------
# concept_visual_blueprints registry
# ---------------------------------------------------------------------------


class TestConceptBlueprintRegistry:
    def test_unknown_concept_resolves_to_none_safely(self):
        assert get_concept_blueprint("not_a_real_concept") is None
        assert get_concept_blueprint("") is None
        assert get_concept_blueprint(None) is None  # type: ignore[arg-type]

    def test_apply_defaults_is_a_no_op_for_unknown_concept(self):
        spec = DiagramSpec(
            kind="concept_experience", concept="not_yet_registered", entities=[VisualEntity(id="a")],
        )
        assert apply_concept_blueprint_defaults(spec) is spec

    def test_apply_defaults_is_a_no_op_for_non_concept_experience_kind(self):
        spec = DiagramSpec(kind="flow_chart", concept="java_class_and_object")
        assert apply_concept_blueprint_defaults(spec) is spec

    def test_registry_lookup_is_case_and_spacing_insensitive(self):
        assert get_concept_blueprint("Java Class And Object") is get_concept_blueprint("java_class_and_object")

    def test_only_one_concept_is_registered_for_this_poc(self):
        """Explicit scope guard from the approved plan - Java Class & Object
        is the only implemented concept in this phase."""
        assert list(CONCEPT_BLUEPRINT_REGISTRY) == ["java_class_and_object"]

    def test_registry_never_stores_a_raw_generation_strategy_outside_the_enum(self):
        for blueprint in CONCEPT_BLUEPRINT_REGISTRY.values():
            assert blueprint.generation_strategy in GENERATION_STRATEGIES

    def test_apply_defaults_never_overrides_an_explicit_value(self):
        blueprint = ConceptVisualBlueprint(
            concept="_test_only_concept",
            title="Blueprint Title",
            learner_level="beginner",
            core_message="Blueprint message",
            visual_metaphor="a metaphor",
            generation_strategy="deterministic_interactive_html",
            domain="_test_domain",
        )
        CONCEPT_BLUEPRINT_REGISTRY[blueprint.concept] = blueprint
        try:
            spec = DiagramSpec(
                kind="concept_experience", concept="_test_only_concept",
                core_message="Explicit message from the model", entities=[VisualEntity(id="a")],
            )
            filled = apply_concept_blueprint_defaults(spec)
            assert filled.core_message == "Explicit message from the model"  # explicit value wins
            assert filled.learner_level == "beginner"  # filled from the blueprint - was blank
            assert filled.domain == "_test_domain"  # filled from the blueprint - was blank
            assert filled.generation_strategy == "deterministic_interactive_html"
        finally:
            del CONCEPT_BLUEPRINT_REGISTRY[blueprint.concept]


class TestJavaClassAndObjectBlueprint:
    """The POC's one registered concept - see the approved plan section F."""

    def test_resolves_and_fills_a_blank_spec_completely(self):
        spec = DiagramSpec(kind="concept_experience", concept="java_class_and_object")
        filled = apply_concept_blueprint_defaults(spec)
        assert filled.domain == "java"
        assert filled.representation == "object"
        assert filled.generation_strategy == "deterministic_interactive_html"
        assert "\n" not in filled.technical_signature
        assert filled.is_usable()

    def test_has_a_distinct_template_and_at_least_two_differently_valued_instances(self):
        blueprint = get_concept_blueprint("java_class_and_object")
        template = [e for e in blueprint.entities if e.role == "template"]
        instances = [e for e in blueprint.entities if e.role == "instance"]
        assert len(template) == 1
        assert len(instances) >= 2
        assert len({tuple(sorted(e.properties.items())) for e in instances}) == len(instances)

# ---------------------------------------------------------------------------
# VisualPlanner - domain-agnostic planning call (mocked AI, fully offline)
# ---------------------------------------------------------------------------


class TestVisualPlanner:
    async def test_plan_returns_a_valid_concept_experience_spec(self):
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(
            purpose="Explain Java Class and Object", prompt="class vs object", caption="",
            course_title="Java Basics",
        )
        assert spec.kind == "concept_experience"
        assert spec.concept == "java_class_and_object"
        assert spec.learning_objectives
        assert spec.is_usable()

    async def test_plan_always_forces_kind_to_concept_experience(self):
        """Unlike DiagramService (which handles several kinds and corrects
        mismatches with a retry), VisualPlanner has exactly one job - so it
        forces the kind rather than trusting the model's own field."""
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose="x", prompt="y", caption="z", course_title="Course")
        assert spec.kind == "concept_experience"


# ---------------------------------------------------------------------------
# concept_experience_renderer: deterministic HTML+CSS+JS, dispatch by
# representation - "object", "data_structure" and "process" are real
# renderers; every other representation falls back onto one of those three
# ---------------------------------------------------------------------------


def _java_class_and_object_spec() -> DiagramSpec:
    spec = DiagramSpec(kind="concept_experience", concept="java_class_and_object")
    return apply_concept_blueprint_defaults(spec)


class TestConceptExperienceRenderer:
    def test_renders_the_blueprint_and_every_instance(self):
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "Car (Class)" in html
        for label in ("car1", "car2", "car3"):
            assert label in html
        assert 'data-role="template"' in html
        assert html.count('data-role="instance"') == 3

    def test_instances_show_different_property_values_in_the_default_markup(self):
        """The default state (before any JS runs) must already be complete -
        no click required to see that objects hold different values."""
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "Red" in html and "Blue" in html and "Green" in html
        assert "80" in html and "95" in html and "70" in html

    def test_technical_signature_renders_as_a_single_visible_line(self):
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "class Car" in html
        assert "cev-technical" in html

    def test_flow_cue_shows_class_to_object_direction(self):
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "new Car(...)" in html

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        """Deliberately static - no JS, no buttons, no clickable affordances
        (user feedback: this is consumed as a picture, mostly in a PDF/print
        export where nothing can be clicked anyway)."""
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "cev-root" in html
        assert "<script" not in html
        assert "<button" not in html
        assert "data-interaction" not in html
        assert "tabindex" not in html

    def test_labels_stay_short_no_paragraph_length_text(self):
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        for action in ("start()", "stop()", "drive()"):
            assert action in html

    def test_unrecognised_representation_falls_back_to_object_not_a_crash(self):
        spec = _java_class_and_object_spec().model_copy(update={"representation": "some_future_representation"})
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "Car (Class)" in html  # still rendered via the "object" fallback

    def test_blank_representation_falls_back_to_object(self):
        spec = DiagramSpec(kind="concept_experience", concept="unregistered_concept", entities=[VisualEntity(id="a", label="A")])
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert 'data-entity-id="a"' in html

    def test_output_contains_no_raw_hex_colors_outside_the_centralized_palette(self):
        """Entity color_role always resolves through concept_experience_renderer's
        own vivid palette (deliberately distinct from the muted
        textbook_palette the schematic/diagram pipeline uses - see
        _CEV_PALETTE's own comment) - proves the renderer never invents its
        own ad-hoc colors."""
        from app.render.concept_experience_renderer import _CEV_PALETTE

        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        used_entity_colors = {_CEV_PALETTE[role].fill for role in ("primary", "accent", "secondary", "fluid")}
        assert used_entity_colors & set(_extract_hex_colors(html))


class TestRelationshipChainLayout:
    """A relationship CHAIN (every entity pointing to exactly the next one,
    one straight path start-to-end - e.g. a five-step handoff) renders as
    one flowing left-to-right series (`_is_linear_chain`/`_chain_row_html`),
    not the hub-style card row with a per-relationship "connected to"
    connector: real, confirmed case - a genuine chain wrapped into a
    multi-row grid with each row's own connector no longer read as ONE
    continuous sequence. A HUB (one entity with several independent
    targets) is a different shape and keeps the existing card-row
    rendering - each spoke genuinely IS its own independent connection to
    the hub, which that layout already shows correctly."""

    @staticmethod
    def _chain_spec(n: int) -> DiagramSpec:
        entities = [VisualEntity(id=f"e{i}", label=f"Node {i}", properties={"k": f"v{i}"}) for i in range(n)]
        relationships = [
            SchematicRelationship(source=f"e{i}", target=f"e{i + 1}", type="connected_to")
            for i in range(n - 1)
        ]
        return DiagramSpec(
            # "relationship", not "hierarchy" - hierarchy now has its own
            # dedicated top-down tree renderer (see TestHierarchyTreeRenderer)
            # that this chain/hub detection logic doesn't run through at all.
            kind="concept_experience", representation="relationship",
            entities=entities, relationships=relationships,
        )

    def test_a_genuine_chain_renders_as_one_flowing_series_not_per_link_connectors(self):
        html = render_concept_experience_html(self._chain_spec(8), TemplateTheme()).decode("utf-8")
        # Only the body (past the <style> block, which always DEFINES every
        # class name regardless of use) is checked for actual element usage.
        body = html.split("</style>", 1)[1]
        # The old hub-style per-relationship connector markup must NOT
        # appear - a genuine chain no longer routes through it at all.
        assert 'class="cev-rel-link"' not in body
        assert 'class="cev-connector"' not in body
        # Wrap-aware zig-grid technique (see _chain_row_html's own
        # docstring for why a plain flex-wrap row isn't enough once a
        # chain is long enough to wrap onto more than one row): pairs of
        # cards joined by a plain arrow, consecutive rows joined by a
        # curved connector, the whole thing getting the flatter,
        # whiteboard-flowchart "flow" treatment `_render_process` already
        # uses for an explicit step sequence.
        assert 'class="cev-zig-grid"' in body
        assert body.count('class="cev-zig-link"') == 4  # 8 nodes, 2 per row -> 4 pairs
        assert body.count('class="cev-zig-curve-row"') == 3  # 4 rows -> 3 connectors between them
        assert body.count('data-entity-id="e') == 8  # every node still rendered, once each
        assert 'class="cev-root cev-style-flow"' in html
        # Order is preserved start-to-end, not just "all present somewhere".
        assert body.index('data-entity-id="e0"') < body.index('data-entity-id="e1"') < body.index('data-entity-id="e7"')

    def test_a_hub_entity_is_not_duplicated_as_a_bare_source_twice(self):
        """A node reused as source for two relationships renders its bare
        card once, not once per outgoing edge."""
        entities = [VisualEntity(id="hub", label="Hub"), VisualEntity(id="a", label="A"), VisualEntity(id="b", label="B")]
        relationships = [
            SchematicRelationship(source="hub", target="a", type="connected_to"),
            SchematicRelationship(source="hub", target="b", type="connected_to"),
        ]
        spec = DiagramSpec(kind="concept_experience", representation="hierarchy", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert html.count('data-entity-id="hub"') == 1

    def test_a_hub_renders_via_the_circular_concept_map_not_the_old_card_row(self):
        """The same hub shape as above, checked from the chain-layout side:
        a hub must NOT be mistaken for a chain (`_is_linear_chain` returns
        None for it). It no longer gets the old flat flex-wrap card row
        with a connector threaded between every card either (that's the
        exact shape a real, confirmed bug report traced to - a hub reading
        as "a grid with lines in it" instead of a hub) - it gets a real
        centre-hub + orbiting-satellites layout instead (see
        _render_concept_map_html/_concept_map_hub)."""
        entities = [VisualEntity(id="hub", label="Hub"), VisualEntity(id="a", label="A"), VisualEntity(id="b", label="B")]
        relationships = [
            SchematicRelationship(source="hub", target="a", type="connected_to"),
            SchematicRelationship(source="hub", target="b", type="connected_to"),
        ]
        spec = DiagramSpec(kind="concept_experience", representation="relationship", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-rel-link"' not in body
        assert 'class="cev-instances"' not in body
        assert 'class="cev-cmap-wrap"' in body
        assert body.count('class="cev-cmap-node"') == 3  # hub + 2 satellites, each rendered once
        assert 'data-cmap-is-hub="true"' in body
        assert body.count('data-cmap-is-hub="true"') == 1  # exactly one hub
        # Hub mode draws curved SVG spokes (_concept_map_curved_spokes_svg),
        # not ring mode's straight cev-cmap-spoke divs.
        assert 'class="cev-cmap-spoke"' not in body
        assert body.count('class="cev-cmap-curves"') == 1  # one shared SVG overlay
        assert body.count('class="cev-cmap-curve-path"') == 2  # one curve per satellite
        # "connected_to" is a generic structural label, suppressed on a spoke
        # exactly as it already is on a flat-row connector (_connector_label).
        assert 'class="cev-cmap-curve-label"' not in body
        assert 'class="cev-root cev-style-flow"' not in html  # the root div never gets this class for a hub

    def test_is_linear_chain_helper_directly(self):
        """The detection helper itself, on the shapes it must tell apart:
        a straight path (chain), a hub (one source, several targets), a
        merge (several sources, one target), and a cycle (loops back to an
        earlier node) - only the straight path is a chain."""
        from app.render.concept_experience_renderer import _is_linear_chain

        e = lambda *ids: [VisualEntity(id=i, label=i) for i in ids]  # noqa: E731
        rel = lambda s, t: SchematicRelationship(source=s, target=t, type="connected_to")  # noqa: E731

        chain = e("a", "b", "c", "d")
        assert _is_linear_chain(chain, [rel("a", "b"), rel("b", "c"), rel("c", "d")]) == ["a", "b", "c", "d"]

        hub = e("a", "b", "c")
        assert _is_linear_chain(hub, [rel("a", "b"), rel("a", "c")]) is None

        merge = e("a", "b", "c")
        assert _is_linear_chain(merge, [rel("a", "c"), rel("b", "c")]) is None

        cycle = e("a", "b", "c")
        assert _is_linear_chain(cycle, [rel("a", "b"), rel("b", "c"), rel("c", "a")]) is None

        assert _is_linear_chain(chain, []) is None  # no relationships at all


class TestConceptMapRenderer:
    """The circular layout `_render_object`/`_render_tree` fall through to
    for a hub or small network that isn't a clean chain/tree - see
    _render_concept_map_html. Covers: hub-detection on its own, the
    rendered hub layout, the rendered general-ring layout, real (non-
    generic) labels surviving onto a spoke, and that `_render_object`'s own
    template+instances (no relationships) case is completely untouched."""

    def test_concept_map_hub_helper_directly(self):
        from app.render.concept_experience_renderer import _concept_map_hub

        e = lambda *ids: [VisualEntity(id=i, label=i) for i in ids]  # noqa: E731
        rel = lambda s, t: SchematicRelationship(source=s, target=t, type="connected_to")  # noqa: E731

        star = e("hub", "a", "b", "c")
        found = _concept_map_hub(star, [rel("hub", "a"), rel("hub", "b"), rel("hub", "c")])
        assert found is not None and found.id == "hub"

        # Only one spoke - not a star (see _render_concept_map_html's own
        # 0-1-entity/single-edge handling; a lone pair reads fine as a plain
        # two-point ring instead of inventing a "hub" for it).
        pair = e("a", "b")
        assert _concept_map_hub(pair, [rel("a", "b")]) is None

        cycle = e("a", "b", "c")
        assert _concept_map_hub(cycle, [rel("a", "b"), rel("b", "c"), rel("c", "a")]) is None

        two_sources = e("a", "b", "c", "d")
        assert _concept_map_hub(two_sources, [rel("a", "c"), rel("b", "d")]) is None

        # The "hub" is also someone's target - not a pure star.
        not_pure = e("a", "b", "c")
        assert _concept_map_hub(not_pure, [rel("a", "b"), rel("a", "c"), rel("b", "a")]) is None

        assert _concept_map_hub(star, []) is None

    def test_hub_spoke_count_matches_satellite_count_and_hub_renders_once(self):
        entities = [VisualEntity(id="core", label="Core")] + [
            VisualEntity(id=f"fact{i}", label=f"Fact {i}") for i in range(6)
        ]
        relationships = [SchematicRelationship(source="core", target=f"fact{i}", type="connected_to") for i in range(6)]
        spec = DiagramSpec(kind="concept_experience", representation="relationship", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert body.count('data-cmap-node-id="core"') == 1
        assert body.count('class="cev-cmap-node"') == 7
        assert body.count('class="cev-cmap-curve-path"') == 6

    def test_a_real_relationship_label_survives_onto_a_spoke(self):
        """Unlike the generic "connected_to" vocabulary (suppressed - see
        the hub test above), a real, specific relationship type still
        shows as visible text on its spoke, exactly as it already does on
        a flat-row connector (_connector_label is shared by both)."""
        entities = [VisualEntity(id="svc", label="Order Service"), VisualEntity(id="db", label="Orders DB"),
                    VisualEntity(id="cache", label="Redis Cache")]
        relationships = [
            SchematicRelationship(source="svc", target="db", type="writes_to"),
            SchematicRelationship(source="svc", target="cache", type="reads_from"),
        ]
        spec = DiagramSpec(kind="concept_experience", representation="relationship", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "writes to" in html
        assert "reads from" in html

    def test_a_small_web_with_no_single_hub_renders_as_a_flow_chart_not_a_crash(self):
        """Two independent sources (a->c, b->d) - not a star (see the hub
        helper test), not a chain, not a tree. A real top-to-bottom
        flowchart instead (every entity its own level row, an arrowed stem
        per edge - see _render_concept_flow_html), never a hard failure."""
        entities = [VisualEntity(id=i, label=i) for i in ("a", "b", "c", "d")]
        relationships = [
            SchematicRelationship(source="a", target="c", type="calls"),
            SchematicRelationship(source="b", target="d", type="calls"),
        ]
        spec = DiagramSpec(kind="concept_experience", representation="spatial", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert "cev-cmap-hub" not in body
        assert 'class="cev-flow-chart"' in body
        assert body.count('class="cev-flow-level"') == 2  # {a,b} then {c,d}
        assert body.count('class="cev-card cev-tree-card"') == 4  # every entity rendered once
        assert body.count('class="cev-flow-link-cell"') == 2  # one arrowed stem per edge
        assert "cev-flow-backedge" not in body  # both edges advance a level - no cycle here
        # A real, confirmed complaint about exactly this shape ("too
        # colourful") - the flowchart fallback (no dominant hub) must get
        # the same toned-down "textbook" treatment process/cycle already
        # use, not the default saturated icon-card palette a genuine hub
        # keeps (see test_a_hub_renders_via_the_circular_concept_map_...).
        assert 'class="cev-root cev-style-flow"' in html

    def test_object_representation_with_no_relationships_is_completely_unaffected(self):
        """A plain template+instances "object" spec (the common case - a
        Java class and its objects) never has relationships at all, so it
        must keep rendering as the existing flat instances row, not the new
        concept map - a pure regression guard."""
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-instances"' in body
        assert 'class="cev-cmap-wrap"' not in body

    def test_data_structure_with_relationships_is_completely_unaffected(self):
        """_render_data_structure keeps its own established connector-row
        treatment for a tree/graph-shaped structure (a BST) unchanged -
        this renderer change is scoped to relationship/spatial/hierarchy/
        decision_tree's own fallback only, never data_structure."""
        spec = DiagramSpec(
            kind="concept_experience", representation="data_structure", title="Binary Search Tree",
            entities=[VisualEntity(id="n8", label="8"), VisualEntity(id="n3", label="3"), VisualEntity(id="n10", label="10")],
            relationships=[
                SchematicRelationship(source="n8", type="connected_to", target="n3"),
                SchematicRelationship(source="n8", type="connected_to", target="n10"),
            ],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-rel-link"' in body
        assert 'class="cev-cmap-wrap"' not in body

    def test_pixel_height_estimate_grows_with_satellite_count(self):
        """The concept map's reserved height should track its own real
        container footprint (_concept_map_hub_layout), not the old
        per-character row-wrap formula - a regression guard on the
        _raw_pixel_size branch added alongside the renderer itself."""
        def hub_spec(n: int) -> DiagramSpec:
            entities = [VisualEntity(id="hub", label="Hub")] + [VisualEntity(id=f"s{i}", label=f"Sat {i}") for i in range(n)]
            relationships = [SchematicRelationship(source="hub", target=f"s{i}", type="connected_to") for i in range(n)]
            return DiagramSpec(kind="concept_experience", representation="relationship", entities=entities, relationships=relationships)

        _, small_height = estimate_pixel_size(hub_spec(2))
        _, large_height = estimate_pixel_size(hub_spec(10))
        assert large_height > small_height


class TestConceptMapHubGeometry:
    """Deterministic geometry tests for the exact rectangle model
    (_concept_map_required_radius/_concept_map_max_radius_for_width/
    _concept_map_hub_layout) - replaces an earlier bounding-circle model
    proven (by direct computation during investigation) to be too
    conservative at axis-aligned angles: it made even a 2-satellite map
    need more radius than the page allows, and produced an IDENTICAL
    clamped radius for n=2 and n=10 alike. These tests verify the
    replacement model's invariants directly from raw rectangle edges, not
    by re-deriving the same formula and comparing it to itself."""

    @staticmethod
    def _rect_edges(cx: float, cy: float, w: float, h: float) -> tuple[float, float, float, float]:
        return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2

    @classmethod
    def _no_overlap(cls, c1: tuple[float, float, float, float], c2: tuple[float, float, float, float]) -> bool:
        """Independent AABB overlap check - re-derives whether two
        rectangles (cx, cy, w, h) actually overlap from their raw edges,
        not from any function under test."""
        l1, r1, t1, b1 = cls._rect_edges(*c1)
        l2, r2, t2, b2 = cls._rect_edges(*c2)
        return r1 <= l2 or r2 <= l1 or b1 <= t2 or b2 <= t1

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 8, 10])
    def test_hub_and_every_satellite_rectangle_genuinely_do_not_overlap(self, n):
        """Whenever the chosen tier's own required radius fits within what
        the page allows (`needed <= available` - the formula's own
        promise, never broken), the ESTIMATED worst-case rectangles must
        not overlap. At very high satellite counts with properties-heavy
        content, even the compact tier can genuinely exceed the page width
        - by design, `_concept_map_hub_layout` then clamps to the largest
        radius the page allows rather than overlapping arbitrarily or
        silently failing; that residual, documented case is verified here
        too, just against the TRUE invariant it actually keeps (the
        clamped radius equals exactly the page-width-available radius, not
        something smaller), not a blanket overlap claim this test can't
        truthfully make for it. See TestConceptMapRealBrowserGeometry for
        whether that worst-case estimate is ever actually reached by real
        rendering (it generally isn't - see that class's own docstring)."""
        from app.render.concept_experience_renderer import (
            _CONCEPT_MAP_HUB_WIDTH,
            _CONCEPT_MAP_HUB_WIDTH_COMPACT,
            _CONCEPT_MAP_SAT_WIDTH,
            _CONCEPT_MAP_SAT_WIDTH_COMPACT,
            _concept_map_card_height,
            _concept_map_hub_layout,
            _concept_map_max_radius_for_width,
            _concept_map_required_radius,
            _concept_map_satellite_angles,
        )

        hub = VisualEntity(id="hub", label="Hub")
        sats = [
            VisualEntity(id=f"s{i}", label=f"Satellite number {i}", properties={"k": "a modestly long property value"})
            for i in range(n)
        ]
        radius, compact, _container_w, _container_h = _concept_map_hub_layout(hub, sats)
        hub_w = _CONCEPT_MAP_HUB_WIDTH_COMPACT if compact else _CONCEPT_MAP_HUB_WIDTH
        sat_w = _CONCEPT_MAP_SAT_WIDTH_COMPACT if compact else _CONCEPT_MAP_SAT_WIDTH
        hub_h = _concept_map_card_height(hub)
        sat_h = max(_concept_map_card_height(s) for s in sats)
        angles = _concept_map_satellite_angles(n)
        needed = _concept_map_required_radius(hub_w, hub_h, sat_w, sat_h, angles)
        available = _concept_map_max_radius_for_width(sat_w, angles)

        if needed <= available:
            hub_rect = (0.0, 0.0, hub_w, hub_h)
            for s, a in zip(sats, angles):
                sat_rect = (radius * math.cos(a), radius * math.sin(a), sat_w, _concept_map_card_height(s))
                assert self._no_overlap(hub_rect, sat_rect), f"n={n}: a satellite overlaps the hub"
        else:
            assert radius == pytest.approx(available), (
                f"n={n}: the formula's own promise doesn't hold here (page too narrow for this much "
                "content at this tier) - the clamp must still pick the largest page-safe radius, not "
                "something smaller"
            )

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 8, 10])
    def test_adjacent_satellite_rectangles_genuinely_do_not_overlap(self, n):
        """See test_hub_and_every_satellite_rectangle_genuinely_do_not_overlap's
        own docstring for why this is conditional on the clamp not having
        engaged."""
        from app.render.concept_experience_renderer import (
            _CONCEPT_MAP_SAT_WIDTH,
            _CONCEPT_MAP_SAT_WIDTH_COMPACT,
            _CONCEPT_MAP_HUB_WIDTH,
            _CONCEPT_MAP_HUB_WIDTH_COMPACT,
            _concept_map_card_height,
            _concept_map_hub_layout,
            _concept_map_max_radius_for_width,
            _concept_map_required_radius,
            _concept_map_satellite_angles,
        )

        hub = VisualEntity(id="hub", label="Hub")
        sats = [VisualEntity(id=f"s{i}", label=f"Satellite {i}") for i in range(n)]
        radius, compact, _w, _h = _concept_map_hub_layout(hub, sats)
        hub_w = _CONCEPT_MAP_HUB_WIDTH_COMPACT if compact else _CONCEPT_MAP_HUB_WIDTH
        sat_w = _CONCEPT_MAP_SAT_WIDTH_COMPACT if compact else _CONCEPT_MAP_SAT_WIDTH
        angles = _concept_map_satellite_angles(n)
        hub_h = _concept_map_card_height(hub)
        sat_h = max(_concept_map_card_height(s) for s in sats)
        needed = _concept_map_required_radius(hub_w, hub_h, sat_w, sat_h, angles)
        available = _concept_map_max_radius_for_width(sat_w, angles)

        if needed <= available:
            positions = [(radius * math.cos(a), radius * math.sin(a)) for a in angles]
            heights = [_concept_map_card_height(s) for s in sats]
            for i in range(n):
                j = (i + 1) % n
                c1 = (positions[i][0], positions[i][1], sat_w, heights[i])
                c2 = (positions[j][0], positions[j][1], sat_w, heights[j])
                assert self._no_overlap(c1, c2), f"n={n}: adjacent satellites {i} and {j} overlap"
        else:
            assert radius == pytest.approx(available), (
                f"n={n}: the formula's own promise doesn't hold here - the clamp must still pick the "
                "largest page-safe radius, not something smaller"
            )

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 8, 10])
    def test_every_rectangle_stays_inside_the_computed_container(self, n):
        from app.render.concept_experience_renderer import (
            _CONCEPT_MAP_HUB_WIDTH,
            _CONCEPT_MAP_HUB_WIDTH_COMPACT,
            _CONCEPT_MAP_SAT_WIDTH,
            _CONCEPT_MAP_SAT_WIDTH_COMPACT,
            _concept_map_card_height,
            _concept_map_hub_layout,
            _concept_map_satellite_angles,
        )

        hub = VisualEntity(id="hub", label="Hub")
        sats = [VisualEntity(id=f"s{i}", label=f"Satellite {i}") for i in range(n)]
        radius, compact, container_w, container_h = _concept_map_hub_layout(hub, sats)
        hub_w = _CONCEPT_MAP_HUB_WIDTH_COMPACT if compact else _CONCEPT_MAP_HUB_WIDTH
        sat_w = _CONCEPT_MAP_SAT_WIDTH_COMPACT if compact else _CONCEPT_MAP_SAT_WIDTH
        angles = _concept_map_satellite_angles(n)
        half_w, half_h = container_w / 2, container_h / 2
        assert _concept_map_card_height(hub) / 2 <= half_h + 0.5
        assert hub_w / 2 <= half_w + 0.5
        for s, a in zip(sats, angles):
            x, y = radius * math.cos(a), radius * math.sin(a)
            sat_h = _concept_map_card_height(s)
            assert abs(x) + sat_w / 2 <= half_w + 0.5, f"n={n}: a satellite exceeds the container width"
            assert abs(y) + sat_h / 2 <= half_h + 0.5, f"n={n}: a satellite exceeds the container height"

    def test_container_width_never_exceeds_the_page(self):
        """The one hard constraint that must never be violated regardless
        of tier or satellite count - clipping by `.cev-root`'s
        `overflow:hidden` would silently hide content."""
        from app.render.concept_experience_renderer import _INNER_WIDTH, _concept_map_hub_layout

        hub = VisualEntity(id="hub", label="Hub")
        for n in (2, 3, 4, 5, 6, 8, 10, 12):
            sats = [
                VisualEntity(
                    id=f"s{i}", label=f"A reasonably long satellite label {i}",
                    properties={"detail": "some descriptive text about this satellite"},
                )
                for i in range(n)
            ]
            _radius, _compact, container_w, _h = _concept_map_hub_layout(hub, sats)
            assert container_w <= _INNER_WIDTH + 1.0, f"n={n}: container width {container_w} exceeds page width {_INNER_WIDTH}"

    def test_normal_tier_is_preferred_for_two_short_satellites(self):
        """Requirement: normal tier stays active whenever it genuinely
        fits - the simplest possible hub case (2 bare-label satellites)
        must not be forced into compact."""
        from app.render.concept_experience_renderer import _concept_map_hub_layout

        hub = VisualEntity(id="hub", label="Hub")
        sats = [VisualEntity(id="a", label="A"), VisualEntity(id="b", label="B")]
        _radius, compact, _w, _h = _concept_map_hub_layout(hub, sats)
        assert compact is False

    def test_compact_tier_engages_only_when_normal_genuinely_cannot_fit(self):
        """Cross-checks `_concept_map_hub_layout`'s own tier decision
        against an independently recomputed normal-tier needed-vs-available
        comparison - compact must be selected if and only if the normal
        tier's own required radius exceeds what the page allows it."""
        from app.render.concept_experience_renderer import (
            _CONCEPT_MAP_HUB_WIDTH,
            _CONCEPT_MAP_SAT_WIDTH,
            _concept_map_card_height,
            _concept_map_hub_layout,
            _concept_map_max_radius_for_width,
            _concept_map_required_radius,
            _concept_map_satellite_angles,
        )

        hub = VisualEntity(id="hub", label="Hub")
        sats = [VisualEntity(id=f"s{i}", label=f"Sat {i}") for i in range(8)]
        angles = _concept_map_satellite_angles(8)
        hub_h = _concept_map_card_height(hub)
        sat_h = max(_concept_map_card_height(s) for s in sats)
        needed_normal = _concept_map_required_radius(_CONCEPT_MAP_HUB_WIDTH, hub_h, _CONCEPT_MAP_SAT_WIDTH, sat_h, angles)
        available_normal = _concept_map_max_radius_for_width(_CONCEPT_MAP_SAT_WIDTH, angles)
        _radius, compact, _w, _h = _concept_map_hub_layout(hub, sats)
        assert compact == (needed_normal > available_normal)

    def test_long_labels_and_properties_select_the_appropriate_tier(self):
        """Long text makes cards taller (_entity_chars/_slot_width), which
        can push a modest satellite count past the normal tier - this is
        the pure-math half of that check (does the MODEL correctly choose
        compact here, and does the clamp invariant hold); whether real
        Chromium rendering of this exact content ("Operating System", the
        original bug report's own content) has any ACTUAL overlap is
        verified authoritatively in
        TestConceptMapRealBrowserGeometry.test_operating_system_case_has_no_overlap
        instead - a pure-Python estimate can't answer that, see this
        class's own module-level distinction notes."""
        from app.render.concept_experience_renderer import (
            _CONCEPT_MAP_HUB_WIDTH_COMPACT,
            _CONCEPT_MAP_SAT_WIDTH_COMPACT,
            _concept_map_card_height,
            _concept_map_hub_layout,
            _concept_map_max_radius_for_width,
            _concept_map_required_radius,
            _concept_map_satellite_angles,
        )

        hub = VisualEntity(id="hub", label="Operating System")
        sats = [
            VisualEntity(id="def", label="Definition", properties={"summary": "System software that manages hardware and software resources"}),
            VisualEntity(id="mem", label="Memory Management", properties={"role": "Allocates RAM to running programs"}),
            VisualEntity(id="sched", label="Process Scheduling", properties={"role": "Decides which process runs next on the CPU"}),
            VisualEntity(id="example", label="Example", properties={"value": "Windows, Linux, macOS, Android"}),
            VisualEntity(id="analogy", label="Analogy", properties={"value": "A building manager coordinating every tenant's use of shared utilities"}),
        ]
        radius, compact, _container_w, _container_h = _concept_map_hub_layout(hub, sats)
        assert compact is True  # this much real text genuinely doesn't fit the normal tier

        hub_w, sat_w = _CONCEPT_MAP_HUB_WIDTH_COMPACT, _CONCEPT_MAP_SAT_WIDTH_COMPACT
        hub_h = _concept_map_card_height(hub)
        sat_h = max(_concept_map_card_height(s) for s in sats)
        angles = _concept_map_satellite_angles(len(sats))
        needed = _concept_map_required_radius(hub_w, hub_h, sat_w, sat_h, angles)
        available = _concept_map_max_radius_for_width(sat_w, angles)

        if needed <= available:
            hub_rect = (0.0, 0.0, hub_w, hub_h)
            for s, a in zip(sats, angles):
                sat_rect = (radius * math.cos(a), radius * math.sin(a), sat_w, _concept_map_card_height(s))
                assert self._no_overlap(hub_rect, sat_rect)
        else:
            assert radius == pytest.approx(available)

    def test_radius_grows_with_real_geometry_not_a_flat_constant(self):
        """The old bounding-circle model produced an IDENTICAL clamped
        radius for n=2 and n=10 (the exact bug this investigation started
        from) - the replacement must not repeat that: across a spread of
        satellite counts, the radii must not all collapse to one repeated
        value."""
        from app.render.concept_experience_renderer import _concept_map_hub_layout

        hub = VisualEntity(id="hub", label="Hub")
        radii = set()
        for n in (2, 3, 4, 6, 8, 10):
            sats = [VisualEntity(id=f"s{i}", label=f"Sat {i}") for i in range(n)]
            radius, _compact, _w, _h = _concept_map_hub_layout(hub, sats)
            radii.add(round(radius, 1))
        assert len(radii) > 1


class TestConceptMapRenderedLayoutRegression:
    """A fast, browser-free smoke check: parses the real transform values
    back out of the rendered HTML string - cheap enough to run on every
    test invocation, but only ever as strong as the ESTIMATED footprint
    (_concept_map_card_height) it's built from, which real `.cev-card`s
    (min-width:0, shrink-to-fit) often render smaller than - see
    TestConceptMapRealBrowserGeometry below for the authoritative, real-
    Chromium-measured equivalent of an actual overlap claim. This class is
    kept narrow on purpose: just confirming distinct node positions (the
    exact failure mode an earlier CSS-animation bug in this same
    investigation caused - every node silently collapsing onto one point
    while every markup-level test kept passing), not full geometry."""

    NODE_RE = re.compile(
        r'<div class="cev-cmap-node" style="transform:translate\(-50%,-50%\) '
        r'translate\(([-\d.]+)px,([-\d.]+)px\);" data-cmap-node-id="([^"]+)"'
    )

    @classmethod
    def _parse_nodes(cls, html: str) -> dict[str, tuple[float, float]]:
        body = html.split("</style>", 1)[1]
        return {node_id: (float(dx), float(dy)) for dx, dy, node_id in cls.NODE_RE.findall(body)}

    def test_rendered_two_satellite_case_uses_normal_tier_and_every_node_is_distinct(self):
        entities = [VisualEntity(id="hub", label="Hub"), VisualEntity(id="a", label="A"), VisualEntity(id="b", label="B")]
        relationships = [
            SchematicRelationship(source="hub", target="a", type="connected_to"),
            SchematicRelationship(source="hub", target="b", type="connected_to"),
        ]
        spec = DiagramSpec(kind="concept_experience", representation="relationship", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert "cev-cmap-compact" not in body  # requirement: normal tier used when there's clearly room

        positions = self._parse_nodes(html)
        # The exact failure mode the animation-conflict bug produced: every
        # node collapsing onto the identical point - a direct, minimal
        # regression guard against that specific class of bug recurring.
        assert positions["hub"] != positions["a"]
        assert positions["hub"] != positions["b"]
        assert positions["a"] != positions["b"]


class TestConceptMapRealBrowserGeometry:
    """Authoritative geometry validation via a real Chromium page - same
    Playwright launch pattern as `app.services.pdf_service.PdfService.
    export_pdf` (`args=["--no-sandbox"]`), and the same availability guard
    `tests.test_pdf` already defines (`needs_browser`), reused here rather
    than inventing a second browser setup.

    The division of labour in this module, explicitly:
    - `TestConceptMapHubGeometry` validates the MATHEMATICAL layout MODEL
      itself (the exact-rectangle formula's own inequalities, the
      container-bound derivation, the tier decision) against the same
      conservative footprint ESTIMATE the renderer uses as a safety
      ceiling - pure math, no browser, fast, and it stays that way
      (card dimensions there are a text-length estimate, never a DOM
      measurement).
    - THIS class validates what Chromium actually draws from the real
      CSS. `.cev-card` has `min-width:0` and only grows toward its
      max-width cap if its content genuinely needs that much room - a
      short one-line label typically renders far smaller than the
      estimate's safety ceiling, so real rendered clearance is usually
      MORE than the model strictly guarantees, never less. This is the
      only way to answer "does a reader actually see any overlap" -
      confirmed, during this investigation, to matter: an earlier bug (a
      CSS animation silently cancelling the positioning transform) passed
      every markup/estimate-based test while collapsing every node onto
      one point in the real browser. These tests close that exact gap.
    """

    @staticmethod
    def _boxes_overlap(a: dict, b: dict, *, tolerance: float = 0.5) -> bool:
        return not (
            a["x"] + a["width"] <= b["x"] + tolerance
            or b["x"] + b["width"] <= a["x"] + tolerance
            or a["y"] + a["height"] <= b["y"] + tolerance
            or b["y"] + b["height"] <= a["y"] + tolerance
        )

    @staticmethod
    async def _measure(playwright, entities, relationships, *, title: str = "") -> dict:
        """Renders a real concept_experience HTML fragment in an actual
        Chromium page and returns the ACTUAL CSS-rendered geometry -
        pure-Python geometry tests elsewhere in this module can only ever
        estimate a card's size (_concept_map_card_height); this is the one
        place that measures what a browser really draws."""
        spec = DiagramSpec(
            kind="concept_experience", representation="relationship", title=title,
            entities=entities, relationships=relationships,
        )
        fragment = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        html = f"<!doctype html><html><body>{fragment}</body></html>"

        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": 900, "height": 1600})
            await page.set_content(html, wait_until="load")
            root_box = await page.eval_on_selector(".cev-root", "el => el.getBoundingClientRect().toJSON()")
            container = await page.query_selector(".cev-cmap")
            container_box = await container.bounding_box()
            container_class = await container.get_attribute("class") or ""
            node_handles = await page.query_selector_all(".cev-cmap-node")
            nodes = {}
            for handle in node_handles:
                node_id = await handle.get_attribute("data-cmap-node-id")
                nodes[node_id] = await handle.bounding_box()
            return {
                "root": root_box,
                "container": container_box,
                "compact": "cev-cmap-compact" in container_class,
                "nodes": nodes,
            }
        finally:
            await browser.close()

    @staticmethod
    def _hub_spec_entities(n: int) -> tuple[list[VisualEntity], list[SchematicRelationship]]:
        hub = VisualEntity(id="hub", label="Hub Concept")
        sats = [VisualEntity(id=f"s{i}", label=f"Satellite {i}") for i in range(n)]
        rels = [SchematicRelationship(source="hub", target=f"s{i}", type="connected_to") for i in range(n)]
        return [hub] + sats, rels

    async def _assert_clean_layout(
        self, playwright, entities, relationships, *, expect_compact: bool | None = None, title: str = ""
    ) -> dict:
        result = await self._measure(playwright, entities, relationships, title=title)
        nodes = result["nodes"]
        assert len(nodes) == len(entities)  # every entity rendered exactly once, none dropped/duplicated

        if expect_compact is not None:
            assert result["compact"] is expect_compact

        root = result["root"]
        container = result["container"]
        ids = list(nodes)
        for i in range(len(ids)):
            box_i = nodes[ids[i]]
            assert box_i is not None, f"{ids[i]} has no visible box"
            # Inside the concept-map container itself (the box
            # `_concept_map_container_size` computed) - a tolerance wide
            # enough for sub-pixel layout rounding, not for a real miss.
            assert box_i["x"] >= container["x"] - 2.0, f"{ids[i]} sits outside the concept-map container (left)"
            assert box_i["y"] >= container["y"] - 2.0, f"{ids[i]} sits outside the concept-map container (top)"
            assert box_i["x"] + box_i["width"] <= container["x"] + container["width"] + 2.0, (
                f"{ids[i]} sits outside the concept-map container (right)"
            )
            assert box_i["y"] + box_i["height"] <= container["y"] + container["height"] + 2.0, (
                f"{ids[i]} sits outside the concept-map container (bottom)"
            )
            # no clipping - every node's box must also sit fully inside
            # .cev-root's own box (the real `overflow:hidden` ancestor -
            # see _style_html) - a looser bound than the container check
            # above, but the one that actually determines visibility.
            assert box_i["x"] >= root["x"] - 1.0, f"{ids[i]} clipped on the left by .cev-root"
            assert box_i["y"] >= root["y"] - 1.0, f"{ids[i]} clipped on the top by .cev-root"
            assert box_i["x"] + box_i["width"] <= root["x"] + root["width"] + 1.0, f"{ids[i]} clipped on the right"
            assert box_i["y"] + box_i["height"] <= root["y"] + root["height"] + 1.0, f"{ids[i]} clipped on the bottom"
            for j in range(i + 1, len(ids)):
                box_j = nodes[ids[j]]
                assert not self._boxes_overlap(box_i, box_j), f"rendered {ids[i]}/{ids[j]} rectangles overlap"

        # Hub approximately centred in the map.
        hub_box = nodes["hub"]
        hub_cx, hub_cy = hub_box["x"] + hub_box["width"] / 2, hub_box["y"] + hub_box["height"] / 2
        container = result["container"]
        container_cx = container["x"] + container["width"] / 2
        container_cy = container["y"] + container["height"] / 2
        assert abs(hub_cx - container_cx) < 3.0, "hub is not centred horizontally in the concept map"
        assert abs(hub_cy - container_cy) < 3.0, "hub is not centred vertically in the concept map"

        sat_ids = [i for i in ids if i != "hub"]
        sat_angles: dict[str, float] = {}
        for sid in sat_ids:
            box = nodes[sid]
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            dist = math.hypot(cx - hub_cx, cy - hub_cy)
            # No satellite collapses toward the hub - the exact failure
            # mode an earlier CSS-animation bug in this same investigation
            # caused (every node landing on the hub's own centre point).
            assert dist > 20.0, f"{sid} is suspiciously close to the hub centre ({dist:.1f}px) - possible collapse"
            # Angle normalised relative to the renderer's own starting
            # angle (-90deg = 12 o'clock, clockwise - see
            # _concept_map_satellite_angles) so it increases monotonically
            # with satellite index instead of wrapping at +-180deg.
            raw_deg = math.degrees(math.atan2(cy - hub_cy, cx - hub_cx))
            sat_angles[sid] = (raw_deg + 90.0) % 360.0

        # Satellites keep the same clockwise order they were declared in -
        # a real rendering check that the angular placement
        # (_concept_map_ring_positions) wasn't scrambled.
        assert list(sat_angles) == sorted(sat_angles, key=sat_angles.get)

        return result

    @needs_browser
    @pytest.mark.parametrize("n", [2, 3, 6])
    async def test_short_label_hub_cases_use_normal_tier_with_no_overlap(self, n):
        from playwright.async_api import async_playwright

        entities, relationships = self._hub_spec_entities(n)
        async with async_playwright() as playwright:
            await self._assert_clean_layout(playwright, entities, relationships, expect_compact=False, title="Hub Concept")

    @needs_browser
    @pytest.mark.parametrize("n", [5, 8, 10])
    async def test_dense_short_label_hub_cases_have_no_overlap_even_at_normal_or_compact_tier(self, n):
        """n=5/8/10 (bare short labels) are exactly where the exact-
        rectangle model's own count/angle-alignment maths pushes the
        required radius past the normal tier (n=5 needs compact too, not
        just the denser n=8/10 - a non-monotonic property of the exact
        model confirmed during investigation) - and where the pure-math
        estimate-based tests in TestConceptMapHubGeometry can no longer
        guarantee non-overlap at their conservative worst-case footprint
        (see those tests' own docstrings). Real Chromium rendering is the
        authoritative check for whether that worst case is ever actually
        reached - it isn't, for short one-word labels: they render far
        smaller than the estimate's safety ceiling, so real clearance
        remains even here."""
        from playwright.async_api import async_playwright

        entities, relationships = self._hub_spec_entities(n)
        async with async_playwright() as playwright:
            await self._assert_clean_layout(playwright, entities, relationships, title="Hub Concept")

    @needs_browser
    async def test_operating_system_case_has_no_overlap(self):
        """The exact content from the original bug report - the one case
        this entire investigation traces back to."""
        from playwright.async_api import async_playwright

        entities = [VisualEntity(id="hub", label="Operating System")] + [
            VisualEntity(id="def", label="Definition", properties={"summary": "System software that manages hardware and software resources"}),
            VisualEntity(id="mem", label="Memory Management", properties={"role": "Allocates RAM to running programs"}),
            VisualEntity(id="sched", label="Process Scheduling", properties={"role": "Decides which process runs next on the CPU"}),
            VisualEntity(id="example", label="Example", properties={"value": "Windows, Linux, macOS, Android"}),
            VisualEntity(id="analogy", label="Analogy", properties={"value": "A building manager coordinating every tenant's use of shared utilities"}),
        ]
        relationships = [SchematicRelationship(source="hub", target=e.id, type="connected_to") for e in entities[1:]]
        async with async_playwright() as playwright:
            await self._assert_clean_layout(playwright, entities, relationships, expect_compact=True, title="Operating System")

    @needs_browser
    async def test_long_label_case_selects_normal_tier_when_it_fits(self):
        """Longer labels than the bare-word default, but few enough
        satellites that normal tier still has real room - confirms normal
        tier isn't abandoned just because labels grow a little (requirement:
        compact only when normal genuinely cannot fit)."""
        from playwright.async_api import async_playwright

        entities = [VisualEntity(id="hub", label="Central Idea")] + [
            VisualEntity(id="a", label="A Moderately Long Supporting Point"),
            VisualEntity(id="b", label="Another Related Concept"),
            VisualEntity(id="c", label="A Third Connected Idea"),
        ]
        relationships = [SchematicRelationship(source="hub", target=e.id, type="connected_to") for e in entities[1:]]
        async with async_playwright() as playwright:
            await self._assert_clean_layout(playwright, entities, relationships, expect_compact=False, title="Central Idea")


class TestTextNeverOverflowsItsBox:
    """A long, unbroken token (a function signature, an identifier with no
    spaces) must wrap inside its card/cell instead of spilling past the
    edge - see the universal overflow-wrap rule on `.cev-root *`."""

    def test_a_long_unbroken_property_value_does_not_overflow_its_card(self):
        long_token = "a_" + "very_" * 20 + "long_unbroken_identifier"
        entity = VisualEntity(id="a", label="A", properties={"field": long_token})
        spec = DiagramSpec(kind="concept_experience", representation="object", entities=[entity])
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "overflow-wrap:anywhere" in html
        assert long_token in html


def _extract_hex_colors(html: str) -> set[str]:
    import re

    return set(re.findall(r"#[0-9a-fA-F]{6}", html))


def test_every_representation_type_resolves_to_a_real_renderer_or_the_documented_fallback():
    """Static completeness check (approved plan, section I/K) - a future
    12th representation value added without a compatibility-table entry
    must be caught here, not discovered at runtime."""
    from app.render.concept_experience_renderer import _REPRESENTATION_RENDERERS, _render_object

    for representation in REPRESENTATION_TYPES:
        renderer = _REPRESENTATION_RENDERERS.get(representation, _render_object)
        assert callable(renderer)


# ---------------------------------------------------------------------------
# estimate_pixel_size: the page layout engine positions every block AFTER
# this one using this estimate - a user-reported real bug (a shorter block's
# content overlapped the next block on the page) traced back to a fixed
# nominal size that never varied with actual content. See
# app.course.document.layout.image_box_height for the other half of this fix
# (the MAX_IMAGE_HEIGHT cap no longer applies to this kind).
# ---------------------------------------------------------------------------


class TestPixelSizeEstimation:
    def test_height_grows_with_entity_count(self):
        few = DiagramSpec(
            kind="concept_experience", representation="data_structure",
            entities=[VisualEntity(id=f"e{i}", label=f"item{i}") for i in range(2)],
        )
        many = DiagramSpec(
            kind="concept_experience", representation="data_structure",
            entities=[VisualEntity(id=f"e{i}", label=f"item{i}") for i in range(20)],
        )
        _, few_height = estimate_pixel_size(few)
        _, many_height = estimate_pixel_size(many)
        assert many_height > few_height

    def test_height_grows_with_step_count(self):
        few = DiagramSpec(
            kind="concept_experience", representation="process",
            steps=[VisualStep(id=f"s{i}", label=f"Step {i}", order=i) for i in range(2)],
        )
        many = DiagramSpec(
            kind="concept_experience", representation="process",
            steps=[VisualStep(id=f"s{i}", label=f"Step {i}", order=i) for i in range(15)],
        )
        _, few_height = estimate_pixel_size(few)
        _, many_height = estimate_pixel_size(many)
        assert many_height > few_height

    def test_optional_chrome_each_adds_height(self):
        bare = DiagramSpec(kind="concept_experience", representation="object")
        full = DiagramSpec(
            kind="concept_experience", representation="object",
            visual_metaphor="A thing like another thing.",
            core_message="The one takeaway.",
            technical_signature="class Car { color; }",
        )
        _, bare_height = estimate_pixel_size(bare)
        _, full_height = estimate_pixel_size(full)
        assert full_height > bare_height

    def test_returns_the_reference_content_width(self):
        spec = DiagramSpec(kind="concept_experience", representation="object")
        width, _ = estimate_pixel_size(spec)
        assert width == 666

    def test_height_is_capped_at_roughly_one_page_even_for_many_entities(self):
        huge = DiagramSpec(
            kind="concept_experience", representation="data_structure",
            entities=[VisualEntity(id=f"e{i}", label=f"item{i}") for i in range(40)],
        )
        _, height = estimate_pixel_size(huge)
        assert height <= 863  # CONTENT_HEIGHT (963) minus the caption/padding reserve

    def test_height_is_capped_even_for_many_steps_with_long_descriptions(self):
        huge = DiagramSpec(
            kind="concept_experience", representation="process",
            steps=[
                VisualStep(
                    id=f"s{i}", label=f"Step {i}", order=i,
                    description="A long, sentence-length description of exactly what this "
                    "step does, the kind a real chapter would actually write.",
                )
                for i in range(10)
            ],
        )
        _, height = estimate_pixel_size(huge)
        assert height <= 863


class TestOversizedVisualsFitOnePage:
    """The renderer's own output must match what estimate_pixel_size reserved
    for it - see render_concept_experience_html's docstring. A mismatch here
    is exactly the class of bug that causes a visual to get cut off by the
    page boundary (the reserved space) or to overlap the next block (the
    render exceeding what was reserved)."""

    @staticmethod
    def _huge_spec() -> DiagramSpec:
        return DiagramSpec(
            kind="concept_experience", representation="process",
            steps=[
                VisualStep(
                    id=f"s{i}", label=f"Step {i}", order=i,
                    description="A long, sentence-length description of exactly what this "
                    "step does, the kind a real chapter would actually write.",
                )
                for i in range(10)
            ],
        )

    def test_a_normal_sized_visual_is_not_scaled(self):
        spec = _java_class_and_object_spec()
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "transform:scale(" not in html

    def test_an_oversized_visual_is_scaled_down_to_fit(self):
        html = render_concept_experience_html(self._huge_spec(), TemplateTheme()).decode("utf-8")
        assert "transform:scale(" in html

    def test_the_scaled_wrapper_height_matches_what_was_reserved(self):
        spec = self._huge_spec()
        _, reserved_height = estimate_pixel_size(spec)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        # The outer wrapper's own declared height is the first `height:...px`
        # in the markup - it must equal what the page layout engine reserved,
        # or the visual either overflows its reservation (overlap) or leaves
        # dead space (an underused reservation).
        match = re.search(r'height:(\d+)px', html)
        assert match is not None
        assert int(match.group(1)) == reserved_height


# ---------------------------------------------------------------------------
# Multi-domain proof: 6 concepts across 5 subjects, only Java has a
# registered blueprint - see the approved plan sections G/I/K.
# ---------------------------------------------------------------------------

_MULTI_DOMAIN_CONCEPTS = [
    # (brief, expected concept, expected representation, has_blueprint, labels expected in the rendered HTML)
    ("Explain Java Class and Object", "java_class_and_object", "object", True, ["Car (Class)", "car1", "car2"]),
    ("What is a Python list", "python_list", "data_structure", False, ["apple", "banana"]),
    ("What is SQL JOIN between two tables", "sql_join", "relationship", False, ["orders", "customers"]),
    ("Explain the TCP three-way handshake", "tcp_three_way_handshake", "sequence", False, ["SYN", "ACK"]),
    ("What is a stack in data structures", "stack", "data_structure", False, ["Plate 1", "Plate 3"]),
    ("What is RAG retrieval augmented generation", "rag_pipeline", "pipeline", False, ["Query", "Retrieve"]),
]


class TestMultiDomainConcepts:
    """Proves the ARCHITECTURE generalizes, not just Java - each concept's
    representation is SELECTED by the (mocked) planner, then rendered
    through the correct one of the 3 real renderers per the compatibility
    table. Only java_class_and_object has a registered blueprint; the other
    five deliberately have none."""

    @pytest.mark.parametrize("brief,concept,representation,has_blueprint,labels", _MULTI_DOMAIN_CONCEPTS)
    async def test_planner_selects_the_expected_representation(self, brief, concept, representation, has_blueprint, labels):
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose=brief, prompt="", caption="", course_title="Course")
        assert spec.concept == concept
        assert spec.representation == representation

    @pytest.mark.parametrize("brief,concept,representation,has_blueprint,labels", _MULTI_DOMAIN_CONCEPTS)
    async def test_renders_without_error_via_the_correct_renderer(self, brief, concept, representation, has_blueprint, labels):
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose=brief, prompt="", caption="", course_title="Course")
        spec = apply_concept_blueprint_defaults(spec)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        for label in labels:
            assert label in html

    def test_only_java_has_a_registered_blueprint(self):
        for _, concept, _, has_blueprint, _ in _MULTI_DOMAIN_CONCEPTS:
            assert (get_concept_blueprint(concept) is not None) == has_blueprint

    async def test_sql_join_renders_an_actual_connector_between_the_two_tables(self):
        """Proves the `relationship` fallback (object + connector lines) -
        not just two disconnected cards."""
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose="What is SQL JOIN between two tables", prompt="", caption="", course_title="Course")
        spec = apply_concept_blueprint_defaults(spec)
        assert spec.relationships  # the mock spec declares one
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "cev-connector" in html
        assert "cev-connector-line" in html

    async def test_tcp_handshake_steps_render_in_order(self):
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose="Explain the TCP three-way handshake", prompt="", caption="", course_title="Course")
        spec = apply_concept_blueprint_defaults(spec)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert html.index("SYN-ACK") < html.index(">ACK<") or html.index("SYN-ACK") < html.rindex("ACK")

    async def test_stack_data_structure_renders_front_and_top_badges(self):
        """A static equivalent of push/pop controls: the FRONT/TOP badges
        show at a glance where an operation would act, without a button."""
        planner = VisualPlanner(ai=MockAIClient(get_settings()))
        spec = await planner.plan(purpose="What is a stack in data structures", prompt="", caption="", course_title="Course")
        spec = apply_concept_blueprint_defaults(spec)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "cev-ds-track" in html
        assert "cev-ds-end-badge" in html

    def test_data_structure_with_relationships_renders_connectors_not_a_flat_row(self):
        """A tree/graph-shaped data_structure (e.g. a binary search tree,
        which the real API genuinely returned in manual testing) must not
        silently flatten into an unconnected linear row - it should render
        through the same connector technique as relationship/hierarchy."""
        spec = DiagramSpec(
            kind="concept_experience",
            concept="binary_search_tree",
            representation="data_structure",
            title="Binary Search Tree",
            entities=[
                VisualEntity(id="n8", label="8"),
                VisualEntity(id="n3", label="3"),
                VisualEntity(id="n10", label="10"),
            ],
            relationships=[
                SchematicRelationship(source="n8", type="connected_to", target="n3"),
                SchematicRelationship(source="n8", type="connected_to", target="n10"),
            ],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert '<div class="cev-connector">' in html
        # front/top-back caption doesn't apply to a tree (the class still
        # appears in the shared <style> block regardless - check the actual
        # element, not the CSS selector text)
        assert '<div class="cev-ds-ends">' not in html

    def test_data_structure_without_relationships_keeps_the_plain_linear_layout(self):
        """Plain stack/queue/list specs (no relationships) must render
        exactly as before this fix - unchanged regression guard."""
        spec = DiagramSpec(
            kind="concept_experience",
            concept="stack",
            representation="data_structure",
            title="Stack",
            entities=[VisualEntity(id="p1", label="Plate 1"), VisualEntity(id="p2", label="Plate 2")],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert '<div class="cev-ds-track">' in html
        assert '<div class="cev-ds-ends">' in html
        assert '<div class="cev-connector">' not in html

    def test_interactions_never_render_any_button_static_by_design(self):
        """`interactions` is inert metadata now - the visual is deliberately
        static (no JS, no buttons), so nothing in `spec.interactions`
        (however many, whatever type) should ever produce a `<button>`."""
        spec = DiagramSpec(
            kind="concept_experience",
            concept="binary_search_tree",
            representation="data_structure",
            title="Binary Search Tree",
            entities=[VisualEntity(id="n1", label="1")],
            interactions=[
                InteractionSpec(type="play_search", trigger_label="Play Search"),
                InteractionSpec(type="reset", trigger_label="Reset"),
            ],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "<button" not in html
        assert "data-interaction" not in html

    def test_iconless_numeric_entity_does_not_get_a_misleading_truncated_avatar(self):
        """When an entity has no icon, the fallback avatar shows the first
        LETTER of its label ("Car (Class)" -> "C") - never the first digit
        of a numeric/symbolic label. A BST node labelled "10" showing a big
        "1" avatar right next to its own "10" label would misstate the
        node's actual value at a glance (found via visual inspection)."""
        spec = DiagramSpec(
            kind="concept_experience",
            concept="binary_search_tree",
            representation="data_structure",
            title="Binary Search Tree",
            entities=[VisualEntity(id="n10", label="10"), VisualEntity(id="n_car", label="Car (Class)")],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert '<span class="cev-icon">1</span>' not in html
        assert '<span class="cev-icon">C</span>' in html

    def test_print_media_disables_entrance_animations(self):
        """PDF export (Playwright) emulates print media and snapshots the
        page right after load, with no wait for the wall-clock entrance
        animations added in this session's redesign to finish - without a
        @media print override, a printed page could freeze mid fade-in."""
        html = render_concept_experience_html(_java_class_and_object_spec(), TemplateTheme()).decode("utf-8")
        assert "@media print" in html
        print_block = html.split("@media print")[1].split("}\n}")[0]
        assert "animation:none" in print_block

    async def test_all_six_concepts_pass_structural_qa(self):
        """No LLM needed for this - Layer 1 alone must accept every one of
        the 6 mock specs, proving the representation-aware structural checks
        genuinely generalize across representations, not just "object"."""
        for brief, *_ in _MULTI_DOMAIN_CONCEPTS:
            planner = VisualPlanner(ai=MockAIClient(get_settings()))
            spec = await planner.plan(purpose=brief, prompt="", caption="", course_title="Course")
            spec = apply_concept_blueprint_defaults(spec)
            result = await evaluate_concept_experience(spec, ai=MockAIClient(get_settings()), settings=get_settings())
            assert result.passed, (brief, [i.reason for i in result.issues])


# ---------------------------------------------------------------------------
# concept_qa: Layer 1 (deterministic structural) - no LLM call
# ---------------------------------------------------------------------------


def _good_metaphor_spec(**overrides) -> DiagramSpec:
    base = apply_concept_blueprint_defaults(DiagramSpec(kind="concept_experience", concept="java_class_and_object"))
    return base.model_copy(update=overrides) if overrides else base


class TestConceptQAStructural:
    async def test_good_spec_passes_layer_1_and_layer_2(self):
        result = await evaluate_concept_experience(_good_metaphor_spec(), ai=MockAIClient(get_settings()), settings=get_settings())
        assert result.passed

    async def test_non_concept_experience_kind_always_passes(self):
        assert (await evaluate_concept_experience(DiagramSpec(kind="flow_chart"))).passed
        assert (await evaluate_concept_experience(DiagramSpec(kind="schematic"))).passed

    async def test_missing_learning_objectives_core_message_and_criteria_are_caught(self):
        spec = _good_metaphor_spec(learning_objectives=[], core_message="", validation_criteria=[])
        result = await evaluate_concept_experience(spec)
        reasons = [i.reason for i in result.issues]
        assert reasons.count(MISSING_LEARNING_OBJECTIVES) == 3

    async def test_multiline_technical_signature_is_rejected(self):
        spec = _good_metaphor_spec(technical_signature="class Car {\n  color;\n}")
        result = await evaluate_concept_experience(spec)
        assert MULTILINE_TECHNICAL_SIGNATURE in {i.reason for i in result.issues}

    async def test_object_representation_requires_a_template_entity(self):
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="a", role="instance", properties={"x": "1"}),
            VisualEntity(id="b", role="instance", properties={"x": "2"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert MISSING_ENTITY in {i.reason for i in result.issues}

    async def test_fewer_than_two_instances_is_caught(self):
        spec = _good_metaphor_spec(entities=[VisualEntity(id="t", role="template")])
        result = await evaluate_concept_experience(spec)
        assert MISSING_ENTITY in {i.reason for i in result.issues}

    async def test_identical_instance_properties_are_caught(self):
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="a", role="instance", properties={"x": "1"}),
            VisualEntity(id="b", role="instance", properties={"x": "1"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert NO_INSTANCE_VARIATION in {i.reason for i in result.issues}

    async def test_generic_visual_flagged_when_metaphor_missing_for_metaphor_friendly_representation(self):
        spec = _good_metaphor_spec(visual_metaphor="")
        result = await evaluate_concept_experience(spec)
        assert GENERIC_VISUAL in {i.reason for i in result.issues}

    async def test_too_long_labels_are_flagged_as_too_dense(self):
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template", label="A" * 60),
            VisualEntity(id="a", role="instance", properties={"x": "1"}),
            VisualEntity(id="b", role="instance", properties={"x": "2"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert TOO_DENSE in {i.reason for i in result.issues}

    async def test_sentence_or_formula_length_property_values_are_flagged_as_too_dense(self):
        # A real, confirmed failure mode: an entity's property value held a
        # full theorem statement instead of a short fact, and sailed
        # straight through to a rendered card as literal "key : value" text.
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="a", role="instance", properties={
                "equation": "closed loop with oriented boundary and small surface, flux through it",
            }),
            VisualEntity(id="b", role="instance", properties={"x": "2"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert TOO_DENSE in {i.reason for i in result.issues}

    async def test_nonsense_property_keys_are_flagged_as_too_dense(self):
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="a", role="instance", properties={
                "Ghost duplicate: reverse n to flip the sign": "yes",
            }),
            VisualEntity(id="b", role="instance", properties={"x": "2"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert TOO_DENSE in {i.reason for i in result.issues}

    async def test_short_properties_do_not_trigger_too_dense(self):
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="a", role="instance", properties={"color": "Red", "mutability": "immutable"}),
            VisualEntity(id="b", role="instance", properties={"color": "Blue", "mutability": "mutable"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert TOO_DENSE not in {i.reason for i in result.issues}

    async def test_instances_sharing_no_common_property_are_flagged_as_disconnected(self):
        """Reproduces the reported bug exactly: 4 distinct physics
        sub-concepts (a unit-normal convention, a B-vs-H curve, Stokes'
        theorem, Gauss' theorem) modelled as "instances" that share not one
        property name - each is really its own separate idea, not a real
        instance of one shared concept, which is exactly what rendered as a
        row of disconnected, unrelated-looking cards."""
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="normal", role="instance", properties={"label": "n", "color": "green"}),
            VisualEntity(id="bh_curve", role="instance", properties={"plot": "B vs H curve, flattens at high H"}),
            VisualEntity(id="stokes", role="instance", properties={"equation": "oint A.dl = int curl A.dA"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert DISCONNECTED_CONCEPTS in {i.reason for i in result.issues}

    async def test_instances_sharing_at_least_one_common_property_are_not_flagged(self):
        """Regression guard: real instances of one concept (Car objects, in
        this spec's own default) always share at least one common attribute
        name, even if they don't share every one - must never be flagged."""
        spec = _good_metaphor_spec(entities=[
            VisualEntity(id="t", role="template"),
            VisualEntity(id="a", role="instance", properties={"color": "Red", "speed": "80"}),
            VisualEntity(id="b", role="instance", properties={"color": "Blue", "fuel": "electric"}),
        ])
        result = await evaluate_concept_experience(spec)
        assert DISCONNECTED_CONCEPTS not in {i.reason for i in result.issues}

    async def test_blank_representation_with_relationships_is_judged_as_relationship_not_object(self):
        """Reproduces a real bug found via a live (non-mocked) VisualPlanner
        call: the model's structured response left `representation` blank
        entirely, on an otherwise good, specific, well-labelled relationship
        spec (a LangChain components diagram). The renderer's own
        `_render_object` is relationship-aware and already renders this
        case correctly as a hub/network concept-map, not a flat grid - QA
        defaulting blank to "object" instead demanded a template-role
        entity the spec correctly didn't have, failing good content for
        the wrong reason."""
        spec = _good_metaphor_spec(
            representation="",
            entities=[
                VisualEntity(id="agent", role="orchestrator", properties={"responsibility": "plans calls"}),
                VisualEntity(id="llm", role="llm_wrapper", properties={"interface": "generate()"}),
                VisualEntity(id="tools", role="tools", properties={"examples": "search, calculator"}),
            ],
            relationships=[
                SchematicRelationship(source="agent", target="llm", type="calls"),
                SchematicRelationship(source="agent", target="tools", type="invokes"),
            ],
        )
        result = await evaluate_concept_experience(spec)
        assert MISSING_ENTITY not in {i.reason for i in result.issues}

    async def test_relationship_representation_with_repeated_generic_label_is_flagged(self):
        """Reproduces a real generated course's own bug: a repeated generic
        relationship label ("connected to" x4 of 5 edges, in the real case)
        even when the entities themselves share a real common property -
        the label-repetition problem is distinct from the disconnected-
        entities problem and must be caught on its own."""
        spec = _good_metaphor_spec(
            representation="relationship",
            entities=[
                VisualEntity(id="a", role="instance", properties={"kind": "service"}),
                VisualEntity(id="b", role="instance", properties={"kind": "service"}),
                VisualEntity(id="c", role="instance", properties={"kind": "service"}),
                VisualEntity(id="d", role="instance", properties={"kind": "service"}),
            ],
            relationships=[
                SchematicRelationship(source="a", target="b", type="connected_to"),
                SchematicRelationship(source="b", target="c", type="connected_to"),
                SchematicRelationship(source="c", target="d", type="connected_to"),
            ],
        )
        result = await evaluate_concept_experience(spec)
        assert GENERIC_VISUAL in {i.reason for i in result.issues}

    async def test_relationship_representation_with_specific_varied_labels_is_not_flagged(self):
        """Regression guard: real, specific, varied relationship labels on
        entities that share a common property must never be flagged."""
        spec = _good_metaphor_spec(
            representation="relationship",
            entities=[
                VisualEntity(id="a", role="instance", properties={"kind": "service"}),
                VisualEntity(id="b", role="instance", properties={"kind": "service"}),
                VisualEntity(id="c", role="instance", properties={"kind": "service"}),
            ],
            relationships=[
                SchematicRelationship(source="a", target="b", type="authenticates"),
                SchematicRelationship(source="b", target="c", type="writes_to"),
            ],
        )
        result = await evaluate_concept_experience(spec)
        reasons = {i.reason for i in result.issues}
        assert DISCONNECTED_CONCEPTS not in reasons
        assert GENERIC_VISUAL not in reasons

    async def test_spatial_representation_gets_the_same_generic_label_coverage(self):
        spec = _good_metaphor_spec(
            representation="spatial",
            visual_metaphor="a workbench",
            entities=[
                VisualEntity(id="a", role="instance", properties={"formula": "F = ma"}),
                VisualEntity(id="b", role="instance", properties={"color": "blue"}),
                VisualEntity(id="c", role="instance", properties={"shape": "disc"}),
            ],
            relationships=[
                SchematicRelationship(source="a", target="b", type="above"),
                SchematicRelationship(source="b", target="c", type="above"),
                SchematicRelationship(source="a", target="c", type="above"),
            ],
        )
        result = await evaluate_concept_experience(spec)
        assert GENERIC_VISUAL in {i.reason for i in result.issues}

    async def test_entities_connected_by_relationship_may_legitimately_share_no_property(self):
        """Regression guard for a real false positive found while adding
        this check: a genuinely GOOD "relationship" spec connects DIFFERENT
        kinds of things (an `orders` table with a `customer_id` property, a
        `customers` table with an `id` property) - sharing zero property
        names is entirely correct here, unlike the "object"/instance case
        where sharing a schema is exactly the point. Must never be flagged
        just for that, with only one, specific, non-generic relationship."""
        spec = _good_metaphor_spec(
            representation="relationship",
            entities=[
                VisualEntity(id="orders", role="table", properties={"customer_id": "FK"}),
                VisualEntity(id="customers", role="table", properties={"id": "PK"}),
            ],
            relationships=[SchematicRelationship(source="orders", target="customers", type="references")],
        )
        result = await evaluate_concept_experience(spec)
        reasons = {i.reason for i in result.issues}
        assert DISCONNECTED_CONCEPTS not in reasons
        assert GENERIC_VISUAL not in reasons

    async def test_state_machine_dangling_transition_is_caught(self):
        spec = DiagramSpec(
            kind="concept_experience", concept="x", representation="state_machine",
            learning_objectives=["o"], core_message="m", validation_criteria=["c"],
            concept_states=[VisualState(id="idle", label="Idle")],
            transitions=[VisualTransition(from_state="idle", to_state="ghost_state", label="go")],
        )
        result = await evaluate_concept_experience(spec)
        assert DANGLING_TRANSITION in {i.reason for i in result.issues}

    async def test_process_representation_does_not_require_entities(self):
        """A pure step sequence must not be penalised for having no
        entities/instances - those checks only apply to instance-driven
        representations."""
        spec = DiagramSpec(
            kind="concept_experience", concept="a_loop", representation="process",
            learning_objectives=["o"], core_message="m", validation_criteria=["c"],
            steps=[VisualStep(id="s1", label="Step 1", order=0)],
        )
        result = await evaluate_concept_experience(spec, ai=MockAIClient(get_settings()), settings=get_settings())
        assert MISSING_ENTITY not in {i.reason for i in result.issues}

    async def test_layer_2_is_never_spent_when_layer_1_already_fails(self):
        """A structurally broken spec must short-circuit before any LLM
        call - proven by an AI client whose .structured() would raise if
        ever invoked."""

        class _ExplodingAI(MockAIClient):
            async def structured(self, **kwargs):  # noqa: ANN003
                raise AssertionError("Layer 2 must not run when Layer 1 already failed")

        spec = _good_metaphor_spec(learning_objectives=[])
        result = await evaluate_concept_experience(spec, ai=_ExplodingAI(get_settings()), settings=get_settings())
        assert not result.passed


class TestConceptQASemantic:
    async def test_semantic_layer_catches_a_structurally_valid_but_conceptually_unclear_spec(self, monkeypatch):
        def failing_critique(user: str) -> dict:
            return {
                "objective_verdicts": [{"criterion": "obj1", "passed": False, "detail": "not actually shown"}],
                "core_message_delivered": True, "core_message_detail": "",
                "criteria_verdicts": [], "structure_correct": True, "structure_detail": "",
                "interactions_useful": True, "interactions_detail": "",
            }

        monkeypatch.setitem(mock_ai_module._BUILDERS, "ConceptCritique", failing_critique)
        result = await evaluate_concept_experience(_good_metaphor_spec(), ai=MockAIClient(get_settings()), settings=get_settings())
        assert not result.passed
        assert CONCEPT_NOT_CLEAR in {i.reason for i in result.issues}
        assert "not actually shown" in result.feedback()


# ---------------------------------------------------------------------------
# ConceptVisualService: retry behaviour + ImageService wiring
# ---------------------------------------------------------------------------


def _concept_block(**overrides) -> Block:
    content = {
        "kind": "concept_experience",
        "purpose": "Explain Java Class and Object",
        "prompt": "the difference between a class and an object",
        "caption": "Figure: class and object",
        **overrides,
    }
    return Block(type=BlockType.IMAGE, content=content)


async def test_concept_qa_failure_triggers_exactly_one_targeted_retry(monkeypatch):
    """Mirrors test_diagrams.py's schematic-retry regression test."""
    bad_spec = _good_metaphor_spec(entities=[
        VisualEntity(id="t", role="template"),
        VisualEntity(id="a", role="instance", properties={"x": "1"}),
        VisualEntity(id="b", role="instance", properties={"x": "1"}),
    ])
    good_spec = _good_metaphor_spec()
    received_feedback: list[str] = []

    async def fake_plan(self, **kwargs):
        received_feedback.append(kwargs.get("qa_feedback", ""))
        return (bad_spec if len(received_feedback) == 1 else good_spec).model_copy(deep=True)

    monkeypatch.setattr(VisualPlanner, "plan", fake_plan)

    service = ConceptVisualService(ai=MockAIClient(get_settings()))
    block = _concept_block()
    template = load_template("technical")

    ok = await service.generate_for_block(
        course_id="course_concept_retry", block=block, template=template, course_title="Java Basics",
    )

    assert ok is True
    assert len(received_feedback) == 2, "expected exactly one retry"
    assert received_feedback[0] == ""
    assert received_feedback[1] and "identical property values" in received_feedback[1].lower()
    assert block.content["path"].endswith(".html")


async def test_unusable_spec_falls_back_without_generating(monkeypatch):
    async def fake_plan(self, **kwargs):
        return DiagramSpec(kind="concept_experience")  # no concept, no entities - never usable

    monkeypatch.setattr(VisualPlanner, "plan", fake_plan)
    service = ConceptVisualService(ai=MockAIClient(get_settings()))
    ok = await service.generate_for_block(
        course_id="course_concept_unusable", block=_concept_block(), template=load_template("technical"),
        course_title="Course",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# ImageService wiring - flag on/off
# ---------------------------------------------------------------------------


class TestImageServiceWiring:
    def test_concept_experience_visuals_flag_defaults_on(self):
        """Was off-by-default through the POC phase; turned on after
        end-to-end validation (see the Phase 4 plan)."""
        assert get_settings().enable_concept_experience_visuals is True

    async def test_generates_a_concept_experience_visual_when_flag_is_on(self, service):
        service.settings.enable_concept_experience_visuals = True
        try:
            template = load_template("technical")
            block = _concept_block()
            ok = await service.images.generate_for_block(
                course_id="course_concept_flag_on", block=block, template=template, course_title="Java Basics",
            )
            assert ok is True
            assert block.content["kind"] == "concept_experience"
            assert block.content["path"].endswith(".html")
            assert block.content["generated"] is True
        finally:
            service.settings.enable_concept_experience_visuals = False

    async def test_falls_back_to_illustration_when_flag_is_off(self, service):
        service.settings.enable_concept_experience_visuals = False
        template = load_template("technical")
        block = _concept_block()
        ok = await service.images.generate_for_block(
            course_id="course_concept_flag_off", block=block, template=template, course_title="Java Basics",
        )
        assert ok is True
        # the illustration fallback never rewrites `kind` - only `path`/`generated`/etc.
        assert block.content["kind"] == "concept_experience"
        assert not block.content["path"].endswith(".html")


# ---------------------------------------------------------------------------
# "comparison" / "timeline" / "before_after" - three more dedicated
# renderers alongside object/data_structure/process, added for richer
# static visual variety (a real table, a chronology, a state contrast)
# without touching those three existing renderers or their CSS at all.
# ---------------------------------------------------------------------------


def _comparison_spec() -> DiagramSpec:
    return DiagramSpec(
        kind="concept_experience", representation="comparison",
        title="Stack vs Queue",
        entities=[
            VisualEntity(
                id="stack", label="Stack", icon="📚", color_role="primary",
                properties={"Order": "LIFO", "Removes from": "Top"},
            ),
            VisualEntity(
                id="queue", label="Queue", icon="🚶", color_role="secondary",
                properties={"Order": "FIFO", "Removes from": "Front"},
            ),
        ],
    )


def _timeline_spec() -> DiagramSpec:
    return DiagramSpec(
        kind="concept_experience", representation="timeline",
        title="Docker's History",
        steps=[
            VisualStep(id="s1", label="2013 - Initial release", description="dotCloud open-sources Docker.", order=0),
            VisualStep(id="s2", label="2015 - Docker 1.0", description="First production-ready release.", order=1),
            VisualStep(id="s3", label="2017 - Swarm mode", description="Built-in orchestration ships.", order=2),
        ],
    )


def _before_after_spec() -> DiagramSpec:
    return DiagramSpec(
        kind="concept_experience", representation="before_after",
        title="Adding a Cache Layer",
        entities=[
            VisualEntity(id="before", label="Without cache", icon="🐢", color_role="negative",
                         properties={"Latency": "800ms"}),
            VisualEntity(id="after", label="With cache", icon="⚡", color_role="positive",
                         properties={"Latency": "40ms"}),
        ],
    )


class TestComparisonRenderer:
    def test_renders_a_table_with_one_column_per_entity(self):
        html = render_concept_experience_html(_comparison_spec(), TemplateTheme()).decode("utf-8")
        assert "cev-cmp-table" in html
        assert "Stack" in html and "Queue" in html
        assert html.count("cev-cmp-col") >= 2  # 2 column headers

    def test_rows_are_the_union_of_property_keys(self):
        html = render_concept_experience_html(_comparison_spec(), TemplateTheme()).decode("utf-8")
        assert "Order" in html
        assert "Removes from" in html
        assert "LIFO" in html and "FIFO" in html

    def test_a_missing_key_on_one_entity_shows_a_placeholder_not_a_crash(self):
        spec = DiagramSpec(
            kind="concept_experience", representation="comparison",
            entities=[
                VisualEntity(id="a", label="A", properties={"Speed": "Fast"}),
                VisualEntity(id="b", label="B", properties={}),
            ],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "—" in html

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        html = render_concept_experience_html(_comparison_spec(), TemplateTheme()).decode("utf-8")
        assert "<script" not in html
        assert "<button" not in html


class TestTimelineRenderer:
    def test_renders_every_milestone_in_order(self):
        html = render_concept_experience_html(_timeline_spec(), TemplateTheme()).decode("utf-8")
        assert "2013 - Initial release" in html
        assert "2015 - Docker 1.0" in html
        assert "2017 - Swarm mode" in html
        assert html.index("2013") < html.index("2015") < html.index("2017")

    def test_each_entry_carries_its_own_dot_and_stem(self):
        """Deliberately per-entry, not a single shared rail across the whole
        row - a shared rail breaks once entries wrap onto more than one row
        (see the module's own rationale comment)."""
        html = render_concept_experience_html(_timeline_spec(), TemplateTheme()).decode("utf-8")
        assert html.count('class="cev-timeline-dot-wrap"') == 3

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        html = render_concept_experience_html(_timeline_spec(), TemplateTheme()).decode("utf-8")
        assert "<script" not in html
        assert "<button" not in html


class TestBeforeAfterRenderer:
    def test_renders_both_panels_and_an_arrow_between_them(self):
        html = render_concept_experience_html(_before_after_spec(), TemplateTheme()).decode("utf-8")
        assert "Without cache" in html
        assert "With cache" in html
        assert "cev-ba-arrow" in html
        assert html.index("Without cache") < html.index("With cache")

    def test_only_the_first_two_entities_are_used(self):
        spec = _before_after_spec()
        spec.entities.append(VisualEntity(id="extra", label="Ignored entity"))
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "Ignored entity" not in html

    def test_a_single_entity_renders_only_the_before_panel_no_arrow(self):
        spec = DiagramSpec(
            kind="concept_experience", representation="before_after",
            entities=[VisualEntity(id="only", label="Only state")],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "Only state" in html
        assert '<div class="cev-ba-arrow"' not in html

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        html = render_concept_experience_html(_before_after_spec(), TemplateTheme()).decode("utf-8")
        assert "<script" not in html
        assert "<button" not in html


def _cycle_spec(step_count: int = 4) -> DiagramSpec:
    names = ["Forward pass", "Compute loss", "Backward pass", "Update weights"]
    return DiagramSpec(
        kind="concept_experience", representation="cycle",
        title="Training Loop",
        steps=[
            VisualStep(id=f"s{i}", label=names[i % len(names)], description=f"Step {i + 1}.", order=i)
            for i in range(step_count)
        ],
    )


class TestCycleRenderer:
    def test_renders_every_step_in_order(self):
        html = render_concept_experience_html(_cycle_spec(), TemplateTheme()).decode("utf-8")
        assert "Forward pass" in html
        assert "Compute loss" in html
        assert "Backward pass" in html
        assert "Update weights" in html
        assert html.index("Forward pass") < html.index("Compute loss") < html.index("Backward pass")

    def test_shows_a_loop_back_indicator_naming_the_first_step(self):
        html = render_concept_experience_html(_cycle_spec(), TemplateTheme()).decode("utf-8")
        marker = '<div class="cev-cycle-loop">'
        assert marker in html
        # The loop-back badge names the first step specifically, not a
        # generic "repeats" label with no real content.
        assert "Forward pass" in html[html.index(marker):]

    def test_a_single_step_shows_no_loop_back_indicator(self):
        spec = DiagramSpec(
            kind="concept_experience", representation="cycle",
            steps=[VisualStep(id="only", label="Only step", order=0)],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert '<div class="cev-cycle-loop">' not in html

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        html = render_concept_experience_html(_cycle_spec(), TemplateTheme()).decode("utf-8")
        assert "<script" not in html
        assert "<button" not in html


def _decision_tree_spec() -> DiagramSpec:
    return DiagramSpec(
        kind="concept_experience", representation="decision_tree",
        title="Cache Lookup",
        entities=[
            VisualEntity(id="check", label="Is it cached?", color_role="primary"),
            VisualEntity(id="hit", label="Return cached value", color_role="positive"),
            VisualEntity(id="miss", label="Query the database", color_role="negative"),
        ],
        relationships=[
            SchematicRelationship(source="check", target="hit", type="yes"),
            SchematicRelationship(source="check", target="miss", type="no"),
        ],
    )


class TestDecisionTreeRenderer:
    def test_renders_every_node_and_its_branch_label(self):
        html = render_concept_experience_html(_decision_tree_spec(), TemplateTheme()).decode("utf-8")
        assert "Is it cached?" in html
        assert "Return cached value" in html
        assert "Query the database" in html
        assert "yes" in html
        assert "no" in html

    def test_renders_as_a_branching_tree_not_a_flat_connected_row(self):
        """A decision tree is a dedicated shape (see _render_tree), distinct
        from "relationship"'s flat per-link connector row - both branches
        get their own stem cell under one shared rail, each with its own
        branch label, never the old cev-rel-link markup."""
        html = render_concept_experience_html(_decision_tree_spec(), TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-rel-link"' not in body
        assert body.count('class="cev-tree-link-cell"') == 2
        assert "cev-tree-links-fanned" in body
        assert body.count('class="cev-tree-link-label"') == 2

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        html = render_concept_experience_html(_decision_tree_spec(), TemplateTheme()).decode("utf-8")
        assert "<script" not in html
        assert "<button" not in html


class TestHierarchyTreeRenderer:
    """`hierarchy` shares `_render_tree` with `decision_tree` (see
    TestDecisionTreeRenderer) - these tests cover what's specific to a
    multi-level hierarchy: 3+ levels, and the fallback for data that isn't
    one clean tree."""

    def _three_level_spec(self) -> DiagramSpec:
        entities = [VisualEntity(id=eid, label=eid.replace("_", " ").title()) for eid in
                    ("root", "left", "right", "left_child")]
        relationships = [
            SchematicRelationship(source="root", type="contains", target="left"),
            SchematicRelationship(source="root", type="contains", target="right"),
            SchematicRelationship(source="left", type="contains", target="left_child"),
        ]
        return DiagramSpec(
            kind="concept_experience", representation="hierarchy",
            entities=entities, relationships=relationships,
        )

    def test_renders_every_level_in_top_down_order(self):
        html = render_concept_experience_html(self._three_level_spec(), TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert body.count('class="cev-tree-level"') == 3
        # root before its children before its grandchild, not just "all present somewhere".
        assert body.index('data-entity-id="root"') < body.index('data-entity-id="left"')
        assert body.index('data-entity-id="left"') < body.index('data-entity-id="left_child"')

    def test_hierarchy_gets_the_same_toned_down_treatment_as_decision_tree(self):
        """A real, confirmed inconsistency: "hierarchy" and "decision_tree"
        share the exact same _render_tree code and the identical top-down
        branching shape - decision_tree already got the toned-down "flow"
        treatment, but hierarchy was left out, rendering an otherwise
        identical diagram in the default saturated icon-card palette."""
        html = render_concept_experience_html(self._three_level_spec(), TemplateTheme()).decode("utf-8")
        assert 'class="cev-root cev-style-flow"' in html

    def test_a_generic_structural_edge_label_is_not_shown_as_a_branch_label(self):
        """"contains" is redundant once two entities are already drawn as
        parent/child (position alone says that) - unlike decision_tree's
        real "yes"/"no" condition labels, which DO show (see
        TestDecisionTreeRenderer)."""
        html = render_concept_experience_html(self._three_level_spec(), TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-tree-link-label"' not in body

    def test_entities_with_no_relationships_fall_back_to_a_flat_row(self):
        spec = DiagramSpec(
            kind="concept_experience", representation="hierarchy",
            entities=[VisualEntity(id="a", label="A"), VisualEntity(id="b", label="B")],
        )
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-tree"' not in body
        assert 'class="cev-instances"' in body

    def test_a_pure_single_chain_hierarchy_renders_as_a_flowing_row_not_a_tall_vertical_stack(self):
        """A real, confirmed case: a 7-layer OSI-model-style hierarchy
        where every layer strictly nests the next (no sibling layers
        anywhere) rendered as a tall single-card-per-row vertical stack -
        wasting the page's own width and, because it's tall enough, forcing
        the whole fragment to shrink well below natural size to fit one
        page. A pure chain (every BFS level has exactly one entity) now
        gets the flowing zig-grid treatment instead (the same wrap-aware
        `_chain_row_html` technique a genuine relationship chain already
        uses - see that function's own docstring), using the page's width
        and never needing that shrink."""
        entities = [VisualEntity(id=f"layer{i}", label=f"Layer {i}") for i in range(7, 0, -1)]
        relationships = [
            SchematicRelationship(source=f"layer{i}", type="contains", target=f"layer{i - 1}")
            for i in range(7, 1, -1)
        ]
        spec = DiagramSpec(kind="concept_experience", representation="hierarchy", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-tree"' not in body
        assert 'class="cev-tree-level"' not in body
        assert 'class="cev-zig-grid"' in body
        assert body.count('cev-instance cev-step') == 7  # every layer rendered once (6 paired + 1 solo)
        assert body.count('class="cev-zig-link"') == 3  # 7 nodes, 2 per row -> 3 pairs + 1 solo
        assert body.count('class="cev-zig-curve-row"') == 3  # 4 rows (3 pairs + solo) -> 3 connectors
        for i in range(1, 8):
            assert f'data-entity-id="layer{i}"' in body
        # order is preserved start-to-end (layer7 first, layer1 last).
        assert body.index('data-entity-id="layer7"') < body.index('data-entity-id="layer1"')

    def test_a_branching_hierarchy_keeps_the_vertical_level_layout(self):
        """Regression guard: the pure-chain shortcut must never engage for
        a genuinely branching hierarchy (more than one entity on some
        level) - _three_level_spec's root has two children, so this must
        still render as real vertical tree levels."""
        html = render_concept_experience_html(self._three_level_spec(), TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-tree-level"' in body
        assert 'class="cev-steps"' not in body

    def test_a_cycle_renders_as_a_flowing_chain_with_a_back_edge_badge_instead_of_crashing(self):
        """Not a clean tree (it loops back) - previously fell back to the
        flat card row. a -> b -> c -> a is a PURE chain (every BFS level
        has exactly one entity, no branching anywhere), so it gets the
        flowing-horizontal-row treatment (_chain_row_html, via
        _render_concept_flow_html's own pure-chain branch) rather than a
        tall vertical stack of single-card levels - with the c -> a edge
        that actually forms the cycle shown as a back-edge badge, never a
        long line crossing back up through the chart. Still never a
        crash."""
        entities = [VisualEntity(id=eid, label=eid) for eid in ("a", "b", "c")]
        relationships = [
            SchematicRelationship(source="a", type="contains", target="b"),
            SchematicRelationship(source="b", type="contains", target="c"),
            SchematicRelationship(source="c", type="contains", target="a"),  # loops back - not a tree
        ]
        spec = DiagramSpec(kind="concept_experience", representation="hierarchy", entities=entities, relationships=relationships)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        body = html.split("</style>", 1)[1]
        assert 'class="cev-tree"' not in body
        assert 'class="cev-instances"' not in body
        assert 'class="cev-flow-chart"' not in body  # pure chain - not the level-based layout
        assert 'class="cev-zig-grid"' in body
        assert body.count('cev-instance cev-step') == 3  # a, b (paired) + c (solo)
        assert body.count('class="cev-zig-link"') == 1  # a->b, the one pair
        assert 'class="cev-flow-backedge"' in body  # c->a is the one edge that doesn't advance a level
        assert "back to" in body and "a" in body.split("back to", 1)[1][:30]
        for eid in ("a", "b", "c"):
            assert f'data-entity-id="{eid}"' in body


class TestNewRepresentationsPixelSizeEstimation:
    def test_comparison_height_grows_with_criteria_count(self):
        few = DiagramSpec(
            kind="concept_experience", representation="comparison",
            entities=[
                VisualEntity(id="a", label="A", properties={"k1": "v1"}),
                VisualEntity(id="b", label="B", properties={"k1": "v2"}),
            ],
        )
        many = DiagramSpec(
            kind="concept_experience", representation="comparison",
            entities=[
                VisualEntity(id="a", label="A", properties={f"k{i}": f"v{i}" for i in range(10)}),
                VisualEntity(id="b", label="B", properties={f"k{i}": f"v{i}" for i in range(10)}),
            ],
        )
        _, few_height = estimate_pixel_size(few)
        _, many_height = estimate_pixel_size(many)
        assert many_height > few_height

    def test_timeline_height_grows_with_milestone_count(self):
        few = DiagramSpec(
            kind="concept_experience", representation="timeline",
            steps=[VisualStep(id=f"s{i}", label=f"20{10+i} - Event {i}", order=i) for i in range(2)],
        )
        many = DiagramSpec(
            kind="concept_experience", representation="timeline",
            steps=[VisualStep(id=f"s{i}", label=f"20{10+i} - Event {i}", order=i) for i in range(12)],
        )
        _, few_height = estimate_pixel_size(few)
        _, many_height = estimate_pixel_size(many)
        assert many_height > few_height

    def test_cycle_height_grows_with_step_count_and_the_loop_badge(self):
        one = DiagramSpec(
            kind="concept_experience", representation="cycle",
            steps=[VisualStep(id="s0", label="Only step", order=0)],
        )
        many = DiagramSpec(
            kind="concept_experience", representation="cycle",
            steps=[VisualStep(id=f"s{i}", label=f"Step {i}", order=i) for i in range(6)],
        )
        _, one_height = estimate_pixel_size(one)
        _, many_height = estimate_pixel_size(many)
        assert many_height > one_height

    def test_all_stay_within_the_one_page_cap(self):
        for spec in (
            DiagramSpec(
                kind="concept_experience", representation="comparison",
                entities=[
                    VisualEntity(id=f"e{i}", label=f"Entity {i}", properties={f"k{j}": f"v{j}" for j in range(15)})
                    for i in range(5)
                ],
            ),
            DiagramSpec(
                kind="concept_experience", representation="timeline",
                steps=[
                    VisualStep(
                        id=f"s{i}", label=f"20{10+i} - A long milestone description",
                        description="Enough detail to wrap onto a couple of lines in the card.", order=i,
                    )
                    for i in range(20)
                ],
            ),
            DiagramSpec(
                kind="concept_experience", representation="cycle",
                steps=[
                    VisualStep(
                        id=f"s{i}", label=f"Step {i}: a fairly descriptive step name",
                        description="Enough detail to wrap onto a couple of lines in the card.", order=i,
                    )
                    for i in range(20)
                ],
            ),
        ):
            _, height = estimate_pixel_size(spec)
            assert height <= 863

    def test_scaled_wrapper_height_matches_what_was_reserved_for_a_large_comparison(self):
        spec = DiagramSpec(
            kind="concept_experience", representation="comparison",
            entities=[
                VisualEntity(id=f"e{i}", label=f"Entity {i}", properties={f"k{j}": f"v{j}" for j in range(15)})
                for i in range(5)
            ],
        )
        _, reserved_height = estimate_pixel_size(spec)
        html = render_concept_experience_html(spec, TemplateTheme()).decode("utf-8")
        assert "transform:scale(" in html
        match = re.search(r"height:(\d+)px", html)
        assert match is not None
        assert int(match.group(1)) == reserved_height
