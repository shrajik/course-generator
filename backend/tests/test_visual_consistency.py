"""Course-wide recurring-concept visual consistency (diagram quality audit
follow-up, Phase 8) - app.services.visual_consistency.

Production-verification follow-up: these tests specifically cover the two
confirmed defects found when auditing the first implementation - (1) the
`diagram` and `concept_experience` pipelines use incompatible icon
vocabularies and must never share state, and (2) the registry must reject
an unrecognised diagram icon rather than record/apply it, plus the LRU
bound that keeps it from growing unboundedly in a long-lived process."""

from __future__ import annotations

from app.schemas.diagram import DiagramNode, DiagramSpec, VisualEntity
from app.services.visual_consistency import (
    VisualConsistencyRegistry,
    apply_concept_consistency,
    apply_diagram_consistency,
)


def test_first_seen_label_is_recorded_not_overridden():
    registry = VisualConsistencyRegistry()
    overrides = registry.apply_and_record(
        "diagram", "course_1", [("Database", "database")], is_valid_icon=lambda i: True
    )
    assert overrides == {}
    assert registry.known_icons("diagram", "course_1") == {"database": "database"}


def test_a_later_diagram_with_a_different_icon_for_the_same_label_gets_overridden():
    registry = VisualConsistencyRegistry()
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=valid)
    overrides = registry.apply_and_record("diagram", "course_1", [("Database", "server")], is_valid_icon=valid)
    assert overrides == {"Database": "database"}


def test_a_later_diagram_with_the_same_icon_already_is_not_flagged_as_an_override():
    registry = VisualConsistencyRegistry()
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=valid)
    overrides = registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=valid)
    assert overrides == {}


def test_label_matching_is_case_and_whitespace_insensitive():
    registry = VisualConsistencyRegistry()
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=valid)
    overrides = registry.apply_and_record("diagram", "course_1", [("  database  ", "server")], is_valid_icon=valid)
    assert overrides == {"  database  ": "database"}


def test_different_courses_never_share_assignments():
    registry = VisualConsistencyRegistry()
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=valid)
    overrides = registry.apply_and_record("diagram", "course_2", [("Database", "server")], is_valid_icon=valid)
    assert overrides == {}
    assert registry.known_icons("diagram", "course_2") == {"database": "server"}


def test_a_label_with_no_icon_at_all_is_never_recorded():
    registry = VisualConsistencyRegistry()
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_1", [("Database", "")], is_valid_icon=valid)
    overrides = registry.apply_and_record("diagram", "course_1", [("Database", "server")], is_valid_icon=valid)
    assert overrides == {}  # nothing was ever recorded, so "server" is simply the new first choice


def test_an_invalid_icon_is_never_recorded_as_the_canonical_choice():
    """The core defect this closes: a model's typo/unrecognised icon string
    must never become "the course's canonical choice" for a label and later
    downgrade a DIFFERENT diagram's own, correctly-recognised icon."""
    registry = VisualConsistencyRegistry()
    is_valid = lambda icon: icon == "database"  # noqa: E731 - only this one keyword is "recognised"
    # First diagram's model made a typo - never recorded.
    first_overrides = registry.apply_and_record(
        "diagram", "course_1", [("Database", "databse")], is_valid_icon=is_valid
    )
    assert first_overrides == {}
    assert registry.known_icons("diagram", "course_1") == {}
    # A later diagram correctly uses the real keyword - nothing to override it with.
    second_overrides = registry.apply_and_record(
        "diagram", "course_1", [("Database", "database")], is_valid_icon=is_valid
    )
    assert second_overrides == {}
    assert registry.known_icons("diagram", "course_1") == {"database": "database"}


def test_diagram_and_concept_namespaces_never_share_assignments():
    """The second core defect this closes: `diagram` keywords and
    `concept_experience` emoji/display-text must never cross-contaminate,
    even for the identical label in the identical course."""
    registry = VisualConsistencyRegistry()
    registry.apply_and_record("diagram", "course_1", [("Database", "database")], is_valid_icon=lambda i: True)
    overrides = registry.apply_and_record(
        "concept", "course_1", [("Database", "📀")], is_valid_icon=lambda i: True
    )
    assert overrides == {}  # the concept namespace has never seen "Database" before
    assert registry.known_icons("concept", "course_1") == {"database": "📀"}
    assert registry.known_icons("diagram", "course_1") == {"database": "database"}


def test_registry_evicts_the_least_recently_used_bucket_once_over_its_bound():
    registry = VisualConsistencyRegistry(max_buckets=2)
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_a", [("X", "a")], is_valid_icon=valid)
    registry.apply_and_record("diagram", "course_b", [("X", "a")], is_valid_icon=valid)
    registry.apply_and_record("diagram", "course_c", [("X", "a")], is_valid_icon=valid)  # pushes course_a out
    assert registry.known_icons("diagram", "course_a") == {}
    assert registry.known_icons("diagram", "course_b") == {"x": "a"}
    assert registry.known_icons("diagram", "course_c") == {"x": "a"}


