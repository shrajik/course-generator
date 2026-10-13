"""MOM quality-governance follow-up - fail-closed chapter inventory.

`StorageService.load_all_chapters` deliberately stays silent-skip (unchanged
for continuity builders, document assembly, the course-detail API - every
one of those callers keeps today's behaviour). `verify_chapter_inventory`
is the new, separate, fail-closed check wired into `submit_for_review` and
`approve_course` instead.

All offline/mock - no live AI calls (USE_DATABASE=false, see conftest.py),
exercised through the real `service`/`technical_input` fixtures already
used throughout this suite.
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import IncompleteChapterInventoryError
from app.schemas.course import GenerateRequest


# --- StorageService.verify_chapter_inventory: pure storage-layer checks -----


async def test_all_expected_chapters_present_and_loadable_has_no_issues(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    expected_ids = [c.chapter_id for c in (await service.get_course_record(record.course_id)).chapters]
    issues = service.storage.verify_chapter_inventory(record.course_id, expected_ids)
    assert issues == []


async def test_a_missing_expected_chapter_is_reported_as_missing(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    stored = await service.get_course_record(record.course_id)
    expected_ids = [c.chapter_id for c in stored.chapters]
    assert len(expected_ids) >= 2

    target = expected_ids[-1]
    path = service.storage.chapter_path(record.course_id, target)
    path.unlink()

    issues = service.storage.verify_chapter_inventory(record.course_id, expected_ids)
    assert len(issues) == 1
    assert issues[0].chapter_id == target
    assert issues[0].status == "missing"
    assert issues[0].detail == ""  # nothing to parse - never invented


async def test_a_corrupt_expected_chapter_is_reported_with_the_real_parse_detail(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    stored = await service.get_course_record(record.course_id)
    expected_ids = [c.chapter_id for c in stored.chapters]

    target = expected_ids[0]
    path = service.storage.chapter_path(record.course_id, target)
    path.write_text("{not valid json", encoding="utf-8")

    issues = service.storage.verify_chapter_inventory(record.course_id, expected_ids)
    assert len(issues) == 1
    assert issues[0].chapter_id == target
    assert issues[0].status == "corrupt"
    assert issues[0].detail  # the real exception message, not fabricated


async def test_non_chapter_json_files_on_disk_do_not_cause_false_issues(service, technical_input):
    """verify_chapter_inventory checks the EXPECTED set, never scans the
    directory - an extra/unrelated file on disk is simply irrelevant."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    stored = await service.get_course_record(record.course_id)
    expected_ids = [c.chapter_id for c in stored.chapters]

    stray = service.storage.chapters_dir(record.course_id) / "chapter_99.json"
    stray.write_text("{not valid json", encoding="utf-8")

    issues = service.storage.verify_chapter_inventory(record.course_id, expected_ids)
    assert issues == []


# --- Decision B: submit_for_review must not trust record.status alone -------


async def test_a_fully_generated_course_submits_for_review_normally(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    submitted = await service.submit_for_review(record.course_id, actor_id=None)
    assert submitted.review_status == "in_review"


async def test_a_partially_generated_ready_course_is_rejected_by_submit_for_review(
    service, technical_input
):
    """The exact ambiguity Decision B resolves: targeted/partial generation
    can mark a course "ready" while other blueprint-planned chapters were
    never attempted. `record.status` alone cannot catch this - the real
    chapter inventory can."""
    record = await service.create_course(technical_input)
    blueprint = await service.get_blueprint(record.course_id)
    first_chapter_id = blueprint.chapters[0].id

    result = await service.generate(
        record.course_id, GenerateRequest(mode="sync", chapter_ids=[first_chapter_id])
    )
    assert result.status == "ready"  # the deceptive signal this test proves is insufficient

    with pytest.raises(IncompleteChapterInventoryError) as excinfo:
        await service.submit_for_review(record.course_id, actor_id=None)
    assert excinfo.value.details["issues"]
    assert all(i["status"] == "missing" for i in excinfo.value.details["issues"])


async def test_a_course_with_a_corrupted_chapter_cannot_be_submitted(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    stored = await service.get_course_record(record.course_id)
    target = stored.chapters[0].chapter_id
    service.storage.chapter_path(record.course_id, target).write_text("{bad", encoding="utf-8")

    with pytest.raises(IncompleteChapterInventoryError):
        await service.submit_for_review(record.course_id, actor_id=None)


# --- approve_course re-verifies independently, never trusting status alone -


async def test_approve_course_rejects_an_incomplete_inventory_even_if_in_review(
    service, technical_input
):
    """Simulates the realistic post-submission-corruption scenario: the
    course WAS complete at submission time, then a chapter file is lost or
    corrupted afterward - approve_course must catch this independently,
    never relying on submit_for_review having already checked it once."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)

    stored = await service.get_course_record(record.course_id)
    target = stored.chapters[-1].chapter_id
    service.storage.chapter_path(record.course_id, target).unlink()

    with pytest.raises(IncompleteChapterInventoryError) as excinfo:
        await service.approve_course(record.course_id, reviewer_id="reviewer-1")
    assert excinfo.value.details["issues"][0]["chapter_id"] == target

    # Never silently approved - review_status must still be in_review.
    stored_after = await service.get_course_record(record.course_id)
    assert stored_after.review_status == "in_review"


async def test_a_complete_course_approves_normally_through_the_full_lifecycle(
    service, non_technical_input
):
    """Full happy-path regression guard: generate -> submit -> approve must
    keep working exactly as before these gates existed."""
    record = await service.create_course(non_technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.submit_for_review(record.course_id, actor_id=None)
    approved = await service.approve_course(record.course_id, reviewer_id="reviewer-1", comment="LGTM")
    assert approved.review_status == "approved"
    assert approved.review_history[-1].action == "approved"
