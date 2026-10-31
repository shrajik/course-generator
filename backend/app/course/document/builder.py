"""Assemble the Course Document from generated chapters."""

from __future__ import annotations

from app.core.ids import block_id as new_block_id
from app.core.ids import document_id_for_course, page_id, utc_now_iso
from app.core.logging import get_logger
from app.course.document.layout import estimate_height, flow_blocks, page_visual_fraction
from app.course.document.layout_validator import validate_document
from app.render.toc_renderer import TocChapter, estimate_toc_pixel_size, render_toc_html
from app.schemas.blocks import BlockType
from app.schemas.blueprint import CourseBlueprint
from app.schemas.document import (
    PAGE_HEIGHT,
    PAGE_MARGIN_BOTTOM,
    Block,
    BlockLayout,
    BlockMeta,
    BlockStyle,
    CourseDocument,
    DocumentMeta,
    Page,
    PageSize,
    SourceRecord,
)
from app.schemas.draft import GeneratedChapter
from app.schemas.template import CourseTemplate
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


def _log_visual_balance(pages: list[Page]) -> None:
    """Observability for the 60/40 text/visual target - a real, measured
    number from the actual paginated output (see page_visual_fraction), not
    a claim based on what the writer's prompt asked for. Logs only; never
    blocks or fails document assembly over this - a thin content page is
    sometimes genuinely correct (a pure-summary page, a quiz page)."""
    content_pages = [p for p in pages if p.kind == "content" and p.blocks]
    if not content_pages:
        return
    thin = [p for p in content_pages if page_visual_fraction(p.blocks) == 0.0]
    if thin:
        log.info(
            "%s/%s content pages have no visual content (page numbers: %s)",
            len(thin), len(content_pages), [p.page_number for p in thin[:10]],
        )


def _log_layout_findings(document: CourseDocument) -> None:
    """Structural sanity check on the just-paginated document (overflow,
    overlap, orphaned-fragment pages - see layout_validator). Logs only,
    same as `_log_visual_balance` - a genuine finding here means the layout
    math produced something the PDF/editor could mis-render, worth knowing
    about immediately rather than only when a reader notices, but it must
    never block a course from finishing generation over it."""
    findings = validate_document(document)
    if findings:
        log.warning(
            "Document %s has %s content page(s) failing layout validation: %s",
            document.document_id, len(findings), findings,
        )


def _page_size_for(page_blocks: list[Block], geometry=None) -> PageSize:
    """The standard `PageSize()` for every ordinary page - EXCEPT when this
    page's own blocks genuinely don't fit inside it.

    The rendered `.page` div is `position: relative; overflow: hidden` at a
    fixed `width`/`height` taken straight from `Page.size` (see
    app.render.html_renderer / course.html.j2), so a block whose
    `layout.y + layout.height` exceeds that box's bottom edge is not "grown
    by the CSS" - it is silently clipped, invisible in the rendered PDF. In
    practice `flow_blocks` keeps every block's reserved height at or under
    one page (see MAX_IMAGE_HEIGHT / concept_experience's own `_MAX_HEIGHT`
    cap), so this only ever fires for the rare, genuinely unsplittable text
    or code block flagged in `flow_blocks`'s own "Fresh page, unsplittable
    and taller than a page" fallback - but when it does, growing this ONE
    page's declared height (never the global default the rest of the
    document uses) is what actually keeps that content visible instead of
    quietly losing it."""
    from app.schemas.template import PageGeometry

    box = geometry or PageGeometry()
    default = PageSize(width=box.width, height=box.height)
    if not page_blocks:
        return default
    content_bottom = max(b.layout.y + b.layout.height for b in page_blocks)
    needed_height = content_bottom + box.margin_bottom
    if needed_height <= box.height:
        return default
    return PageSize(width=box.width, height=round(needed_height, 2))


def _stack(blocks: list[Block], *, start_y: float, gap: float = 20.0) -> list[Block]:
    """Position front-matter blocks top-down using the layout estimator."""
    y = start_y
    for index, block in enumerate(blocks):
        block.layout.x = 64
        block.layout.y = y if index == 0 else y + gap
        block.layout.height = round(estimate_height(block), 2)
        y = block.layout.y + block.layout.height
    return blocks


