"""Deterministic, zero-AI-call icon artwork for a fixed set of GENERIC
pedagogical section openers (Quiz, Summary, Best Practices/Pro Tips,
Common Mistakes, Exercise, Challenge, Learning Objectives) - see
`ImageService._generate_illustration`'s own docstring for the
`illustration_style == "section_intro"` contract this plugs into.

Root cause this closes: these openers carry no subject-specific content
at all (unlike a real `textbook`/companion illustration) - every chapter
in every course gets the exact same "Quiz" icon, the exact same "Common
Mistakes" icon, with only the course's own accent colour changing. Asking
a raster image model to draw one anyway bought nothing (no real
information content) while paying its full failure cost: a production
audit of a real generated course (ThermoDynamics, doc_a2f5625a7fd9) found
10/10 of that course's image-generation failures were exactly this
category - MAI-Image-2.6 baking garbled pseudo-text/pseudo-math into these
specific "strictly no text" icon prompts despite the explicit no-text
instruction, correctly withheld by the fallback-cascade QA rather than
published, but still a 100%-avoidable cost. A small set of hand-authored,
theme-coloured SVG glyphs is strictly better here: it can never fail
generation, never calls an image model (so it never consumes the bounded
retry budget or adds to provider rate-limit pressure), and - being
identical in style every time - reads as a more deliberate, consistent
textbook icon system than a fresh AI picture reinterpreting "Quiz"
differently every chapter.

Every OTHER section_intro image (a course's actual subject matter -
"Core Concepts", "Technical Example", a chapter's opening orientation
picture) is untouched and keeps going through the ordinary AI
illustration path - this module only ever intercepts the fixed, subject-
agnostic set below.
"""

from __future__ import annotations

import re

from app.schemas.template import TemplateTheme

_ICON_WIDTH = 400
_ICON_HEIGHT = 300

# Matched with `re.search` (word-boundary anchored) against a section's own
# heading text (ImageService's `section_title`, the nearest preceding
# heading block) - case-insensitive. A per-item custom suffix a course adds
# to its own exercise/challenge heading ("Exercise: Interpolation and
# Dimensional Check", "Challenge: EOS Fit and Sensitivity" - both real,
# confirmed examples) still matches, since the pattern only needs to find
# "exercise"/"challenge" somewhere in the text, not match it exactly.
_SECTION_ICON_PATTERNS: dict[str, str] = {
    "learning_objectives": r"\blearning objectives?\b",
    "common_mistakes": r"\bcommon mistakes?\b",
    "best_practices": r"\bbest practices?\b|\bpro tips?\b",
    "challenge": r"\bchallenge\b",
    "exercise": r"\bexercises?\b",
    "quiz": r"\bquiz(?:zes)?\b",
    "summary": r"\bsummary\b",
}


def match_generic_section_icon(section_title: str) -> str | None:
    """Returns the icon kind a section's heading text matches, or `None`
    when it names no known generic pedagogical section - every other
    (subject-specific) section_intro keeps going through the ordinary AI
    illustration path untouched."""
    text = section_title.strip().lower()
    if not text:
        return None
    for kind, pattern in _SECTION_ICON_PATTERNS.items():
        if re.search(pattern, text):
            return kind
    return None


def _card(soft: str, border: str, body: str) -> bytes:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_ICON_WIDTH}" height="{_ICON_HEIGHT}" '
        f'viewBox="0 0 {_ICON_WIDTH} {_ICON_HEIGHT}" role="img" aria-hidden="true">'
        f'<rect x="0" y="0" width="{_ICON_WIDTH}" height="{_ICON_HEIGHT}" rx="18" fill="{soft}"/>'
        f'<rect x="2" y="2" width="{_ICON_WIDTH - 4}" height="{_ICON_HEIGHT - 4}" rx="16" '
        f'fill="none" stroke="{border}" stroke-width="2"/>'
        f"{body}"
        f"</svg>"
    )
    return svg.encode("utf-8")


