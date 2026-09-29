"""Post-layout validation.

`flow_blocks` (app.course.document.layout) computes every block's absolute
`layout.x/y/width/height` and `_page_size_for` (app.course.document.builder)
sizes each `Page` to actually contain them - but nothing ever checked the
RESULT against what the renderer will actually do with it. This module is
that check: given an already-paginated document (or a single page's already-
laid-out blocks), report whether it is structurally sound - no block
clipped past its page's own bottom edge, no two blocks overlapping, no
page that's empty or reduced to an orphaned heading fragment - so a caller
(the repair pipeline, a test, a future editor action) can tell the
difference between "the layout math ran" and "the layout math produced
something safe to render."

This never re-derives or fixes layout; it only inspects the output flow_blocks
already produced. `.page` in the rendered HTML is `position: relative;
overflow: hidden` at a fixed size taken from `Page.size` (see
app.render.html_renderer / course.html.j2) - a block whose bottom edge
exceeds that box is not "grown by the CSS", it is silently invisible, which
is exactly the "clipped or missing content" failure mode section 6 of the
page-splitting brief asks to catch.
"""

from __future__ import annotations

import re

from app.schemas.document import PAGE_MARGIN_BOTTOM, Block, CourseDocument, Page

# `_clone` (layout.py) gives a split block's tail fragment an id of
# `{original_id}_c1` (chained again as `{original_id}_c1_c1` if that
# fragment itself later needs a further split) - always appended at the
# very END of the id. A block's own random suffix (see app.core.ids.block_id,
# 12 lowercase hex chars) can coincidentally CONTAIN the substring "_c"
# without being a split fragment at all (e.g. "block_c4b4cdb94513" - the hex
# suffix happens to start with "c"), so stripping it with a plain
# `.split("_c")` is wrong: this must only ever match a real `_c1` suffix
# anchored to the end of the string.
_SPLIT_SUFFIX_RE = re.compile(r"(?:_c1)+$")


def original_id(block_id: str) -> str:
    """The id of the block this one was split from, or `block_id` itself if
    it was never split."""
    return _SPLIT_SUFFIX_RE.sub("", block_id)

# Estimated heights err high by design (see layout.py's own `_SAFETY`
# multiplier) but are still estimates, not a text-shaping engine - a couple
# of px of rounding drift is expected and harmless. Flag only a genuinely
# meaningful overflow/overlap, not sub-pixel noise.
_TOLERANCE = 2.0

# A content page holding less than this much total rendered height is
# suspiciously thin - typically a lone orphaned heading left behind by a
# split, not a deliberate short page (see `MIN_ORPHAN_SPACE` in layout.py,
# which already tries to prevent this at flow time; this is the after-the-
# fact check that catches it if it still happens).
MIN_PAGE_CONTENT_HEIGHT = 40.0


def _bottom(block: Block) -> float:
    return block.layout.y + block.layout.height


def page_overflow(page: Page) -> float:
    """How far the lowest block's bottom edge extends past `page`'s own
    declared content area, in px. 0.0 (or negative) means every block fits
    inside this specific page's own size - which may be taller than the
    document default (see `_page_size_for`)."""
    if not page.blocks:
        return 0.0
    limit = page.size.height - PAGE_MARGIN_BOTTOM
    return max((_bottom(b) - limit for b in page.blocks), default=0.0)


def overlapping_pairs(page_blocks: list[Block]) -> list[tuple[str, str]]:
    """Block id pairs whose vertical extents genuinely intersect.
    `flow_blocks` stacks blocks sequentially top-down, so this should never
    happen by construction - this exists to CATCH a regression in that
    invariant, not to fix one."""
    ordered = sorted(page_blocks, key=lambda b: b.layout.y)
    pairs = []
    for a, b in zip(ordered, ordered[1:]):
        if _bottom(a) - _TOLERANCE > b.layout.y:
            pairs.append((a.id, b.id))
    return pairs