def _cover_page(
    blueprint: CourseBlueprint, template: CourseTemplate, chapter_count: int
) -> Page:
    theme = template.theme
    # Front matter shares the content pages' physical box, or the exported
    # PDF would mix two page sizes.
    geometry = theme.geometry()
    blocks = [
        Block(
            id=new_block_id(),
            type=BlockType.HEADING,
            content={"text": blueprint.course_title, "level": 1},
            style=BlockStyle(
                font_size=42,
                font_weight=800,
                color=theme.text_color,
                align="left",
                line_height=1.18,
            ),
            layout=BlockLayout(x=64, width=666),
            meta=BlockMeta(origin="system", section_key="cover"),
        ),
        Block(
            id=new_block_id(),
            type=BlockType.PARAGRAPH,
            content={
                "text": blueprint.course_summary
                or f"A {template.name.lower()} course in {chapter_count} chapters."
            },
            style=BlockStyle(font_size=17, color=theme.muted_color, line_height=1.6),
            layout=BlockLayout(x=64, width=620),
            meta=BlockMeta(origin="system", section_key="cover"),
        ),
        Block(
            id=new_block_id(),
            type=BlockType.DIVIDER,
            content={},
            style=BlockStyle(border_color=theme.accent_color, border_width=3),
            layout=BlockLayout(x=64, width=120, height=6),
            meta=BlockMeta(origin="system", section_key="cover"),
        ),
        Block(
            id=new_block_id(),
            type=BlockType.PARAGRAPH,
            content={
                "text": f"Audience: {blueprint.audience}\n"
                f"Template: {template.name}\n"
                f"Chapters: {chapter_count}"
            },
            style=BlockStyle(font_size=13.5, color=theme.muted_color, line_height=1.9),
            layout=BlockLayout(x=64, width=620),
            meta=BlockMeta(origin="system", section_key="cover"),
        ),
    ]
    if blueprint.learning_objectives:
        blocks.append(
            Block(
                id=new_block_id(),
                type=BlockType.LEARNING_OBJECTIVES,
                content={
                    "title": "What you will be able to do",
                    "items": blueprint.learning_objectives[:6],
                },
                style=BlockStyle.model_validate(
                    template.style_for(BlockType.LEARNING_OBJECTIVES)
                ),
                layout=BlockLayout(x=64, width=666),
                meta=BlockMeta(origin="system", section_key="cover"),
            )
        )
    _stack(blocks, start_y=260.0, gap=22.0)
    # The divider is a thin rule - keep it thin regardless of the estimator.
    for block in blocks:
        if block.type is BlockType.DIVIDER:
            block.layout.height = 6.0
    return Page(
        id=page_id(1),
        page_number=1,
        kind="cover",
        size=PageSize(width=geometry.width, height=geometry.height),
        background=template.theme.page_background,
        blocks=blocks,
    )


def _toc_page(
    chapters: list[GeneratedChapter],
    template: CourseTemplate,
    *,
    course_title: str,
    course_id: str,
    storage: StorageService,
) -> Page:
    """A colorful, static chapter-card grid - same deterministic, AI-free,
    "one motionless picture" contract as a concept_experience visual (see
    app.render.toc_renderer), stored and inlined the exact same way so the
    editor/preview/PDF all render it identically without any new plumbing."""
    geometry = template.theme.geometry()
    toc_chapters = [
        TocChapter(number=chapter.chapter_number, title=chapter.title, summary=chapter.summary)
        for chapter in chapters
    ]
    html_bytes = render_toc_html(toc_chapters, course_title=course_title, theme=template.theme)
    relative = storage.save_asset(course_id, html_bytes, extension="html")
    width, height = estimate_toc_pixel_size(toc_chapters)

    block = Block(
        id=new_block_id(),
        type=BlockType.IMAGE,
        content={
            "kind": "toc",
            "path": relative,
            "asset_id": relative.rsplit("/", 1)[-1],
            "generated": True,
            "alt": "Course contents overview",
            "width": width,
            "height": height,
        },
        style=BlockStyle(),
        layout=BlockLayout(x=64, y=72, width=666),
        meta=BlockMeta(origin="system", section_key="toc"),
    )
    block.layout.height = round(estimate_height(block), 2)
    return Page(
        id=page_id(2),
        page_number=2,
        kind="toc",
        size=PageSize(width=geometry.width, height=geometry.height),
        background=template.theme.page_background,
        blocks=[block],
    )


# --- sources (MOM Phase 2.1) -------------------------------------------------


