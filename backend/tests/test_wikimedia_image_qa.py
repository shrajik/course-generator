"""Vision-model quality gate for a downloaded Wikimedia Commons candidate -
app.services.wikimedia_image_qa. Mirrors the existing
test_verify_image_relevance_* tests in test_image_service.py: a stub
`ai.structured` controls the response, nothing hits a real model."""

from __future__ import annotations

from app.services.openai_service import get_ai_client
from app.services.wikimedia_image_qa import (
    WikimediaCandidateCheck,
    candidate_passed,
    evaluate_candidate,
)


async def test_blank_brief_is_a_no_op_pass():
    """Nothing to grade a candidate against - same "can't check, don't
    pretend to" contract diagram_content_qa.evaluate_content uses for a
    blank brief."""
    check = await evaluate_candidate(b"png-bytes", brief="", course_title="Biology")
    assert check == WikimediaCandidateCheck()
    assert candidate_passed(check)


async def test_a_vision_call_failure_is_marked_errored_never_a_silent_pass(monkeypatch):
    ai = get_ai_client()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(ai, "structured", fake_structured)
    check = await evaluate_candidate(
        b"png-bytes", brief="Explain mitosis", course_title="Biology", ai=ai
    )
    assert check.errored is True
    # Every judgment field still defaults to True (never a confirmed defect
    # the candidate never earned) - but `candidate_passed` must still treat
    # this as a fail, since nothing was actually verified.
    assert check.on_topic is True
    assert candidate_passed(check) is False


async def test_candidate_passed_requires_every_judgment_to_be_true():
    base = WikimediaCandidateCheck()
    assert candidate_passed(base) is True
    assert candidate_passed(base.model_copy(update={"on_topic": False})) is False
    assert candidate_passed(base.model_copy(update={"textbook_style": False})) is False
    assert candidate_passed(base.model_copy(update={"english_labels_only": False})) is False
    assert candidate_passed(base.model_copy(update={"scientifically_accurate": False})) is False


async def test_evaluate_candidate_passes_the_image_bytes_to_the_vision_call(monkeypatch):
    ai = get_ai_client()
    seen = {}

    async def fake_structured(*, schema, system, user, model, purpose, phase, image=None, **kwargs):
        seen["image"] = image
        seen["purpose"] = purpose
        return schema()

    monkeypatch.setattr(ai, "structured", fake_structured)
    await evaluate_candidate(b"the-image-bytes", brief="Explain mitosis", course_title="Biology", ai=ai)
    assert seen["image"] == b"the-image-bytes"
    assert seen["purpose"] == "wikimedia_candidate_check"
