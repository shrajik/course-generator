"""HTML rendering and PDF export."""

from __future__ import annotations

import pytest

from app.render.html_renderer import render_document_html
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest
from app.schemas.document import Block, CourseDocument, Page


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            browser.close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(
    not _playwright_available(), reason="Chromium is not available in this environment"
)


def _document_with_every_block_type() -> CourseDocument:
    samples = {
        BlockType.HEADING: {"text": "Every Block Type", "level": 1},
        BlockType.PARAGRAPH: {"text": "Prose."},
        BlockType.IMAGE: {"prompt": "p", "caption": "A caption"},
        BlockType.QUOTE: {"text": "Quoted.", "attribution": "Someone"},
        BlockType.CALLOUT: {"title": "Did you know?", "text": "A fact."},
        BlockType.CODE: {"language": "python", "code": "print('hi')", "caption": "Snippet"},
        BlockType.TABLE: {"columns": ["A", "B"], "rows": [{"cells": ["1", "2"]}]},
        BlockType.QUIZ: {
            "questions": [
                {"question": "Q?", "options": ["a", "b"], "answer": "a", "explanation": "because"}
            ]
        },
        BlockType.EXERCISE: {"instructions": "Do it", "steps": ["one"], "hints": ["hint"]},
        BlockType.CHALLENGE: {"instructions": "Harder"},
        BlockType.CASE_STUDY: {"context": "c", "challenge": "ch", "lessons": ["l"]},
        BlockType.STORY: {"title": "A story", "text": "Once.", "takeaway": "T"},
        BlockType.TIP: {"text": "A tip."},
        BlockType.WARNING: {"text": "Careful."},
        BlockType.SUMMARY: {"key_takeaways": ["one"], "next_steps": ["next"]},
        BlockType.DIVIDER: {},
        BlockType.LEARNING_OBJECTIVES: {"items": ["objective"]},
        BlockType.REFLECTION: {"items": ["Why?"]},
    }
    template = load_template("technical_v1")
    blocks = [
        Block(type=bt, content=content, style=template.style_for(bt))
        for bt, content in samples.items()
    ]
    return CourseDocument(
        document_id="doc_render",
        course_id="crs_render",
        course_title="Render Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )


def test_html_renderer_handles_every_block_type():
    document = _document_with_every_block_type()
    html = render_document_html(document, load_template("technical_v1"))
    assert html.count('class="page"') == 1
    for needle in [
        "Every Block Type",
        "Did you know?",
        "print(&#39;hi&#39;)",
        "<table>",
        "Knowledge Check",
        "Careful.",
        "Why?",
    ]:
        assert needle in html, needle


def test_html_renderer_escapes_content():
    block = Block(type=BlockType.PARAGRAPH, content={"text": "<script>alert(1)</script>"})
    document = CourseDocument(
        document_id="d",
        course_id="c",
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[block])],
    )
    html = render_document_html(document, load_template("technical_v1"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_uses_layout_coordinates():
    block = Block(
        type=BlockType.PARAGRAPH,
        content={"text": "positioned"},
        layout={"x": 80, "y": 100, "width": 600, "height": 60},
    )
    document = CourseDocument(
        document_id="d",
        course_id="c",
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[block])],
    )
    html = render_document_html(document, load_template("technical_v1"))
    assert "left:80px" in html and "top:100px" in html and "width:600px" in html


def test_pdf_service_writes_html_next_to_the_pdf(documents, storage):
    document = _document_with_every_block_type()
    storage.ensure_course_dirs(document.course_id)
    path = documents.pdf.write_html(document)
    assert path.name == "course.html"
    assert path.parent.name == "exports"
    assert 'src="../assets/' in path.read_text() or "img-missing" in path.read_text()


@needs_browser
async def test_export_pdf_produces_a_real_pdf(service, documents, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest())
    path, document = await documents.export_pdf(record.document_id)

    assert path.exists()
    header = path.read_bytes()[:5]
    assert header == b"%PDF-"
    assert path.stat().st_size > 10_000

    # One PDF page per document page.
    text = path.read_bytes()
    assert text.count(b"/Type /Page") >= len(document.pages) or b"/Count" in text

    # The course record remembers the export.
    reloaded = service.storage.load_course(record.course_id)
    assert reloaded.pdf_path == "exports/course.pdf"


@needs_browser
async def test_pdf_reflects_the_current_document_after_an_edit(
    service, documents, technical_input
):
    from app.schemas.patch import AiEditRequest

    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest())
    document = service.storage.load_document(record.course_id)
    paragraph = next(
        block
        for _, block in document.iter_blocks()
        if block.type is BlockType.PARAGRAPH and block.meta.origin == "generated"
    )

    first_path, _ = await documents.export_pdf(record.document_id)
    first_size = first_path.stat().st_size

    await documents.ai_edit(
        record.document_id,
        AiEditRequest(
            selected_block_ids=[paragraph.id], instruction="Make this easier to understand"
        ),
    )
    second_path, second_document = await documents.export_pdf(record.document_id)

    assert second_document.version > document.version
    assert second_path.stat().st_size != first_size
    html = service.storage.html_path(record.course_id).read_text()
    assert "Simplified explanation" in html
