"""Math/scientific notation accuracy: real client-side KaTeX typesetting
(see app.render.html_renderer's own module docstring) plus the
`.katex-error` QA gate (app.services.math_qa, wired into PdfService.export_pdf).
"""

from __future__ import annotations

from app.render.html_renderer import STATIC_DIR, render_document_html
from app.course.templates.registry import load_template
from app.schemas.document import Block, CourseDocument, Page
from app.services.math_qa import build_report
from tests.test_pdf import needs_browser


def _math_document(course_id: str, *texts: str) -> CourseDocument:
    blocks = [Block(type="heading", content={"text": "Math Test", "level": 1})]
    blocks += [Block(type="paragraph", content={"text": text}) for text in texts]
    return CourseDocument(
        document_id=f"doc_{course_id}",
        course_id=course_id,
        course_title="Applied Statistics",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )


# --- html_renderer: KaTeX assets are wired in and actually exist on disk ---


def test_rendered_html_links_katex_css_and_scripts_at_real_on_disk_paths():
    document = _math_document("crs_math_assets", "Plain text, no math here.")
    html = render_document_html(document, load_template("technical_v1"))

    assert '<link rel="stylesheet" href="file://' in html
    assert "katex.min.css" in html
    assert "katex.min.js" in html
    assert "auto-render.min.js" in html
    assert "mhchem.min.js" in html
    assert "window.__mathRendered" in html
    assert "renderMathInElement" in html

    # The URIs aren't just plausible-looking strings - the files they point
    # at are genuinely vendored on disk (see STATIC_DIR/katex/).
    assert (STATIC_DIR / "katex" / "katex.min.css").is_file()
    assert (STATIC_DIR / "katex" / "katex.min.js").is_file()
    assert (STATIC_DIR / "katex" / "auto-render.min.js").is_file()
    assert (STATIC_DIR / "katex" / "mhchem.min.js").is_file()


def test_jinja_escaping_leaves_latex_backslashes_and_dollars_untouched():
    """The whole architecture depends on this: Jinja's autoescape only
    touches HTML-special characters, never backslashes or dollar signs, so
    `$E = mc^2$` written directly into paragraph text survives into the
    HTML byte-for-byte and is readable by KaTeX's client-side auto-render."""
    document = _math_document("crs_math_escaping", "The famous equation $E = mc^2$ holds.")
    html = render_document_html(document, load_template("technical_v1"))
    assert "$E = mc^2$" in html


# --- math_qa.build_report: pure, deterministic -----------------------------


def test_build_report_passes_with_no_errors():
    report = build_report("crs_1", [])
    assert report.passed is True
    assert report.warnings() == []


def test_build_report_deduplicates_identical_error_texts():
    report = build_report("crs_1", ["KaTeX parse error: bad", "KaTeX parse error: bad", ""])
    assert report.passed is False
    assert len(report.findings) == 1
    assert report.findings[0].detail == "KaTeX parse error: bad"
    assert len(report.warnings()) == 1


def test_build_report_keeps_distinct_error_texts_separate():
    report = build_report("crs_1", ["error A", "error B"])
    assert len(report.findings) == 2


# --- end-to-end: real KaTeX typesetting via a real headless browser -------


@needs_browser
async def test_inline_and_display_math_are_actually_typeset(documents, storage):
    document = _math_document(
        "crs_math_inline_display",
        "The famous equation $E = mc^2$ describes mass-energy equivalence.",
        "Integrating gives $$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$ as shown above.",
    )
    storage.ensure_course_dirs(document.course_id)
    html_path = documents.pdf.write_html(document)

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.goto(html_path.resolve().as_uri(), wait_until="load")
            await page.wait_for_function("window.__mathRendered === true", timeout=10000)
            katex_count = await page.evaluate("document.querySelectorAll('.katex').length")
            error_count = await page.evaluate("document.querySelectorAll('.katex-error').length")
            # Real typesetting happened (KaTeX wraps each parsed expression
            # in its own .katex element) and none of it errored.
            assert katex_count >= 2
            assert error_count == 0
        finally:
            await browser.close()


@needs_browser
async def test_chemistry_notation_via_mhchem_is_typeset_correctly(documents, storage):
    document = _math_document(
        "crs_math_chemistry",
        "Water forms from hydrogen and oxygen: $\\ce{2H2 + O2 -> 2H2O}$.",
    )
    storage.ensure_course_dirs(document.course_id)
    html_path = documents.pdf.write_html(document)

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.goto(html_path.resolve().as_uri(), wait_until="load")
            await page.wait_for_function("window.__mathRendered === true", timeout=10000)
            katex_count = await page.evaluate("document.querySelectorAll('.katex').length")
            error_count = await page.evaluate("document.querySelectorAll('.katex-error').length")
            assert katex_count >= 1
            assert error_count == 0
        finally:
            await browser.close()


@needs_browser
async def test_a_genuinely_broken_latex_expression_is_caught_and_reported(documents, storage):
    """A deliberately malformed expression (unbalanced braces) must not be
    silently swallowed - PdfService.export_pdf's own math-QA check writes
    it to a sidecar file precisely so it's not just invisible in the PDF."""
    # \frac takes two arguments - this one is missing its second, which
    # KaTeX genuinely cannot parse (confirmed directly: errors=1, katex=0).
    document = _math_document(
        "crs_math_broken", "This is deliberately broken: $\\frac{1}$ oops.",
    )
    storage.ensure_course_dirs(document.course_id)

    await documents.pdf.export_pdf(document)

    qa_path = storage.course_dir(document.course_id) / "math_qa.json"
    assert qa_path.is_file()
    import json

    payload = json.loads(qa_path.read_text(encoding="utf-8"))
    assert payload["findings"]


@needs_browser
async def test_clean_math_produces_no_qa_sidecar_file(documents, storage):
    document = _math_document("crs_math_clean", "A clean equation: $a^2 + b^2 = c^2$.")
    storage.ensure_course_dirs(document.course_id)

    await documents.pdf.export_pdf(document)

    qa_path = storage.course_dir(document.course_id) / "math_qa.json"
    assert not qa_path.exists()
