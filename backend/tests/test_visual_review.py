"""VisualReviewService.review_document's `_qa_job_id` skip - pipeline
reliability follow-up (reducing redundant AI calls, requirement 5).

An image `ImageService.generate_missing` just generated and fully validated
(relevance/text checks already passed) in THIS SAME run gets tagged
`content["_qa_job_id"] = job_id` (see ImageService.generate_missing). Passing
that same `job_id` into `review_document` must skip re-spending a vision
call re-checking it - this file proves the skip actually fires with zero AI
calls, and that anything without a matching marker still gets the real check.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.schemas.visual_review import ImageAssessment
from app.services.mock_ai import MockAIClient
from app.services.storage_service import StorageService
from app.services.visual_review import VisualReviewService, core_failures, image_verdict


class _ExplodingAI(MockAIClient):
    async def structured(self, **kwargs):  # noqa: ANN003
        raise AssertionError("A freshly-validated image must not be re-checked with another AI call")


def _image_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": "illustration", **content})


def _document(course_id: str, block: Block) -> CourseDocument:
    return CourseDocument(
        document_id="d", course_id=course_id, course_title="Biology",
        template_id="technical", pages=[Page(blocks=[block])],
    )


async def test_a_freshly_validated_image_is_skipped_with_zero_ai_calls(tmp_path):
    settings = get_settings()
    storage = StorageService(settings)
    block = _image_block(path="assets/x.png", generated=True, _qa_job_id="job-1")
    document = _document("course_skip_fresh", block)
    template = load_template("technical")

    svc = VisualReviewService(ai=_ExplodingAI(settings), settings=settings, storage=storage)

    report = await svc.review_document(document, template, fix=True, job_id="job-1")

    assert len(report.checks) == 1
    assert report.checks[0].status == "skipped"
    assert "Already validated" in (report.checks[0].note or "")


async def test_a_different_or_missing_job_id_still_gets_checked(tmp_path):
    """Not skipped: the block either has no marker (an image reused from an
    earlier run) or a marker from a DIFFERENT job - either way it must still
    go through `_check`, not be silently waved through."""
    settings = get_settings()
    storage = StorageService(settings)
    no_marker = _image_block(path="assets/missing.png", generated=True)
    document = _document("course_no_marker", no_marker)
    template = load_template("technical")

    svc = VisualReviewService(ai=MockAIClient(settings), settings=settings, storage=storage)
    report = await svc.review_document(document, template, fix=True, job_id="job-1")

    # `_check` ran (the file doesn't exist on disk) rather than being skipped
    # outright - a different status/note than the skip path above proves it
    # actually reached the real check instead of short-circuiting.
    assert report.checks[0].status == "unchecked"
    assert any("missing" in p.lower() for p in report.checks[0].problems)

    stale_marker = _image_block(path="assets/missing.png", generated=True, _qa_job_id="job-0")
    document2 = _document("course_stale_marker", stale_marker)
    report2 = await svc.review_document(document2, template, fix=True, job_id="job-1")
    assert report2.checks[0].status == "unchecked"


# ---------------------------------------------------------------------------
# style_compliant: the only automated check on whether a textbook-style
# illustration actually followed its flat-2D-vector style contract, since
# MAI-Image-2.6 has no negative-prompt/style parameter at the API level -
# the whole contract lives in prompt text with no model-level enforcement.
# ---------------------------------------------------------------------------


def _good_assessment(**overrides) -> ImageAssessment:
    return ImageAssessment(
        depicts="x", relevant_to_topic=True, matches_text=True, misleading=False,
        necessary=True, text_in_image_correct=None, labels_and_arrows_correct=None,
        **overrides,
    )


def test_image_verdict_ignores_style_compliant_when_not_a_textbook_style_picture():
    """A plain (non-textbook) illustration never needed this field answered -
    `textbook_style=False` (the default) must neither require nor fail on it,
    even if a model answered it anyway."""
    assessment = _good_assessment(style_compliant=False)
    status, _problems, _regen = image_verdict(assessment, decorative_opener=False, meant_as_diagram=False)
    assert status == "passed"


def test_image_verdict_fails_a_textbook_picture_that_is_not_style_compliant():
    assessment = _good_assessment(style_compliant=False)
    status, problems, regenerate = image_verdict(
        assessment, decorative_opener=False, meant_as_diagram=False, textbook_style=True,
    )
    assert status == "failed"
    assert regenerate is True
    assert any("style" in p.lower() for p in problems)


def test_image_verdict_is_unchecked_when_style_compliant_was_never_answered():
    assessment = _good_assessment(style_compliant=None)
    status, _problems, _regen = image_verdict(
        assessment, decorative_opener=False, meant_as_diagram=False, textbook_style=True,
    )
    assert status == "unchecked"


def test_image_verdict_passes_a_textbook_picture_that_is_style_compliant():
    assessment = _good_assessment(style_compliant=True)
    status, _problems, _regen = image_verdict(
        assessment, decorative_opener=False, meant_as_diagram=False, textbook_style=True,
    )
    assert status == "passed"


def test_core_failures_counts_a_style_violation_as_one_point():
    from app.schemas.visual_review import VisualCheck

    check = VisualCheck(block_id="b", kind="illustration")
    with_style_fail = core_failures(_good_assessment(style_compliant=False), check)
    without = core_failures(_good_assessment(style_compliant=True), check)
    assert with_style_fail == without + 1
