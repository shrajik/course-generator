"""The automatic PDF spacing/pagination QA gate - app.services.pdf_spacing_qa.
Pure, deterministic, no AI call: it only reads layout.y/height off an
already-flowed CourseDocument (plus, optionally, a real PDF's page count via
pypdf), the same ground truth app.course.document.layout computed."""

from __future__ import annotations

import pypdf

from app.course.document.layout import CONTENT_HEIGHT, PAGE_MARGIN_TOP
from app.schemas.blocks import BlockType
from app.schemas.document import Block, BlockLayout, BlockMeta, CourseDocument, DocumentMeta, Page
from app.services.pdf_spacing_qa import check_pdf_spacing


def _block(block_type: BlockType, y: float, height: float, *, chapter_id: str | None = None) -> Block:
    return Block(
        type=block_type,
        content={"text": "x"} if block_type is BlockType.PARAGRAPH else {},
        layout=BlockLayout(x=72, y=y, width=666, height=height),
        meta=BlockMeta(chapter_id=chapter_id),
    )


def _document(pages: list[Page]) -> CourseDocument:
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=pages,
    )


def _full_page(page_number: int, *, chapter_id: str) -> Page:
    # Fills the page almost completely - last block ends right at the bottom.
    bottom = PAGE_MARGIN_TOP + CONTENT_HEIGHT
    return Page(
        id=f"p{page_number}", page_number=page_number, kind="content",
        blocks=[_block(BlockType.PARAGRAPH, PAGE_MARGIN_TOP, bottom - PAGE_MARGIN_TOP - 5, chapter_id=chapter_id)],
    )


def test_a_tightly_packed_document_passes():
    document = _document([_full_page(1, chapter_id="ch1"), _full_page(2, chapter_id="ch1")])
    report = check_pdf_spacing(document)
    assert report.ok
    assert report.findings == []
    assert report.blank_pages == []


def test_a_page_with_a_large_trailing_gap_is_flagged():
    page1 = Page(
        id="p1", page_number=1, kind="content",
        blocks=[_block(BlockType.PARAGRAPH, PAGE_MARGIN_TOP, 100.0, chapter_id="ch1")],
    )
    page2 = _full_page(2, chapter_id="ch1")  # NOT the chapter's last page - page1 can't just be "the end"
    document = _document([page1, page2])
    report = check_pdf_spacing(document)
    assert not report.ok
    assert len(report.findings) == 1
    assert report.findings[0].page_number == 1
    assert report.findings[0].blank_pct > 50


def test_the_last_page_of_a_chapter_is_exempt_from_the_blank_check():
    page1 = _full_page(1, chapter_id="ch1")
    page2 = Page(
        id="p2", page_number=2, kind="content",
        blocks=[_block(BlockType.PARAGRAPH, PAGE_MARGIN_TOP, 100.0, chapter_id="ch1")],
    )  # trails off with a big gap, but it's the chapter's own last page - expected, not a bug
    document = _document([page1, page2])
    report = check_pdf_spacing(document)
    assert report.ok


def test_a_fully_blank_page_is_flagged():
    document = _document([
        _full_page(1, chapter_id="ch1"),
        Page(id="p2", page_number=2, kind="content", blocks=[]),
    ])
    report = check_pdf_spacing(document)
    assert not report.ok
    assert report.blank_pages == [2]


def test_non_content_pages_are_never_checked():
    cover = Page(id="p0", page_number=1, kind="cover", blocks=[])
    document = _document([cover, _full_page(2, chapter_id="ch1")])
    report = check_pdf_spacing(document)
    assert report.ok
    assert report.content_pages_checked == 1


def test_footer_total_matching_the_real_pdf_page_count_passes(tmp_path):
    document = _document([_full_page(1, chapter_id="ch1")])
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)  # exactly 1 page, matching document.pages
    pdf_path = tmp_path / "course.pdf"
    with open(pdf_path, "wb") as handle:
        writer.write(handle)

    report = check_pdf_spacing(document, pdf_path=pdf_path)
    assert report.ok
    assert report.footer_total == 1
    assert report.real_pdf_pages == 1


def test_a_footer_total_mismatched_with_the_real_pdf_page_count_fails(tmp_path):
    document = _document([_full_page(1, chapter_id="ch1")])
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)  # 2 real pages vs document.pages' 1
    pdf_path = tmp_path / "course.pdf"
    with open(pdf_path, "wb") as handle:
        writer.write(handle)

    report = check_pdf_spacing(document, pdf_path=pdf_path)
    assert not report.ok
    assert not report.page_count_matches
    assert "does not match" in report.summary()


def test_omitting_pdf_path_skips_the_page_count_cross_check_without_failing():
    document = _document([_full_page(1, chapter_id="ch1")])
    report = check_pdf_spacing(document)
    assert report.page_count_matches
    assert report.real_pdf_pages is None
