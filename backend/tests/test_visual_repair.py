"""Post-pagination visual audit and repair - see
app.course.document.visual_repair. Covers the pure decision logic directly
(page_needs_visual / insertion position) and the end-to-end repair flow
through DocumentService.repair_visual_coverage (mocked AI, real pagination).
"""

from __future__ import annotations

from app.course.document.visual_repair import (
    MIN_WORDS_TO_ILLUSTRATE,
    _insertion_index,
    _page_text,
    page_needs_visual,
)
from app.schemas.blocks import BlockType
from app.schemas.document import (
    Block,
    BlockMeta,
    CourseDocument,
    DocumentMeta,
    Page,
)


def _paragraph(words: int, *, text: str | None = None) -> Block:
    body = text or " ".join(["placeholder"] * words)
    return Block(type=BlockType.PARAGRAPH, content={"text": body})


def _heading(text: str) -> Block:
    return Block(type=BlockType.HEADING, content={"text": text, "level": 1})


def _image(*, kind: str = "illustration", width: int = 800, height: int = 300) -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": kind, "path": "assets/x", "width": width, "height": height})


def _quiz() -> Block:
    return Block(type=BlockType.QUIZ, content={"questions": [{"question": "q?", "answer": "a"}]})


# ---------------------------------------------------------------------------
# page_needs_visual - the 8 cases the brief explicitly asks to cover
# ---------------------------------------------------------------------------


def test_a_text_only_page_needs_a_visual():
    page = [_heading("Topic"), _paragraph(120), _paragraph(80)]
    assert page_needs_visual(page)


def test_a_page_with_a_tiny_token_visual_still_needs_more():
    """A page whose only visual is small relative to its text still reads
    as visually thin - page_visual_fraction, not just "has an image"."""
    page = [_heading("Topic"), _paragraph(200), _paragraph(200), _image(height=40)]
    assert page_needs_visual(page)


def test_a_page_with_a_substantial_visual_does_not_need_one():
    page = [_heading("Topic"), _paragraph(80), _image(height=400)]
    assert not page_needs_visual(page)


def test_an_empty_page_does_not_need_a_visual():
    assert not page_needs_visual([])


def test_a_short_page_below_the_word_floor_does_not_need_a_visual():
    """Not enough real content to plan a genuinely relevant visual for -
    forcing one here would be exactly the "irrelevant filler image" the
    brief says never to insert."""
    words = MIN_WORDS_TO_ILLUSTRATE - 10
    page = [_heading("Topic"), _paragraph(words)]
    assert not page_needs_visual(page)


def test_a_pure_quiz_page_never_needs_a_visual_however_long():
    page = [_heading("Check your understanding"), _quiz(), _quiz(), _quiz()]
    assert not page_needs_visual(page)


def test_a_pure_summary_page_never_needs_a_visual():
    page = [
        Block(
            type=BlockType.SUMMARY,
            content={"key_takeaways": ["a" * 20] * 10, "next_steps": ["b" * 20] * 5},
        )
    ]
    assert not page_needs_visual(page)


def test_page_needs_visual_works_the_same_for_any_subject():
    """The decision is entirely about word count and visual fraction, never
    about topic/subject - the same function must fire identically for a
    technical page and a humanities page with the same shape."""
    technical = [_heading("Binary Search Trees"), _paragraph(150, text="binary tree node left right " * 30)]
    history = [_heading("The French Revolution"), _paragraph(150, text="revolution monarchy liberty " * 30)]
    assert page_needs_visual(technical)
    assert page_needs_visual(history)


# ---------------------------------------------------------------------------
# insertion position
# ---------------------------------------------------------------------------


def test_insertion_index_lands_right_after_the_last_textual_block():
    heading = _heading("Topic")
    p1 = _paragraph(50)
    p2 = _paragraph(50)
    quiz = _quiz()
    page = [heading, p1, p2, quiz]
    assert _insertion_index(page) == 3  # after p2, before the quiz


def test_insertion_index_falls_back_to_the_end_with_no_textual_block():
    page = [_heading("Topic"), _quiz()]
    assert _insertion_index(page) == 2


def test_page_text_only_includes_heading_and_textual_blocks():
    page = [_heading("My Heading"), _paragraph(5, text="body text here"), _quiz()]
    text = _page_text(page)
    assert "My Heading" in text
    assert "body text here" in text
    assert "questions" not in text.lower()


# ---------------------------------------------------------------------------
# end-to-end through DocumentService (mocked AI, real pagination/reflow)
# ---------------------------------------------------------------------------


