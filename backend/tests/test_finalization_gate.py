"""MOM Phase 9 - final document completeness gate.

Reuses the existing orchestration end to end: `CourseService._finalise`'s own
`status`/`warnings` fields (no new status, no new state machine) plus the
quality framework built in Phase 8. These tests prove the gate is genuinely
wired into the real (mock) generation pipeline, not just unit-tested in
isolation.
"""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError
from app.schemas.course import GenerateRequest
from app.services.quality_framework import QualityReport


async def test_a_level_3_technical_course_surfaces_a_quality_gate_warning(
    service, technical_input
):
    """The mock technical writer never adds a scenario block (see
    test_content_level.py's own equivalent test), so a level-3 technical
    course is a real, reliable way to produce a genuine hard failure and
    prove the gate actually surfaces it - not a hand-built fixture."""
    level_3_input = technical_input.model_copy(update={"content_level": 3})
    record = await service.create_course(level_3_input)

    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert any(w.startswith("quality gate:") for w in result.warnings)
    stored = await service.get_course_record(record.course_id)
    assert any(w.startswith("quality gate:") for w in stored.warnings)
    # The gate is advisory - it must never downgrade a course that otherwise
    # generated successfully away from "ready".
    assert stored.status == "ready"


async def test_a_clean_course_has_no_quality_gate_warning(service, non_technical_input):
    """Regression guard: a course with nothing wrong must not get a
    spurious warning - the gate only speaks up when there's a real finding."""
    record = await service.create_course(non_technical_input)

    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert not any(w.startswith("quality gate:") for w in result.warnings)


async def test_get_quality_report_reflects_current_persisted_state(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    report = await service.get_quality_report(record.course_id)

    assert isinstance(report, QualityReport)
    assert report.course_id == record.course_id
    assert report.document_id


async def test_get_quality_report_on_an_unknown_course_raises_not_found(service):
    with pytest.raises(NotFoundError):
        await service.get_quality_report("crs_does_not_exist")


async def test_get_quality_report_before_generation_has_no_document_findings(
    service, technical_input
):
    """A course that was only created (never generated) has no document yet -
    the report must degrade gracefully (empty document-related findings)
    rather than raise, since `chapters`/`document` are both legitimately
    empty at this stage."""
    record = await service.create_course(technical_input)

    report = await service.get_quality_report(record.course_id)

    assert report.document_id == ""
    assert report.findings == []
