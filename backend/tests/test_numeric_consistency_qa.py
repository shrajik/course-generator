"""Deterministic numeric-claim consistency check - app.services.
numeric_consistency_qa (Canonical Visual Spec milestone, Phase 2). Pure
Python, no AI client involved - every case here is a plain unit test."""

from __future__ import annotations

from app.services.numeric_consistency_qa import (
    IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT,
    IDENTIFIER_VALUE_NEEDS_REVIEW,
    NUMERIC_CONTRADICTS_SOURCE_TEXT,
    find_identifier_value_issues,
    find_numeric_contradictions,
)


# --- Equivalent representations must NEVER be flagged. ---------------------

def test_percent_and_decimal_equivalence_is_not_flagged():
    assert find_numeric_contradictions(
        "The probability of success is 50% in this scenario.",
        "Research shows the success probability is 0.5 for this exact scenario.",
    ) == []


def test_fraction_and_percent_equivalence_is_not_flagged():
    assert find_numeric_contradictions(
        "Half (1/2) of the sample reacted.",
        "About 50% of the sample reacted in the trial.",
    ) == []


def test_unicode_vulgar_fraction_equivalence_is_not_flagged():
    assert find_numeric_contradictions(
        "½ of the sample reacted in the trial.",
        "0.5 of the sample reacted, per the original study.",
    ) == []


def test_scientific_notation_forms_are_equivalent():
    assert find_numeric_contradictions(
        "Light travels at 3 x 10^8 m/s in a vacuum.",
        "The speed of light is 3e8 m/s, a universal constant.",
    ) == []


def test_comma_grouped_and_scientific_notation_are_equivalent():
    assert find_numeric_contradictions(
        "Light speed is about 300,000,000 m/s in vacuum.",
        "The speed of light constant is 3 × 10^8 m/s exactly.",
    ) == []


def test_small_rounding_difference_is_not_flagged():
    """Generous relative tolerance absorbs genuine rounding (3.14 vs the
    fuller 3.14159) without papering over a real, substantive mismatch."""
    assert find_numeric_contradictions(
        "Pi is approximately 3.14 in this calculation.",
        "In this calculation, pi is taken as 3.14159.",
    ) == []


# --- Genuine contradictions must be flagged, conservatively. ---------------

def test_a_genuine_percent_contradiction_is_flagged():
    issues = find_numeric_contradictions(
        "The reaction yield was 75% under these conditions.",
        "Under these exact conditions, the reaction yield was only 25%.",
    )
    assert len(issues) == 1
    assert issues[0].reason == NUMERIC_CONTRADICTS_SOURCE_TEXT
    assert issues[0].deterministic is True
    assert "75%" in issues[0].detail and "25%" in issues[0].detail


def test_a_genuine_contradiction_with_shared_specific_keyword_is_flagged():
    issues = find_numeric_contradictions(
        "The boiling point of water at sea level is 120 degrees Celsius in this diagram.",
        "At sea level, the boiling point of water is 100 degrees Celsius, a well-known constant.",
    )
    assert len(issues) == 1
    assert "120" in issues[0].detail and "100" in issues[0].detail


def test_a_large_magnitude_contradiction_is_flagged():
    issues = find_numeric_contradictions(
        "Light speed is given here as 3 x 10^7 m/s.",
        "The speed of light constant is 3 x 10^8 m/s.",
    )
    assert len(issues) == 1


# --- Absent numbers must route to "unsupported", never FAIL here. ----------

def test_a_number_with_no_counterpart_anywhere_in_the_source_is_not_flagged():
    assert find_numeric_contradictions(
        "The satellite orbits at an altitude of 400 km.",
        "This chapter covers orbital mechanics and satellite communication basics.",
    ) == []


def test_blank_source_excerpt_is_not_flagged():
    assert find_numeric_contradictions("The yield was 75%.", "") == []


def test_blank_claim_text_is_not_flagged():
    assert find_numeric_contradictions("", "The yield was 75%.") == []


def test_claim_text_with_no_numbers_at_all_is_not_flagged():
    assert find_numeric_contradictions(
        "This diagram shows the general shape of the reaction pathway.",
        "The reaction yield was 75% under these exact conditions.",
    ) == []


# --- Precision guard: shared GENERIC words must never anchor a match. ------

def test_different_numbers_sharing_only_a_generic_word_are_not_flagged():
    """"Step 3" and "step 9" share only the structural word "step" - a
    coincidental digit difference next to a generic word must never be
    treated as a real contradiction (that needs a SPECIFIC shared keyword)."""
    assert find_numeric_contradictions(
        "Step 3 shows the temperature reaching 100 degrees.",
        "The first step was simple, but by step 9 pressure had changed.",
    ) == []


def test_equal_numbers_sharing_only_a_generic_word_are_not_flagged():
    assert find_numeric_contradictions(
        "There are 12 planets discussed in this unit's bonus trivia.",
        "The course has 12 chapters total, numbered for navigation.",
    ) == []