def _collect_sources(
    chapters: list[GeneratedChapter], storage: StorageService, course_id: str
) -> list[SourceRecord]:
    """Every distinct external source cited anywhere in the course - read
    back from each chapter's own already-persisted research artifact (see
    ResearchAgent._merge_citations / storage.save_research), deduplicated by
    URL, in first-seen order. Never invents a reference: a chapter with no
    research on disk (mock mode, research skipped, a course generated before
    this feature existed) simply contributes none - see "never fabricate...
    sources" in the MOM."""
    by_url: dict[str, SourceRecord] = {}
    order: list[str] = []
    for chapter in chapters:
        if not storage.has_research(course_id, chapter.chapter_id, chapter.chapter_number):
            continue
        try:
            research = storage.load_research(course_id, chapter.chapter_id, chapter.chapter_number)
        except Exception:  # noqa: BLE001 - a corrupt/missing research file must never break document assembly
            log.warning("Could not load research for %s while collecting sources", chapter.chapter_id)
            continue
        for reference in research.payload.references:
            url = reference.url.strip()
            if not url:
                continue
            record = by_url.get(url)
            if record is None:
                by_url[url] = SourceRecord(
                    title=reference.title or url,
                    url=url,
                    note=reference.note,
                    author=reference.author,
                    source_type=reference.source_type,
                    relevance=reference.relevance,
                    chapter_ids=[chapter.chapter_id],
                )
                order.append(url)
            elif chapter.chapter_id not in record.chapter_ids:
                record.chapter_ids.append(chapter.chapter_id)
    return [by_url[url] for url in order]


#  Every source block carries this section_key - NOT a distinct page `kind`,
#  so it keeps ordinary "content" page rendering (page footer, validator
#  coverage, etc.) exactly like a real chapter page. `reflow_document` below
#  uses this marker to keep sources out of the general chapter-content remix
#  after an edit, so they stay together at the end instead of getting
#  reshuffled in among whichever chapter content they happen to reflow next to.
SOURCES_SECTION_KEY = "sources"


def _source_text(source: SourceRecord) -> str:
    """One source's full Sources-page text, as `\\n\\n`-joined lines -
    `_paragraphs` (app.render.html_renderer) already splits exactly this
    way into separate `<p>` tags, and the frontend's `whitespace-pre-wrap`
    editor preserves the same breaks, so this needs no new rendering
    machinery on either surface.

    The headline (`title — url`) is byte-identical to this function's
    pre-existing, already-tested output (course-sources follow-up: richer
    metadata only ever ADDS lines below it, never replaces or duplicates
    it) - every previously-generated course's source text still parses and
    displays exactly as it always did, and the SAME url is never linked
    twice on the page."""
    label = source.title or source.url
    headline = f"{label} — {source.url}" if source.url and source.url != label else label
    lines = [headline]
    if source.relevance.strip():
        lines.append(f"Covers: {source.relevance.strip()}")
    type_and_author = " — ".join(
        part.strip() for part in (source.source_type, source.author) if part.strip()
    )
    if type_and_author:
        lines.append(type_and_author)
    return "\n\n".join(lines)


def _source_blocks(sources: list[SourceRecord]) -> list[Block]:
    """Heading + one paragraph per source, laid out by the same `flow_blocks`
    every other page already uses - no bespoke renderer. When `sources` is
    empty, discloses the real basis of the content instead of silently
    omitting the section or inventing references (MOM Phase 2.1: "If an
    output is based entirely on... internal knowledge, accurately disclose
    the basis of the content")."""
    meta = BlockMeta(origin="system", section_key=SOURCES_SECTION_KEY)
    blocks: list[Block] = [
        Block(
            type=BlockType.HEADING,
            content={"text": "Sources & Further Reading", "level": 1},
            meta=meta.model_copy(),
        )
    ]
    if not sources:
        blocks.append(
            Block(
                type=BlockType.PARAGRAPH,
                content={
                    "text": (
                        "This course was generated without citable external web sources - its "
                        "content is based on the writing model's own training knowledge rather "
                        "than retrieved research material."
                    )
                },
                meta=meta.model_copy(),
            )
        )
        return blocks
    for source in sources:
        blocks.append(
            Block(type=BlockType.PARAGRAPH, content={"text": _source_text(source)}, meta=meta.model_copy())
        )
    return blocks


def blocks_from_chapters(chapters: list[GeneratedChapter]) -> list[Block]:
    blocks: list[Block] = []
    for chapter in chapters:
        for index, payload in enumerate(chapter.blocks):
            block = Block.model_validate(payload)
            block.meta.chapter_id = block.meta.chapter_id or chapter.chapter_id
            block.meta.chapter_number = block.meta.chapter_number or chapter.chapter_number
            if index == 0 and block.type is BlockType.HEADING:
                block.content["level"] = 1
            blocks.append(block)
    return blocks


