"""Apply validated patches to a Course Document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.ids import block_id as new_block_id
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.schemas.blocks import BlockType, merge_content, validate_content
from app.schemas.document import Block, BlockLayout, BlockMeta, BlockStyle, CourseDocument
from app.schemas.patch import DocumentPatch
from app.schemas.template import CourseTemplate

log = get_logger(__name__)


@dataclass
class PatchResult:
    applied: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    images_to_generate: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _reject(result: PatchResult, operation: Any, reason: str) -> None:
    log.warning("Rejected %s: %s", getattr(operation, "type", "?"), reason)
    result.rejected.append(
        {
            "type": getattr(operation, "type", "unknown"),
            "block_id": getattr(operation, "block_id", None),
            "reason": reason,
        }
    )


def _locate(document: CourseDocument, block_id: str) -> tuple[int, int] | None:
    for page_index, page in enumerate(document.pages):
        for block_index, block in enumerate(page.blocks):
            if block.id == block_id:
                return page_index, block_index
    return None


def _new_block_from_payload(
    payload: Any, template: CourseTemplate, reference: Block | None
) -> Block:
    block_type = BlockType(payload.type)
    style = {**template.style_for(block_type), **(payload.style or {})}
    meta = BlockMeta(origin="inserted", updated_at=utc_now_iso())
    if reference is not None:
        meta.chapter_id = reference.meta.chapter_id
        meta.chapter_number = reference.meta.chapter_number
        meta.section_key = reference.meta.section_key
    return Block(
        id=new_block_id(),
        type=block_type,
        content=validate_content(block_type, payload.content or {}),
        style=BlockStyle.model_validate(style),
        layout=BlockLayout(),
        meta=meta,
    )


def apply_patch(
    document: CourseDocument,
    patch: DocumentPatch,
    template: CourseTemplate,
    *,
    reflow: bool = True,
) -> PatchResult:
    """Apply every operation that is valid; report the rest. Mutates `document`."""
    result = PatchResult()

    for operation in patch.operations:
        op_type = operation.type
        try:
            if op_type == "update_block":
                located = _locate(document, operation.block_id)
                if located is None:
                    _reject(result, operation, "block not found")
                    continue
                page, index = located
                block = document.pages[page].blocks[index]
                if operation.content is not None:
                    block.content = merge_content(block.type, block.content, operation.content)
                if operation.style is not None:
                    block.style = BlockStyle.model_validate(
                        {**block.style.model_dump(exclude_none=True), **operation.style}
                    )
                if operation.layout is not None:
                    block.layout = BlockLayout.model_validate(
                        {**block.layout.model_dump(), **operation.layout}
                    )
                block.meta.origin = "edited"
                block.meta.updated_at = utc_now_iso()
                result.applied.append(f"update_block:{block.id}")

            elif op_type == "update_style":
                located = _locate(document, operation.block_id)
                if located is None:
                    _reject(result, operation, "block not found")
                    continue
                page, index = located
                block = document.pages[page].blocks[index]
                block.style = BlockStyle.model_validate(
                    {**block.style.model_dump(exclude_none=True), **operation.style}
                )
                block.meta.updated_at = utc_now_iso()
                result.applied.append(f"update_style:{block.id}")

            elif op_type == "delete_block":
                located = _locate(document, operation.block_id)
                if located is None:
                    _reject(result, operation, "block not found")
                    continue
                page, index = located
                document.pages[page].blocks.pop(index)
                result.applied.append(f"delete_block:{operation.block_id}")

            elif op_type == "replace_block":
                located = _locate(document, operation.block_id)
                if located is None:
                    _reject(result, operation, "block not found")
                    continue
                page, index = located
                old = document.pages[page].blocks[index]
                replacement = _new_block_from_payload(operation.block, template, old)
                replacement.id = old.id  # keep the id stable for the editor UI
                replacement.meta.origin = "edited"
                document.pages[page].blocks[index] = replacement
                if replacement.type is BlockType.IMAGE and not replacement.content.get("path"):
                    result.images_to_generate.append(replacement.id)
                result.applied.append(f"replace_block:{replacement.id}")

            elif op_type == "insert_block":
                anchor_id = operation.after_block_id or operation.before_block_id
                if anchor_id:
                    located = _locate(document, anchor_id)
                    if located is None:
                        _reject(result, operation, "anchor block not found")
                        continue
                    page, index = located
                    reference = document.pages[page].blocks[index]
                    position = index + 1 if operation.after_block_id else index
                else:
                    target = document.page_by_number(operation.page_number or 0)
                    if target is None:
                        _reject(result, operation, "page_number not found")
                        continue
                    page = document.pages.index(target)
                    reference = target.blocks[-1] if target.blocks else None
                    position = len(target.blocks)
                block = _new_block_from_payload(operation.block, template, reference)
                document.pages[page].blocks.insert(position, block)
                if block.type is BlockType.IMAGE and not block.content.get("path"):
                    result.images_to_generate.append(block.id)
                result.applied.append(f"insert_block:{block.id}")

            elif op_type == "replace_image":
                located = _locate(document, operation.block_id)
                if located is None:
                    _reject(result, operation, "block not found")
                    continue
                page, index = located
                block = document.pages[page].blocks[index]
                if block.type is not BlockType.IMAGE:
                    _reject(result, operation, f"block is {block.type.value}, not an image")
                    continue
                update = {
                    "prompt": operation.prompt,
                    "path": None,
                    "generated": False,
                    "error": None,
                }
                if operation.caption is not None:
                    update["caption"] = operation.caption
                if operation.alt is not None:
                    update["alt"] = operation.alt
                block.content = merge_content(block.type, block.content, update)
                block.meta.origin = "edited"
                block.meta.updated_at = utc_now_iso()
                result.images_to_generate.append(block.id)
                result.applied.append(f"replace_image:{block.id}")

            else:  # pragma: no cover - the union prevents this
                _reject(result, operation, "unsupported operation")

        except Exception as exc:  # noqa: BLE001 - one bad op must not kill the patch
            _reject(result, operation, f"{type(exc).__name__}: {exc}")

    if result.changed:
        if reflow:
            reflow_document(document, template)
        document.touch()
    return result
