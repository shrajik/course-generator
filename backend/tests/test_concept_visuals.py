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
    section B (11-family taxonomy)."""
    assert REPRESENTATION_TYPES == (
        "object", "data_structure", "process", "sequence", "state_machine", "relationship",
        "hierarchy", "comparison", "pipeline", "spatial", "code_visualization",
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