def test_registry_touching_a_bucket_keeps_it_from_being_evicted():
    registry = VisualConsistencyRegistry(max_buckets=2)
    valid = lambda i: True  # noqa: E731
    registry.apply_and_record("diagram", "course_a", [("X", "a")], is_valid_icon=valid)
    registry.apply_and_record("diagram", "course_b", [("X", "a")], is_valid_icon=valid)
    registry.apply_and_record("diagram", "course_a", [("Y", "a")], is_valid_icon=valid)  # touches course_a again
    registry.apply_and_record("diagram", "course_c", [("X", "a")], is_valid_icon=valid)  # should evict course_b now
    assert registry.known_icons("diagram", "course_b") == {}
    assert registry.known_icons("diagram", "course_a") != {}


def test_apply_diagram_consistency_overrides_a_mismatched_node_icon_and_reports_a_change():
    registry = VisualConsistencyRegistry()
    import app.services.visual_consistency as vc_module
    original_registry = vc_module._registry
    vc_module._registry = registry
    try:
        first = DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id="a", label="Database", icon="database")],
        )
        assert apply_diagram_consistency("course_x", first) is False  # first sighting, nothing to override

        second = DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id="b", label="Database", icon="server")],
        )
        changed = apply_diagram_consistency("course_x", second)
        assert changed is True
        assert second.nodes[0].icon == "database"
    finally:
        vc_module._registry = original_registry


def test_apply_diagram_consistency_ignores_an_unrecognised_icon_keyword():
    registry = VisualConsistencyRegistry()
    import app.services.visual_consistency as vc_module
    original_registry = vc_module._registry
    vc_module._registry = registry
    try:
        first = DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id="a", label="Database", icon="not_a_real_icon_keyword")],
        )
        apply_diagram_consistency("course_x2", first)
        assert registry.known_icons("diagram", "course_x2") == {}  # never recorded

        second = DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id="b", label="Database", icon="database")],  # a real one
        )
        changed = apply_diagram_consistency("course_x2", second)
        assert changed is False  # nothing to override it with
        assert second.nodes[0].icon == "database"
    finally:
        vc_module._registry = original_registry


def test_apply_diagram_consistency_is_a_no_op_when_nothing_needs_overriding():
    registry = VisualConsistencyRegistry()
    import app.services.visual_consistency as vc_module
    original_registry = vc_module._registry
    vc_module._registry = registry
    try:
        spec = DiagramSpec(kind="flow_chart", nodes=[DiagramNode(id="a", label="New Concept", icon="gear")])
        assert apply_diagram_consistency("course_y", spec) is False
        assert spec.nodes[0].icon == "gear"
    finally:
        vc_module._registry = original_registry


def test_apply_concept_consistency_overrides_a_mismatched_entity_icon():
    registry = VisualConsistencyRegistry()
    import app.services.visual_consistency as vc_module
    original_registry = vc_module._registry
    vc_module._registry = registry
    try:
        first = DiagramSpec(
            kind="concept_experience", concept="c1",
            entities=[VisualEntity(id="a", label="Database", icon="🗄️")],
        )
        assert apply_concept_consistency("course_z", first) is False

        second = DiagramSpec(
            kind="concept_experience", concept="c2",
            entities=[VisualEntity(id="b", label="Database", icon="📀")],
        )
        assert apply_concept_consistency("course_z", second) is True
        assert second.entities[0].icon == "🗄️"
    finally:
        vc_module._registry = original_registry


def test_diagram_and_concept_pipelines_in_the_same_course_never_cross_contaminate():
    """Regression guard for the fixed defect - a label used in BOTH
    pipelines in the same course must keep each pipeline's own icon
    vocabulary, never leak one into the other."""
    registry = VisualConsistencyRegistry()
    import app.services.visual_consistency as vc_module
    original_registry = vc_module._registry
    vc_module._registry = registry
    try:
        diagram_spec = DiagramSpec(
            kind="flow_chart", nodes=[DiagramNode(id="a", label="Database", icon="database")],
        )
        apply_diagram_consistency("course_shared", diagram_spec)

        concept_spec = DiagramSpec(
            kind="concept_experience", concept="c",
            entities=[VisualEntity(id="b", label="Database", icon="📀")],
        )
        # Must NOT be overridden to the diagram pipeline's "database" keyword.
        changed = apply_concept_consistency("course_shared", concept_spec)
        assert changed is False
        assert concept_spec.entities[0].icon == "📀"
    finally:
        vc_module._registry = original_registry
