"""Audience/template compatibility gate - see app.services.audience_gate's
own module docstring for the real, confirmed production failure this closes
(an advanced-researcher course generated against a Kids Course template with
nothing ever catching the contradiction)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailedError
from app.schemas.template import CourseTemplate
from app.services.audience_gate import AudienceCompatibility, check_audience_compatibility


class _FakeAI:
    """A minimal AIClient double - records every call and returns a fixed
    `AudienceCompatibility` verdict, so each test controls exactly what the
    model "decided" without depending on MockAIClient's generic _BUILDERS
    registry (which has no entry for this schema and would just return the
    schema's own safe defaults)."""

    def __init__(self, compatible: bool, reason: str = "") -> None:
        self._verdict = AudienceCompatibility(compatible=compatible, reason=reason)
        self.calls: list[dict] = []

    async def structured(self, *, schema, system, user, **kwargs):
        self.calls.append({"schema": schema, "system": system, "user": user, **kwargs})
        return self._verdict


def _template(audience: str = "") -> CourseTemplate:
    return CourseTemplate(template_id="uploaded:kids-test", kind="non_technical", name="Kids Course", audience=audience)


@pytest.mark.asyncio
async def test_a_compatible_audience_and_template_passes():
    ai = _FakeAI(compatible=True)
    await check_audience_compatibility(
        ai, course_audience="New backend engineers", template=_template("Software engineers")
    )
    assert len(ai.calls) == 1  # the gate did run its judgment call


@pytest.mark.asyncio
async def test_a_confirmed_mismatch_blocks_course_creation_with_a_clear_message():
    ai = _FakeAI(compatible=False, reason="one is for young children, the other for expert researchers")
    with pytest.raises(ValidationFailedError) as excinfo:
        await check_audience_compatibility(
            ai,
            course_audience="Advanced practitioners: researchers and senior scientists",
            template=_template("Children aged 6 to 11 (Class 1 to 5)"),
        )
    message = str(excinfo.value)
    assert "Children aged 6 to 11" in message
    assert "Advanced practitioners" in message
    assert "one is for young children" in message


@pytest.mark.asyncio
async def test_a_blank_template_audience_skips_the_check_entirely():
    ai = _FakeAI(compatible=False)  # would block if ever actually called
    await check_audience_compatibility(ai, course_audience="Advanced researchers", template=_template(""))
    assert ai.calls == []  # never even invoked - not silently passing a call that ran


@pytest.mark.asyncio
async def test_a_blank_course_audience_skips_the_check_entirely():
    ai = _FakeAI(compatible=False)  # would block if ever actually called
    await check_audience_compatibility(ai, course_audience="", template=_template("Children aged 6 to 11"))
    assert ai.calls == []


@pytest.mark.asyncio
async def test_a_both_sides_blank_skips_the_check_entirely():
    ai = _FakeAI(compatible=False)  # would block if ever actually called
    await check_audience_compatibility(ai, course_audience="", template=_template(""))
    assert ai.calls == []
