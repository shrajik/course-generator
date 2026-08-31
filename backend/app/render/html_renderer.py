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
from app.schemas.document import PAGE_HEIGHT, PAGE_MARGIN_X, PAGE_WIDTH, Block, CourseDocument
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
) -> str:
    theme = template.theme.model_dump(mode="json")
    theme.update(document.meta.theme or {})

    pages: list[dict[str, Any]] = []
    for page in document.pages:
        blocks: list[dict[str, Any]] = []
        for block in page.blocks:
            blocks.append(
                {
                    "type": block.type.value,
                    "content": block.content,
                    "css": _css(block, theme),
                    "paragraphs": _paragraphs(block),
                    "image_src": _image_src(block, asset_prefix),
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
        page_width=PAGE_WIDTH,
        page_height=PAGE_HEIGHT,
        margin_x=PAGE_MARGIN_X,
    )
