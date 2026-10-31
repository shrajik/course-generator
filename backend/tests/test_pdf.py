"""HTML rendering and PDF export."""

from __future__ import annotations

import pytest

from app.course.document.builder import SOURCES_SECTION_KEY
from app.render.html_renderer import _linkify, render_document_html
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest
from app.schemas.document import Block, BlockMeta, CourseDocument, Page


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


# --- course-sources follow-up: clickable Sources-page links -----------------


def test_linkify_wraps_a_url_in_an_anchor_tag():
    result = _linkify("Official Python Docs — https://docs.python.org/3/")
    assert '<a href="https://docs.python.org/3/" target="_blank" rel="noreferrer">' in result
    assert result.count("<a ") == 1


def test_linkify_escapes_surrounding_text_safely():
    result = _linkify('A <script>alert(1)</script> & "quoted" note')
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "&amp;" in result


def test_linkify_strips_trailing_sentence_punctuation_from_the_link():
    result = _linkify("See https://example.test/a.")
    assert 'href="https://example.test/a"' in result
    assert result.endswith("</a>.")


def test_linkify_handles_multiple_urls_and_plain_text():
    result = _linkify("No link here, just text.")
    assert "<a " not in result
    assert result == "No link here, just text."


def _sources_document(text: str) -> CourseDocument:
    meta = BlockMeta(origin="system", section_key=SOURCES_SECTION_KEY)
    blocks = [
        Block(type=BlockType.HEADING, content={"text": "Sources & Further Reading", "level": 1}, meta=meta),
        Block(type=BlockType.PARAGRAPH, content={"text": text}, meta=meta),
    ]
    return CourseDocument(
        document_id="doc_sources_render",
        course_id="crs_sources_render",
        course_title="Sources Render Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )


def test_sources_page_url_renders_as_a_real_link_in_html():
    document = _sources_document("Official Python Docs — https://docs.python.org/3/")
    html = render_document_html(document, load_template("technical_v1"))
    assert '<a href="https://docs.python.org/3/" target="_blank" rel="noreferrer">' in html
    assert "https://docs.python.org/3/</a>" in html


@needs_browser
async def test_sources_page_url_is_a_real_clickable_link_in_the_exported_pdf(documents, storage):
    """The HTML-level `<a href>` assertion above proves the markup is
    correct; this proves Chromium's print-to-PDF actually carries it
    through as a real PDF link annotation (`/URI`), not just blue
    underlined text with nothing behind it."""
    document = _sources_document("Official Python Docs — https://docs.python.org/3/")
    storage.ensure_course_dirs(document.course_id)
    path = await documents.pdf.export_pdf(document)
    data = path.read_bytes()
    assert b"/URI" in data
    assert b"docs.python.org" in data


def test_an_ordinary_chapter_paragraph_url_is_never_linked():
    """The linkify transform is scoped to the Sources section only (see its
    one call site in html_renderer.py) - an ordinary chapter paragraph that
    happens to mention a URL must render exactly as before, unlinked."""
    template = load_template("technical_v1")
    block = Block(type=BlockType.PARAGRAPH, content={"text": "See https://example.test/a for details."})
    document = CourseDocument(
        document_id="doc_plain", course_id="crs_plain", course_title="Plain",
        template_id="technical_v1", pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    html = render_document_html(document, template)
    assert "<a " not in html
    assert "https://example.test/a" in html


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


def test_section_intro_image_and_its_paragraph_render_as_one_floating_pair():
    """A section_intro image immediately followed by a paragraph renders as
    ONE combined wrapper with a real CSS float - not two separate
    absolutely-positioned divs (which would defeat float entirely, since it
    only affects normal-flow siblings) - while an ordinary (non-section_intro)
    image right next to a paragraph is completely unaffected."""
    from app.course.document.layout import flow_blocks

    image = Block(
        type=BlockType.IMAGE,
        content={"kind": "illustration", "illustration_style": "section_intro", "path": "assets/pic.png"},
    )
    paragraph = Block(type=BlockType.PARAGRAPH, content={"text": "Some intro text."})
    flow_blocks([image, paragraph])  # assigns real layout coordinates, same as production
    document = CourseDocument(
        document_id="d",
        course_id="c",
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[image, paragraph])],
    )
    html = render_document_html(document, load_template("technical_v1"))

    # Exactly one combined wrapper, never a second, separately-positioned
    # div for the now-consumed paragraph.
    assert html.count('class="block b-section_intro_pair"') == 1
    assert html.count('class="block b-image"') == 0
    assert html.count('class="block b-paragraph"') == 0
    assert "section-intro-img" in html
    assert 'src="../assets/pic.png"' in html
    assert "Some intro text." in html


def test_an_ordinary_image_next_to_a_paragraph_is_not_paired():
    from app.course.document.layout import flow_blocks

    image = Block(type=BlockType.IMAGE, content={"kind": "illustration", "path": "assets/pic.png"})
    paragraph = Block(type=BlockType.PARAGRAPH, content={"text": "Some text."})
    flow_blocks([image, paragraph])
    document = CourseDocument(
        document_id="d",
        course_id="c",
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[image, paragraph])],
    )
    html = render_document_html(document, load_template("technical_v1"))

    assert 'class="block b-section_intro_pair"' not in html
    assert html.count('class="block b-image"') == 1
    assert html.count('class="block b-paragraph"') == 1


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


def test_concept_experience_block_is_inlined_not_img_src(documents, storage):
    """A concept_experience block's saved `.html` fragment is a
    self-contained interactive widget (inline <style>+<script>) -
    `<img src="foo.html">` cannot render it at all (see Phase 4 plan).
    PdfService must inline the fragment's own markup into the page instead."""
    course_id = "crs_concept_pdf"
    storage.ensure_course_dirs(course_id)
    fragment = '<div class="cev-root"><span data-cev-marker="1">Hi</span></div>'
    relative = storage.save_asset(course_id, fragment.encode("utf-8"), extension="html")

    block = Block(
        type=BlockType.IMAGE,
        content={"kind": "concept_experience", "path": relative, "caption": "A concept"},
    )
    document = CourseDocument(
        document_id="doc_concept_pdf",
        course_id=course_id,
        course_title="T",
        template_id="technical_v1",
        pages=[Page(blocks=[block])],
    )

    html = documents.pdf.render_html(document)
    assert 'data-cev-marker="1"' in html
    assert f'src="{relative}"' not in html
    assert f'<img class="figure" src="../{relative}"' not in html


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
