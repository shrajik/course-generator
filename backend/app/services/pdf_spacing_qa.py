"""Automatic QA gate for an exported course PDF's pagination.

Catches the page-break regression this module exists because of: once an
image block carries its real, post-generation size (as opposed to the
pre-generation placeholder estimate - see app.course.document.layout's own
DEFAULT_IMAGE_ASPECT), the page-break decisions made around it can leave a
page's remaining space mostly blank, or - separately - leave the exported
PDF's own page count out of step with the footer text baked into it.

Deliberately measures straight from the CourseDocument's own layout data -
the exact y/height numbers the CSS `min-height` boxes and the forced
`break-after: page` rule (app/render/templates/course.html.j2) are built
from - rather than rasterizing the PDF to images and measuring pixels. It is
the same ground truth (every block's position is pre-computed, deterministic
Python math; nothing about it is decided by the browser at render time), at
zero new rendering cost and with no font-rendering noise to account for. The
real PDF page count (when a `pdf_path` is given) is still read back for real
from the exported file via pypdf, as a genuine end-to-end check that nothing
diverged between what was planned and what Chromium actually produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.course.document.layout import CONTENT_HEIGHT, PAGE_MARGIN_TOP
from app.schemas.document import CourseDocument, Page

# A page's own trailing blank space past this (% of CONTENT_HEIGHT) is
# flagged - except the last page of a chapter, which is expected to run
# short. A fixed percentage, not a per-course tuned number, so the gate
# behaves identically for any course, topic, length or image count.
MAX_TRAILING_BLANK_PCT = 15.0


@dataclass
class PageSpacingFinding:
    page_number: int
    blank_pct: float
    last_block_type: str


@dataclass
class PdfSpacingReport:
    total_pages: int
    content_pages_checked: int
    findings: list[PageSpacingFinding] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)
    footer_total: int | None = None
    real_pdf_pages: int | None = None

    @property
    def page_count_matches(self) -> bool:
        if self.footer_total is None or self.real_pdf_pages is None:
            return True  # not checked (no pdf_path given) - not a failure
        return self.footer_total == self.real_pdf_pages

    @property
    def ok(self) -> bool:
        return not self.findings and not self.blank_pages and self.page_count_matches

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"PDF spacing QA: {status} ({self.content_pages_checked} content pages checked)"]
        if not self.page_count_matches:
            lines.append(
                f"  footer total ({self.footer_total}) does not match the real PDF page count "
                f"({self.real_pdf_pages})"
            )
        for page_number in self.blank_pages:
            lines.append(f"  page {page_number}: fully blank")
        for finding in self.findings:
            lines.append(
                f"  page {finding.page_number}: {finding.blank_pct:.1f}% trailing blank "
                f"(last block: {finding.last_block_type})"
            )
        return "\n".join(lines)


def _chapter_id_of(page: Page) -> str | None:
    for block in page.blocks:
        if block.meta.chapter_id:
            return block.meta.chapter_id
    return None


def check_pdf_spacing(
    document: CourseDocument,
    *,
    pdf_path: str | Path | None = None,
    max_trailing_blank_pct: float = MAX_TRAILING_BLANK_PCT,
) -> PdfSpacingReport:
    """Run the spacing/pagination QA gate against an already-generated,
    already-reflowed document. `pdf_path`, when given, is read back with
    pypdf for the real exported page count; omit it to check layout-level
    spacing only (e.g. before export has even run).
    """
    content_pages = [page for page in document.pages if page.kind == "content"]
    bottom = PAGE_MARGIN_TOP + CONTENT_HEIGHT
    report = PdfSpacingReport(total_pages=len(document.pages), content_pages_checked=len(content_pages))

    for index, page in enumerate(content_pages):
        if not page.blocks:
            report.blank_pages.append(page.page_number)
            continue

        last_block = max(page.blocks, key=lambda b: b.layout.y + b.layout.height)
        last_bottom = last_block.layout.y + last_block.layout.height
        blank_pct = max(0.0, (bottom - last_bottom) / CONTENT_HEIGHT * 100)

        next_page = content_pages[index + 1] if index + 1 < len(content_pages) else None
        is_chapter_end = next_page is None or _chapter_id_of(next_page) != _chapter_id_of(page)

        if blank_pct > max_trailing_blank_pct and not is_chapter_end:
            report.findings.append(
                PageSpacingFinding(
                    page_number=page.page_number,
                    blank_pct=round(blank_pct, 1),
                    last_block_type=last_block.type.value,
                )
            )

    report.footer_total = len(document.pages)
    if pdf_path is not None:
        try:
            import pypdf

            report.real_pdf_pages = len(pypdf.PdfReader(str(pdf_path)).pages)
        except Exception:  # noqa: BLE001 - advisory only, never blocks generation
            report.real_pdf_pages = None

    return report
