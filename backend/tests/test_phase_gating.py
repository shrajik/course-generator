"""Content Studio Phase 3: ADDIE phase-gated approval (analysis_design,
development, implementation_evaluation) - see CoursePhases' own docstring
in app/schemas/course.py for why there are 3 real phases, not 5.

All offline/mock - no live AI calls (MOCK_OPENAI=true, USE_DATABASE=false,
see conftest.py), exercised through the real `service`/`technical_input`
fixtures already used throughout this suite.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError
from app.schemas.course import GenerateRequest
from app.schemas.design import DesignModels


def _addie_input(base):
    return base.model_copy(update={"design_models": DesignModels(models=["addie"])})


def _addie_sam_input(base):
    return base.model_copy(update={"design_models": DesignModels(models=["addie", "sam"], sam_loop="SAM1")})


# --- phases only activates for ADDIE-without-SAM -----------------------------


async def test_phases_is_none_without_any_design_model(service, technical_input):
    record = await service.create_course(technical_input)
    assert record.phases is None


async def test_phases_is_none_for_sam_alone(service, technical_input):
    sam_input = technical_input.model_copy(
        update={"design_models": DesignModels(models=["sam"], sam_loop="SAM1")}
    )
    record = await service.create_course(sam_input)
    assert record.phases is None


async def test_phases_is_none_for_addie_plus_sam_together(service, technical_input):
    """SAM already has its own, different iteration-gating mechanism - ADDIE
    phase-gating must never also activate alongside it."""
    record = await service.create_course(_addie_sam_input(technical_input))
    assert record.phases is None


async def test_phases_is_none_for_bloom_or_kirkpatrick_alone(service, technical_input):
    bloom_input = technical_input.model_copy(update={"design_models": DesignModels(models=["bloom"])})
    record = await service.create_course(bloom_input)
    assert record.phases is None


async def test_phases_activates_for_addie_alone(service, technical_input):
    record = await service.create_course(_addie_input(technical_input), run_planner=False)
    assert record.phases is not None
    assert record.phases.analysis_design.status == "pending"
    assert record.phases.development.status == "pending"
    assert record.phases.implementation_evaluation.status == "pending"


# --- plan_course sets analysis_design to in_review ---------------------------


async def test_planning_sets_analysis_design_to_in_review(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))  # run_planner=True by default
    assert record.phases.analysis_design.status == "in_review"
    assert record.phases.development.status == "pending"


# --- approve_outline also approves analysis_design ---------------------------


async def test_approve_outline_also_approves_analysis_design(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))
    approved = await service.approve_outline(record.course_id, "author-1")
    assert approved.phases.analysis_design.status == "approved"
    assert approved.phases.analysis_design.approved_by == "author-1"
    assert approved.phases.analysis_design.approved_at is not None


async def test_approve_outline_is_a_no_op_for_phases_on_a_non_addie_course(service, technical_input):
    record = await service.create_course(technical_input)
    approved = await service.approve_outline(record.course_id, "author-1")
    assert approved.phases is None  # completely unaffected


# --- generation sets development to in_review only when truly complete -------


async def test_generation_sets_development_to_in_review_when_inventory_is_complete(
    service, technical_input
):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    stored = await service.get_course_record(record.course_id)
    assert stored.phases.development.status == "in_review"


async def test_a_partial_generation_does_not_advance_development(service, technical_input):
    """A real, confirmed risk this guards against: `status == 'ready'` alone
    does NOT mean every blueprint-planned chapter exists (targeted/partial
    generation) - the phase must only advance on the REAL inventory."""
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    blueprint = await service.get_blueprint(record.course_id)
    first_chapter_id = blueprint.chapters[0].id
    result = await service.generate(
        record.course_id, GenerateRequest(mode="sync", chapter_ids=[first_chapter_id])
    )
    assert result.status == "ready"  # the deceptive signal - status alone would suggest done
    stored = await service.get_course_record(record.course_id)
    assert stored.phases.development.status == "pending"  # never advanced


# --- approve_phase enforces order ---------------------------------------------


async def test_approve_phase_rejects_analysis_design_directing_to_approve_outline(
    service, technical_input
):
    record = await service.create_course(_addie_input(technical_input))
    with pytest.raises(ConflictError, match="approve_outline"):
        await service.approve_phase(record.course_id, "analysis_design", "author-1")


async def test_approve_phase_rejects_development_before_analysis_design(service, technical_input):
    record = await service.create_course(_addie_input(technical_input), run_planner=False)
    with pytest.raises(ConflictError, match="Analysis & Design must be approved"):
        await service.approve_phase(record.course_id, "development", "author-1")


async def test_approve_phase_rejects_development_before_generation_finishes(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    # Not generated yet - development is still "pending", not "in_review".
    with pytest.raises(ConflictError, match="has not finished generating"):
        await service.approve_phase(record.course_id, "development", "author-1")


async def test_approve_phase_rejects_implementation_evaluation_before_development(
    service, technical_input
):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    with pytest.raises(ConflictError, match="Development must be approved"):
        await service.approve_phase(record.course_id, "implementation_evaluation", "author-1")


async def test_approve_phase_fails_clearly_when_phases_is_not_active(service, technical_input):
    record = await service.create_course(technical_input)
    with pytest.raises(ConflictError, match="does not have phase-gated approval active"):
        await service.approve_phase(record.course_id, "development", "author-1")


# --- approve_phase("development") cascades implementation_evaluation ---------


async def test_approving_development_cascades_implementation_evaluation_to_in_review(
    service, technical_input
):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    approved = await service.approve_phase(record.course_id, "development", "author-1")
    assert approved.phases.development.status == "approved"
    assert approved.phases.implementation_evaluation.status == "in_review"


async def test_approving_an_already_approved_phase_is_idempotent(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    first = await service.approve_phase(record.course_id, "development", "author-1")
    second = await service.approve_phase(record.course_id, "development", "author-2")
    assert second.phases.development.approved_by == first.phases.development.approved_by  # unchanged


async def test_implementation_evaluation_can_be_approved_once_in_review(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.approve_phase(record.course_id, "development", "author-1")
    approved = await service.approve_phase(record.course_id, "implementation_evaluation", "author-1")
    assert approved.phases.implementation_evaluation.status == "approved"


# --- submit_for_review requires every phase approved, when phases active ----


async def test_submit_for_review_is_blocked_until_implementation_evaluation_is_approved(
    service, technical_input
):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.approve_phase(record.course_id, "development", "author-1")
    with pytest.raises(ConflictError, match="Implementation & Evaluation has not been approved"):
        await service.submit_for_review(record.course_id, actor_id=None)


async def test_submit_for_review_succeeds_once_every_phase_is_approved(service, technical_input):
    record = await service.create_course(_addie_input(technical_input))
    await service.approve_outline(record.course_id, "author-1")
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    await service.approve_phase(record.course_id, "development", "author-1")
    await service.approve_phase(record.course_id, "implementation_evaluation", "author-1")
    submitted = await service.submit_for_review(record.course_id, actor_id=None)
    assert submitted.review_status == "in_review"


async def test_submit_for_review_is_unaffected_when_phases_is_none(service, technical_input):
    """A non-ADDIE course's existing, already-tested submit_for_review
    behaviour must be completely untouched by this feature."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    submitted = await service.submit_for_review(record.course_id, actor_id=None)
    assert submitted.review_status == "in_review"


