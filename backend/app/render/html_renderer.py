"""Course Document JSON -> HTML.

Pure, deterministic and AI-free: the PDF is just a projection of the document, so
rendering must never introduce content of its own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.course.document.layout import image_box_height
from app.schemas.blocks import BlockType
from app.course.templates.docx_parser.placeholders import resolve
from app.schemas.document import Block, CourseDocument
from app.schemas.template import CourseTemplate

TEMPLATES_DIR = Path(__file__).parent / "templates"

_environment = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

_PARAGRAPH_SPLIT = re.compile(r"\n{2,}")

# Blocks whose box should render its background/border chrome.
_PANEL_TYPES = {
    BlockType.CALLOUT,
    BlockType.TIP,
    BlockType.WARNING,
    BlockType.QUIZ,
    BlockType.EXERCISE,
    BlockType.CHALLENGE,
    BlockType.CASE_STUDY,
    BlockType.SUMMARY,
    BlockType.LEARNING_OBJECTIVES,
    BlockType.REFLECTION,
    BlockType.STORY,
    BlockType.IMAGE,
}


def _n(value: float | int) -> str:
    """Compact CSS numbers: 80.0 -> '80', 12.50 -> '12.5'."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _code_panel_css(block: Block) -> str:
    """Code chrome lives on an inner panel so the caption stays readable."""
    style = block.style
    rules = ["display:block"]
    if style.background:
        rules.append(f"background:{style.background}")
    if style.color:
        rules.append(f"color:{style.color}")
    if style.font_family:
        rules.append(f"font-family:{style.font_family}")
    if style.font_size:
        rules.append(f"font-size:{_n(style.font_size)}px")
    if style.line_height:
        rules.append(f"line-height:{style.line_height}")
    if style.border_radius:
        rules.append(f"border-radius:{_n(style.border_radius)}px")
    if style.border_color:
        rules.append(f"border:{_n(style.border_width or 1)}px solid {style.border_color}")
    rules.append(f"padding:{_n(style.padding or 14)}px")
    return ";".join(rules) + ";"


def _css(block: Block, theme: dict[str, Any]) -> str:
    layout = block.layout
    style = block.style
    rules: list[str] = [
        f"left:{_n(layout.x)}px",
        f"top:{_n(layout.y)}px",
        f"width:{_n(layout.width)}px",
        f"min-height:{_n(layout.height)}px",
    ]
    if layout.z_index:
        rules.append(f"z-index:{layout.z_index}")
    if block.type is BlockType.CODE:
        # Everything visual is applied to the inner code panel instead.
        return ";".join(rules) + ";"
    if style.font_family:
        rules.append(f"font-family:{style.font_family}")
    if style.font_size:
        rules.append(f"font-size:{_n(style.font_size)}px")
    if style.font_weight:
        rules.append(f"font-weight:{style.font_weight}")
    if style.line_height:
        rules.append(f"line-height:{style.line_height}")
    if style.color:
        rules.append(f"color:{style.color}")
    if style.align:
        rules.append(f"text-align:{style.align}")
    if style.italic:
        rules.append("font-style:italic")
    if style.letter_spacing:
        rules.append(f"letter-spacing:{_n(style.letter_spacing)}px")
    if block.type in _PANEL_TYPES:
        if style.background:
            rules.append(f"background:{style.background}")
        if style.border_color:
            width = style.border_width or 1
            rules.append(f"border:{_n(width)}px solid {style.border_color}")
        if style.border_radius:
            rules.append(f"border-radius:{_n(style.border_radius)}px")
        if style.padding:
            rules.append(f"padding:{_n(style.padding)}px")
    elif style.background:
        rules.append(f"background:{style.background}")
    if block.type is BlockType.IMAGE:
        rules.append("overflow:hidden")
    return ";".join(rules) + ";"


# `<jargon term="...">term</jargon>` is a *content* marker: the interactive
# course player turns it into a tappable term with a definition, so it stays
# in the stored Course Document. A PDF has no such affordance, and Jinja's
# autoescaping would render the tag as visible text, so it is stripped here -
# at the render boundary only, leaving the document itself untouched.
#
# Deliberately tolerant: a malformed, unclosed or otherwise unexpected jargon
# tag is removed too, so nothing of this shape can reach a PDF again whatever
# the writer emits.
# Attribute-aware rather than a plain `[^>]*`: a term can legitimately
# contain ">" (for example term="Focus > Busyness"), and a naive match would
# stop at that character and leave the rest of the tag visible.
_JARGON_TAG = re.compile(
    r"""</?\s*jargon\b(?:[^>"']|"[^"]*"|'[^']*')*>""",
    re.IGNORECASE,
)


