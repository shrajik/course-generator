"""AI edit patches.

The editor agent must return operations, never a whole document. Every patch is
validated by Pydantic before it is applied.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.blocks import BlockType

_AI = ConfigDict(extra="ignore")


class NewBlock(BaseModel):
    """Payload for insert_block / replace_block."""

    model_config = _AI

    type: BlockType
    content: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)


class UpdateBlockOp(BaseModel):
    model_config = _AI

    type: Literal["update_block"] = "update_block"
    block_id: str
    content: dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    layout: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "UpdateBlockOp":
        if self.content is None and self.style is None and self.layout is None:
            raise ValueError("update_block requires at least one of content/style/layout")
        return self


class DeleteBlockOp(BaseModel):
    model_config = _AI

    type: Literal["delete_block"] = "delete_block"
    block_id: str


class InsertBlockOp(BaseModel):
    model_config = _AI

    type: Literal["insert_block"] = "insert_block"
    block: NewBlock
    after_block_id: str | None = None
    before_block_id: str | None = None
    page_number: int | None = None

    @model_validator(mode="after")
    def _anchor_required(self) -> "InsertBlockOp":
        if not any([self.after_block_id, self.before_block_id, self.page_number]):
            raise ValueError(
                "insert_block requires after_block_id, before_block_id or page_number"
            )
        return self


class ReplaceBlockOp(BaseModel):
    model_config = _AI

    type: Literal["replace_block"] = "replace_block"
    block_id: str
    block: NewBlock


class ReplaceImageOp(BaseModel):
    model_config = _AI

    type: Literal["replace_image"] = "replace_image"
    block_id: str
    prompt: str
    caption: str | None = None
    alt: str | None = None


class UpdateStyleOp(BaseModel):
    model_config = _AI

    type: Literal["update_style"] = "update_style"
    block_id: str
    style: dict[str, Any]


PatchOperation = Annotated[
    Union[
        UpdateBlockOp,
        DeleteBlockOp,
        InsertBlockOp,
        ReplaceBlockOp,
        ReplaceImageOp,
        UpdateStyleOp,
    ],
    Field(discriminator="type"),
]

SUPPORTED_OPERATIONS = (
    "update_block",
    "delete_block",
    "insert_block",
    "replace_block",
    "replace_image",
    "update_style",
)


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    operations: list[PatchOperation] = Field(default_factory=list)
    reasoning: str = ""
    notes: str = ""


class AiEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_block_ids: list[str] = Field(min_length=1)
    instruction: str = Field(min_length=1, max_length=4000)
    apply: bool = True
    regenerate_images: bool = True


class AiEditResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_id: str
    version: int
    applied: bool
    patch: DocumentPatch
    applied_operations: list[str] = Field(default_factory=list)
    rejected_operations: list[dict[str, Any]] = Field(default_factory=list)
    pages: int = 0
