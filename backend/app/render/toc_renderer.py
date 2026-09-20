"""A colorful, static "what's in this course" index page.

Same philosophy as `app.render.concept_experience_renderer`: deterministic,
AI-free, deliberately colorful (a course overview should feel inviting, not
like a legal document's table of contents), and deliberately static - no JS,
no buttons, just one motionless picture, since it's consumed the same way
(inlined into the page DOM by the frontend, and by Playwright for PDF
export). Reuses that module's vivid palette so a course's cover/TOC and its
concept visuals read as the same product, not two different tools.

Sized and capped the same way, too: a course can have anywhere from 2 to 20+
chapters, and this block can never spill onto a second page (see
`app.course.document.builder._toc_page`, which always places it alone on
page 2) - so it's uniformly scaled down to fit one page's worth of height
whenever there are enough chapters that it would otherwise overflow, exactly
mirroring `concept_experience_renderer.estimate_pixel_size`/
`render_concept_experience_html`'s own cap-and-scale contract.
"""

from __future__ import annotations

import math
from html import escape as _esc

from app.render.concept_experience_renderer import _CEV_PALETTE, _resolve_color_role
from app.schemas.document import CONTENT_HEIGHT
from app.schemas.template import TemplateTheme

# Every palette role except "neutral" (reserved as the fallback for an
# unrecognised role elsewhere) - cycled in a fixed order so the same chapter
# count always produces the same colours, run to run.
_BADGE_ROLES = [role for role in _CEV_PALETTE if role != "neutral"]

_REFERENCE_WIDTH = 666  # matches CONTENT_WIDTH in app.schemas.document
_MAX_HEIGHT = CONTENT_HEIGHT - 100.0  # see estimate_toc_pixel_size for the reserve
_ESTIMATE_SAFETY = 1.15


class TocChapter:
    """The handful of fields this renderer actually needs from a
    `GeneratedChapter` - kept narrow so tests can build one without the
    rest of that model's machinery."""

    __slots__ = ("number", "title", "summary")

    def __init__(self, number: int, title: str, summary: str = "") -> None:
        self.number = number
        self.title = title
        self.summary = summary


def _card_html(chapter: TocChapter, role_name: str) -> str:
    role = _resolve_color_role(role_name)
    css_vars = f"--toc-fill:{role.fill};--toc-stroke:{role.stroke};--toc-text:{role.text};"
    summary_html = (
        f'<p class="toc-card-summary">{_esc(chapter.summary.strip())}</p>'
        if chapter.summary.strip()
        else ""
    )
    return f"""\
<div class="toc-card" style="{css_vars}">
  <div class="toc-badge">{chapter.number}</div>
  <div class="toc-card-body">
    <p class="toc-card-title">{_esc(chapter.title)}</p>
    {summary_html}
  </div>
</div>"""


