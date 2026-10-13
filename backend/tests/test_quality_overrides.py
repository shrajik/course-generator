"""MOM quality-governance follow-up - the four distinct, admin-only
quality-gate override actions (service layer).

All offline/mock (USE_DATABASE=false, see conftest.py) - these exercise
`CourseService.approve_course_override_*` directly rather than through the
HTTP layer, since the admin-only role check and the quality-gate logic are
independent concerns; the role/403 wiring has its own DB-gated API-level
coverage (see test_review_workflow.py's existing pattern) which this
environment cannot run without TEST_DATABASE_URL.
"""

from __future__ import annotations

import pytest

import app.services.course_service as course_service_module
import app.services.quality_framework as qf_module
from app.core.errors import (
    ConflictError,
    IncompleteChapterInventoryError,
    QualityApprovalBlockedError,
    QualityEvaluationFailedError,
    ValidationFailedError,
)
from app.schemas.course import GenerateRequest


async def _submitted_level_3_course(service, technical_input):
    """A real, reliable confirmed-blocker fixture: the mock technical writer
    never adds a case_study/story block (see test_content_level.py's own
    equivalent), so this reliably produces an independently_verified
    structural blocker, not a hand-built one."""
    level_3_input = technical_input.model_copy(update={"content_level": 3})
    record = await service.create_course(level_3_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)
    return record


# --- override_approved_blockers ---------------------------------------------


async def test_normal_approval_is_blocked_by_a_confirmed_blocker(service, technical_input):
    record = await _submitted_level_3_course(service, technical_input)
    with pytest.raises(QualityApprovalBlockedError) as excinfo:
        await service.approve_course(record.course_id, reviewer_id="reviewer-1")
    findings = excinfo.value.details["findings"]
    assert findings
    assert all(f["verification_status"] == "independently_verified" for f in findings)


async def test_override_approved_blockers_succeeds_and_records_evidence(service, technical_input):
    record = await _submitted_level_3_course(service, technical_input)
    approved = await service.approve_course_override_blockers(
        record.course_id, "admin-1", "Accepting known level-3 gap for this release."
    )
    assert approved.review_status == "approved"
    entry = approved.review_history[-1]
    assert entry.action == "override_approved_blockers"
    assert entry.by == "admin-1"
    assert entry.override_evidence.findings
    assert entry.override_evidence.standards_version


async def test_override_approved_blockers_rejects_an_empty_reason(service, technical_input):
    record = await _submitted_level_3_course(service, technical_input)
    with pytest.raises(ValidationFailedError):
        await service.approve_course_override_blockers(record.course_id, "admin-1", "   ")


async def test_override_approved_blockers_still_requires_in_review(service, technical_input):
    record = await service.create_course(technical_input)  # still "draft"
    with pytest.raises(ConflictError):
        await service.approve_course_override_blockers(record.course_id, "admin-1", "reason")


async def test_wrong_override_for_confirmed_blockers_does_not_silently_succeed(
    service, technical_input
):
    """override_approved_incomplete_chapters must not approve past a
    confirmed-blocker problem it isn't scoped to."""
    record = await _submitted_level_3_course(service, technical_input)
    with pytest.raises(ValidationFailedError):
        # The inventory IS complete here - this override has nothing to do.
        await service.approve_course_override_incomplete_chapters(
            record.course_id, "admin-1", "reason"
        )
    stored = await service.get_course_record(record.course_id)
    assert stored.review_status == "in_review"  # never silently approved


# --- override_approved_verification_failed ----------------------------------


async def test_normal_approval_is_blocked_by_a_verification_failure(
    service, technical_input, monkeypatch
):
    def _boom(document):
        raise RuntimeError("document mid-rebuild")

    # document_findings() now calls validate_document_structured() directly
    # (Phase 3.3/3.4: structured layout findings) - patch that real call
    # site, not the legacy validate_document() wrapper it no longer invokes.
    monkeypatch.setattr(qf_module, "validate_document_structured", _boom)
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    with pytest.raises(QualityApprovalBlockedError) as excinfo:
        await service.approve_course(record.course_id, reviewer_id="reviewer-1")
    findings = excinfo.value.details["findings"]
    assert all(f["verification_status"] == "verification_failed" for f in findings)


async def test_override_approved_verification_failed_succeeds_with_evidence(
    service, technical_input, monkeypatch
):
    def _boom(document):
        raise RuntimeError("document mid-rebuild")

    monkeypatch.setattr(qf_module, "validate_document_structured", _boom)
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    approved = await service.approve_course_override_verification_failed(
        record.course_id, "admin-1", "Layout verifier is flaky; content was reviewed manually."
    )
    assert approved.review_status == "approved"
    entry = approved.review_history[-1]
    assert entry.action == "override_approved_verification_failed"
    assert entry.override_evidence.verifier == "layout_validator"
    assert "document mid-rebuild" in (entry.override_evidence.verification_error or "")
    # Wording discipline: never claims a confirmed defect.
    assert "defect" not in approved.review_comment.lower()


