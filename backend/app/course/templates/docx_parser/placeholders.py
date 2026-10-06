"""Controlled `{{PLACEHOLDER}}` mapping for uploaded DOCX templates.

An uploaded template marks the spots it expects the generator to fill. This
module is the *allow-list* for those spots: a placeholder is only ever
substituted when it appears here with an explicit meaning. Anything else is
reported as unknown (upload validation and generation logs) rather than being
blindly replaced or silently dropped - see `resolve` and `unknown_names`.

Deliberately not a general templating engine: arbitrary text substitution
into AI-generated content is how a "template" quietly becomes an injection
vector, and how a typo'd placeholder becomes an invisible empty string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

# `{{ NAME }}` with optional inner whitespace; names are upper snake case.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


@dataclass(frozen=True)
class Placeholder:
    """One supported substitution point."""

    name: str
    description: str
    # Reads the resolution context; returns "" when the value is unknown, in
    # which case the placeholder is left visible rather than blanked out.
    resolve: Callable[[dict[str, Any]], str]


def _ctx(key: str) -> Callable[[dict[str, Any]], str]:
    def read(context: dict[str, Any]) -> str:
        value = context.get(key)
        return "" if value is None else str(value)

    return read


SUPPORTED: dict[str, Placeholder] = {
    placeholder.name: placeholder
    for placeholder in (
        Placeholder("COURSE_TITLE", "The course title", _ctx("course_title")),
        Placeholder("COURSE_SUBTITLE", "Course subtitle or tagline", _ctx("course_subtitle")),
        Placeholder("COURSE_DESCRIPTION", "Course summary", _ctx("course_description")),
        Placeholder("AUDIENCE", "Target audience", _ctx("audience")),
        Placeholder("LEVEL", "Learner level/education attainment", _ctx("level")),
        Placeholder("DOMAIN", "Course subject domain", _ctx("domain")),
        Placeholder("OBJECTIVES", "Course learning objectives", _ctx("objectives")),
        Placeholder("PREREQUISITES", "Course prerequisites", _ctx("prerequisites")),
        Placeholder("DELIVERY_CHUNK_SIZE", "Delivery chunk word cap", _ctx("chunk_word_cap")),
        Placeholder("DIFFICULTY", "Difficulty level", _ctx("difficulty")),
        Placeholder("LEARNER_ROLE", "Learner's role or context", _ctx("learner_role")),
        Placeholder("PRIOR_KNOWLEDGE", "Assumed prior knowledge", _ctx("prior_knowledge")),
        Placeholder("CHAPTER_NUMBER", "Current chapter number", _ctx("chapter_number")),
        Placeholder("CHAPTER_TITLE", "Current chapter title", _ctx("chapter_title")),
        Placeholder("VERSION", "Template/course version", _ctx("version")),
        Placeholder("EFFECTIVE_DATE", "Generation date", _ctx("effective_date")),
        Placeholder("LANGUAGE", "Course language", _ctx("language")),
    )
}


def names_in(text: str) -> list[str]:
    """Every placeholder name in `text`, in order, without duplicates."""
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text or "")))


def unknown_names(names: list[str]) -> list[str]:
    return [name for name in names if name not in SUPPORTED]


def resolve(text: str, context: dict[str, Any]) -> str:
    """Substitute supported placeholders that have a value.

    An unsupported placeholder, or a supported one with no value in this
    context, is left exactly as written. Leaving `{{FOO}}` visible in the
    output is a bug someone can see and report; silently replacing it with an
    empty string is a bug nobody notices until a client does.
    """

    def substitute(match: re.Match[str]) -> str:
        placeholder = SUPPORTED.get(match.group(1))
        if placeholder is None:
            return match.group(0)
        value = placeholder.resolve(context)
        return value if value else match.group(0)

    return PLACEHOLDER_RE.sub(substitute, text or "")


def context_from(
    course_input: Any, *, blueprint: Any = None, chapter: Any = None
) -> dict[str, Any]:
    """Build the resolution context from the objects generation already has.

    Tolerates missing attributes so it can be called from anywhere in the
    pipeline, including before a blueprint exists.
    """
    from datetime import date

    profile = getattr(course_input, "learner_profile", None)
    context: dict[str, Any] = {
        "course_title": getattr(course_input, "course_title", ""),
        "audience": getattr(course_input, "target_audience", ""),
        "language": getattr(course_input, "language", ""),
        "effective_date": date.today().isoformat(),
    }
    if profile is not None:
        context["level"] = getattr(profile, "edu_level", "")
        context["domain"] = getattr(profile, "target_domain", "")
        context["chunk_word_cap"] = getattr(profile, "chunk_word_cap", "")
        context["learner_role"] = getattr(profile, "age_group", "")
    if blueprint is not None:
        context["course_description"] = getattr(blueprint, "course_summary", "")
        objectives = getattr(blueprint, "learning_objectives", None) or []
        context["objectives"] = "; ".join(str(item) for item in objectives)
        context["prerequisites"] = "; ".join(
            str(item) for item in (getattr(blueprint, "prerequisites", None) or [])
        )
    if chapter is not None:
        context["chapter_title"] = getattr(chapter, "title", "")
        context["chapter_number"] = getattr(chapter, "order", "")
    return {key: value for key, value in context.items() if value not in (None, "")}