async def _seeded_thin_document(service, documents, technical_input):
    """A page just over the word floor but with plenty of spare room -
    small enough that a generated visual fits alongside it on the SAME
    page, so a successful repair should leave nothing still deficient.
    (A page whose existing text already fills most of the page is a
    different scenario - it can't gain a full-size visual without
    overflowing to a new page, which is normal pagination, not a repair
    failure; that's not what this test is checking.)"""
    record = await service.create_course(technical_input, run_planner=False)
    blocks = [
        Block(id="heading", type=BlockType.HEADING, content={"text": "Iterators", "level": 1}),
        Block(
            id="para1", type=BlockType.PARAGRAPH,
            content={"text": " ".join(["iterator protocol next stopiteration"] * 16)},
            meta=BlockMeta(chapter_id="ch1", chapter_number=1),
        ),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    saved = await documents.save(record.document_id, document)
    return record, saved


async def test_repair_visual_coverage_inserts_a_visual_into_a_thin_page(service, documents, technical_input):
    record, document = await _seeded_thin_document(service, documents, technical_input)
    assert len(document.pages[0].blocks) == 2  # heading + paragraph, no visual yet

    response = await documents.repair_visual_coverage(record.document_id)

    assert len(response.repaired) == 1
    assert response.repaired[0]["page"] == 1
    assert response.still_deficient == []

    updated = await documents.load(record.document_id)
    image_blocks = [b for p in updated.pages for b in p.blocks if b.type is BlockType.IMAGE]
    assert len(image_blocks) == 1
    assert image_blocks[0].content.get("path")  # actually generated, not just planned
    assert image_blocks[0].meta.origin == "inserted"
    assert image_blocks[0].meta.chapter_id == "ch1"  # inherited from the page's own blocks


async def test_repair_visual_coverage_is_a_no_op_on_an_already_balanced_document(
    service, documents, technical_input
):
    record = await service.create_course(technical_input, run_planner=False)
    blocks = [
        Block(id="heading", type=BlockType.HEADING, content={"text": "Iterators", "level": 1}),
        Block(id="para1", type=BlockType.PARAGRAPH, content={"text": "short intro"}),
        Block(
            id="image1", type=BlockType.IMAGE,
            content={"kind": "illustration", "path": "assets/existing.png", "width": 800, "height": 500},
        ),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    await documents.save(record.document_id, document)

    response = await documents.repair_visual_coverage(record.document_id)

    assert response.repaired == []
    assert response.still_deficient == []
    updated = await documents.load(record.document_id)
    assert len(updated.pages[0].blocks) == 3  # untouched


async def test_repair_visual_coverage_reports_a_failed_generation_not_silently(
    service, documents, technical_input, monkeypatch
):
    record, document = await _seeded_thin_document(service, documents, technical_input)

    async def fake_generate_for_block(self, **kwargs):
        return False  # every generation attempt fails

    from app.services.image_service import ImageService

    monkeypatch.setattr(ImageService, "generate_for_block", fake_generate_for_block)

    response = await documents.repair_visual_coverage(record.document_id)

    assert response.repaired == []
    assert len(response.failed) >= 1
    assert response.failed[0]["page"] == 1
    assert len(response.still_deficient) == 1  # never silently marked as fixed


# ---------------------------------------------------------------------------
# Context propagation (chapter_title/section_title/key_concept) - a real,
# confirmed gap: this call site used to pass only course_title, so a repair-
# inserted visual got course-level context only, unlike every visual
# generated through the normal generate_missing() path. Fixed by reusing
# image_service.chapter_section_context - the same production mechanism,
# never a second implementation.
# ---------------------------------------------------------------------------


async def _seeded_thin_document_with_rich_context(service, documents, technical_input):
    """Same shape as `_seeded_thin_document`, plus a section-level heading
    and a `learning_objectives` block - enough structure for
    chapter_section_context to resolve all three fields to something
    non-empty, so a propagation test can actually prove something."""
    record = await service.create_course(technical_input, run_planner=False)
    blocks = [
        Block(id="heading", type=BlockType.HEADING, content={"text": "Newton's Laws of Motion", "level": 1}),
        Block(id="subheading", type=BlockType.HEADING, content={"text": "Newton's Second Law", "level": 2}),
        Block(
            id="objectives", type=BlockType.LEARNING_OBJECTIVES,
            content={"items": ["Explain how force relates to acceleration for a fixed mass"]},
        ),
        Block(
            id="para1", type=BlockType.PARAGRAPH,
            content={"text": " ".join(["force mass acceleration newton second law"] * 16)},
            meta=BlockMeta(chapter_id="ch1", chapter_number=1),
        ),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    saved = await documents.save(record.document_id, document)
    return record, saved


async def test_repair_visual_coverage_forwards_chapter_section_and_key_concept(
    service, documents, technical_input, monkeypatch
):
    record, document = await _seeded_thin_document_with_rich_context(service, documents, technical_input)

    captured: dict = {}

    async def fake_generate_for_block(self, **kwargs):
        captured.update(kwargs)
        block = kwargs["block"]
        block.content = merge_content(
            block.type, block.content, {"path": "assets/fake.png", "width": 800, "height": 300}
        )
        return True

    from app.schemas.blocks import merge_content
    from app.services.image_service import ImageService

    monkeypatch.setattr(ImageService, "generate_for_block", fake_generate_for_block)

    response = await documents.repair_visual_coverage(record.document_id)

    assert len(response.repaired) == 1  # the fix doesn't break the existing repair flow
    assert captured["chapter_title"] == "Newton's Laws of Motion"
    assert captured["section_title"] == "Newton's Second Law"
    assert captured["key_concept"] == "Explain how force relates to acceleration for a fixed mass"


async def test_repair_visual_coverage_uses_safe_empty_context_when_unresolvable(
    service, documents, technical_input, monkeypatch
):
    """No heading at all anywhere in the document - chapter_section_context
    has nothing to resolve. Must degrade to empty strings (the existing,
    already-safe default on generate_for_block), never raise."""
    record = await service.create_course(technical_input, run_planner=False)
    blocks = [
        Block(
            id="para1", type=BlockType.PARAGRAPH,
            content={"text": " ".join(["orphan paragraph with no heading anywhere"] * 16)},
        ),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    await documents.save(record.document_id, document)

    captured: dict = {}

    async def fake_generate_for_block(self, **kwargs):
        captured.update(kwargs)
        block = kwargs["block"]
        block.content = merge_content(
            block.type, block.content, {"path": "assets/fake.png", "width": 800, "height": 300}
        )
        return True

    from app.schemas.blocks import merge_content
    from app.services.image_service import ImageService

    monkeypatch.setattr(ImageService, "generate_for_block", fake_generate_for_block)

    response = await documents.repair_visual_coverage(record.document_id)

    assert len(response.repaired) == 1  # still succeeds - never crashes on unresolved context
    assert captured["chapter_title"] == ""
    assert captured["section_title"] == ""
    assert captured["key_concept"] == ""
