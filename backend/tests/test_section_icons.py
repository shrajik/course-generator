"""See app.render.section_icons's own module docstring for the production
root cause this closes (ThermoDynamics/doc_a2f5625a7fd9): every observed
image-generation failure in that course was a generic pedagogical section
opener (Quiz/Summary/Best Practices/Common Mistakes/Exercise), which this
module replaces with a deterministic, zero-AI-call SVG glyph."""

from __future__ import annotations

import re

from app.render.section_icons import (
    _SECTION_ICON_PATTERNS,
    match_generic_section_icon,
    render_section_icon_svg,
)
from app.schemas.template import TemplateTheme


def test_known_generic_pedagogical_sections_match_their_icon_kind():
    cases = {
        "Quiz": "quiz",
        "Summary": "summary",
        "Common Mistakes": "common_mistakes",
        "Best Practices": "best_practices",
        "Pro Tips": "best_practices",  # non_technical_v1.json's own label for the same idea
        "Learning Objectives": "learning_objectives",
        "Challenge": "challenge",
        "Exercise": "exercise",
        # A per-item custom suffix (a real, confirmed pattern this course
        # generator produces) must still match its generic icon kind.
        "Exercise: Interpolation and Dimensional Check": "exercise",
        "Challenge: EOS Fit and Sensitivity": "challenge",
        "QUIZ": "quiz",  # case-insensitive
    }
    for section_title, expected_kind in cases.items():
        assert match_generic_section_icon(section_title) == expected_kind, section_title


def test_subject_specific_sections_never_match_a_generic_icon():
    """A course's actual subject matter must keep going through the
    ordinary AI illustration path - this module only ever intercepts the
    fixed, subject-agnostic set above."""
    for section_title in (
        "",
        "Core Concepts",
        "Technical Example: Turbine, Compressor, and Heat Exchanger",
        "Second Law, Entropy, and Practical Consequences",
        "Visual Explanation",
        "How It Works",
    ):
        assert match_generic_section_icon(section_title) is None, section_title


def test_every_pattern_key_renders_without_raising():
    theme = TemplateTheme()
    for kind in _SECTION_ICON_PATTERNS:
        svg = render_section_icon_svg(kind, theme)
        assert svg.startswith(b"<svg")
        assert b"</svg>" in svg
        # The theme's own accent colour must actually be used, not a fixed
        # hardcoded palette - otherwise every course's icons would look
        # identical regardless of its template theme.
        assert theme.accent_color.encode("utf-8") in svg


def test_rendered_icon_is_well_formed_xml():
    """A malformed SVG (unescaped `&`/`<` in an attribute, mismatched tags)
    would fail to embed in the PDF export - these icons use no user-
    supplied text at all, but this still catches a hand-authoring slip in
    any of the per-kind path/shape markup."""
    import xml.etree.ElementTree as ET

    theme = TemplateTheme()
    for kind in _SECTION_ICON_PATTERNS:
        ET.fromstring(render_section_icon_svg(kind, theme))


def test_icon_is_the_exact_fixed_section_intro_display_size():
    theme = TemplateTheme()
    svg = render_section_icon_svg("quiz", theme).decode("utf-8")
    assert re.search(r'width="400"', svg)
    assert re.search(r'height="300"', svg)


def test_an_unrecognised_kind_still_renders_something_rather_than_raising():
    """Defensive only - every real caller first checks
    `match_generic_section_icon` returned non-None, so this path is
    unreachable in practice, but the function stays total."""
    svg = render_section_icon_svg("not_a_real_kind", TemplateTheme())
    assert svg.startswith(b"<svg")