def validate_page(page: Page) -> list[str]:
    """Plain-English validation findings for one already-paginated page.
    Empty list = the page is structurally sound. Only ever called on
    content pages that have blocks - an empty page or a non-content page
    (cover/toc) is a caller's decision, not a finding here."""
    findings: list[str] = []

    overflow = page_overflow(page)
    if overflow > _TOLERANCE:
        findings.append(f"content overflows page {page.page_number} by {overflow:.0f}px")

    for a_id, b_id in overlapping_pairs(page.blocks):
        findings.append(f"blocks '{a_id}' and '{b_id}' overlap on page {page.page_number}")

    total_height = sum(b.layout.height for b in page.blocks)
    if 0 < total_height < MIN_PAGE_CONTENT_HEIGHT:
        findings.append(
            f"page {page.page_number} holds almost no content ({total_height:.0f}px) - "
            "likely an orphaned fragment left behind by a split"
        )

    return findings


# Key used in `validate_document`'s result for a finding that isn't about
# any one page - a cross-page ordering problem, which by definition spans
# more than one page number.
DOCUMENT_LEVEL = 0


def validate_document(
    document: CourseDocument, *, original_block_order: list[str] | None = None
) -> dict[int, list[str]]:
    """Every content page's findings, keyed by page_number - only pages
    with at least one finding are included. `original_block_order`, when
    given, additionally checks that content wasn't reordered relative to
    the full document's pre-layout sequence, checked GLOBALLY across every
    content page (not just within one) since a real reordering bug could
    just as easily swap material across a page boundary as within one page.
    Ids not present in `original_block_order` (a block inserted after the
    fact, e.g. by visual repair) are ignored by this check entirely - it is
    never a finding for a document to have gained new content, only for its
    original content to have been shuffled. Any mismatch is reported once,
    under `DOCUMENT_LEVEL`, rather than attributed to a single page number."""
    problems: dict[int, list[str]] = {}
    for page in document.pages:
        if page.kind != "content" or not page.blocks:
            continue
        findings = validate_page(page)
        if findings:
            problems[page.page_number] = findings

    if original_block_order is not None:
        known = set(original_block_order)
        rendered_order = [
            original_id(block.id)
            for page in document.pages
            if page.kind == "content"
            for block in page.blocks
        ]
        # A split block's head and tail fragments map to the same
        # `original_id` and are always adjacent (the tail is placed
        # immediately after the head, on the very next page - see
        # flow_blocks) - collapse those consecutive repeats so a
        # legitimate split isn't mistaken for a duplicate/reordering.
        rendered_known_order = []
        for bid in rendered_order:
            if bid not in known:
                continue
            if rendered_known_order and rendered_known_order[-1] == bid:
                continue
            rendered_known_order.append(bid)
        expected_order = [bid for bid in original_block_order if bid in set(rendered_known_order)]
        if rendered_known_order != expected_order:
            problems.setdefault(DOCUMENT_LEVEL, []).append(
                "original content was reordered relative to its pre-layout sequence"
            )

    return problems


def content_integrity_report(
    original_block_ids: list[str], document: CourseDocument
) -> dict[str, list[str]]:
    """Confirms every original block survived pagination exactly once - a
    split block (`{id}` head + `{id}_c1` tail, chained as `{id}_c1_c1` if
    the tail itself is split again) counts as one occurrence of its
    original id (see `original_id`). Returns
    `{"missing": [...], "duplicated": [...]}`, both empty on a clean run."""
    literal_ids = [block.id for _, block in document.iter_blocks()]
    base_ids_present = {original_id(bid) for bid in literal_ids}

    missing = [bid for bid in original_block_ids if bid not in base_ids_present]
    duplicated = sorted({bid for bid in literal_ids if literal_ids.count(bid) > 1})
    return {"missing": missing, "duplicated": duplicated}