# --- API endpoint round-trip --------------------------------------------------


async def test_the_full_phase_workflow_through_the_api(client):
    """submit-for-review requires a logged-in user even in no-DB test mode
    (see get_current_user, unlike every other endpoint here) - that part of
    the workflow is already covered at the service level above
    (test_submit_for_review_is_blocked_until_implementation_evaluation_is_approved
    / test_submit_for_review_succeeds_once_every_phase_is_approved); this
    test covers the phase-approval endpoints themselves end to end."""
    created = client.post("/api/courses", json={
        "course_title": "Incident Reporting", "toc": [{"title": "Spot an incident"}, {"title": "Report it"}],
        "target_audience": "Support agents", "template": "technical", "run_planner": True,
        "design_models": {"models": ["addie"]},
    }).json()
    course_id = created["course_id"]
    assert created["phases"]["analysis_design"]["status"] == "in_review"

    approved_outline = client.post(f"/api/courses/{course_id}/approve-outline").json()
    assert approved_outline["phases"]["analysis_design"]["status"] == "approved"

    # Can't approve development before it's generated.
    too_early = client.post(f"/api/courses/{course_id}/phases/development/approve")
    assert too_early.status_code == 409

    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    approved_dev = client.post(f"/api/courses/{course_id}/phases/development/approve").json()
    assert approved_dev["phases"]["development"]["status"] == "approved"
    assert approved_dev["phases"]["implementation_evaluation"]["status"] == "in_review"

    approved_ie = client.post(f"/api/courses/{course_id}/phases/implementation_evaluation/approve").json()
    assert approved_ie["phases"]["implementation_evaluation"]["status"] == "approved"
