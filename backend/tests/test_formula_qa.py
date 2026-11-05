"""app.services.formula_qa - deterministic, AST-based verification of a
governing equation (ImageContent.formula_expression). No LLM, no OCR, no
`eval()` - see the module's own docstring for the exact scope and its
honestly-documented limits."""

from __future__ import annotations

from app.services.formula_qa import (
    FORMULA_MISMATCH,
    MALFORMED_EXPRESSION,
    evaluate_formula,
)


# --- correct formulas: exact match, commutative reorder, swapped sides ------


def test_an_exact_match_against_the_reference_passes_and_is_verifiable():
    result = evaluate_formula("F = m * a")
    assert result.passed
    assert result.verifiable


def test_commutative_reordering_of_a_multiplication_still_passes():
    """m * a and a * m are the same quantity - a real formula author has no
    reason to prefer one order over the other, and this must not be
    mistaken for a mismatch."""
    result = evaluate_formula("F = a * m")
    assert result.passed
    assert result.verifiable


def test_swapped_sides_of_the_equation_still_passes():
    """'m * a = F' means exactly what 'F = m * a' means."""
    result = evaluate_formula("m * a = F")
    assert result.passed
    assert result.verifiable


def test_a_formula_using_parentheses_and_a_fraction_coefficient_passes():
    result = evaluate_formula("KE = (1/2) * m * v^2")
    assert result.passed
    assert result.verifiable


# --- legitimate algebraic rearrangements (release-readiness audit fix) ------
# Real, confirmed false-positive this section guards against: "a = F / m"
# (solving Newton's second law for acceleration) was being reported as a
# confirmed `formula_mismatch` against "F = m * a" before this fix, even
# though it is the exact same physical law. See formula_qa.py's own
# `_rearranged_forms` docstring for the precise, bounded scope (a simple
# 2-factor product/quotient reference only).


def test_isolating_the_first_factor_of_a_multiplication_reference_passes():
    """F = m * a  =>  a legitimate rewrite isolating m: m = F / a."""
    result = evaluate_formula("m = F / a")
    assert result.passed
    assert result.verifiable


def test_isolating_the_second_factor_of_a_multiplication_reference_passes():
    """F = m * a  =>  a legitimate rewrite isolating a: a = F / m - the
    EXACT real, confirmed false positive this fix addresses."""
    result = evaluate_formula("a = F / m")
    assert result.passed
    assert result.verifiable


def test_isolating_either_factor_of_a_division_reference_passes():
    """rho = m / V  =>  both legitimate rewrites: m = rho * V (isolating
    the numerator) and V = m / rho (isolating the denominator)."""
    assert evaluate_formula("m = rho * V").passed
    assert evaluate_formula("V = m / rho").passed


def test_rearrangement_support_also_works_with_an_exponent_operand():
    """E = m * c^2  =>  m = E / c^2 - the bounded fix treats "c^2" as one
    atomic factor (see _simple_operand), not something it tries to solve
    c out of."""
    result = evaluate_formula("m = E / c^2")
    assert result.passed
    assert result.verifiable


def test_an_incorrect_rearrangement_is_still_caught_as_a_mismatch():
    """The bounded rearrangement fix must not become so permissive that it
    accepts genuinely wrong equations just because they share a reference's
    symbols and *a* division operator - "a = m / F" is NOT a valid
    rearrangement of F = m * a (it claims the opposite relationship)."""
    result = evaluate_formula("a = m / F")
    assert not result.passed
    assert result.issues[0].reason == FORMULA_MISMATCH


def test_an_incorrect_division_rearrangement_is_still_caught():
    """The direct counterpart for a division-shaped reference: "V = rho / m"
    is NOT a valid rearrangement of rho = m / V."""
    result = evaluate_formula("V = rho / m")
    assert not result.passed
    assert result.issues[0].reason == FORMULA_MISMATCH


def test_rearranging_a_three_factor_formula_is_a_documented_residual_limitation():
    """Honest, deliberate limitation (see formula_qa.py's own module
    docstring): `_rearranged_forms` only covers a simple 2-factor
    product/quotient. "g = PE / (m * h)" is a legitimate, CORRECT
    rearrangement of "PE = m * g * h" (3 factors), but this module cannot
    yet recognise it - it is reported as a confirmed `formula_mismatch`,
    a real false positive this test exists to keep visible (never let a
    future change silently "fix" this without updating this test AND the
    module's own documented scope) rather than silently accepting it."""
    result = evaluate_formula("g = PE / (m * h)")
    assert not result.passed
    assert result.issues[0].reason == FORMULA_MISMATCH


