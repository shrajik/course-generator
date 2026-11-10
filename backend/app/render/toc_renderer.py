"""A proper, textbook-style Table of Contents page.

Same deterministic/AI-free, "one motionless picture" contract as
`app.render.concept_experience_renderer` - inlined into the page DOM by the
frontend, and by Playwright for PDF export, never a second rendering path.

Sized and capped the same way too: a course can have anywhere from 2 to
20+ chapters, and this block can never spill onto a second page (see
`app.course.document.builder._toc_page`, which always places it alone on
page 2) - so the table is uniformly scaled down to fit one page's worth of
height whenever there are enough rows that it would otherwise overflow,
exactly mirroring `concept_experience_renderer.estimate_pixel_size`'s own
cap-and-scale contract.

Real page numbers aren't known yet when `_toc_page` first builds this page
(chapter content hasn't been laid out). `TocChapter.page`/`TocSection.page`
default to 0 and the table simply omits a page number until
`app.course.document.builder.finalize_toc_page` re-renders this same block,
once, after the whole document's pagination is final - see that function's
own docstring for the two-pass contract.
"""

from __future__ import annotations

import math
from html import escape as _esc

from app.schemas.document import CONTENT_HEIGHT
from app.schemas.template import TemplateTheme

_REFERENCE_WIDTH = 666  # matches CONTENT_WIDTH in app.schemas.document
_MAX_HEIGHT = CONTENT_HEIGHT - 100.0  # see estimate_toc_pixel_size for the reserve
_ESTIMATE_SAFETY = 1.15


class TocSection:
    """One sub-section row ("1.1 Learning Objectives") - derived from a
    real level>=3 heading inside its chapter, never invented. `learn_line`
    is the short "what you'll learn" text for this sub-section specifically
    (left "" when nothing concise is available nearby - never fabricated)."""

    __slots__ = ("number", "title", "page", "learn_line")

    def __init__(self, number: str, title: str, *, page: int = 0, learn_line: str = "") -> None:
        self.number = number
        self.title = title
        self.page = page
        self.learn_line = learn_line


class TocChapter:
    """The handful of fields this renderer actually needs from a
    `GeneratedChapter` - kept narrow so tests can build one without the
    rest of that model's machinery."""

    __slots__ = ("number", "title", "summary", "page", "sections")

    def __init__(
        self,
        number: int,
        title: str,
        summary: str = "",
        *,
        page: int = 0,
        sections: list[TocSection] | None = None,
    ) -> None:
        self.number = number
        self.title = title
        self.summary = summary
        self.page = page
        self.sections = sections or []


def _page_cell(page: int) -> str:
    return str(page) if page > 0 else ""


def _chapter_row_html(chapter: TocChapter) -> str:
    return f"""\
<tr class="toc-chapter-row">
  <td class="toc-no">{chapter.number}</td>
  <td class="toc-title">{_esc(chapter.title)}</td>
  <td class="toc-learn">{_esc(chapter.summary.strip())}</td>
  <td class="toc-page">{_page_cell(chapter.page)}</td>
</tr>"""


def _section_row_html(chapter: TocChapter, section: TocSection) -> str:
    return f"""\
<tr class="toc-section-row">
  <td class="toc-no">{_esc(section.number)}</td>
  <td class="toc-title">{_esc(section.title)}</td>
  <td class="toc-learn">{_esc(section.learn_line.strip())}</td>
  <td class="toc-page">{_page_cell(section.page)}</td>
</tr>"""


def _style_html(theme: TemplateTheme) -> str:
    # A real printed textbook's contents page, not a spreadsheet export: no
    # grid lines boxing in every cell, no shaded header band, no zebra
    # striping - just generous whitespace, a bold chapter number/title, a
    # single hairline rule ABOVE each chapter to group its own sub-sections
    # underneath it, and the page number set in quiet tabular-nums so a
    # column of them lines up the way it does in a real printed index.
    return f"""\
<style>
.toc-root {{
  font-family:{theme.font_family};color:{theme.text_color};
  background:#ffffff;border-radius:6px;
  padding:34px 32px;box-sizing:border-box;
}}
.toc-root *{{box-sizing:border-box;}}
.toc-title{{font-size:24px;font-weight:800;text-align:center;margin:2px 0 3px;letter-spacing:-.01em;}}
.toc-subtitle{{font-size:11px;color:{theme.muted_color};text-align:center;margin:0 0 26px;
  text-transform:uppercase;letter-spacing:.18em;}}
table.toc-table{{width:100%;border-collapse:collapse;}}
.toc-table thead th{{
  color:{theme.muted_color};font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.1em;text-align:left;
  padding:0 10px 9px;border-bottom:1px solid {theme.border_color};
}}
.toc-table thead th.toc-page{{text-align:right;}}
.toc-table thead th.toc-no{{width:30px;}}
.toc-table td{{padding:5px 10px;vertical-align:top;font-size:12.5px;}}
.toc-chapter-row td{{
  font-weight:700;font-size:14px;padding-top:17px;padding-bottom:3px;
  border-top:1px solid {theme.border_color};
}}
.toc-chapter-row td.toc-no{{width:30px;color:{theme.muted_color};}}
.toc-chapter-row td.toc-learn{{font-weight:400;font-style:italic;color:{theme.muted_color};font-size:11.5px;}}
.toc-section-row td{{font-weight:400;color:{theme.text_color};font-size:12px;padding-top:3px;padding-bottom:3px;}}
.toc-section-row td.toc-title{{padding-left:26px;}}
.toc-section-row td.toc-no{{color:{theme.muted_color};}}
.toc-section-row td.toc-learn{{color:{theme.muted_color};font-style:italic;font-weight:400;}}
td.toc-learn{{color:{theme.muted_color};}}
td.toc-page{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:600;}}
.toc-section-row td.toc-page{{font-weight:400;color:{theme.muted_color};}}
</style>"""


