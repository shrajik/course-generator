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
    return f"""\
<style>
.toc-root {{
  font-family:{theme.font_family};color:{theme.text_color};
  background:#ffffff;border:1px solid {theme.border_color};border-radius:6px;
  padding:28px;box-sizing:border-box;
}}
.toc-root *{{box-sizing:border-box;}}
.toc-title{{font-size:22px;font-weight:800;text-align:center;margin:4px 0 2px;letter-spacing:-.01em;}}
.toc-subtitle{{font-size:12.5px;color:{theme.muted_color};text-align:center;margin:0 0 18px;
  text-transform:uppercase;letter-spacing:.08em;}}
table.toc-table{{width:100%;border-collapse:collapse;}}
.toc-table thead th{{
  background:#f3f4f6;color:{theme.text_color};font-size:11.5px;font-weight:700;
  text-transform:uppercase;letter-spacing:.04em;text-align:left;
  padding:8px 10px;border-bottom:2px solid {theme.border_color};
}}
.toc-table thead th.toc-page{{text-align:right;}}
.toc-table td{{padding:7px 10px;border-bottom:1px solid {theme.border_color};font-size:12.5px;vertical-align:top;}}
.toc-chapter-row td{{font-weight:700;font-size:13.5px;}}
.toc-chapter-row td.toc-no{{width:42px;}}
.toc-section-row td{{font-weight:400;color:{theme.text_color};}}
.toc-section-row td.toc-title{{padding-left:22px;}}
.toc-section-row td.toc-no{{color:{theme.muted_color};}}
td.toc-learn{{color:{theme.muted_color};}}
td.toc-page{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}}
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
    height = 56.0  # .toc-root vertical padding
    height += 28.0  # title
    height += 24.0  # subtitle + margin
    height += 34.0  # header row

    row_pad = 14.0  # vertical padding across a row's own two cells
    for chapter in chapters:
        height += row_pad + _learn_line_count(chapter.summary) * 15.0
        for section in chapter.sections:
            height += row_pad + _learn_line_count(section.learn_line) * 14.0

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