def test_an_identifier_like_label_never_yields_a_false_numeric_claim():
    """Real-course verification fix: "R1" (a resistor's REFERENCE NAME, not
    a quantity) must never be misparsed as the bare claim "1" - confirmed
    against a real generated circuit-analysis course where a node labelled
    "R1" with detail "10 kOhm" produced a spurious contradiction against
    every unrelated number in the source text that happened to share generic
    context. A digit immediately preceded by a letter is never the start of
    a real number; a digit immediately FOLLOWED by a letter (a unit suffix,
    e.g. "10kOhm") must still parse normally."""
    assert find_numeric_contradictions(
        "R1 10 kOhm, feeding the divider",
        "Assume a 5 V source feeding R1 = 10 kOhm and R2 = 10 kOhm.",
    ) == []


def test_at_most_max_issues_are_returned():
    claim = "Sensor widgets: 1 widgets, 2 widgets, 3 widgets, 4 widgets."
    source = "Sensor widgets: 10 widgets, 20 widgets, 30 widgets, 40 widgets."
    issues = find_numeric_contradictions(claim, source, max_issues=2)
    assert len(issues) <= 2


# --- Short-identifier value matching (context-aware, not a threshold tweak) -

def test_a_chemical_symbol_value_contradiction_is_caught():
    issues = find_identifier_value_issues("Si = 28", "Si = 14")
    assert len(issues) == 1
    assert issues[0].reason == IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT
    assert "Si" in issues[0].detail


def test_a_matching_chemical_symbol_value_is_silent():
    assert find_identifier_value_issues("Fe = 26", "Fe = 26") == []


def test_a_circuit_reference_designator_value_contradiction_is_caught():
    issues = find_identifier_value_issues("R1 = 20 kOhm", "R1 = 10 kOhm")
    assert len(issues) == 1
    assert issues[0].reason == IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT


def test_a_matching_circuit_reference_designator_value_is_silent():
    assert find_identifier_value_issues("I1 = 2 A", "I1 = 2 A") == []


def test_a_short_identifier_absent_from_the_source_stays_silent():
    """Never this module's business - see NUMERIC_CONTRADICTS_SOURCE_TEXT's
    own "absence is never a reason to flag" rule, which applies identically
    here: an identifier the source excerpt never assigns at all routes to
    the separate, LLM-judged "unsupported claim" path instead, never a FAIL
    or a REVIEW from this deterministic layer."""
    assert find_identifier_value_issues("Na = 23", "This text never mentions that identifier at all.") == []


def test_an_identifier_with_only_an_incompatible_unit_in_source_needs_review():
    issues = find_identifier_value_issues("V1 = 5 ohms", "V1 = 5 volts")
    assert len(issues) == 1
    assert issues[0].reason == IDENTIFIER_VALUE_NEEDS_REVIEW


def test_an_internally_ambiguous_source_needs_review_not_a_guess():
    """The source excerpt itself states two different values for the same
    identifier - never silently pick one to compare against, and never
    assume the same short identifier means the same thing without real
    contextual support (hard requirement, Canonical Visual Spec milestone)."""
    issues = find_identifier_value_issues(
        "V1 = 7V", "Elsewhere V1 = 5V and also V1 = 9V in a different example",
    )
    assert len(issues) == 1
    assert issues[0].reason == IDENTIFIER_VALUE_NEEDS_REVIEW


def test_an_internally_ambiguous_source_still_passes_silently_if_one_value_matches():
    """Genuine support from at least one real, compatible occurrence is not
    overridden just because the source ALSO states something else elsewhere
    - this is support, not a contradiction."""
    assert find_identifier_value_issues(
        "V1 = 5V", "Elsewhere V1 = 5V and also V1 = 9V in a different example",
    ) == []


def test_an_ordinary_short_word_followed_by_an_unrelated_number_is_not_misread():
    """"n is used 3 times" must never be misread as the claim "n = 3" - the
    number must sit immediately at the assignment, not merely appear later
    in the same sentence."""
    assert find_identifier_value_issues(
        "n = 3", "In this section n is used 3 times for counting, unrelated to the formula.",
    ) == []


def test_the_ohm_symbol_and_the_spelled_out_unit_are_treated_as_the_same_value():
    """Real-course verification fix (Final Implementation hardening pass):
    confirmed against real course crs_594e8482f753's own chapter_01.json,
    whose own prose spells its resistor values with the Greek Omega symbol
    ("R1=10 kΩ") - a diagram labelling the identical value "R1 = 10 kOhm"
    (spelled out) was being misread as carrying a different, INCOMPATIBLE
    unit purely from the symbol-vs-word difference, routing a genuinely
    matching value to REVIEW instead of a silent pass. A genuinely
    different value must still be a confirmed FAIL either way."""
    source = "Build a resistor divider (e.g., R1=10 kΩ, R2=10 kΩ) from a 5 V source to ground."
    assert find_identifier_value_issues("R1 = 10 kOhm", source) == []
    assert find_identifier_value_issues("R1 = 10 kΩ", source) == []
    issues = find_identifier_value_issues("R1 = 20 kOhm", source)
    assert len(issues) == 1
    assert issues[0].reason == IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT


def test_identifier_forms_with_subscripts_and_unicode_normalise_to_the_same_identifier():
    """R_1 (LaTeX), R^1, and R₁ (Unicode subscript) must all be recognised
    as the SAME identifier as plain R1."""
    for written_form in ("R_1", "R^1", "R₁"):
        issues = find_identifier_value_issues(f"{written_form} = 20 kOhm", "R1 = 10 kOhm")
        assert len(issues) == 1, f"failed for {written_form!r}"
        assert issues[0].reason == IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT
