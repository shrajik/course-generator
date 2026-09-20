"""Assemble the Course Document from generated chapters."""

from __future__ import annotations

from app.core.ids import block_id as new_block_id
from app.core.ids import document_id_for_course, page_id, utc_now_iso
from app.core.logging import get_logger
from app.course.document.layout import estimate_height, flow_blocks
from app.render.toc_renderer import TocChapter, estimate_toc_pixel_size, render_toc_html
from app.schemas.blocks import BlockType
from app.schemas.blueprint import CourseBlueprint
from app.schemas.document import (
    Block,
    BlockLayout,
    BlockMeta,
    BlockStyle,
    CourseDocument,
    DocumentMeta,
    Page,
    PageSize,
)
from app.schemas.draft import GeneratedChapter
from app.schemas.template import CourseTemplate
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


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
        size=PageSize(),
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
        size=PageSize(),
        background=template.theme.page_background,
        blocks=[block],
    )


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
                    storage=storage or get_storage(),
                )
            )

    offset = len(pages)
    for index, page_blocks in enumerate(flow_blocks(content_blocks), start=1):
        number = offset + index
        pages.append(
            Page(
                id=page_id(number),
                page_number=number,
                kind="content",
                size=PageSize(),
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
        ),
        pages=pages,
    )
    log.info(
        "Built document %s: %s pages from %s chapters",
        document.document_id,
        len(document.pages),
        len(chapters),
    )
    return document


def reflow_document(document: CourseDocument, template: CourseTemplate) -> CourseDocument:
    """Re-paginate content pages after an edit, preserving front matter."""
    front = [page for page in document.pages if page.kind in {"cover", "toc"}]
    content_blocks = [
        block for page in document.pages if page.kind == "content" for block in page.blocks
    ]
    pages = list(front)
    for index, page_blocks in enumerate(flow_blocks(content_blocks), start=1):
        number = len(front) + index
        pages.append(
            Page(
                id=page_id(number),
                page_number=number,
                kind="content",
                size=PageSize(),
                background=template.theme.page_background,
                blocks=page_blocks,
            )
        )
    for number, page in enumerate(pages, start=1):
        page.page_number = number
        page.id = page_id(number)
    document.pages = pages
    return document
