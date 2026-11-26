"""Correction-intent classification (diagram quality audit follow-up,
Phase 7) - a deterministic, zero-AI-call classifier over the user's OWN
typed `replace_image` correction text, shared by both the `diagram`
pipeline (app.services.diagram_service) and the `concept_experience`
pipeline (app.services.visual_planner) - a genuinely generic, text-only
classifier with no coupling to either pipeline's own spec shape, so it
lives here once rather than duplicated (unlike the smaller, pipeline-
specific logic those two modules deliberately DO still duplicate, e.g.
each module's own relationship-label-verbosity check).

Different kinds of correction genuinely need different POLICIES - "fix
the overlapping arrow text" must never touch what the diagram actually
says, while "make this suitable for children" is explicitly asking for
real simplification the default "preserve everything" correction-mode
rule would otherwise resist. Checked in priority order (most consequential/
specific intent first) so a prompt that touches on more than one signal
(e.g. "simplify this for a younger audience") still resolves to ONE clear
policy rather than an ambiguous blend. `None` when nothing matches - the
existing general correction-mode language in DIAGRAM_SYSTEM/CONCEPT_SYSTEM
still applies on its own, unchanged, exactly as before this classifier
existed."""

from __future__ import annotations

import re

_CORRECTION_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "type_change",
        re.compile(
            r"\b(different (kind|type|shape|diagram)|redesign|convert (it|this) (in)?to|"
            r"change (it|this) (in)?to a|instead of a .* diagram|use a .* (diagram|chart) instead)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "audience",
        re.compile(
            r"\b(for (a )?(child|children|kid|kids|beginner|beginners|student|students|"
            r"non[- ]?technical|layman|laypeople)|(younger|older|less technical|more technical|"
            r"more advanced|simpler) audience|audience[- ]appropriate)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "density",
        re.compile(
            r"\b(too (crowded|dense|busy|cluttered)|less (crowded|dense|busy|cluttered)|"
            r"too much (text|detail|information)|simplify|declutter|consolidate|"
            r"fewer (nodes|steps|boxes|items)|reduce (the )?(clutter|density|detail))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "structural",
        re.compile(
            r"\b(add (a|an|the)|remove (a|an|the)|rename|reconnect|new (node|step|entity|edge)|"
            r"missing (node|step|entity|edge|connection)|delete (a|an|the))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "semantic",
        re.compile(
            r"\b(is (wrong|incorrect|inaccurate)|should be|actually (means|is)|"
            r"factually|this relationship is (wrong|incorrect)|get(s)? .* backwards|"
            r"mislabel(l)?ed|wrong (label|relationship|connection|direction))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "expansion",
        re.compile(
            r"\b(add more (detail|information|context)|expand (on|this)|elaborate|go into more detail)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "styling",
        re.compile(
            r"\b(colou?rs?|palette|fonts?|styl(e|ing)|theme|background|border|shadow|bold|italic|"
            r"make it (look|more) (colou?rful|professional|modern|clean))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "geometry",
        re.compile(
            r"\b(overlap(ping|s)?|cramped|cut off|clipped|spacing|too (close|tight|small|big)|"
            r"doesn'?t fit|out of (the )?canvas|crossing|collision|crosses?|off[- ]?screen)\b",
            re.IGNORECASE,
        ),
    ),
)

# One targeted policy sentence per intent - injected into the correction
# block ALONGSIDE (never replacing) the general correction-mode rule
# already in DIAGRAM_SYSTEM/CONCEPT_SYSTEM, so the model gets both the
# universal "this is a correction" framing and a specific, sharpened
# instruction for exactly what kind of correction this one is.
CORRECTION_INTENT_POLICY: dict[str, str] = {
    "geometry": (
        "This reads as a PURE layout/geometry fix (spacing, overlap, clipping, routing) - preserve "
        "the exact content, wording, structure and relationships; change ONLY what affects how it's "
        "laid out, never what it says."
    ),
    "styling": (
        "This reads as a PURE visual-styling change (colour, font, look and feel) - preserve the "
        "exact content, wording, structure and relationships; change ONLY visual presentation, "
        "never what it says."
    ),
    "audience": (
        "This is an AUDIENCE-ADAPTATION request - actively simplify or adjust terminology, "
        "complexity and level of detail for the audience named; real simplification in service of "
        "this is expected and correct here, not something the default 'preserve everything' rule "
        "should resist."
    ),
    "density": (
        "This is a DENSITY-REDUCTION request - actively consolidate, shorten and simplify; genuine "
        "trimming/merging in service of this is expected and correct here, not something the "
        "default 'preserve everything' rule should resist, as long as the core subject survives."
    ),
    "structural": (
        "This names a SPECIFIC structural change (add/remove/rename/reconnect) - apply exactly "
        "that change; copy every other node/edge/label you are not asked to change verbatim."
    ),
    "semantic": (
        "This names a SPECIFIC factual/relationship error - fix exactly that one fact/connection; "
        "preserve every other correct fact and the overall structure unchanged."
    ),
    "type_change": (
        "This asks for a DIFFERENT representation/kind - redesign using the newly-requested shape, "
        "while preserving every fact/relationship the current one got right."
    ),
    "expansion": (
        "This is a CONTENT-EXPANSION request - add the specific additional detail asked for while "
        "preserving everything already there unchanged."
    ),
}


def classify_correction_intent(prompt: str) -> str | None:
    """Returns the single best-matching intent label for `prompt` (checked
    in the priority order `_CORRECTION_INTENT_PATTERNS` defines), or `None`
    if nothing matches - a deliberately narrow, keyword-based classifier,
    not an LLM call (see this module's own docstring for why)."""
    text = prompt.strip()
    if not text:
        return None
    for intent, pattern in _CORRECTION_INTENT_PATTERNS:
        if pattern.search(text):
            return intent
    return None
