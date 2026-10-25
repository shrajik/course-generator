"""Template configuration schema.

A template is content structure + presentation rules, plus (optionally) a
`TemplateBlueprint` of writing and visual rules parsed from an uploaded file.
Every template shares one AI pipeline; the writer simply receives the selected
configuration.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.blocks import BlockType
from app.schemas.template_blueprint import TemplateBlueprint
from app.schemas.document import (
    PAGE_HEIGHT,
    PAGE_MARGIN_BOTTOM,
    PAGE_MARGIN_TOP,
    PAGE_MARGIN_X,
    PAGE_WIDTH,
)


class TemplateSection(BaseModel):
    """One expected part of a chapter."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    required: bool = False
    block_types: list[BlockType] = Field(default_factory=list)
    guidance: str = ""
    max_blocks: int = 3


class PageGeometry(BaseModel):
    """Physical page box, in CSS pixels at 96 dpi.

    Defaults are the constants the layout engine has always used
    (app.schemas.document), so a template that specifies no geometry lays out
    and renders byte-identically to how it did before templates could carry
    geometry at all.
    """

    model_config = ConfigDict(extra="forbid")

    width: float = PAGE_WIDTH
    height: float = PAGE_HEIGHT
    margin_x: float = PAGE_MARGIN_X
    margin_top: float = PAGE_MARGIN_TOP
    margin_bottom: float = PAGE_MARGIN_BOTTOM

    @property
    def content_width(self) -> float:
        return self.width - 2 * self.margin_x

    @property
    def content_height(self) -> float:
        return self.height - self.margin_top - self.margin_bottom


class TemplateTheme(BaseModel):
    model_config = ConfigDict(extra="allow")

    font_family: str = "Inter, Segoe UI, Helvetica, Arial, sans-serif"
    mono_font_family: str = "JetBrains Mono, Consolas, Menlo, monospace"
    text_color: str = "#111827"
    muted_color: str = "#4b5563"
    accent_color: str = "#2563eb"
    accent_soft: str = "#eff6ff"
    page_background: str = "#ffffff"
    surface_color: str = "#f8fafc"
    border_color: str = "#e5e7eb"
    base_font_size: float = 15.0
    base_line_height: float = 1.6

    # --- extracted-template extensions ------------------------------------
    # Every field below defaults to "" / None / the existing behaviour, so
    # technical_v1.json and non_technical_v1.json - which declare none of
    # them - keep producing exactly the styling they produced before. They
    # exist so a parsed DOCX template can carry what the renderer can
    # actually honour (see app.course.templates.docx_parser.style).
    heading_font_family: str = ""       # falls back to font_family
    heading_color: str = ""             # falls back to text_color
    table_header_background: str = ""   # falls back to surface_color
    table_border_color: str = ""        # falls back to border_color
    paragraph_spacing_em: float = 0.4

    # Page furniture, drawn on every page by the Jinja page markup
    # (app/render/templates/course.html.j2) and mirrored by the editor.
    header_text: str = ""
    footer_text: str = ""
    # None = not specified: the footer keeps its page number, as it always
    # has. False is an explicit "no page numbers" (a DOCX footer without a
    # PAGE field); True an explicit yes.
    show_page_numbers: bool | None = None

    # None = "use the layout engine's built-in page box".
    page: PageGeometry | None = None

    def geometry(self) -> PageGeometry:
        """The page box in force - never None, so callers need no fallback."""
        return self.page or PageGeometry()

    def heading_family(self) -> str:
        return self.heading_font_family or self.font_family

    def heading_ink(self) -> str:
        return self.heading_color or self.text_color


class CourseTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    kind: str  # technical | non_technical
    name: str
    description: str = ""
    sections: list[TemplateSection] = Field(default_factory=list)
    allowed_block_types: list[BlockType] = Field(default_factory=list)
    required_block_types: list[BlockType] = Field(default_factory=list)
    writer_guidance: str = ""
    research_guidance: str = ""
    review_focus: list[str] = Field(default_factory=list)
    image_guidance: str = ""
    theme: TemplateTheme = Field(default_factory=TemplateTheme)
    block_styles: dict[BlockType, dict[str, Any]] = Field(default_factory=dict)
    # Writing/visual rules from an uploaded template. None for every template
    # that predates blueprints, which keeps their prompts unchanged.
    blueprint: TemplateBlueprint | None = None

    # --- helpers ---------------------------------------------------------
    def section_labels(self) -> list[str]:
        return [s.label for s in self.sections]

    def required_sections(self) -> list[TemplateSection]:
        return [s for s in self.sections if s.required]

    def style_for(self, block_type: BlockType) -> dict[str, Any]:
        return dict(self.block_styles.get(block_type, {}))

    def is_allowed(self, block_type: BlockType) -> bool:
        return not self.allowed_block_types or block_type in self.allowed_block_types

    def outline_for_prompt(self) -> str:
        lines = []
        for section in self.sections:
            flag = "REQUIRED" if section.required else "optional"
            types = ", ".join(bt.value for bt in section.block_types) or "any"
            lines.append(
                f"- {section.label} [{flag}] (key: {section.key}; "
                f"block types: {types}) - {section.guidance}"
            )
        return "\n".join(lines)
