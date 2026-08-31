"""Course Document - the single source of truth.

Content, style and layout are deliberately separate so the future Next.js editor
can mutate any of the three independently. The PDF is only a projection of this.
"""

from __future__ import annotations

from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ids import block_id as new_block_id
from app.core.ids import utc_now_iso
from app.schemas.blocks import BlockType, validate_content

# A4 at 96 DPI - the same numbers are used by the CSS page box.
PAGE_WIDTH = 794.0
PAGE_HEIGHT = 1123.0
PAGE_MARGIN_X = 64.0
PAGE_MARGIN_TOP = 72.0
PAGE_MARGIN_BOTTOM = 88.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * PAGE_MARGIN_X
CONTENT_HEIGHT = PAGE_HEIGHT - PAGE_MARGIN_TOP - PAGE_MARGIN_BOTTOM


class BlockStyle(BaseModel):
    """Presentation only. Unknown keys are kept so templates can extend styling."""

    model_config = ConfigDict(extra="allow")

    font_family: str | None = None
    font_size: float | None = None
    font_weight: int | None = None
    line_height: float | None = None
    color: str | None = None
    background: str | None = None
    border_color: str | None = None
    border_radius: float | None = None
    border_width: float | None = None
    padding: float | None = None
    align: Literal["left", "center", "right", "justify"] | None = None
    italic: bool | None = None
    letter_spacing: float | None = None
    accent_color: str | None = None


class BlockLayout(BaseModel):
    model_config = ConfigDict(extra="allow")

    x: float = PAGE_MARGIN_X
    y: float = PAGE_MARGIN_TOP
    width: float = CONTENT_WIDTH
    height: float = 40.0
    z_index: int = 0


class BlockMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    chapter_id: str | None = None
    chapter_number: int | None = None
    section_key: str | None = None
    origin: str = "generated"  # generated | edited | inserted | system
    continued: bool = False  # part of a block split across pages
    updated_at: str | None = None


class Block(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=new_block_id)
    type: BlockType
    content: dict[str, Any] = Field(default_factory=dict)
    style: BlockStyle = Field(default_factory=BlockStyle)
    layout: BlockLayout = Field(default_factory=BlockLayout)
    meta: BlockMeta = Field(default_factory=BlockMeta)

    @model_validator(mode="after")
    def _validate_content(self) -> "Block":
        # Validate against the registered content model for this block type.
        object.__setattr__(self, "content", validate_content(self.type, self.content))
        return self

    def text_preview(self, limit: int = 400) -> str:
        parts: list[str] = []
        for key in ("title", "text", "instructions", "caption", "context", "purpose"):
            value = self.content.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        for key in ("items", "key_takeaways", "steps", "core_concepts"):
            value = self.content.get(key)
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
        joined = " ".join(parts)
        return joined[:limit]


class PageSize(BaseModel):
    model_config = ConfigDict(extra="allow")

    width: float = PAGE_WIDTH
    height: float = PAGE_HEIGHT


class Page(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = "page_1"
    page_number: int = 1
    size: PageSize = Field(default_factory=PageSize)
    background: str | None = None
    kind: str = "content"  # cover | toc | content
    blocks: list[Block] = Field(default_factory=list)


class DocumentMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    audience: str = ""
    template_name: str = ""
    chapter_ids: list[str] = Field(default_factory=list)
    theme: dict[str, Any] = Field(default_factory=dict)
    generated_with: str = ""


class CourseDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_id: str
    course_id: str
    course_title: str
    template_id: str
    version: int = 1
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    meta: DocumentMeta = Field(default_factory=DocumentMeta)
    pages: list[Page] = Field(default_factory=list)

    # --- lookups ----------------------------------------------------------
    def iter_blocks(self) -> Iterator[tuple[Page, Block]]:
        for page in self.pages:
            for block in page.blocks:
                yield page, block

    def find_block(self, block_id: str) -> tuple[Page, Block] | None:
        for page, block in self.iter_blocks():
            if block.id == block_id:
                return page, block
        return None

    def block_ids(self) -> list[str]:
        return [block.id for _, block in self.iter_blocks()]

    def blocks_of_type(self, block_type: BlockType) -> list[Block]:
        return [block for _, block in self.iter_blocks() if block.type == block_type]

    def page_by_number(self, page_number: int) -> Page | None:
        return next((p for p in self.pages if p.page_number == page_number), None)

    def touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now_iso()
