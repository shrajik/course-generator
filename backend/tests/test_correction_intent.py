"""Correction-intent classification (diagram quality audit follow-up,
Phase 7) - app.services.correction_intent."""

from __future__ import annotations

from app.services.correction_intent import CORRECTION_INTENT_POLICY, classify_correction_intent


def test_geometry_intent_is_detected():
    assert classify_correction_intent("Fix the overlapping arrow text") == "geometry"


def test_styling_intent_is_detected():
    assert classify_correction_intent("Change the colors to be more professional") == "styling"


def test_audience_intent_is_detected():
    assert classify_correction_intent("Make this suitable for children") == "audience"


def test_density_intent_is_detected():
    assert classify_correction_intent("This is too crowded, simplify it") == "density"


def test_structural_intent_is_detected():
    assert classify_correction_intent("Add a node for the validation step") == "structural"


def test_semantic_intent_is_detected():
    assert classify_correction_intent("This relationship is wrong, it should be reversed") == "semantic"


def test_type_change_intent_is_detected():
    assert classify_correction_intent("Redesign this as a timeline instead") == "type_change"


def test_expansion_intent_is_detected():
    assert classify_correction_intent("Can you expand on this with more detail") == "expansion"


def test_no_match_returns_none():
    assert classify_correction_intent("ok") is None
    assert classify_correction_intent("") is None
    assert classify_correction_intent("   ") is None


def test_priority_order_resolves_ambiguous_prompts_to_one_intent():
    """"Simplify this for a younger audience" touches both audience and
    density signals - audience is checked first (more specific/consequential)
    so it must win, not density."""
    assert classify_correction_intent("Simplify this for a younger audience") == "audience"


def test_every_intent_has_a_policy_sentence():
    from app.services.correction_intent import _CORRECTION_INTENT_PATTERNS

    for intent, _pattern in _CORRECTION_INTENT_PATTERNS:
        assert intent in CORRECTION_INTENT_POLICY, f"{intent} has no policy sentence"
        assert CORRECTION_INTENT_POLICY[intent].strip()