def build_document(
    *,
    course_id: str,
    blueprint: CourseBlueprint,
    template: CourseTemplate,
    chapters: list[GeneratedChapter],
    include_front_matter: bool = True,
    existing: CourseDocument | None = None,
    storage: StorageService | None = None,
) -> CourseDocument:
    # The template's own page box - built-ins declare none and therefore get
    # exactly the box the layout engine has always used.
    geometry = template.theme.geometry()
    storage = storage or get_storage()
    content_blocks = blocks_from_chapters(chapters)
    pages: list[Page] = []

    if include_front_matter:
        pages.append(_cover_page(blueprint, template, len(chapters)))
        if len(chapters) > 1:
            pages.append(
                _toc_page(
                    chapters,
                    template,
                    course_title=blueprint.course_title,
                    course_id=course_id,
                    storage=storage,
                )
            )

    offset = len(pages)
    for index, page_blocks in enumerate(flow_blocks(content_blocks, geometry=geometry), start=1):
        number = offset + index
        pages.append(
            Page(
                id=page_id(number),
                page_number=number,
                kind="content",
                size=_page_size_for(page_blocks, geometry),
                background=template.theme.page_background,
                blocks=page_blocks,
            )
        )

    # Sources - always appended at the end (MOM Phase 2.1). A separate
    # flow_blocks pass, never mixed into the chapter content loop above, so
    # it reliably starts on its own fresh page(s) rather than tacking onto
    # whatever space happened to be left on the last chapter's page.
    sources = _collect_sources(chapters, storage, course_id)
    source_blocks = _source_blocks(sources)
    for page_blocks in flow_blocks(source_blocks, geometry=geometry):
        pages.append(
            Page(
                id=page_id(len(pages) + 1),
                page_number=len(pages) + 1,
                kind="content",
                size=_page_size_for(page_blocks, geometry),
                background=template.theme.page_background,
                blocks=page_blocks,
            )
        )

    document = CourseDocument(
        document_id=document_id_for_course(course_id),
        course_id=course_id,
        course_title=blueprint.course_title,
        template_id=template.template_id,
        version=(existing.version + 1) if existing else 1,
        created_at=existing.created_at if existing else utc_now_iso(),
        updated_at=utc_now_iso(),
        meta=DocumentMeta(
            audience=blueprint.audience,
            template_name=template.name,
            chapter_ids=[chapter.chapter_id for chapter in chapters],
            theme=template.theme.model_dump(mode="json"),
            generated_with="course-creator-poc",
            sources=sources,
        ),
        pages=pages,
    )
    log.info(
        "Built document %s: %s pages from %s chapters",
        document.document_id,
        len(document.pages),
        len(chapters),
    )
    _log_visual_balance(document.pages)
    _log_layout_findings(document)
    return document


def reflow_document(document: CourseDocument, template: CourseTemplate) -> CourseDocument:
    """Re-paginate content pages after an edit, preserving front matter."""
    geometry = template.theme.geometry()
    front = [page for page in document.pages if page.kind in {"cover", "toc"}]
    all_content_blocks = [
        block for page in document.pages if page.kind == "content" for block in page.blocks
    ]
    # Sources (see _source_blocks/SOURCES_SECTION_KEY) are real "content"-kind
    # pages (so they keep normal footer/rendering), but must never get
    # reshuffled in among chapter content by a generic reflow - keep them out
    # of the main remix and re-flow them separately, still at the end.
    content_blocks = [
        block for block in all_content_blocks if block.meta.section_key != SOURCES_SECTION_KEY
    ]
    source_blocks = [
        block for block in all_content_blocks if block.meta.section_key == SOURCES_SECTION_KEY
    ]
    pages = list(front)
    for page_blocks in flow_blocks(content_blocks, geometry=geometry):
        pages.append(
            Page(
                id=page_id(len(pages) + 1),
                page_number=len(pages) + 1,
                kind="content",
                size=_page_size_for(page_blocks, geometry),
                background=template.theme.page_background,
                blocks=page_blocks,
            )
        )
    for page_blocks in flow_blocks(source_blocks, geometry=geometry):
        pages.append(
            Page(
                id=page_id(len(pages) + 1),
                page_number=len(pages) + 1,
                kind="content",
                size=_page_size_for(page_blocks, geometry),
                background=template.theme.page_background,
                blocks=page_blocks,
            )
        )
    for number, page in enumerate(pages, start=1):
        page.page_number = number
        page.id = page_id(number)
    document.pages = pages
    _log_visual_balance(document.pages)
    _log_layout_findings(document)
    return document
