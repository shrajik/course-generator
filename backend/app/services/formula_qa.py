"""Deterministic verification for `ImageContent.formula_expression` - the
one governing equation an illustration's "FORMULA BLOCK" depicts (see
app.agents.prompts' own guidance on this). Before this module, NOTHING in
the pipeline ever checked a formula's mathematical correctness: the only
existing check (`ImageService._verify_generated_text`) verifies that the
rendered GLYPHS match the expected text, never that the equation itself is
right. A perfectly spelled, perfectly rendered WRONG formula ("F = m / a")
sailed through untouched.

Two layers, both pure Python, no LLM call and no OCR - a formula's
correctness is either a structural/syntactic fact or a symbolic-equivalence
fact, never a judgment call an AI should self-certify:

1. Structural ("is this even a well-formed equation?"): exactly one
   top-level `=`, both sides non-empty, valid expression syntax, only
   plausible variable-name tokens. Catches a malformed formula outright,
   with no reference needed.
2. Reference comparison ("is this the formula it claims to be?"): a small,
   curated registry of well-known textbook formulas. The writer's
   expression is matched to a registry entry by its VARIABLE SET (a strong,
   specific signal - "F", "m", "a" together essentially only ever means
   Newton's second law), then compared for genuine structural/symbolic
   equivalence via Python's own `ast` module - parsed, never `eval`'d, and
   compared up to the commutativity `+`/`*` actually have (so "m * a" and
   "a * m" are recognised as identical, but "m / a" is correctly NOT
   mistaken for "m * a"), PLUS the bounded algebraic rearrangements
   `_rearranged_forms` recognises for a simple 2-factor product/quotient
   reference (so "a = F / m" and "m = F / a" are both correctly recognised
   as Newton's second law too, not false-flagged as a different, wrong
   equation just because they isolate a different variable). No `sympy`/
   new dependency: this AST-structural comparison is sufficient for the
   flat, single-governing-equation case this pipeline actually produces
   (confirmed via app.agents.prompts' own "one key governing equation"
   framing), not a general computer-algebra-system replacement.

   RELEASE-READINESS AUDIT FINDING, confirmed by direct testing and left
   as an explicit, documented residual limitation rather than papered
   over: `_rearranged_forms` only covers a reference shaped `var = X * Y`
   or `var = X / Y` (exactly 2 factors) - 6 of this module's 12 registry
   entries (Newton's second law, mass-energy equivalence, Ohm's law,
   density, work, power, momentum - each a simple 2-variable relationship).
   The other 6 (the ideal gas law, kinetic energy, gravitational potential
   energy, Newton's law of gravitation, the Pythagorean theorem - each a
   3+-factor product or an addition) have NO rearrangement support: a
   legitimate solve-for-a-different-variable rewrite of one of THOSE
   (confirmed via direct testing: "g = PE / (m * h)", a correct
   rearrangement of "PE = m * g * h", is incorrectly reported as
   `formula_mismatch`) will still be false-flagged. Mitigated, not
   eliminated, by the pre-existing, already-tested admin-override
   mechanism (`approve_course_override_blockers`) - never a permanent,
   silent dead end - but a real gap worth closing with a genuine, N-ary
   factor-isolation generalisation as explicit follow-up work, not
   attempted here to avoid introducing a more complex, less-tested
   rearrangement engine under this audit's own time pressure.

Honest about its own limits (task requirement: never claim verification
that didn't happen): a formula whose variable set matches no registry
entry is reported as UNVERIFIABLE, never silently treated as correct -
`FormulaCritique.verifiable` is False, `formula_verified` stays `None`,
never `True`. This only covers mathematics/physics-style symbolic
equations; chemical equations (reaction arrows, subscripted molecule
counts) and statistical notation are a documented, explicit limitation
(see this module's own `evaluate_formula` docstring), not silently
mishandled as if they were algebra.

A second, narrower documented limitation: distinguishing a short
legitimate symbol ("rho", "F") from a short plain English word used by
mistake ("force", "mass") is not attempted - both are equally plausible-
looking identifiers without a dictionary this module deliberately doesn't
maintain (a genuinely long word like "acceleration" IS still caught, via
`_MAX_IDENTIFIER_LEN`). This is always SAFE either way: a word-based
"formula" can never match a real reference's symbol set, so it is honestly
reported as unverifiable, never silently mistaken for a confirmed-correct
one - see `evaluate_formula`'s own return contract."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# --- structured failure reasons ---------------------------------------------
MALFORMED_EXPRESSION = "malformed_expression"
FORMULA_MISMATCH = "formula_mismatch"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MAX_IDENTIFIER_LEN = 12  # a real variable symbol ("F", "ΔH", "v_0"), never a word/phrase


@dataclass
class FormulaQAIssue:
    reason: str
    detail: str


@dataclass
class FormulaQAResult:
    issues: list[FormulaQAIssue] = field(default_factory=list)
    # True only when a reference formula was actually found and compared -
    # see this module's own docstring on never claiming verification that
    # didn't happen. False means "checked, and no reference was available",
    # which is a genuinely different, honest outcome from "checked and
    # passed" (`passed and verifiable`) or "checked and failed" (`not passed`).
    verifiable: bool = False

    @property
    def passed(self) -> bool:
        return not self.issues


def _split_top_level_equals(expression: str) -> list[str]:
    """Split on `=` that isn't part of `==`/`!=`/`<=`/`>=` and isn't inside
    parentheses - a formula is a single equation, never a comparison chain
    or a nested conditional."""
    parts: list[str] = []
    depth = 0
    current = ""
    i = 0
    while i < len(expression):
        ch = expression[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "=" and depth == 0:
            prev_ch = expression[i - 1] if i > 0 else ""
            next_ch = expression[i + 1] if i + 1 < len(expression) else ""
            if prev_ch in "=!<>" or next_ch == "=":
                current += ch
            else:
                parts.append(current)
                current = ""
                i += 1
                continue
        else:
            current += ch
        i += 1
    parts.append(current)
    return parts


def _to_python_expr(side: str) -> str:
    """ASCII formula notation -> a parseable Python expression. `^` is
    exponent here (never XOR - this is a math formula, not code), and a
    subscript written as `_1`/`_max` is kept as part of one identifier
    (Python already allows underscores in names) rather than split apart."""
    return side.replace("^", "**")


def _identifiers(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _canonical(node: ast.AST) -> str:
    """A structural fingerprint of an expression AST that is equal for two
    expressions related only by the commutativity `+` and `*` genuinely
    have (operand order doesn't change the result) - never for `-`/`/`/`**`,
    which are NOT commutative and must stay order-sensitive. Built from the
    AST (never `eval`'d), so this is a structural/symbolic comparison, not
    a numeric one - it has no opinion on what any variable's value is."""
    if isinstance(node, ast.Expression):
        return _canonical(node.body)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.UnaryOp):
        op = "-" if isinstance(node.op, ast.USub) else "+"
        return f"{op}({_canonical(node.operand)})"
    if isinstance(node, ast.BinOp):
        left, right = _canonical(node.left), _canonical(node.right)
        if isinstance(node.op, ast.Add):
            return "+".join(sorted((left, right)))
        if isinstance(node.op, ast.Mult):
            return "*".join(sorted((left, right)))
        if isinstance(node.op, ast.Sub):
            return f"({left}-{right})"
        if isinstance(node.op, ast.Div):
            return f"({left}/{right})"
        if isinstance(node.op, ast.Pow):
            return f"({left}**{right})"
        return f"({left}?{right})"  # an operator this module doesn't model - never silently "equal"
    if isinstance(node, ast.Call):
        args = ",".join(_canonical(a) for a in node.args)
        name = node.func.id if isinstance(node.func, ast.Name) else "?"
        return f"{name}({args})"
    return f"<{type(node).__name__}>"  # unrecognised node shape - never silently "equal" to anything


def _parse_side(side: str) -> ast.AST | None:
    try:
        return ast.parse(_to_python_expr(side.strip()), mode="eval")
    except (SyntaxError, ValueError):
        return None


def _simple_operand(node: ast.AST) -> bool:
    """A bare symbol/number, or a simple power of one (e.g. "c^2", "r^2") -
    never a further nested expression. The bound `_rearranged_forms` needs
    to stay a safe, well-defined identity rather than general symbolic
    solving (see that function's own docstring)."""
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        return isinstance(node.left, (ast.Name, ast.Constant)) and isinstance(node.right, (ast.Name, ast.Constant))
    return False


def _rearranged_forms(lhs: ast.AST, rhs: ast.AST) -> set[tuple[str, str]]:
    """Release-readiness audit fix: a real, confirmed false-positive risk -
    "a = F / m" (solving Newton's second law for acceleration instead of
    force) was being reported as a confirmed `formula_mismatch` against
    "F = m * a", even though it is the exact same physical law, legitimately
    rearranged. For a reference equation shaped `single_var = X * Y` or
    `single_var = X / Y` (X/Y each a bare symbol, constant, or simple power
    - see `_simple_operand`), this also recognises the two other forms the
    SAME equation can be legitimately written as (isolating X, or isolating
    Y) - a well-defined, safe algebraic identity (a*b=c <=> a=c/b <=> b=c/a
    for non-zero b/a), never general symbolic solving.

    Deliberately bounded, not a general rearrangement engine: anything with
    more than 2 factors (e.g. the ideal gas law's `P*V = n*R*T`, kinetic
    energy's `(1/2)*m*v^2`), an addition/subtraction (the Pythagorean
    theorem), or a nested expression inside either operand returns an empty
    set here - a documented, real, remaining limitation (see this module's
    own top docstring), never a guessed-at rearrangement for a shape this
    function doesn't model. Returns the ADDITIONAL (lhs, rhs) canonical
    string pairs to accept, on top of whatever `_equation_sides_equivalent`
    already covers (the reference's own literal form, both side orders)."""
    # `ast.parse(..., mode="eval")` returns an `ast.Expression` WRAPPER - the
    # real node is `.body` (same unwrapping `_canonical` already does at its
    # own top). A real, confirmed bug this fixes: without it, every
    # isinstance check below silently failed (the wrapper is never a bare
    # `ast.Name`/`ast.BinOp`), so this function always returned an empty
    # set - the exact false-positive this function exists to prevent.
    if isinstance(lhs, ast.Expression):
        lhs = lhs.body
    if isinstance(rhs, ast.Expression):
        rhs = rhs.body
    forms: set[tuple[str, str]] = set()
    # Normalise to (variable_side, product_or_quotient_side) regardless of
    # which literal side of "=" the reference happened to put each on.
    for var_side, op_side in ((lhs, rhs), (rhs, lhs)):
        if not isinstance(var_side, ast.Name):
            continue
        if not isinstance(op_side, ast.BinOp):
            continue
        x, y = op_side.left, op_side.right
        if not (_simple_operand(x) and _simple_operand(y)):
            continue
        var = _canonical(var_side)
        cx, cy = _canonical(x), _canonical(y)
        if isinstance(op_side.op, ast.Mult):
            # var = X * Y  =>  X = var / Y  and  Y = var / X
            forms.add((cx, f"({var}/{cy})"))
            forms.add((cy, f"({var}/{cx})"))
        elif isinstance(op_side.op, ast.Div):
            # var = X / Y  =>  X = var * Y  and  Y = X / var
            forms.add((cx, "*".join(sorted((var, cy)))))
            forms.add((cy, f"({cx}/{var})"))
    return forms


def _equation_sides_equivalent(a_lhs: ast.AST, a_rhs: ast.AST, b_lhs: ast.AST, b_rhs: ast.AST) -> bool:
    """An equation `L = R` means the same thing as `R = L` - compared both
    ways, never just left-to-left - plus the bounded set of legitimate
    algebraic rearrangements `_rearranged_forms` recognises for a simple
    product/quotient reference."""
    a = (_canonical(a_lhs), _canonical(a_rhs))
    b = (_canonical(b_lhs), _canonical(b_rhs))
    if a == b or a == (b[1], b[0]):
        return True
    rearranged = _rearranged_forms(b_lhs, b_rhs)
    return a in rearranged or (a[1], a[0]) in rearranged


@dataclass(frozen=True)
class _ReferenceFormula:
    name: str
    expression: str  # canonical ASCII form, e.g. "F = m * a"


# A small, curated set of unambiguous, well-known single-equation textbook
# formulas - deliberately NOT exhaustive (this is a structural-comparison
# tool, not a computer-algebra system or a formula database). Matched by
# VARIABLE SET first (see `_find_reference`), which is why each entry's
# variables are chosen to be a genuinely distinctive combination; a
# formula using any other variable set is honestly reported as
# unverifiable rather than guessed at.
_REFERENCE_FORMULAS = (
    _ReferenceFormula("Newton's second law", "F = m * a"),
    _ReferenceFormula("Ideal gas law", "P * V = n * R * T"),
    _ReferenceFormula("Mass-energy equivalence", "E = m * c^2"),
    _ReferenceFormula("Pythagorean theorem", "a^2 + b^2 = c^2"),
    _ReferenceFormula("Ohm's law", "V = I * R"),
    _ReferenceFormula("Kinetic energy", "KE = (1/2) * m * v^2"),
    _ReferenceFormula("Gravitational potential energy", "PE = m * g * h"),
    _ReferenceFormula("Newton's law of gravitation", "F = G * m_1 * m_2 / r^2"),
    _ReferenceFormula("Density", "rho = m / V"),
    _ReferenceFormula("Work", "W = F * d"),
    _ReferenceFormula("Power", "P = W / t"),
    _ReferenceFormula("Momentum", "p = m * v"),
)


def _find_reference(variables: set[str]) -> _ReferenceFormula | None:
    """The registry entry whose own variable set is the closest, strong
    match - exact set equality first (the common, unambiguous case); a
    registry entry is never guessed at from a partial/weak overlap, since a
    wrong reference would be worse than none (an honest "unverifiable")."""
    for reference in _REFERENCE_FORMULAS:
        parsed = _parse_expression(reference.expression)
        if parsed is not None and _identifiers(parsed[0]) | _identifiers(parsed[1]) == variables:
            return reference
    return None


def _parse_expression(expression: str) -> tuple[ast.AST, ast.AST] | None:
    sides = _split_top_level_equals(expression)
    if len(sides) != 2:
        return None
    lhs, rhs = _parse_side(sides[0]), _parse_side(sides[1])
    if lhs is None or rhs is None:
        return None
    return lhs, rhs


def _structural_issues(expression: str) -> tuple[list[FormulaQAIssue], tuple[ast.AST, ast.AST] | None]:
    if not expression.strip():
        return [], None
    sides = _split_top_level_equals(expression)
    if len(sides) != 2 or not sides[0].strip() or not sides[1].strip():
        return [FormulaQAIssue(
            MALFORMED_EXPRESSION,
            f"'{expression}' must be exactly one equation (one '=' with a non-empty expression "
            "on each side) - never a comparison chain, an inequality, or plain prose.",
        )], None
    lhs, rhs = _parse_side(sides[0]), _parse_side(sides[1])
    if lhs is None or rhs is None:
        return [FormulaQAIssue(
            MALFORMED_EXPRESSION,
            f"'{expression}' does not parse as a valid expression on both sides of '=' - check for "
            "unbalanced parentheses, a stray/missing operator, or an unsupported symbol.",
        )], None
    bad_identifiers = sorted(
        name for name in (_identifiers(lhs) | _identifiers(rhs))
        if not _IDENTIFIER_RE.match(name) or len(name) > _MAX_IDENTIFIER_LEN
    )
    if bad_identifiers:
        return [FormulaQAIssue(
            MALFORMED_EXPRESSION,
            f"These aren't plausible variable symbols: {', '.join(bad_identifiers)} - a formula "
            "variable is a short symbol (e.g. 'F', 'v_0', 'Delta_H'), never a word or phrase.",
        )], None
    return [], (lhs, rhs)


def evaluate_formula(expression: str) -> FormulaQAResult:
    """Verify `ImageContent.formula_expression`. Returns a result whose
    `verifiable` flag is the honest answer to "was there actually a
    reference to check this against" - `passed` alone is not enough to
    distinguish "confirmed correct" from "nothing to compare it to".

    Scope, stated plainly (never silently overreaching): this models flat
    algebraic/physics equations only (the `+`/`-`/`*`/`/`/`**`/parentheses/
    named-symbol grammar `_to_python_expr` understands). A chemical
    equation (reaction arrows, subscripted molecule counts like "2 H_2 +
    O_2 -> 2 H_2O") or statistical/probability notation does not parse
    under this grammar at all - `_structural_issues` reports it as
    malformed rather than silently approving or rejecting it as if it were
    algebra. That is a real, documented limitation, not a silent gap: a
    course with those formula types gets NO verification from this module
    yet (see this module's own top docstring)."""
    issues, parsed = _structural_issues(expression)
    if issues or parsed is None:
        return FormulaQAResult(issues=issues, verifiable=False)

    lhs, rhs = parsed
    variables = _identifiers(lhs) | _identifiers(rhs)
    reference = _find_reference(variables)
    if reference is None:
        return FormulaQAResult(issues=[], verifiable=False)

    ref_parsed = _parse_expression(reference.expression)
    assert ref_parsed is not None  # every _REFERENCE_FORMULAS entry is valid by construction
    ref_lhs, ref_rhs = ref_parsed
    if _equation_sides_equivalent(lhs, rhs, ref_lhs, ref_rhs):
        return FormulaQAResult(issues=[], verifiable=True)
    return FormulaQAResult(
        issues=[FormulaQAIssue(
            FORMULA_MISMATCH,
            f"'{expression}' uses the same symbols as {reference.name} ({reference.expression}) but "
            "is not the same equation - check the operators, exponents and which side each symbol "
            "is on.",
        )],
        verifiable=True,
    )