def render_toc_html(chapters: list[TocChapter], *, course_title: str, theme: TemplateTheme) -> bytes:
    """Dispatches to a static, self-contained HTML+CSS fragment - see the
    module docstring for why (table layout, static, capped-and-scaled like
    concept_experience)."""
    rows: list[str] = []
    for chapter in chapters:
        rows.append(_chapter_row_html(chapter))
        rows.extend(_section_row_html(chapter, section) for section in chapter.sections)
    rows_html = "".join(rows)

    body = f"""\
<div class="toc-root">
{_style_html(theme)}
<p class="toc-title">{_esc(course_title)}</p>
<p class="toc-subtitle">Table of Contents</p>
<table class="toc-table">
<thead><tr>
  <th>No.</th><th>Title</th><th>What You Will Learn</th><th class="toc-page">Page</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>"""
    html_bytes = body.encode("utf-8")

    width, natural_height, capped_height, scale = _fit_scale(chapters)
    if scale >= 1.0:
        return html_bytes
    return _wrap_scaled(
        html_bytes, width=width, natural_height=natural_height, capped_height=capped_height, scale=scale
    )


def _wrap_scaled(body: bytes, *, width: int, natural_height: float, capped_height: int, scale: float) -> bytes:
    # overflow:visible, not hidden - same reasoning as
    # concept_experience_renderer._wrap_scaled: this is an estimate, not a
    # text-shaping engine, and a residual underestimate should spill a few
    # pixels rather than silently clip a row out of view.
    wrapper = (
        f'<div style="width:{width}px;height:{capped_height}px;'
        f'display:flex;justify-content:center;">'
        f'<div style="width:{width}px;height:{math.ceil(natural_height)}px;flex:none;'
        f'transform:scale({scale:.4f});transform-origin:top center;">'
        f"{body.decode('utf-8')}"
        f"</div></div>"
    )
    return wrapper.encode("utf-8")


def _learn_line_count(text: str, *, chars_per_line: int = 70) -> int:
    text = text.strip()
    if not text:
        return 1  # an empty cell still occupies one row of height
    return max(1, math.ceil(len(text) / chars_per_line))


def _raw_pixel_size(chapters: list[TocChapter]) -> tuple[int, float]:
    width = _REFERENCE_WIDTH
    height = 68.0  # .toc-root vertical padding
    height += 30.0  # title
    height += 36.0  # subtitle + margin
    height += 24.0  # header row

    chapter_row_pad = 20.0  # a chapter row's own top/bottom padding
    section_row_pad = 6.0  # a (tighter) sub-section row's own top/bottom padding
    for chapter in chapters:
        height += chapter_row_pad + _learn_line_count(chapter.summary) * 15.0
        for section in chapter.sections:
            height += section_row_pad + _learn_line_count(section.learn_line) * 14.0

    return width, height * _ESTIMATE_SAFETY


def _fit_scale(chapters: list[TocChapter]) -> tuple[int, float, int, float]:
    width, natural_height = _raw_pixel_size(chapters)
    capped_height = math.ceil(min(natural_height, _MAX_HEIGHT))
    if natural_height <= _MAX_HEIGHT:
        return width, natural_height, capped_height, 1.0
    return width, natural_height, capped_height, (_MAX_HEIGHT / natural_height) * 0.92


def estimate_toc_pixel_size(chapters: list[TocChapter]) -> tuple[int, int]:
    """The intrinsic width/height to store on the block's content - the same
    role `concept_experience_renderer.estimate_pixel_size` plays for a
    concept visual, so the page layout engine reserves exactly what this
    renderer will actually draw."""
    width, natural_height, capped_height, _scale = _fit_scale(chapters)
    return width, capped_height
