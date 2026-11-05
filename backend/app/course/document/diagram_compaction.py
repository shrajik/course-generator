"""Post-pagination diagram compaction.

`shrink_image_width_to_fit` (app.course.document.layout) already narrows an
oversized flow_chart diagram's DISPLAY width when it doesn't fit the page
slot it must share with its lead-in text - but that's a CSS-level, display-
only lever, deliberately bounded (_MIN_DIAGRAM_SHRINK_WIDTH_FRACTION) to
protect legibility, and on its own it doesn't always recover enough page
space: the group still moves whole to a fresh page, leaving the ORIGINAL
page's own remainder wasted (confirmed via a real generated course, up to
750px/78% of a page).

This module is the generation-time complement, run once after the normal
reflow: for a page that's STILL left with a large trailing gap, find the
flow_chart diagram that got bridged away from it (via `flow_blocks`'s own
keep-with-lead-in-text rule), re-render it from its own ALREADY-STORED
DiagramSpec - no new AI call - at a tighter vertical spine spacing
(`render_diagram_svg_fitting`, app.render.diagram_renderer) sized to fit
back into that page's own remaining space, save it as the block's new
asset, and re-paginate. Font size and node-box size are never touched -
only the breathing room between consecutive flow_chart steps - so a
compacted diagram reads exactly as clearly as the original, just tighter.

Combines with (never replaces) the width-shrink: a diagram compacted here
first needs a much smaller width-shrink, if any, to finish fitting - two
independent, additive, legibility-safe levers rather than one aggressive
one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.course.document.layout import (
    BLOCK_GAP,
    CONTENT_HEIGHT,
    PAGE_MARGIN_TOP,
    SECTION_GAP,
    estimate_height,
)
from app.render.diagram_renderer import render_diagram_svg_fitting
from app.schemas.blocks import BlockType, merge_content
from app.schemas.diagram import DiagramSpec
from app.schemas.document import CourseDocument, Page
from app.schemas.template import CourseTemplate
from app.services.storage_service import StorageService

log = get_logger(__name__)

MAX_COMPACTION_PASSES = 2
# Matches app.services.pdf_spacing_qa.MAX_TRAILING_BLANK_PCT - the same bar
# a page is judged against, so this module's job is "make the QA gate pass",
# not an independently-chosen number.
MAX_TRAILING_BLANK_PCT = 15.0
# A little real headroom on top of the exact computed budget, not a
# razor's-edge fit - estimate_height's own "errs high, min-height
# tolerates it" margin applies to everything downstream of this module too.
_FIT_MARGIN = 20.0


class CompactionReport(BaseModel):
    passes_run: int = 0
    compacted: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)


def _trailing_blank(page: Page) -> float:
    bottom = PAGE_MARGIN_TOP + CONTENT_HEIGHT
    if not page.blocks:
        return CONTENT_HEIGHT
    last_bottom = max(b.layout.y + b.layout.height for b in page.blocks)
    return max(0.0, bottom - last_bottom)


def _find_compactable_diagram(next_page: Page, attempted: set[str]):
    return next(
        (
            block for block in next_page.blocks
            if block.type is BlockType.IMAGE
            and block.content.get("kind") == "diagram"
            and (block.content.get("diagram_kind") or "") in ("flow_chart", "process", "cycle")
            and block.content.get("spec")
            and block.id not in attempted
        ),
        None,
    )


def _gap_before(block) -> float:
    return SECTION_GAP if block.type is BlockType.HEADING else BLOCK_GAP


def _diagram_height_budget(page: Page, next_page: Page, target_block) -> float:
    """How much height `target_block` itself could use if the WHOLE group
    that was bridged away WITH it (every block on `next_page` before it -
    its lead-in heading/paragraph) were placed back at the end of `page`
    instead. Mirrors flow_blocks's own _pending_visual_run/
    try_shrink_trailing_visual math exactly (app.course.document.layout) -
    reconstructed after the fact, since that live decision's own numbers
    aren't kept once reflow finishes. NOT simply "how much blank space is
    on `page`": a confirmed bug in an earlier version of this function used
    that raw number directly, which is almost always far bigger than what
    the diagram actually needs to recover (the page's blank space already
    reflects everything ELSE that didn't fit there either), so compaction
    never found anything worth doing.
    """
    index = next_page.blocks.index(target_block)
    budget = _trailing_blank(page)
    for sibling in next_page.blocks[: index + 1]:
        budget -= _gap_before(sibling)
        if sibling is target_block:
            break
        budget -= estimate_height(sibling)
    return budget


async def compact_diagrams_for_spacing(
    document: CourseDocument,
    template: CourseTemplate,
    *,
    storage: StorageService,
    max_passes: int = MAX_COMPACTION_PASSES,
) -> CompactionReport:
    """Mutates `document` in place (re-paginating as needed); does not save
    it or write anything beyond the new SVG asset file(s) - the caller's own
    existing save step persists the result, same contract as
    `audit_and_repair_visuals`."""
    report = CompactionReport()
    attempted_block_ids: set[str] = set()

    for pass_number in range(1, max_passes + 1):
        report.passes_run = pass_number
        content_pages = [page for page in document.pages if page.kind == "content" and page.blocks]
        any_compacted = False

        for index, page in enumerate(content_pages):
            blank_pct = _trailing_blank(page) / CONTENT_HEIGHT * 100
            if blank_pct <= MAX_TRAILING_BLANK_PCT:
                continue
            next_page = content_pages[index + 1] if index + 1 < len(content_pages) else None
            if next_page is None:
                continue  # the document's own last page - expected to run short

            target_block = _find_compactable_diagram(next_page, attempted_block_ids)
            if target_block is None:
                continue
            attempted_block_ids.add(target_block.id)

            try:
                spec = DiagramSpec.model_validate(target_block.content["spec"])
            except Exception as exc:  # noqa: BLE001 - a bad stored spec skips, never aborts the pass
                report.skipped.append({"block_id": target_block.id, "reason": f"invalid stored spec: {exc}"})
                continue

            current_height = float(target_block.content.get("height") or 0.0)
            budget = _diagram_height_budget(page, next_page, target_block) - _FIT_MARGIN
            if budget < CONTENT_HEIGHT * 0.2:
                # Not a diagram-sizing problem at all - the group's own
                # lead-in text already consumes nearly the whole budget, so
                # no amount of compacting the diagram would meaningfully
                # help. Leave it to the existing width-shrink / normal
                # pagination instead of compacting into an unreasonably
                # short diagram just to chase a number.
                report.skipped.append({
                    "block_id": target_block.id,
                    "reason": f"available budget too small to compact into ({budget:.0f}px)",
                })
                continue
            svg_bytes, width, height = render_diagram_svg_fitting(
                spec, template.theme, max_height=budget
            )
            if height >= current_height:
                report.skipped.append({
                    "block_id": target_block.id,
                    "reason": "compaction made no real difference",
                })
                continue

            relative = storage.save_asset(document.course_id, svg_bytes, extension="svg")
            target_block.content = merge_content(
                target_block.type,
                target_block.content,
                {
                    "path": relative,
                    "asset_id": relative.rsplit("/", 1)[-1],
                    "width": width,
                    "height": height,
                },
            )
            any_compacted = True
            report.compacted.append({
                "block_id": target_block.id,
                "page_freed": page.page_number,
                "old_height": current_height,
                "new_height": height,
            })

        if not any_compacted:
            break
        reflow_document(document, template)

    return report
