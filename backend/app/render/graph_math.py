"""Safe evaluation of an AI-supplied function expression, plus shared axis
math for graph/chart rendering (`app.render.diagram_renderer`'s `graph` kind).

Same safety discipline `app.services.formula_qa` already established for a
different job (comparing two sides of an equation for equivalence): parse
with `ast.parse(..., mode="eval")`, then walk the tree by hand accepting only
a fixed whitelist of node types - `eval`/`exec` are never called on anything
derived from model output.
"""

from __future__ import annotations

import ast
import math

_ALLOWED_CALLS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "abs": abs,
}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e}


class _Unsafe(Exception):
    """Raised for any expression node outside the accepted whitelist."""


def _eval_node(node: ast.AST, x: float) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, x)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise _Unsafe(f"non-numeric constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "x":
            return x
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        raise _Unsafe(f"unknown name: {node.id}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand, x)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, x)
        right = _eval_node(node.right, x)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            return left**right
        raise _Unsafe(f"unsupported operator: {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
            raise _Unsafe("call to a function outside the allowed whitelist")
        if node.keywords:
            raise _Unsafe("keyword arguments are not allowed")
        args = [_eval_node(arg, x) for arg in node.args]
        return _ALLOWED_CALLS[node.func.id](*args)
    raise _Unsafe(f"unsupported expression node: {type(node).__name__}")


def safe_eval(expression: str, x: float) -> float | None:
    """Evaluate `expression` (a function of `x`) at one point. `None` for
    anything unparseable/unsafe, or a math-domain error (e.g. log of a
    negative number, division by zero) - never raises, so a caller sampling
    many points can simply skip the ones that fail."""
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree, x)
    except (_Unsafe, SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    if not math.isfinite(value):
        return None
    return value


def sample_function(expression: str, x_min: float, x_max: float, steps: int = 120) -> list[tuple[float, float]]:
    """Evenly-spaced (x, y) samples across [x_min, x_max]; points the
    expression can't be evaluated at are simply omitted, never crash the
    caller."""
    if steps < 2 or x_max <= x_min:
        return []
    step = (x_max - x_min) / (steps - 1)
    points: list[tuple[float, float]] = []
    for i in range(steps):
        px = x_min + i * step
        py = safe_eval(expression, px)
        if py is not None:
            points.append((px, py))
    return points


def nice_ticks(lo: float, hi: float, target_count: int = 5) -> list[float]:
    """"Nice numbers" tick spacing (1/2/5 x 10^n steps) - the standard
    algorithm behind every charting library's default axis ticks, so a
    native graph reads with the same readable gridlines a human would draw
    by hand instead of raw, awkward min/max fractions."""
    if hi <= lo:
        return [lo]
    span = hi - lo
    raw_step = span / max(target_count, 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for multiple in (1, 2, 5, 10):
        step = multiple * magnitude
        if step >= raw_step:
            break
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    value = start
    # A small epsilon guards against float drift leaving the final tick
    # just short of `hi` after many additions.
    while value <= hi + step * 1e-9:
        if value >= lo - step * 1e-9:
            ticks.append(round(value, 10))
        value += step
    return ticks