def _style_html(theme: TemplateTheme) -> str:
    return f"""\
<style>
.toc-root {{
  font-family:{theme.font_family};color:{theme.text_color};
  background:linear-gradient(180deg, color-mix(in srgb, {theme.accent_color} 8%, #fffdf7), #fffdf7);
  border:1px solid {theme.border_color};border-radius:22px;padding:28px;box-sizing:border-box;
  box-shadow:0 2px 4px rgba(0,0,0,.05), 0 16px 32px -18px rgba(0,0,0,.22);
  position:relative;overflow:hidden;
}}
.toc-root::before {{
  content:"";position:absolute;inset:0 0 auto 0;height:7px;
  background:linear-gradient(90deg, #f472b6, #fb923c, #facc15, #4ade80, #22d3ee, #60a5fa, #c084fc);
}}
.toc-root *{{box-sizing:border-box;}}
.toc-title{{font-size:23px;font-weight:800;text-align:center;margin:4px 0 4px;letter-spacing:-.01em;}}
.toc-subtitle{{font-size:13px;color:{theme.muted_color};text-align:center;margin:0 0 20px;}}
.toc-grid{{display:flex;flex-wrap:wrap;gap:14px;justify-content:center;}}
.toc-card{{
  display:flex;gap:12px;align-items:flex-start;flex:1 1 280px;max-width:300px;min-width:220px;
  border-radius:16px;border:3px solid var(--toc-stroke,{theme.border_color});
  background:linear-gradient(155deg, color-mix(in srgb, var(--toc-fill,{theme.surface_color}) 40%, #fff), color-mix(in srgb, var(--toc-fill,{theme.surface_color}) 62%, #fff));
  padding:14px 16px;
  box-shadow:0 3px 0 color-mix(in srgb, var(--toc-stroke,{theme.border_color}) 55%, transparent), 0 6px 14px rgba(0,0,0,.07);
}}
.toc-badge{{
  flex:none;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:var(--toc-stroke,{theme.accent_color});color:#fff;font-size:17px;font-weight:800;
  box-shadow:inset 0 -3px 0 rgba(0,0,0,.15), 0 2px 4px rgba(0,0,0,.12);
}}
.toc-card-body{{min-width:0;}}
.toc-card-title{{margin:2px 0 0;font-size:14.5px;font-weight:800;color:var(--toc-text,{theme.text_color});line-height:1.3;}}
.toc-card-summary{{margin:5px 0 0;font-size:11.5px;line-height:1.5;color:{theme.muted_color};
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}}
@media print {{
  .toc-card{{box-shadow:none;}}
}}
</style>"""


def render_toc_html(
    chapters: list[TocChapter], *, course_title: str, theme: TemplateTheme
) -> bytes:
    """Dispatches to a static, self-contained HTML+CSS fragment - see the
    module docstring for why (colorful, static, capped-and-scaled like
    concept_experience)."""
    cards_html = "".join(
        _card_html(chapter, _BADGE_ROLES[index % len(_BADGE_ROLES)])
        for index, chapter in enumerate(chapters)
    )
    body = f"""\
<div class="toc-root">
{_style_html(theme)}
<p class="toc-title">{_esc(course_title)}</p>
<p class="toc-subtitle">What's in this course</p>
<div class="toc-grid">{cards_html}</div>
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
    # pixels rather than silently clip a chapter card out of view.
    wrapper = (
        f'<div style="width:{width}px;height:{capped_height}px;'
        f'display:flex;justify-content:center;">'
        f'<div style="width:{width}px;height:{math.ceil(natural_height)}px;flex:none;'
        f'transform:scale({scale:.4f});transform-origin:top center;">'
        f"{body.decode('utf-8')}"
        f"</div></div>"
    )
    return wrapper.encode("utf-8")


def _raw_pixel_size(chapters: list[TocChapter]) -> tuple[int, float]:
    width = _REFERENCE_WIDTH
    height = 56.0  # .toc-root vertical padding
    height += 32.0  # title
    height += 28.0  # subtitle + margin

    inner_width = width - 56  # .toc-root's own 28px side padding
    card_width = 300 + 14  # max-width + gap
    per_row = max(1, inner_width // card_width)
    rows = math.ceil(len(chapters) / per_row) if chapters else 0
    # A card is at least ~74px (badge + one title line) and grows with a
    # long summary (line-clamped to 3 lines, ~17px each) - the same
    # "assume the busiest card in the row" logic
    # concept_experience_renderer uses, so a handful of long summaries
    # can't silently under-reserve the whole grid.
    busiest_summary_lines = max((_summary_lines(c.summary) for c in chapters), default=0)
    card_height = 50.0 + min(busiest_summary_lines, 3) * 17.0
    height += rows * (card_height + 14.0)

    return width, height * _ESTIMATE_SAFETY


def _summary_lines(summary: str, *, chars_per_line: int = 38) -> int:
    text = summary.strip()
    if not text:
        return 0
    return max(1, math.ceil(len(text) / chars_per_line))


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
