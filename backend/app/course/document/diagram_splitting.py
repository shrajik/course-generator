"""Inserts the later parts of a diagram `DiagramService` already split.

`DiagramService.generate_for_block` (app.services.diagram_service) is the
only place that KNOWS a diagram needed splitting (`_linear_chain_split`'s
own docstring has the full "why" and "when this is provably safe") - but it
has no access to the document/page to insert new sibling blocks itself, the
same reason `app.course.document.visual_repair` exists as its own
document-level pass rather than living inside `ImageService`. This module is
that missing half: for every block carrying a `pending_diagram_splits`
payload (each later part's spec plus its ALREADY-RENDERED SVG bytes, in
order - no new render, no new AI call, every part was validated together
before `DiagramService` ever committed to the split), save that SVG as its
own asset, build a new sibling `Block` right after the original (and after
each previously-inserted part, so order on the page matches part order),
clear the transient payload off the original block, and re-paginate once at
the end - the same "mutate document in place, caller saves it" contract
`compact_diagrams_for_spacing` already keeps.
"""

from __future__ import annotations

import base64

from pydantic import BaseModel, Field

from app.core.ids import block_id as new_block_id
from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.schemas.blocks import BlockType, merge_content
from app.schemas.diagram import DiagramSpec
from app.schemas.document import Block, BlockLayout, BlockMeta, CourseDocument
from app.schemas.template import CourseTemplate
from app.services.storage_service import StorageService

log = get_logger(__name__)


class SplitReport(BaseModel):
    inserted: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)


def _build_split_part_block(*, original: Block, spec: DiagramSpec, path: str, width: int, height: int) -> Block:
    content = {
        **{
            key: original.content.get(key)
            for key in ("kind", "diagram_kind", "purpose", "caption")
            if key in original.content
        },
        "prompt": original.content.get("prompt", ""),
        "generated": True,
        "error": None,
        "path": path,
        "asset_id": path.rsplit("/", 1)[-1],
        "width": width,
        "height": height,
        "spec": spec.model_dump(mode="json"),
        # Never inherits the first part's own findings - they were measured
        # against the UNSPLIT spec and no longer describe this part alone.
        "visual_qa_findings": [],
    }
    return Block(
        id=new_block_id(),
        type=BlockType.IMAGE,
        content=content,
        style=original.style.model_copy(deep=True),
        layout=BlockLayout(),
        meta=BlockMeta(
            chapter_id=original.meta.chapter_id,
            chapter_number=original.meta.chapter_number,
            section_key=original.meta.section_key,
            origin="inserted",
        ),
    )


def insert_split_diagrams(
    document: CourseDocument, template: CourseTemplate, *, storage: StorageService,
) -> SplitReport:
    """Mutates `document` in place (re-paginating if anything was inserted);
    does not save it or write anything beyond the new SVG asset file(s) -
    the caller's own existing save step persists the result, same contract
    as `compact_diagrams_for_spacing`/`audit_and_repair_visuals`."""
    report = SplitReport()
    any_inserted = False

    for page in document.pages:
        if page.kind != "content":
            continue
        # Snapshot the block list before mutating it - inserting while
        # iterating `page.blocks` directly would skip/duplicate entries.
        for original in list(page.blocks):
            payloads = original.content.get("pending_diagram_splits")
            if not payloads:
                continue
            insertion_index = page.blocks.index(original) + 1
            for payload in payloads:
                try:
                    spec = DiagramSpec.model_validate(payload["spec"])
                    svg_bytes = base64.b64decode(payload["svg_base64"])
                    width = int(payload["width"])
                    height = int(payload["height"])
                except Exception as exc:  # noqa: BLE001 - a malformed part skips, never aborts the pass
                    report.skipped.append({"block_id": original.id, "reason": f"invalid split payload: {exc}"})
                    continue

                relative = storage.save_asset(document.course_id, svg_bytes, extension="svg")
                new_block = _build_split_part_block(
                    original=original, spec=spec, path=relative, width=width, height=height,
                )
                page.blocks.insert(insertion_index, new_block)
                insertion_index += 1
                any_inserted = True
                report.inserted.append({
                    "block_id": new_block.id,
                    "after_block_id": original.id,
                    "page": page.page_number,
                    "kind": spec.normalised_kind(),
                })
            original.content = merge_content(original.type, original.content, {"pending_diagram_splits": []})

    if any_inserted:
        reflow_document(document, template)
    return report