def render_section_icon_svg(icon_kind: str, theme: TemplateTheme) -> bytes:
    """A clean, theme-coloured glyph for one of `_SECTION_ICON_PATTERNS`'
    keys, at the exact fixed size (400x300) a section_intro image always
    displays at (see `ImageService._SECTION_INTRO_WIDTH`/`_HEIGHT`), so no
    resize step is needed on the way out - this is already its final,
    correctly-proportioned intrinsic size."""
    accent = theme.accent_color
    soft = theme.accent_soft
    border = theme.border_color
    cx, cy = _ICON_WIDTH / 2, _ICON_HEIGHT / 2

    if icon_kind == "quiz":
        body = (
            f'<circle cx="{cx}" cy="{cy}" r="70" fill="none" stroke="{accent}" stroke-width="10"/>'
            f'<text x="{cx}" y="{cy + 32}" font-family="Georgia, serif" font-size="110" '
            f'font-weight="bold" fill="{accent}" text-anchor="middle">?</text>'
        )
    elif icon_kind == "summary":
        lines = "".join(
            f'<rect x="{cx - 90}" y="{cy - 55 + i * 35}" width="{180 if i < 2 else 120}" '
            f'height="14" rx="7" fill="{accent}" opacity="{1 - i * 0.15}"/>'
            for i in range(3)
        )
        body = (
            f"{lines}"
            f'<circle cx="{cx + 95}" cy="{cy + 50}" r="26" fill="{accent}"/>'
            f'<path d="M {cx + 84} {cy + 50} l 8 8 l 16 -16" stroke="{soft}" '
            f'stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    elif icon_kind == "common_mistakes":
        body = (
            f'<path d="M {cx} {cy - 75} L {cx + 85} {cy + 60} L {cx - 85} {cy + 60} Z" '
            f'fill="none" stroke="{accent}" stroke-width="10" stroke-linejoin="round"/>'
            f'<rect x="{cx - 6}" y="{cy - 35}" width="12" height="55" rx="6" fill="{accent}"/>'
            f'<circle cx="{cx}" cy="{cy + 40}" r="8" fill="{accent}"/>'
        )
    elif icon_kind == "best_practices":
        body = (
            f'<rect x="{cx - 70}" y="{cy - 85}" width="140" height="170" rx="14" '
            f'fill="none" stroke="{accent}" stroke-width="8"/>'
            f'<rect x="{cx - 30}" y="{cy - 100}" width="60" height="26" rx="8" fill="{accent}"/>'
            f'<path d="M {cx - 38} {cy - 10} l 24 24 l 48 -56" stroke="{accent}" '
            f'stroke-width="10" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    elif icon_kind == "exercise":
        body = (
            f'<rect x="{cx - 75}" y="{cy - 90}" width="150" height="180" rx="10" '
            f'fill="none" stroke="{accent}" stroke-width="8"/>'
            f'<line x1="{cx - 45}" y1="{cy - 40}" x2="{cx + 45}" y2="{cy - 40}" '
            f'stroke="{accent}" stroke-width="7" stroke-linecap="round"/>'
            f'<line x1="{cx - 45}" y1="{cy}" x2="{cx + 45}" y2="{cy}" '
            f'stroke="{accent}" stroke-width="7" stroke-linecap="round"/>'
            f'<line x1="{cx - 45}" y1="{cy + 40}" x2="{cx + 10}" y2="{cy + 40}" '
            f'stroke="{accent}" stroke-width="7" stroke-linecap="round"/>'
        )
    elif icon_kind == "challenge":
        body = (
            f'<line x1="{cx - 55}" y1="{cy - 90}" x2="{cx - 55}" y2="{cy + 90}" '
            f'stroke="{accent}" stroke-width="9" stroke-linecap="round"/>'
            f'<path d="M {cx - 55} {cy - 85} L {cx + 75} {cy - 55} L {cx - 55} {cy - 10} Z" fill="{accent}"/>'
        )
    elif icon_kind == "learning_objectives":
        body = (
            f'<circle cx="{cx}" cy="{cy}" r="85" fill="none" stroke="{accent}" stroke-width="8"/>'
            f'<circle cx="{cx}" cy="{cy}" r="52" fill="none" stroke="{accent}" stroke-width="8"/>'
            f'<circle cx="{cx}" cy="{cy}" r="16" fill="{accent}"/>'
        )
    else:
        # Unreachable in practice (every caller first checks
        # `match_generic_section_icon` returned non-None, and that function
        # only ever returns a key handled above) - a plain dot rather than
        # raising keeps this function total if a kind is ever added to the
        # pattern table without a matching branch here.
        body = f'<circle cx="{cx}" cy="{cy}" r="60" fill="{accent}"/>'

    return _card(soft, border, body)