# --- incorrect formulas: changed operators, wrong exponent, wrong variable --


def test_a_changed_operator_is_caught_as_a_mismatch():
    """Real defect class this module exists to catch: dividing instead of
    multiplying changes the physics entirely."""
    result = evaluate_formula("F = m / a")
    assert not result.passed
    assert result.verifiable  # a reference WAS found - this is a confirmed mismatch, not "unverifiable"
    assert result.issues[0].reason == FORMULA_MISMATCH


def test_a_missing_exponent_is_caught_as_a_mismatch():
    result = evaluate_formula("E = m * c")
    assert not result.passed
    assert result.issues[0].reason == FORMULA_MISMATCH


def test_a_wrong_variable_against_a_known_reference_is_caught():
    """Newton's second law uses F/m/a - 'F = m * v' sharing two of three
    symbols is NOT close enough for this module to assume it means Newton's
    second law (that would be guessing); it correctly reports no reference
    could be matched at all, rather than falsely flagging the wrong reason."""
    result = evaluate_formula("F = m * v")
    assert result.passed  # nothing to compare against, so no issue is raised
    assert not result.verifiable  # but it is honestly NOT "confirmed correct" either


# --- malformed expressions ----------------------------------------------------


def test_a_comparison_instead_of_an_equation_is_malformed():
    result = evaluate_formula("F == m * a")
    assert not result.passed
    assert result.issues[0].reason == MALFORMED_EXPRESSION
    assert not result.verifiable


def test_two_equals_signs_is_malformed():
    result = evaluate_formula("F = m * a = 5")
    assert not result.passed
    assert result.issues[0].reason == MALFORMED_EXPRESSION


def test_unbalanced_parentheses_is_malformed():
    result = evaluate_formula("F = (m * a")
    assert not result.passed
    assert result.issues[0].reason == MALFORMED_EXPRESSION


def test_plain_prose_instead_of_an_equation_is_malformed():
    result = evaluate_formula("The force equals mass times acceleration")
    assert not result.passed
    assert result.issues[0].reason == MALFORMED_EXPRESSION


def test_a_blank_expression_is_not_malformed_just_nothing_to_check():
    """The caller (ImageService) only ever calls this when
    `formula_expression` is non-blank in the first place - but the function
    itself must still behave sanely on "", never crash or false-flag it."""
    result = evaluate_formula("")
    assert result.passed
    assert not result.verifiable


def test_a_long_implausible_identifier_is_malformed():
    """A variable symbol is a short name ('F', 'v_0'), never a long word
    smuggled in as an identifier - the length cap (`_MAX_IDENTIFIER_LEN`)
    is what actually makes the longer plain-prose test above fail
    structurally, so it's worth a dedicated, narrower test of exactly that
    rule, using a single over-long "identifier" rather than a whole
    sentence."""
    result = evaluate_formula("F = photosynthesis_rate_constant_value * a")
    assert not result.passed
    assert result.issues[0].reason == MALFORMED_EXPRESSION


def test_a_short_english_word_used_as_a_symbol_is_honestly_unverifiable_not_falsely_correct():
    """A genuinely hard, acknowledged edge case: "force"/"mass" are short
    enough to structurally parse as plausible-looking symbols (the length
    cap alone can't distinguish a short word from a short legitimate
    symbol like "rho" without a dictionary this module deliberately
    doesn't maintain). The safe, honest outcome either way: this can never
    match a known reference's symbol set, so it is reported as
    unverifiable - never mistaken for a confirmed-correct formula."""
    result = evaluate_formula("force = mass * acceleration")
    assert result.passed  # not flagged as malformed (a real, documented limitation)
    assert not result.verifiable  # but never silently treated as "correct" either


# --- subscripts --------------------------------------------------------------


def test_correct_subscripted_variables_pass():
    result = evaluate_formula("F = G * m_1 * m_2 / r^2")
    assert result.passed
    assert result.verifiable


def test_a_changed_subscript_is_never_silently_accepted_as_the_reference_formula():
    """m_1 renamed to m_x changes the symbol SET itself - subscripted
    identifiers are compared exactly, like any other symbol, so this no
    longer matches Newton's law of gravitation's own {F, G, m_1, m_2, r} at
    all. Reported honestly as "nothing to compare against" rather than a
    false, overconfident "mismatch" claim against a formula it may not even
    be attempting any more - but critically, NEVER silently treated as
    "confirmed correct" either."""
    result = evaluate_formula("F = G * m_x * m_2 / r^2")
    assert result.passed  # no issue is raised - nothing to compare against
    assert not result.verifiable  # but never silently "correct" either
