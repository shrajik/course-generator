"""Writing-style rules as deterministic code: the base guide and client overrides.

`style_guide_brief` renders the rules injected into the writer/reviewer
prompts; `style_issues` are the deterministic reviewer checks that back them.
Both are pure functions of the `StyleGuide` - and both return nothing when no
style guide was chosen, which keeps prompts and reviews for existing courses
exactly as they were.

The MSTP rules themselves live in app/course/pedagogy.py (`mstp_rule_lines`):
they were written there first, and the learner-profile brief still uses them
when no explicit style guide is chosen.

Prompt-safety note (see app/course/pedagogy.py): no line may start with a
number and a dot, and the reserved labels COURSE TITLE:, CHAPTER TITLE: and
TEMPLATE KIND: must not appear.
"""

from __future__ import annotations

import re

from app.course.pedagogy import mstp_rule_lines
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES
from app.schemas.document import Block
from app.schemas.review import ReviewIssue
from app.schemas.style import STYLE_GUIDE_LABELS, StyleGuide

_CITATION_RULE = {
    "author_date": (
        "Cite every sourced claim in author-date form: a short parenthetical (author, year) "
        "in the text and a reference list."
    ),
    "notes_bibliography": (
        "Cite every sourced claim in notes-bibliography form: a numbered footnote or endnote "
        "marker in the text and a bibliography."
    ),
}


def cmos_rule_lines(style: StyleGuide) -> list[str]:
    return [
        "WRITING STYLE (Chicago Manual of Style house rules):",
        "- Write in a formal editorial register, not in direct second-person instruction.",
        "- Spell out whole numbers under 101 in running text, and follow CMOS hyphenation and "
        "capitalisation rules for compound modifiers.",
        "- Use one citation system for the whole deliverable and never mix systems. "
        + _CITATION_RULE[style.citation_system],
        "- A book- or report-length deliverable follows manuscript front-matter conventions: "
        "title, contents and an executive summary.",
    ]


def style_guide_brief(style: StyleGuide | None) -> str:
    """The style rules for the prompts, or "" when no style guide is chosen."""
    if style is None:
        return ""

    lines = [f"STYLE GUIDE FOR THIS COURSE: {STYLE_GUIDE_LABELS[style.base]}."]
    lines += mstp_rule_lines() if style.base == "mstp" else cmos_rule_lines(style)

    if style.has_overrides:
        lines += ["", "CLIENT STYLE OVERRIDES (these take precedence over the base guide):"]
        if style.brand_terms:
            lines.append(
                "- Always write these terms exactly as shown, including capitalisation: "
                + "; ".join(style.brand_terms)
                + "."
            )
        if style.banned_words:
            lines.append(
                "- Never use these words or phrases, in any form: " + "; ".join(style.banned_words) + "."
            )
        lines.append(
            "- If an override conflicts with a base-guide rule, follow the override and say so in "
            "one short note, so a person can confirm it rather than the conflict being resolved "
            "silently."
        )
    return "\n".join(lines)


# --- reviewer checks ---------------------------------------------------------


def _text_blocks(blocks: list[Block]) -> list[tuple[int, str]]:
    return [
        (index, str(block.content.get("text", "")))
        for index, block in enumerate(blocks)
        if block.type in TEXTUAL_BLOCK_TYPES
    ]


def _term_pattern(term: str) -> re.Pattern[str]:
    # Whole-word where the term starts/ends with a word character, so "app" does
    # not match inside "apple" but "C++" still matches.
    prefix = r"\b" if re.match(r"\w", term) else ""
    suffix = r"\b" if re.search(r"\w$", term) else ""
    return re.compile(prefix + re.escape(term) + suffix, re.IGNORECASE)


_SECOND_PERSON_RE = re.compile(r"\byou(?:r|rs|rself)?\b", re.IGNORECASE)
# A bare whole number 1-100 in running text. Digits touching other characters
# (versions, ranges, percentages, times, identifiers) are left alone.
_BARE_NUMBER_RE = re.compile(r"(?<![\w.,:/%$#+-])(?:100|[1-9]\d?)(?![\w.,:/%+-]|\s*(?:%|percent))")


def style_issues(blocks: list[Block], style: StyleGuide | None) -> list[ReviewIssue]:
    """Deterministic checks for the chosen style guide. [] when none is chosen."""
    if style is None:
        return []
    issues: list[ReviewIssue] = []

    def add(severity: str, index: int, description: str, suggestion: str) -> None:
        issues.append(
            ReviewIssue(
                severity=severity,
                category="style",
                block_index=index,
                description=description,
                suggestion=suggestion,
            )
        )

    text_blocks = _text_blocks(blocks)

    for term in style.banned_words:
        pattern = _term_pattern(term)
        for index, text in text_blocks:
            if pattern.search(text):
                add("major", index, f'Client-banned wording "{term}" is used.',
                    f'Rewrite the sentence without "{term}".')

    for term in style.brand_terms:
        pattern = _term_pattern(term)
        for index, text in text_blocks:
            wrong = sorted({m.group(0) for m in pattern.finditer(text) if m.group(0) != term})
            if wrong:
                add("minor", index, f'Brand term written as "{wrong[0]}" instead of "{term}".',
                    f'Write it exactly as "{term}".')

    if style.base == "cmos":
        for index, text in text_blocks:
            if _SECOND_PERSON_RE.search(text):
                add("minor", index, "Direct second person (\"you\") in a formal CMOS text.",
                    "Use a formal editorial register instead of addressing the reader directly.")
            if _BARE_NUMBER_RE.search(text):
                add("minor", index, "A number under 101 is written as digits in running text.",
                    "Spell out whole numbers under 101 (CMOS), unless it is a measurement or a reference.")
    return issues