def strip_jargon(value: Any) -> Any:
    """Remove jargon tags from a string, or from every string nested inside a
    list or dict.

    Returns new objects rather than editing in place, so the caller's
    `Block.content` - which the course player still needs the tags in - is
    never mutated.
    """
    if isinstance(value, str):
        return _JARGON_TAG.sub("", value)
    if isinstance(value, list):
        return [strip_jargon(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_jargon(item) for key, item in value.items()}
    return value


def _paragraphs(block: Block) -> list[str]:
    text = str(block.content.get("text") or "")
    return [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]


def _image_src(block: Block, asset_prefix: str) -> str | None:
    path = block.content.get("path")
    if not path:
        return None
    return f"{asset_prefix}{path}"


def render_document_html(
    document: CourseDocument,
    template: CourseTemplate,
    *,
    asset_prefix: str = "../",
    language: str = "en",
    inline_fragments: dict[str, str] | None = None,
) -> str:
    theme = template.theme.model_dump(mode="json")
    theme.update(document.meta.theme or {})
    # Resolved values, so the stylesheet never has to express a fallback: an
    # uploaded template that specified no heading face still gets the body
    # face here rather than an empty font-family declaration.
    theme["heading_family"] = theme.get("heading_font_family") or theme["font_family"]
    theme["heading_ink"] = theme.get("heading_color") or theme["text_color"]
    theme["table_border"] = theme.get("table_border_color") or theme["border_color"]
    theme["table_header_bg"] = theme.get("table_header_background") or "transparent"
    theme.setdefault("paragraph_spacing_em", 0.4)
    # A DOCX header/footer usually contains placeholders ("{{COURSE_TITLE}} |
    # {{CHAPTER_TITLE}}"). Resolve the ones this renderer can actually know;
    # anything unsupported stays visible as written rather than becoming an
    # empty string (see docx_parser.placeholders.resolve).
    context = {
        "course_title": document.course_title,
        "audience": document.meta.audience or "",
        "language": language,
    }
    theme["header_line"] = resolve(theme.get("header_text") or "", context)
    theme["footer_line"] = resolve(theme.get("footer_text") or "", context)
    geometry = template.theme.geometry()
    inline_fragments = inline_fragments or {}

    pages: list[dict[str, Any]] = []
    for page in document.pages:
        blocks: list[dict[str, Any]] = []
        for block in page.blocks:
            # A concept_experience or toc block is a self-contained static
            # fragment (inline <style>, no script) - an <img src="foo.html">
            # cannot render it at all, so its own markup is inlined straight
            # into the page DOM instead (the caller supplies it, already read
            # from disk, so this function stays filesystem-agnostic). Every
            # other block keeps using image_src, unchanged.
            inline_html = None
            path = block.content.get("path")
            if block.content.get("kind") in ("concept_experience", "toc") and path in inline_fragments:
                # Generated fragments are built from the same AI text, so they
                # can carry the marker too.
                inline_html = strip_jargon(inline_fragments[path])
            blocks.append(
                {
                    "type": block.type.value,
                    # One choke point: every block type reads its text from
                    # `content` or `paragraphs`, so stripping both covers
                    # headings, tables, quizzes, lists, exercises and captions
                    # without touching each branch of the Jinja template.
                    "content": strip_jargon(block.content),
                    "css": _css(block, theme),
                    "paragraphs": strip_jargon(_paragraphs(block)),
                    "image_src": _image_src(block, asset_prefix),
                    "inline_html": inline_html,
                    "image_box": round(image_box_height(block), 2),
                    "panel_css": _code_panel_css(block)
                    if block.type is BlockType.CODE
                    else "",
                    "accent": block.style.accent_color or theme.get("accent_color"),
                    "accent_soft": theme.get("surface_color"),
                    "divider_color": block.style.border_color or theme.get("border_color"),
                }
            )
        pages.append(
            {
                "number": page.page_number,
                "width": page.size.width,
                "height": page.size.height,
                "background": page.background or theme.get("page_background", "#ffffff"),
                "blocks": blocks,
                "show_footer": page.kind == "content",
            }
        )

    jinja_template = _environment.get_template("course.html.j2")
    return jinja_template.render(
        document=document,
        pages=pages,
        theme=theme,
        language=language,
        page_width=geometry.width,
        page_height=geometry.height,
        margin_x=geometry.margin_x,
    )
