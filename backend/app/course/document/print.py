"""Make a document safe to print: every page fits its sheet.

A stored document can legitimately contain a page taller than a sheet. The
layout engine grows a page rather than clip a block it cannot split (see the
"Fresh page, unsplittable and taller than a page" fallback in
`layout.flow_blocks`), and the editor grows a page when a code cell's output
pushes the content below it down (`growBlock` in the frontend). That is right
for editing - one cell stays one cell - but a PDF sheet has a fixed size, so
content past its bottom edge is cut off or spills onto an unrelated sheet.

`paginate_for_print` is the step that closes that gap. It works on a COPY, so
the stored document - and what the editor shows - is never altered, and it only
touches the pages that actually overflow: a document where everything fits is
returned as-is, byte for byte.

For an overflowing page the blocks are laid out again with the same engine that
paginated the document in the first place, with one difference: a code cell
too tall for any page may be split between lines (`split_code_cell`). Cells
that merely do not fit the *remaining* space are moved whole to the next page.
"""

from __future__ import annotations

from app.core.ids import page_id
from app.course.document.builder import _page_size_for
from app.course.document.layout import flow_blocks
from app.course.document.layout_validator import page_overflow
from app.core.logging import get_logger
from app.schemas.blocks import BlockType
from app.schemas.document import CourseDocument, Page
from app.schemas.template import CourseTemplate, PageGeometry

log = get_logger(__name__)

# Estimates err high by design; a couple of px of drift is not an overflow.
_TOLERANCE = 2.0


def _needs_repagination(page: Page, sheet: PageGeometry) -> bool:
    if page.kind != "content":
        return False  # cover and contents pages are generated at sheet size
    if page.size.height > sheet.height + _TOLERANCE:
        return True
    return page_overflow(page) > _TOLERANCE


def _reading_order(page: Page):
    # A section_intro image shares its paragraph's y; the pair is only
    # re-recognised when the image comes first.
    return sorted(
        page.blocks,
        key=lambda b: (b.layout.y, 0 if b.type is BlockType.IMAGE else 1, b.layout.x),
    )


def paginate_for_print(document: CourseDocument, template: CourseTemplate) -> CourseDocument:
    sheet = template.theme.geometry()
    if not any(_needs_repagination(page, sheet) for page in document.pages):
        return document

    printable = document.model_copy(deep=True)
    pages: list[Page] = []
    for page in printable.pages:
        if not _needs_repagination(page, sheet):
            pages.append(page)
            continue

        laid_out = flow_blocks(
            _reading_order(page), geometry=sheet, split_oversized_cells=True
        )
        log.info(
            "PRINT_REPAGINATED page=%s sheets=%s height=%s",
            page.page_number,
            len(laid_out),
            page.size.height,
        )
        for blocks in laid_out:
            pages.append(
                page.model_copy(
                    update={
                        "size": _page_size_for(blocks, sheet),
                        "blocks": blocks,
                    }
                )
            )

    # Ids and numbers follow the new order.
    for number, page in enumerate(pages, start=1):
        page.page_number = number
        page.id = page_id(number)
    printable.pages = pages
    return printable
