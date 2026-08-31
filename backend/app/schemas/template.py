"""Template configuration schema.

A template is *only* content structure + presentation rules. Both templates share
one AI pipeline; the writer simply receives the selected configuration.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.blocks import BlockType


class TemplateSection(BaseModel):
    """One expected part of a chapter."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    required: bool = False
    block_types: list[BlockType] = Field(default_factory=list)
    guidance: str = ""
    max_blocks: int = 3


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
