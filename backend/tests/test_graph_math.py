"""Safe function evaluation and axis-tick math for native graph/chart
rendering - app.render.graph_math."""

from __future__ import annotations

from app.render.graph_math import nice_ticks, safe_eval, sample_function


# --- safe_eval: correctness ---------------------------------------------------


def test_simple_polynomial():
    assert safe_eval("x**2", 3) == 9
    assert safe_eval("2*x + 1", 5) == 11
    assert safe_eval("x - 4", 10) == 6


def test_trig_and_constants():
    assert abs(safe_eval("sin(x)", 0) - 0) < 1e-9
    assert abs(safe_eval("cos(0)", 0) - 1) < 1e-9
    assert abs(safe_eval("x", 0) - 0) < 1e-9


def test_unary_minus():
    assert safe_eval("-x**2", 3) == -9


def test_nested_calls():
    assert abs(safe_eval("sqrt(x)", 16) - 4) < 1e-9


# --- safe_eval: rejects anything outside the whitelist ------------------------


def test_rejects_import_attempt():
    assert safe_eval("__import__('os').system('echo hi')", 0) is None


def test_rejects_attribute_access():
    assert safe_eval("x.__class__", 0) is None


def test_rejects_unknown_function():
    assert safe_eval("eval('1')", 0) is None


def test_rejects_unknown_name():
    assert safe_eval("y + 1", 0) is None


def test_rejects_multiple_statements():
    assert safe_eval("x = 1; x", 0) is None


def test_rejects_non_numeric_constant():
    assert safe_eval('"x"', 0) is None


# --- safe_eval: domain errors never raise -------------------------------------


def test_log_of_negative_is_none_not_a_crash():
    assert safe_eval("log(x)", -1) is None


def test_division_by_zero_is_none_not_a_crash():
    assert safe_eval("1 / x", 0) is None


def test_overflow_is_none_not_a_crash():
    assert safe_eval("exp(x)", 10000) is None


# --- sample_function -----------------------------------------------------------


def test_sample_function_covers_the_domain():
    points = sample_function("x**2", -2, 2, steps=5)
    xs = [x for x, _ in points]
    assert xs[0] == -2 and xs[-1] == 2
    assert all(y == x**2 for x, y in points)


def test_sample_function_skips_unevaluable_points():
    points = sample_function("log(x)", -1, 1, steps=5)
    # Every x <= 0 is a domain error for log - only the positive half survives.
    assert all(x > 0 for x, _ in points)


def test_sample_function_empty_for_degenerate_range():
    assert sample_function("x", 5, 5) == []
    assert sample_function("x", 5, -5) == []


# --- nice_ticks -----------------------------------------------------------------


def test_nice_ticks_uses_round_evenly_spaced_numbers():
    """Standard "nice numbers" tick-spacing: ticks land on round 1/2/5 x 10^n
    steps, not raw fractions of the range - the last tick may legitimately
    fall short of `hi` (same convention most charting libraries use; the
    axis's own drawn extent comes from the real data range, not from where
    the tick marks happen to land)."""
    ticks = nice_ticks(0, 97)
    assert ticks[0] <= 0
    assert len(ticks) >= 2
    spacing = ticks[1] - ticks[0]
    assert spacing in (10, 20, 50)  # a "nice" step for a ~97-wide range
    assert all(abs((b - a) - spacing) < 1e-9 for a, b in zip(ticks, ticks[1:]))


def test_nice_ticks_handles_a_degenerate_range():
    assert nice_ticks(5, 5) == [5]


def test_nice_ticks_small_range():
    ticks = nice_ticks(0, 1)
    assert ticks[0] <= 0 and ticks[-1] >= 1