async def test_a_fresh_recomputation_after_the_verifier_recovers_approves_normally(
    service, technical_input, monkeypatch
):
    """The verifier failing once must not permanently block the course -
    once it succeeds again (a fresh approve_course call), normal approval
    works with no override needed. The flakiness is injected only AROUND
    the approve_course calls (generation itself runs unpatched), so this
    isolates the verifier's own recovery rather than an unrelated earlier
    call to it during generation's own advisory gate."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    calls = {"n": 0}
    original = qf_module.validate_document_structured

    def _flaky(document):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return original(document)

    # document_findings() now calls validate_document_structured() directly
    # (Phase 3.3/3.4: structured layout findings) - patch that real call
    # site, not the legacy validate_document() wrapper it no longer invokes.
    monkeypatch.setattr(qf_module, "validate_document_structured", _flaky)

    with pytest.raises(QualityApprovalBlockedError):
        await service.approve_course(record.course_id, reviewer_id="reviewer-1")

    approved = await service.approve_course(record.course_id, reviewer_id="reviewer-1")
    assert approved.review_status == "approved"
    assert approved.review_history[-1].action == "approved"  # no override needed


# --- override_approved_evaluation_failure -----------------------------------


async def test_normal_approval_is_blocked_by_a_whole_report_evaluation_failure(
    service, technical_input, monkeypatch
):
    def _boom(**kwargs):
        raise RuntimeError("synthetic quality-evaluation failure")

    monkeypatch.setattr(course_service_module, "build_course_quality_report", _boom)
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    with pytest.raises(QualityEvaluationFailedError):
        await service.approve_course(record.course_id, reviewer_id="reviewer-1")


async def test_override_approved_evaluation_failure_succeeds_with_evidence(
    service, technical_input, monkeypatch
):
    def _boom(**kwargs):
        raise RuntimeError("synthetic quality-evaluation failure")

    monkeypatch.setattr(course_service_module, "build_course_quality_report", _boom)
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    approved = await service.approve_course_override_evaluation_failure(
        record.course_id, "admin-1", "Evaluated manually while the gate is broken."
    )
    assert approved.review_status == "approved"
    entry = approved.review_history[-1]
    assert entry.action == "override_approved_evaluation_failure"
    assert "synthetic quality-evaluation failure" in entry.override_evidence.evaluation_error


async def test_override_approved_evaluation_failure_rejects_a_clean_course(
    service, non_technical_input
):
    """This override is scoped to a crashed computation - calling it when
    evaluation actually succeeded must not silently approve."""
    record = await service.create_course(non_technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    with pytest.raises(ValidationFailedError):
        await service.approve_course_override_evaluation_failure(
            record.course_id, "admin-1", "reason"
        )


# --- override_approved_incomplete_chapters ----------------------------------


async def test_override_approved_incomplete_chapters_succeeds_with_evidence(
    service, non_technical_input
):
    record = await service.create_course(non_technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    stored = await service.get_course_record(record.course_id)
    target = stored.chapters[-1].chapter_id
    service.storage.chapter_path(record.course_id, target).unlink()

    approved = await service.approve_course_override_incomplete_chapters(
        record.course_id, "admin-1", "Chapter file lost after submission; re-generating separately."
    )
    assert approved.review_status == "approved"
    entry = approved.review_history[-1]
    assert entry.action == "override_approved_incomplete_chapters"
    affected = entry.override_evidence.affected_chapters
    assert affected and affected[0]["chapter_id"] == target
    assert affected[0]["status"] == "missing"
    # Wording discipline: unreadable/missing, never "confirmed defect".
    assert "defect" not in approved.review_comment.lower()


async def test_override_approved_incomplete_chapters_does_not_bypass_other_blockers(
    service, technical_input
):
    """The override lifts ONLY the inventory gate - if the remaining,
    readable content also has a confirmed blocker, that still blocks."""
    record = await _submitted_level_3_course(service, technical_input)
    stored = await service.get_course_record(record.course_id)
    target = stored.chapters[-1].chapter_id
    service.storage.chapter_path(record.course_id, target).unlink()

    with pytest.raises(QualityApprovalBlockedError):
        await service.approve_course_override_incomplete_chapters(
            record.course_id, "admin-1", "reason"
        )
    stored_after = await service.get_course_record(record.course_id)
    assert stored_after.review_status == "in_review"  # never silently approved


async def test_override_approved_incomplete_chapters_rejects_an_already_complete_course(
    service, non_technical_input
):
    record = await service.create_course(non_technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    with pytest.raises(ValidationFailedError):
        await service.approve_course_override_incomplete_chapters(
            record.course_id, "admin-1", "reason"
        )


# --- cross-cutting: the four actions stay genuinely distinct ----------------


async def test_the_four_override_actions_are_genuinely_distinct_values():
    actions = {
        "override_approved_blockers",
        "override_approved_verification_failed",
        "override_approved_evaluation_failure",
        "override_approved_incomplete_chapters",
    }
    assert len(actions) == 4  # sanity: no accidental collision in this test file's own usage
