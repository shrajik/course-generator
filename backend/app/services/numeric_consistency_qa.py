"""Deterministic numeric-claim consistency check between a visual's own
text (a diagram's node/edge labels and details, or a raster illustration's
expected labels/brief) and the course's own written source excerpt (see
`ImageService.chapter_section_context`'s `source_excerpt` - the Canonical
Visual Spec milestone).

NEVER a scientific-correctness check. `app.services.formula_qa` already
owns that job - it parses a symbolic equation (AST, both sides) and matches
it against a small, fixed registry of ~12 formulas KNOWN to be correct
(E=mc^2, F=ma, ...), answering "is this equation one of the ones I know to
be true". This module answers a completely different question: "does a bare
NUMBER this visual states agree with the number the course's OWN TEXT
states for what looks like the same quantity" - a source-CONSISTENCY check,
never a truth check. A number can pass this check while being scientifically
wrong (if the course's own source text shares that same error), and a number
can fail it while being scientifically correct (if the course's source text
itself has a typo) - this module has no opinion either way, by design. Every
finding this module raises is named `numeric_contradicts_source_text`,
deliberately never `is_scientifically_correct` or `is_numerically_correct` -
see `NumericConsistencyIssue`'s own docstring, which makes this impossible
to miss.

Modeled on formula_qa's own shape (a small, dependency-free, pure-Python
AST/regex parser; a frozen result object; explicit, narrow issue reasons)
but NOT importing or duplicating any of its registry-matching - the two
modules are complementary, not overlapping: a diagram/image can trip this
module's check, formula_qa's check, both, or neither, completely
independently.

Deliberately CONSERVATIVE by construction (hard requirement, Canonical
Visual Spec milestone): a number present in the visual's own text that the
source excerpt simply never mentions at all is NEVER flagged here - that is
the softer "unsupported claim" case, which only an LLM judgment call
(`diagram_content_qa`'s `unsupported_claims` / `ImageRelevanceCheck`'s
matching field) can meaningfully make, since recognising "this number is
about the same real-world quantity as the one two paragraphs away" needs
more context than a bare numeric/keyword match can safely claim. This
module only ever flags a HIGH-CONFIDENCE, textually-anchored mismatch: both
the visual's number and a number in the source excerpt must sit next to at
least one shared, specific (non-generic) keyword before they are even
compared - a bare coincidental digit match with no shared context is never
enough to call a contradiction. When unsure, this module stays silent and
lets the (non-deterministic, more context-aware) LLM judgment call decide
instead - never the reverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.render.periodic_table_data import ELEMENTS

NUMERIC_CONTRADICTS_SOURCE_TEXT = "numeric_contradicts_source_text"
# Canonical Visual Spec milestone, short-identifier hardening: a CONFIRMED
# mismatch for a short identifier (`Si`, `R1`, `I1`, ...) explicitly assigned
# a value in BOTH the visual's own text and the course's source excerpt,
# where both sides resolve to a single, compatible-unit, genuinely different
# value - exactly as high-confidence as `NUMERIC_CONTRADICTS_SOURCE_TEXT`
# above (same hard-fail/fallback treatment downstream), just reached via
# identifier matching instead of bare shared-keyword proximity.
IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT = "identifier_value_contradicts_source_text"
# Deliberately NOT the same severity as the reason above. Hard requirement
# (Canonical Visual Spec milestone): "if identifier resolution remains
# ambiguous, return REVIEW rather than guessing" - covers every case this
# module can detect a specific identifier=value CLAIM but cannot confidently
# confirm or deny it: the identifier never appears in an assignment form
# anywhere in the source excerpt at all, every occurrence in the source uses
# an incompatible/different unit, or the source excerpt itself states more
# than one distinct value for the same identifier (internally ambiguous -
# never guess which one the diagram should agree with). The caller must
# route this to the SAME "flagged for a human, never silently published as
# verified, never a hard block either" treatment `source_grounding_
# unsupported`/`contradicts_source_text`'s sibling already gets - see this
# module's own `find_identifier_value_issues` docstring for exactly which
# three outcomes (silent pass / this / the reason above) apply when.
IDENTIFIER_VALUE_NEEDS_REVIEW = "identifier_value_ambiguous_needs_review"

# How close two normalised values must be to count as "the same number" -
# generous enough to absorb genuine rounding (3.14 vs 3.14159, "about 300
# million" vs 299,792,458) without papering over a real, substantively
# different value (3.14 vs 2.72). Relative, not absolute, so this behaves
# sensibly across wildly different magnitudes (atomic radii vs. the speed of
# light) without a separate per-unit tuning table.
_RELATIVE_TOLERANCE = 0.03
# A tiny absolute floor so two numbers that both round to ~0 (e.g. 0.0 and
# 0.001) are never treated as a "huge relative mismatch" by a near-zero
# denominator - the classic relative-tolerance degenerate case.
_ABSOLUTE_FLOOR = 1e-9

# Unicode vulgar-fraction characters this module treats as a plain ratio
# (½ -> 0.5), the same equivalence class as a written "1/2" or "0.5" - see
# this module's own docstring on equivalent representations.
_UNICODE_FRACTIONS: dict[str, float] = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8, "⅙": 1 / 6,
    "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

# Matches, in priority order (first alternative that matches wins at each
# position - Python's `re` tries alternatives left to right):
#  1. scientific notation with an explicit "times ten to the power" form:
#     "3 x 10^8", "3 × 10^8", "3*10^8", "6.022 x 10^23"
#  2. bare exponential notation: "3e8", "6.022E23", "1.5e-10"
#  3. a plain decimal/integer, optionally comma-grouped ("300,000,000",
#     "1,234.5"), optionally followed immediately by "%"
#  4. a simple written fraction "a/b" (e.g. "1/2") - deliberately requires
#     digits on both sides so this never matches a date-like "10/2024" or a
#     ratio-less slash in unrelated text; callers needing date exclusion
#     entirely can pre-filter, this module only parses what it's given.
#
# `_NOT_AFTER_LETTER` (real-course verification fix, Canonical Visual Spec
# milestone): a leading digit immediately preceded by a Latin letter is
# never treated as the start of a real number - this is what an identifier
# like "R1" (a resistor's REFERENCE NAME, not a quantity), "H2O" or a
# node id "step3" looks like, and a real circuit-diagram node label
# ("R1", detail "10 kOhm") was confirmed, against a real generated course's
# own chapter text, to otherwise misparse "R1" as the bare numeric claim
# "1". Also excludes "_" and "^"/"{" immediately before the digit - a real
# generated course's own LaTeX notation ("$R_1 = 10...$", "$R_2^{-1}$")
# confirmed these same subscript/exponent markers otherwise leak the index
# ITSELF (e.g. "1" from "R_1") as a spurious, meaningless numeric claim.
# Deliberately NOT a trailing lookahead too: a digit immediately FOLLOWED
# by a letter is the extremely common, legitimate "number+unit" case
# ("10kOhm", "5V", "3kg") and must still parse as a real number.
_NOT_AFTER_LETTER = r"(?<![A-Za-z_^{])"
_NUMBER_RE = re.compile(
    r"""
    (?P<sci_mul>
        """ + _NOT_AFTER_LETTER + r"""-?\d[\d,]*(?:\.\d+)?
        \s*[x×*]\s*
        10\s*\^?\s*(?P<sci_exp>-?\d+)
    )
    |
    (?P<sci_e>
        """ + _NOT_AFTER_LETTER + r"""-?\d[\d,]*(?:\.\d+)?[eE](?P<e_exp>-?\d+)
    )
    |
    (?P<fraction>
        """ + _NOT_AFTER_LETTER + r"""-?\d+\s*/\s*\d+
    )
    |
    (?P<plain>
        """ + _NOT_AFTER_LETTER + r"""-?\d[\d,]*(?:\.\d+)?(?:\s*%)?
    )
    """,
    re.VERBOSE,
)

# Words too generic/structural to ever anchor a numeric match on their own -
# "step 1" vs "step 2" sharing the word "step" must never be treated as the
# same claim just because both are near a number. Kept short and obviously
# structural/stopword-ish on purpose; this is a precision guard, not an
# attempt at real NLP.
_GENERIC_CONTEXT_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at",
    "and", "or", "for", "with", "this", "that", "step", "stage", "part", "item",
    "number", "value", "about", "approximately", "around", "roughly", "than",
    "more", "less", "first", "second", "third", "next", "then", "point",
})
_MIN_CONTEXT_WORD_LEN = 4
_CONTEXT_WINDOW_CHARS = 40


@dataclass(frozen=True)
class NumericClaim:
    """One number found in some text, with just enough surrounding context
    to judge, conservatively, whether a second claim elsewhere is plausibly
    about the SAME real-world quantity."""

    value: float
    raw: str
    context: frozenset[str]
    is_percent: bool = False
    unit: str = ""


@dataclass(frozen=True)
class NumericConsistencyIssue:
    """A single, deterministic, HIGH-CONFIDENCE numeric mismatch between a
    visual's own text and the course's own source excerpt.

    `reason` is always `NUMERIC_CONTRADICTS_SOURCE_TEXT`
    ("numeric_contradicts_source_text") - this is a SOURCE-CONSISTENCY
    finding, never a claim about objective/scientific correctness (that is
    `formula_qa`'s separate, registry-based job - see this module's own
    top-of-file docstring). `deterministic` is always True: every value here
    came from a pure-Python regex/float parse and comparison, never a model
    call."""

    reason: str
    detail: str
    deterministic: bool = field(default=True, init=False)


def _normalise_match(match: re.Match[str]) -> NumericClaim | None:
    raw = match.group(0)
    if match.group("sci_mul"):
        mantissa_text = raw.split("x")[0].split("×")[0].split("*")[0]
        try:
            mantissa = float(mantissa_text.replace(",", "").strip())
            exponent = int(match.group("sci_exp"))
        except ValueError:
            return None
        return NumericClaim(value=mantissa * (10**exponent), raw=raw, context=frozenset())
    if match.group("sci_e"):
        try:
            return NumericClaim(value=float(raw.replace(",", "")), raw=raw, context=frozenset())
        except ValueError:
            return None
    if match.group("fraction"):
        numerator, _, denominator = raw.partition("/")
        try:
            denom = float(denominator.strip())
            if denom == 0:
                return None
            return NumericClaim(value=float(numerator.strip()) / denom, raw=raw, context=frozenset())
        except ValueError:
            return None
    plain = match.group("plain")
    if plain is None:
        return None
    is_percent = plain.strip().endswith("%")
    number_text = plain.strip().rstrip("%").replace(",", "").strip()
    if not number_text or number_text == "-":
        return None
    try:
        value = float(number_text)
    except ValueError:
        return None
    # Canonical equivalence class (hard requirement): "50%" and "0.5" and
    # "1/2" must all normalise to the SAME comparable value - dividing a
    # percentage by 100 puts it in the same plain-ratio space a fraction or
    # decimal already uses.
    return NumericClaim(value=value / 100 if is_percent else value, raw=raw, context=frozenset(), is_percent=is_percent)


def _context_words(text: str, start: int, end: int) -> frozenset[str]:
    window = text[max(0, start - _CONTEXT_WINDOW_CHARS):start] + " " + text[end:end + _CONTEXT_WINDOW_CHARS]
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", window.lower())
    return frozenset(
        w for w in words
        if len(w) >= _MIN_CONTEXT_WORD_LEN and w not in _GENERIC_CONTEXT_WORDS
    )


# Real-course verification fix: a shared TOPIC word nearby ("feeding") is
# not enough on its own when a sentence packs several different numeric
# facts together ("Assume a 5 V source feeding R1 = 10 kOhm...") - a
# confirmed false positive matched a diagram's own "10 kOhm" against that
# same sentence's unrelated "5 V" supply voltage. The immediate UNIT text
# right after a number (skipping a short run of punctuation/markup so this
# still works through light LaTeX, e.g. "10\,\text{k}\Omega") is a much
# stronger, cheap second signal: two numbers that are both clearly
# carrying DIFFERENT units can never be the same quantity, regardless of
# shared context words. Used only as a VETO (if both sides have a detected
# unit and they differ, never flag) - it can only make this check MORE
# conservative, never less, so an imperfect extraction (picking up a
# stray word through markup) only risks a missed flag, never a false one.
_UNIT_SKIP_CHARS = 6
_UNIT_MAX_CHARS = 10

# Real-course verification fix (Final Implementation hardening pass): the
# veto above is "two DIFFERENT units can never match", but a real generated
# circuit-analysis course wrote its own source text as "R1=10 kOhm, R1=10
# kOmega" using the Greek letter Omega (kΩ) while a diagram's own label
# just as legitimately spells it out ("10 kOhm") - confirmed, via real
# course crs_594e8482f753's own chapter_01.json, to otherwise misfire this
# veto on a genuinely MATCHING value purely because one side used the
# symbol and the other spelled it out. Ω/ω is substituted to the
# same ASCII "ohm" BEFORE the unit letters are read, so both forms land on
# one shared token; the plural/short-word aliases below cover the other
# common SI units a physics/electronics course's own prose is equally
# likely to spell out in full even when a diagram/claim uses the bare
# symbol (e.g. "5 volts" vs "5V"). Deliberately a short, explicit table -
# never a general physics-unit database - covering only the units this
# codebase's own real course data and test suite have actually exercised;
# an unlisted unit is simply compared as-is, same as before this fix.
_UNIT_ALIASES = {
    "ohms": "ohm", "kohms": "kohm", "mohms": "mohm", "megaohms": "megaohm",
    "volts": "v", "volt": "v",
    "amps": "a", "amp": "a", "amperes": "a", "ampere": "a",
    "watts": "w", "watt": "w",
    "hertz": "hz",
    "farads": "f", "farad": "f",
    "henries": "h", "henry": "h",
    "grams": "g", "gram": "g",
    "meters": "m", "meter": "m", "metres": "m", "metre": "m",
    "seconds": "s", "second": "s",
}


def _adjacent_unit(text: str, end: int) -> str:
    tail = text[end:end + _UNIT_SKIP_CHARS + _UNIT_MAX_CHARS]
    skipped = tail.lstrip("\\,.(){}[] \t").replace("Ω", "ohm").replace("ω", "ohm")
    letters = re.match(r"[A-Za-z]+", skipped)
    if not letters:
        return ""
    unit = letters.group(0).lower()
    return _UNIT_ALIASES.get(unit, unit)


def extract_numeric_claims(text: str) -> list[NumericClaim]:
    """Every number in `text`, each paired with its own nearby context
    words (see `_context_words`) - the free-text equivalent of
    `formula_qa`'s AST parse, except the ground truth being checked is the
    course's own prose, not a fixed formula registry. Unicode vulgar
    fractions (½, ⅓, ...) are pulled out first and replaced with a plain
    ASCII placeholder so the main numeric regex never has to special-case
    them, then re-attached with their own real context window."""
    if not text:
        return []
    claims: list[NumericClaim] = []
    # Deliberately NOT NFKC-normalised here first: Unicode NFKC
    # compatibility-decomposes a vulgar fraction character (e.g. U+00BD "½")
    # into "1⁄2" (digit, FRACTION SLASH, digit) - exactly the form this
    # function's own `_UNICODE_FRACTIONS` dict lookup below is meant to
    # intercept. Normalising first would silently turn every "½" into a bare
    # "1" immediately followed by an unrelated "2" by the time the dict
    # lookup ever ran, misparsing "½ of the sample" as the integer 1 (a real,
    # confirmed bug this comment documents, not a hypothetical one) - the
    # dict below must see the original, undecomposed glyph.
    working = text
    for char, value in _UNICODE_FRACTIONS.items():
        start = 0
        while True:
            idx = working.find(char, start)
            if idx == -1:
                break
            context = _context_words(working, idx, idx + 1)
            claims.append(NumericClaim(value=value, raw=char, context=context))
            start = idx + 1
    # Replace fraction glyphs with a neutral placeholder so the regex below
    # (which knows nothing about them) can't misparse a digit sitting right
    # next to one (e.g. "2½" - the "2" is a real, separate integer claim).
    for char in _UNICODE_FRACTIONS:
        working = working.replace(char, " ")
    for match in _NUMBER_RE.finditer(working):
        claim = _normalise_match(match)
        if claim is None:
            continue
        context = _context_words(working, match.start(), match.end())
        unit = "%" if claim.is_percent else _adjacent_unit(working, match.end())
        claims.append(NumericClaim(
            value=claim.value, raw=claim.raw, context=context, is_percent=claim.is_percent, unit=unit,
        ))
    return claims


def _is_high_confidence_mismatch(a: NumericClaim, b: NumericClaim) -> bool:
    if not (a.context & b.context):
        return False
    # The unit veto (see `_adjacent_unit`'s own docstring): two numbers
    # carrying clearly DIFFERENT units can never be the same quantity,
    # regardless of how many context words they happen to share - this is
    # what actually distinguishes "feeding R1 = 10 kOhm" (unit "kohm")
    # from the same sentence's own unrelated "5 V" supply voltage (unit
    # "v"). Only vetoes when BOTH sides have a real, non-empty detected
    # unit - an absent unit on either side is never itself a reason to
    # suppress a match, it just means this extra signal has nothing to add.
    if a.unit and b.unit and a.unit != b.unit:
        return False
    scale = max(abs(a.value), abs(b.value), _ABSOLUTE_FLOOR)
    return abs(a.value - b.value) / scale > _RELATIVE_TOLERANCE


def find_numeric_contradictions(
    claim_text: str, source_excerpt: str, *, max_issues: int = 5
) -> list[NumericConsistencyIssue]:
    """Compare every number in `claim_text` (a diagram's node/edge labels
    and details, or an illustration's own brief/expected labels) against
    every number in `source_excerpt` (the course's own real chapter prose -
    see `ImageService.chapter_section_context`), and return a HIGH-CONFIDENCE,
    deterministic mismatch for each pair that (a) sits next to at least one
    shared, specific keyword (never a bare coincidental digit match) and (b)
    genuinely disagrees in value beyond generous rounding tolerance (see
    `_RELATIVE_TOLERANCE`).

    Deliberately returns nothing when `source_excerpt` is blank, or when a
    claim number has no matching context anywhere in the excerpt - an
    ABSENT number is never this module's business (see this module's own
    docstring: that routes to the separate, LLM-judged "unsupported claim"
    path instead, never a FAIL here)."""
    if not claim_text.strip() or not source_excerpt.strip():
        return []
    claim_numbers = extract_numeric_claims(claim_text)
    source_numbers = extract_numeric_claims(source_excerpt)
    if not claim_numbers or not source_numbers:
        return []
    issues: list[NumericConsistencyIssue] = []
    reported: set[tuple[str, str]] = set()
    for claim in claim_numbers:
        for source in source_numbers:
            if not _is_high_confidence_mismatch(claim, source):
                continue
            key = (claim.raw, source.raw)
            if key in reported:
                continue
            reported.add(key)
            issues.append(NumericConsistencyIssue(
                reason=NUMERIC_CONTRADICTS_SOURCE_TEXT,
                detail=(
                    f"This visual states \"{claim.raw}\", but the course's own text states "
                    f"\"{source.raw}\" for what appears to be the same quantity "
                    f"(shared context: {', '.join(sorted(claim.context & source.context)[:3])})."
                ),
            ))
            if len(issues) >= max_issues:
                return issues
    return issues


# --- short-identifier value matching (Si=14, R1=10kOhm, I1=2A, ...) --------
#
# The bare numeric check above deliberately never anchors on a SHORT context
# word (`_MIN_CONTEXT_WORD_LEN = 4`) - a real, confirmed gap: "Si", "Fe",
# "R1", "I1" are all 1-2 letters (plus an optional digit), so a genuinely
# wrong value attached to one of them ("Si = 28" when the course's own text
# says "Si = 14") passed through completely unnoticed, silently, every time.
# This is NOT fixed by lowering `_MIN_CONTEXT_WORD_LEN` (an explicit,
# rejected approach - hard requirement, Canonical Visual Spec milestone):
# that would make EVERY short word a valid anchor, including ordinary
# prose ("is", "of", "an") sitting near an unrelated number purely by
# coincidence - exactly the false-positive flood the length floor exists to
# prevent for ordinary prose. Short SCIENTIFIC IDENTIFIERS are a genuinely
# different, narrower case: they only ever carry a specific numeric meaning
# when they appear in an explicit ASSIGNMENT/DEFINITION position (`X = v`,
# `X: v`, `X is v`, `X equals v`) - that STRUCTURAL pattern, not bare length,
# is what makes a token trustworthy as an identifier here.
_ELEMENT_SYMBOLS = frozenset(e.symbol for e in ELEMENTS)
# Common short reference-designator letters a circuit/physics/math text
# actually uses this way (R1, C2, V3, I1, L1 for resistor/capacitor/voltage/
# current/inductor; also bare single-letter variables like n, x, t). Kept as
# documentation of intent, not an allow-list filter - see `_IDENTIFIER_RE`'s
# own comment on why gating by assignment SYNTAX already does the real work,
# a prefix allow-list would only add false confidence without adding safety.
_SUBSCRIPT_DIGIT_MAP = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")

# A short identifier token: 1-2 letters (any case - "Si"/"n"/"pH"/"R"),
# optionally followed by a subscript/plain digit suffix written as `_1`,
# `^1`, a Unicode subscript digit, or a bare digit ("R1") - all four are the
# SAME identifier once normalised (see `_normalise_identifier`). Bounded to
# 1-2 letters + up to 2 digits deliberately (never a general "any short
# word") so this can never swallow an ordinary short English word that
# happens to precede "is"/"=" in unrelated prose, beyond what genuine
# assignment phrasing already guards against.
_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<ident>[A-Za-z]{1,2}(?:[_^]?[0-9₀-₉]{1,2})?)(?![A-Za-z0-9])"
    r"[ \t]*(?:=|:|\bis\b|\bequals\b)[ \t]*",
)
# The value "tail" is read as a plain SLICE of the original text starting at
# the match's own end, deliberately NOT captured as part of the regex match
# itself (a real, confirmed bug an earlier version of this had: capturing up
# to 24 trailing characters into the match's own span made `re.finditer`
# resume scanning AFTER that whole captured tail, silently skipping a
# SECOND identifier assignment sitting inside that same window - e.g. "V1 =
# 5V and also V1 = 9V" only ever found the first "V1", never the second,
# because the first match's span had already swallowed the text the second
# identifier needed). Keeping the match span short (identifier + separator
# only) means `finditer` resumes right after the separator, so a second
# assignment nearby is always still reachable.
_IDENTIFIER_TAIL_CHARS = 24


def _normalise_identifier(raw: str) -> str:
    """`R_1`, `R^1`, `R₁` and `R1` are all the SAME identifier - strip the
    LaTeX subscript/exponent markers and fold a Unicode subscript digit down
    to its plain ASCII form, so matching doesn't depend on which written
    form the writer agent or the course's own source text happened to use.
    Case is deliberately PRESERVED (never casefolded): `C` (a capacitor
    reference, or carbon) and `c` (e.g. the speed-of-light symbol) are
    genuinely different identifiers in real scientific writing, and
    conflating them would be a real, confirmed-risk false match, not a
    safety improvement."""
    return raw.translate(_SUBSCRIPT_DIGIT_MAP).replace("_", "").replace("^", "")


@dataclass(frozen=True)
class IdentifierClaim:
    """One `<identifier> = <value>` (or `:`/`is`/`equals`) assignment found
    in some text. `identifier` is the normalised form (see `_normalise_
    identifier`); `raw_identifier` keeps the original written form only for
    building a human-readable detail message."""

    identifier: str
    raw_identifier: str
    value: NumericClaim


def extract_identifier_claims(text: str) -> list[IdentifierClaim]:
    """Every short-identifier assignment in `text` - the identifier-aware
    sibling of `extract_numeric_claims` above, sharing its exact number
    parsing (`_NUMBER_RE`/`_normalise_match`/`_adjacent_unit`) so a value's
    unit/scientific-notation/percentage/fraction handling is identical
    either way, never duplicated. Silently skips an assignment-shaped match
    whose tail doesn't actually contain a parseable number (e.g. `"R1 is
    the resistor nearest the source"`) - only a REAL numeric value counts as
    a claim here."""
    claims: list[IdentifierClaim] = []
    for match in _IDENTIFIER_RE.finditer(text):
        tail = text[match.end():match.end() + _IDENTIFIER_TAIL_CHARS]
        number_match = _NUMBER_RE.search(tail)
        if number_match is None or number_match.start() > 3:
            # The number must sit right at the start of the tail (allowing
            # a couple of stray chars like a space or an opening paren) -
            # otherwise this is some unrelated later number in the same
            # sentence, not the value this identifier was actually assigned
            # (e.g. "n is used 3 times" must never read "n = 3").
            continue
        claim = _normalise_match(number_match)
        if claim is None:
            continue
        unit = "%" if claim.is_percent else _adjacent_unit(tail, number_match.end())
        claims.append(IdentifierClaim(
            identifier=_normalise_identifier(match.group("ident")),
            raw_identifier=match.group("ident"),
            value=NumericClaim(
                value=claim.value, raw=number_match.group(0).strip(), context=frozenset(),
                is_percent=claim.is_percent, unit=unit,
            ),
        ))
    return claims


def _values_agree(a: NumericClaim, b: NumericClaim) -> bool:
    if a.unit and b.unit and a.unit != b.unit:
        return False
    scale = max(abs(a.value), abs(b.value), _ABSOLUTE_FLOOR)
    return abs(a.value - b.value) / scale <= _RELATIVE_TOLERANCE


def find_identifier_value_issues(
    claim_text: str, source_excerpt: str, *, max_issues: int = 5
) -> list[NumericConsistencyIssue]:
    """The identifier-aware counterpart to `find_numeric_contradictions`.
    For every `<identifier> = <value>` the visual's own text states, look
    for the SAME identifier assigned a value anywhere in the course's own
    source excerpt, and resolve to exactly one of three outcomes - never a
    silent fourth "didn't check" outcome the way an ordinary unanchored
    number would get above:

    - SILENT PASS (no issue at all): the identifier is assigned a
      compatible, numerically-agreeing value in the source excerpt.
    - `IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT` (confirmed, high-confidence
      - same downstream treatment as a plain numeric contradiction): the
      identifier resolves to a single, compatible-unit value in the source
      excerpt that genuinely disagrees.
    - `IDENTIFIER_VALUE_NEEDS_REVIEW` (never a block, never a silent pass -
      flagged for a human): the identifier never appears in assignment form
      anywhere in the excerpt at all, every occurrence uses an incompatible
      unit, or the excerpt itself states more than one distinct value for
      the identifier (an internally ambiguous source - never guess which
      one the diagram is supposed to match).

    An identifier that appears in a LATER, unrelated section with a
    different meaning is handled by construction, not by extra logic here:
    `source_excerpt` is already scoped to the visual's own section (see
    `ImageService.chapter_section_context`) - an occurrence outside that
    scope was never passed in, so it can never silently match."""
    if not claim_text.strip() or not source_excerpt.strip():
        return []
    claim_idents = extract_identifier_claims(claim_text)
    if not claim_idents:
        return []
    source_idents = extract_identifier_claims(source_excerpt)
    by_identifier: dict[str, list[IdentifierClaim]] = {}
    for ident_claim in source_idents:
        by_identifier.setdefault(ident_claim.identifier, []).append(ident_claim)

    issues: list[NumericConsistencyIssue] = []
    reported: set[str] = set()
    for claim in claim_idents:
        if claim.identifier in reported:
            continue
        matches = by_identifier.get(claim.identifier)
        if not matches:
            continue  # never seen in the source excerpt at all - not this module's business (see module docstring)
        compatible = [m for m in matches if not (claim.value.unit and m.value.unit and claim.value.unit != m.value.unit)]
        is_element = claim.raw_identifier in _ELEMENT_SYMBOLS
        element_note = f" ({claim.raw_identifier} is a recognised element symbol)" if is_element else ""
        if not compatible:
            reported.add(claim.identifier)
            issues.append(NumericConsistencyIssue(
                reason=IDENTIFIER_VALUE_NEEDS_REVIEW,
                detail=(
                    f"This visual states {claim.raw_identifier} = {claim.value.raw}{element_note}, but the "
                    f"course's own text only mentions {claim.raw_identifier} with a different/incompatible "
                    "unit - unable to confidently confirm or contradict this value."
                ),
            ))
            continue
        if any(_values_agree(claim.value, m.value) for m in compatible):
            reported.add(claim.identifier)
            continue  # silent pass - confirmed consistent
        distinct_values = {round(m.value.value, 9) for m in compatible}
        reported.add(claim.identifier)
        if len(distinct_values) > 1:
            issues.append(NumericConsistencyIssue(
                reason=IDENTIFIER_VALUE_NEEDS_REVIEW,
                detail=(
                    f"This visual states {claim.raw_identifier} = {claim.value.raw}{element_note}, but the "
                    f"course's own text states more than one different value for {claim.raw_identifier} "
                    "- unable to confirm which one this visual should agree with."
                ),
            ))
        else:
            source_value = compatible[0]
            issues.append(NumericConsistencyIssue(
                reason=IDENTIFIER_VALUE_CONTRADICTS_SOURCE_TEXT,
                detail=(
                    f"This visual states {claim.raw_identifier} = {claim.value.raw}{element_note}, but the "
                    f"course's own text states {claim.raw_identifier} = {source_value.value.raw}."
                ),
            ))
        if len(issues) >= max_issues:
            return issues
    return issues
