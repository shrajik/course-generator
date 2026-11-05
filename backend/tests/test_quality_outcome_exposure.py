"""Quality audit follow-up (Phase 3): expose the already-persisted
QualitySnapshot.outcome through the synchronous generation response, the
course-detail response, and the admin course response - without touching
CourseStatus, review_status, or quality-gate evaluation itself.

All offline/mock - no live AI calls, no real course generation beyond the
existing MOCK_OPENAI-backed `service`/`technical_input` fixtures already used
throughout this test suite.
"""

from __future__ import annotations

import app.services.course_service as course_service_module
from app.schemas.course import GenerateRequest
from app.services.quality_framework import QualityReport, quality_snapshot_from_report


def _failing_report(course_id: str) -> QualityReport:
    from app.services.quality_framework import QualityFinding

    return QualityReport(
        course_id=course_id,
        document_id="doc_1",
        findings=[
            QualityFinding(
                artifact="image:block_1",
                dimension="visual",
                severity="blocker",
                reason="image_generation_failed",
                description="synthetic blocker for this test",
                remediation="n/a",
                source="test",
            )
        ],
    )


def _passing_report(course_id: str) -> QualityReport:
    return QualityReport(course_id=course_id, document_id="doc_1")


# --- quality_snapshot_from_report (already covered in test_quality_gate_policy.py,
# not duplicated here) feeds every assertion below via the real persisted
# CourseRecord.quality_snapshot - these tests check it reaches the three
# response shapes the task asks for, not the snapshot's own construction.


async def test_sync_generate_response_reports_quality_outcome_failed(
    service, technical_input, monkeypatch
):
    monkeypatch.setattr(
        course_service_module, "build_course_quality_report", lambda **kw: _failing_report(kw["course_id"])
    )
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert result.quality_outcome == "failed"
    # Status must be completely unaffected - no new CourseStatus value, no
    # repurposing of the existing one.
    assert result.status == "ready"


async def test_sync_generate_response_reports_quality_outcome_passed(service, technical_input):
    """The real mock pipeline's generated course has no quality defects, so
    this exercises the genuine, unforced "passed" path end to end."""
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert result.quality_outcome == "passed"
    assert result.status == "ready"


async def test_sync_generate_response_quality_outcome_is_none_when_evaluation_itself_failed(
    service, technical_input, monkeypatch
):
    """Backward-compatible default: never fabricate an outcome that wasn't
    actually produced (mirrors CourseRecord.quality_snapshot's own contract)."""

    def _boom(**kwargs):
        raise RuntimeError("synthetic evaluation failure")

    monkeypatch.setattr(course_service_module, "build_course_quality_report", _boom)
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert result.quality_outcome is None
    assert result.status == "ready"


def test_quality_outcome_field_defaults_to_none_for_a_response_built_without_it():
    """A caller that doesn't pass quality_outcome at all (e.g. code written
    before this field existed) still constructs a valid GenerateResponse -
    additive, not required."""
    from app.schemas.course import GenerateResponse

    response = GenerateResponse(course_id="c1", document_id="d1", status="ready")
    assert response.quality_outcome is None


async def test_course_detail_endpoint_includes_the_quality_snapshot(client):
    """The course-detail response (GET /api/courses/{id}) already dumps the
    whole CourseRecord, which already carries quality_snapshot (TASK 2) - no
    new backend field needed there, just confirming it actually reaches the
    real HTTP response body."""
    response = client.post(
        "/api/courses",
        json={
            "course_title": "Quality Outcome Detail Test",
            "toc": [{"title": "Chapter One"}],
            "target_audience": "engineers",
            "template": "technical",
        },
    )
    course_id = response.json()["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})

    body = client.get(f"/api/courses/{course_id}").json()
    assert "quality_snapshot" in body["course"]
    assert body["course"]["quality_snapshot"]["outcome"] in ("passed", "failed")
    # Unaffected by this phase, per the strict invariants - still the
    # existing CourseStatus vocabulary.
    assert body["course"]["status"] == "ready"


def test_course_detail_quality_snapshot_is_null_before_any_generation(client):
    response = client.post(
        "/api/courses",
        json={
            "course_title": "Never Generated",
            "toc": [{"title": "Chapter One"}],
            "target_audience": "engineers",
            "template": "technical",
        },
    )
    course_id = response.json()["course_id"]

    body = client.get(f"/api/courses/{course_id}").json()
    assert body["course"]["quality_snapshot"] is None


def test_admin_course_response_reports_quality_outcome_from_metadata():
    """Unit-level: _to_admin_course_response reads quality_snapshot out of
    the generic metadata_json sweep exactly like every other TASK 2 field -
    constructed directly here rather than through a live DB round trip,
    matching this suite's existing admin-schema unit-test style."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.api.admin import _to_admin_course_response
    from app.db.models import Course

    report = _failing_report("crs_admin_test")
    snapshot = quality_snapshot_from_report(report)
    now = datetime.now(timezone.utc)
    course = Course(
        id=uuid4(),
        course_id="crs_admin_test",
        document_id="doc_admin_test",
        title="Admin Quality Outcome Test",
        status="ready",
        template_id="technical_v1",
        input_json={},
        metadata_json={"quality_snapshot": snapshot.model_dump(mode="json")},
        created_at=now,
        updated_at=now,
    )
    response = _to_admin_course_response(course)
    assert response.quality_outcome == "failed"
    assert response.status == "ready"


def test_admin_course_response_quality_outcome_is_none_without_a_snapshot():
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.api.admin import _to_admin_course_response
    from app.db.models import Course

    now = datetime.now(timezone.utc)
    course = Course(
        id=uuid4(),
        course_id="crs_admin_old",
        document_id="doc_admin_old",
        title="Pre-Phase-2 Course",
        status="ready",
        template_id="technical_v1",
        input_json={},
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    response = _to_admin_course_response(course)
    assert response.quality_outcome is None
