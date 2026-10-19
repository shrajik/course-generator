"""The end-of-course compliance report: QA checklist, accessibility, metadata."""

from __future__ import annotations

from app.course.compliance import build_report, contrast_ratio
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page


def _block(block_type: BlockType, **content) -> Block:
    return Block(type=block_type, content=content)


def _doc(blocks: list[Block], theme: dict | None = None) -> CourseDocument:
    doc = CourseDocument(
        document_id="doc_x", course_id="crs_x", course_title="Intro", template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    if theme is not None:
        doc.meta.theme = theme
    return doc


def _good_chapter() -> list[Block]:
    return [
        _block(BlockType.HEADING, text="Chapter One", level=1),
        _block(BlockType.LEARNING_OBJECTIVES, items=["Describe the pipeline."]),
        _block(BlockType.PARAGRAPH, text=" ".join(["word"] * 200) + " For help, see the FAQ."),
        _block(BlockType.QUIZ, questions=[{"question": "Q?"}]),
    ]


def _status(report, index: int) -> str:
    return report.qa_checks[index - 1].status


def test_there_are_always_eight_checks_in_order():
    report = build_report(_doc(_good_chapter()))
    assert [c.id for c in report.qa_checks] == [f"qa_{i}" for i in range(1, 9)]


def test_a_complete_chapter_passes_the_content_checks():
    report = build_report(_doc(_good_chapter()))
    assert [_status(report, i) for i in (1, 2, 4, 5, 7)] == ["pass"] * 5


def test_assessment_and_technology_are_marked_for_human_review_not_passed():
    report = build_report(_doc(_good_chapter()))
    assert _status(report, 3) == "review"  # mapping to objectives needs a person
    assert _status(report, 6) == "review"  # no packaging standard exists


def test_missing_objectives_fail_the_objectives_check():
    blocks = [b for b in _good_chapter() if b.type is not BlockType.LEARNING_OBJECTIVES]
    assert _status(build_report(_doc(blocks)), 2) == "fail"


def test_an_unmeasurable_verb_in_an_objective_fails():
    blocks = _good_chapter()
    blocks[1] = _block(BlockType.LEARNING_OBJECTIVES, items=["Understand the pipeline."])
    report = build_report(_doc(blocks))
    assert _status(report, 2) == "fail" and "understand" in report.qa_checks[1].note.lower()


def test_objectives_after_practice_fail():
    blocks = _good_chapter()
    blocks[1], blocks[3] = blocks[3], blocks[1]
    assert _status(build_report(_doc(blocks)), 2) == "fail"


def test_a_chapter_without_assessment_or_practice_fails():
    blocks = [b for b in _good_chapter() if b.type is not BlockType.QUIZ]
    report = build_report(_doc(blocks))
    assert _status(report, 3) == "fail" and _status(report, 5) == "fail"


def test_no_learner_support_is_flagged_for_client_input():
    blocks = _good_chapter()
    blocks[2] = _block(BlockType.PARAGRAPH, text=" ".join(["word"] * 200))
    report = build_report(_doc(blocks))
    assert _status(report, 7) == "fail" and "client input" in report.qa_checks[6].note


def test_a_thin_chapter_fails_instructional_materials():
    blocks = _good_chapter()
    blocks[2] = _block(BlockType.PARAGRAPH, text="Too short. See the FAQ.")
    assert _status(build_report(_doc(blocks)), 4) == "fail"


def test_an_image_without_alt_text_is_an_accessibility_failure():
    blocks = _good_chapter() + [_block(BlockType.IMAGE, kind="illustration", alt="")]
    report = build_report(_doc(blocks))
    assert any(f.area == "Images" and f.severity == "fail" for f in report.accessibility_flags)
    assert _status(report, 8) == "fail"
    assert report.asset_requirements and report.asset_requirements[0].present is False


def test_a_decorative_image_needs_no_alt_text():
    blocks = _good_chapter() + [_block(BlockType.IMAGE, kind="illustration", alt="", decorative=True)]
    report = build_report(_doc(blocks))
    assert not any(f.area == "Images" for f in report.accessibility_flags)


def test_a_skipped_heading_level_is_flagged():
    blocks = _good_chapter() + [_block(BlockType.HEADING, text="Deep", level=4)]
    assert any(f.area == "Structure" for f in build_report(_doc(blocks)).accessibility_flags)


def test_low_contrast_text_is_flagged():
    report = build_report(_doc(_good_chapter(), theme={"text_color": "#cccccc", "page_background": "#ffffff"}))
    assert any(f.area == "Color/contrast" for f in report.accessibility_flags)


def test_contrast_ratio_matches_the_wcag_formula():
    assert round(contrast_ratio("#000000", "#ffffff"), 1) == 21.0
    assert contrast_ratio("not-a-colour", "#ffffff") is None


def test_pdf_ua_is_always_left_for_a_human():
    flags = build_report(_doc(_good_chapter())).accessibility_flags
    assert any(f.area == "PDF" and f.severity == "review" for f in flags)


def test_lom_metadata_carries_the_required_fields():
    meta = build_report(_doc(_good_chapter())).metadata
    for key in ("Title", "Description", "Keywords", "Learning resource type", "Difficulty/level",
                "Typical learning time", "Technical requirements/format", "Rights/licensing",
                "Language", "Version number", "Last generated", "Owner/requester", "Source standard"):
        assert key in meta, key


def test_dublin_core_metadata_has_its_own_fields():
    meta = build_report(_doc(_good_chapter()), schema="dublin_core").metadata
    for key in ("Title", "Creator", "Subject", "Publisher", "Identifier", "Rights", "Contributor"):
        assert key in meta, key
    assert "Keywords" not in meta


def test_the_report_is_deterministic_for_the_same_input():
    a = build_report(_doc(_good_chapter()), now="2026-01-01T00:00:00")
    b = build_report(_doc(_good_chapter()), now="2026-01-01T00:00:00")
    assert a.to_dict() == b.to_dict()


def test_unresolved_flags_list_everything_that_did_not_pass():
    report = build_report(_doc(_good_chapter()))
    assert len(report.unresolved_flags) == sum(1 for c in report.qa_checks if c.status != "pass")


def test_an_empty_course_fails_every_check_instead_of_crashing():
    report = build_report(_doc([]))
    assert all(c.status == "fail" for c in report.qa_checks)


def test_the_endpoint_returns_a_report_for_a_generated_course(client):
    created = client.post("/api/courses", json={
        "course_title": "Intro to RAG", "toc": [{"title": "What is RAG"}],
        "target_audience": "Engineers", "template": "technical", "run_planner": False,
    }).json()
    client.post(f"/api/courses/{created['course_id']}/generate", json={"mode": "sync"})
    body = client.get(f"/api/documents/{created['document_id']}/compliance").json()
    assert len(body["qa_checks"]) == 8 and body["summary"]
    assert "Title" in body["metadata"]
    dc = client.get(f"/api/documents/{created['document_id']}/compliance?schema=dublin_core").json()
    assert "Creator" in dc["metadata"]
    assert client.get(f"/api/documents/{created['document_id']}/compliance?schema=bogus").status_code == 422
