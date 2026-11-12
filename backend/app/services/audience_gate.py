"""Audience/template compatibility gate.

Real, confirmed production failure this closes: a course was created with
`target_audience` = "researchers and senior scientists... advanced
practitioners" against an uploaded template whose own document states it is
for "Children aged 6 to 11 (Class 1 to 5)". Nothing caught the contradiction
- `CourseInput.target_audience` and the chosen template were completely
independent, and `CourseTemplate` had no audience field to even compare
against (see that field's own docstring). The course generated a graduate-
level quantum-chemistry course squeezed through a kids'-book module
structure: mismatched tone, reading level, section order and page budget
throughout.

This is a real compatibility JUDGMENT (e.g. "advanced practitioners" vs.
"Children aged 6 to 11" needs reasoning, not keyword overlap), so it is one
small, focused model call - never a guess from string matching, and never
run at all unless BOTH sides have a real, stated audience to compare.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.schemas.template import CourseTemplate
from app.services.openai_service import AIClient

log = get_logger(__name__)

_SYSTEM = (
    "You decide whether a course's intended audience is compatible with a template's own "
    "stated audience. Judge ONLY audience compatibility - never content quality, topic, or "
    "anything else. Two audiences are INCOMPATIBLE when a course genuinely built for one "
    "would be unusable, unreadable, or inappropriate for the other - for example, a template "
    "written for young children versus a course meant for expert researchers, or a template "
    "for absolute beginners versus a course explicitly for advanced practitioners. They are "
    "COMPATIBLE whenever they reasonably overlap, or one is simply a more specific or more "
    "general version of the other (e.g. \"software engineers\" and \"backend developers\")."
)


class AudienceCompatibility(BaseModel):
    model_config = ConfigDict(extra="ignore")

    compatible: bool = True
    reason: str = ""


async def check_audience_compatibility(
    ai: AIClient, *, course_audience: str, template: CourseTemplate
) -> None:
    """Raises `ValidationFailedError` when `course_audience` and the
    template's own stated `template.audience` are a real, confirmed
    mismatch - meant to be called before any generation starts, so a
    blocked course never reaches planning or writing.

    A deliberate no-op (no model call at all) whenever either side is
    blank - every built-in template and most uploaded ones state no
    audience, which must keep working exactly as it always has."""
    stated_course_audience = (course_audience or "").strip()
    stated_template_audience = (template.audience or "").strip()
    if not stated_course_audience or not stated_template_audience:
        return

    result = await ai.structured(
        schema=AudienceCompatibility,
        system=_SYSTEM,
        user=(
            f"Course's intended audience: {stated_course_audience}\n"
            f"Template \"{template.name}\" is written for: {stated_template_audience}\n\n"
            "Are these two audiences compatible?"
        ),
        purpose="audience_template_compatibility",
    )
    if not result.compatible:
        log.warning(
            "Blocked course creation: template '%s' audience (%r) vs course audience (%r) - %s",
            template.template_id, stated_template_audience, stated_course_audience, result.reason,
        )
        raise ValidationFailedError(
            f"This template is written for \"{stated_template_audience}\", but this course's "
            f"audience is \"{stated_course_audience}\""
            + (f" - {result.reason}" if result.reason else "")
            + ". Choose a different template or adjust the course's target audience."
        )
