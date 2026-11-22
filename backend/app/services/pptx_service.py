"""PowerPoint export: Course Document JSON -> a real, editable .pptx.

Sibling to pdf_service.py, but a structurally different projection: the PDF
path goes through HTML/CSS/Playwright (a print-shaped renderer), while this
path builds native PowerPoint shapes directly with python-pptx, because a
.pptx has no HTML-rendering step at all - text has to be real runs in a real
text frame, a table has to be a real table shape, not a picture of one.

The document's own pixel-positioned block model (Block.layout.x/y/width/
height, set by the SAME editor/layout engine the PDF path reads) maps onto
slides unusually cleanly: one Page -> one slide, one Block -> one positioned
shape at its own real x/y/width/height, so the exported deck matches what the
editor actually shows instead of a generic top-to-bottom reflow.

SVG assets (every diagram/concept_experience/toc visual) cannot be embedded
in a .pptx directly - python-pptx only accepts raster image bytes - so they
are rasterised to PNG first via the same Playwright/Chromium infrastructure
already used elsewhere in this codebase for SVG verification, not a new
one-off tool.
"""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from app.core.errors import RenderError
from app.core.logging import get_logger
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

# 96 CSS px (the document's own coordinate system, see schemas/document.py's
# PAGE_WIDTH/PAGE_HEIGHT) = 1 inch = 914400 EMU, so 1 px = 9525 EMU. The same
# fixed ratio PowerPoint/python-pptx itself uses for "pixels at 96 dpi".
PX_TO_EMU = 9525

_DEFAULT_TEXT_COLOR = "1A1A1A"
_DEFAULT_FONT = "Calibri"

_LIST_BLOCK_TYPES = {BlockType.LEARNING_OBJECTIVES, BlockType.REFLECTION}

# Visual card treatment for block types with no native OOXML/interactive
# equivalent (there is no "quiz widget"/"code cell" shape in the PowerPoint
# format, and VBA macros are explicitly out of scope - a real compatibility/
# security risk, not a style choice) - each gets a distinct, color-coded
# bordered card with a small type label instead, so its purpose is obvious
# at a glance the same way it already is in the web editor/PDF view.
_CARD_STYLES: dict[BlockType, tuple[str, str, str]] = {
    # block type -> (fill hex, label text, label/accent hex)
    BlockType.QUIZ: ("E8F0FE", "QUIZ", "1A56C4"),
    BlockType.EXERCISE: ("FFF1E0", "EXERCISE", "B5540A"),
    BlockType.CHALLENGE: ("FDE6EC", "CHALLENGE", "B3184A"),
    BlockType.CASE_STUDY: ("F2E9FB", "CASE STUDY", "5B2C9E"),
}
_CODE_CARD_FILL = "272B33"
_CODE_CARD_TEXT = "E8E8E8"


def _set_letter_spacing(run, points: float) -> None:
    """`letter_spacing` has no python-pptx property - OOXML exposes it as the
    `spc` attribute (hundredths of a point) on a run's `<a:rPr>`, so this sets
    it directly via the same lxml element python-pptx's own `font.bold`/
    `font.size` setters already operate on underneath (`get_or_add_rPr` is a
    standard, stable python-pptx oxml method, not a private hack)."""
    r_pr = run._r.get_or_add_rPr()
    r_pr.set("spc", str(int(round(points * 100))))


def _emu(px: float) -> Emu:
    return Emu(int(round(px * PX_TO_EMU)))


def _hex_to_rgb(value: str | None, default: str = _DEFAULT_TEXT_COLOR) -> RGBColor:
    raw = (value or default).lstrip("#")
    if len(raw) != 6:
        raw = default
    try:
        return RGBColor(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return RGBColor.from_string(default)


def _align_for(value: str | None) -> PP_ALIGN:
    return {
        "left": PP_ALIGN.LEFT,
        "center": PP_ALIGN.CENTER,
        "right": PP_ALIGN.RIGHT,
        "justify": PP_ALIGN.JUSTIFY,
    }.get(value or "left", PP_ALIGN.LEFT)


class PptxService:
    def __init__(self, storage: StorageService | None = None) -> None:
        self.storage = storage or get_storage()

    # --- asset handling -----------------------------------------------------

    async def _rasterize_svg(self, svg_path: Path, width_px: float, height_px: float) -> bytes:
        """Renders an SVG asset to PNG bytes at the given pixel size via a
        headless Chromium page - reuses the same Playwright dependency the
        PDF path already requires, rather than adding a new SVG library."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RenderError(
                "Playwright is not installed - cannot rasterise an SVG asset for PPTX export"
            ) from exc

        svg_markup = svg_path.read_text(encoding="utf-8")
        width = max(1, int(round(width_px)))
        height = max(1, int(round(height_px)))
        html = (
            "<!doctype html><html><head><style>"
            f"html,body{{margin:0;padding:0;width:{width}px;height:{height}px;background:transparent;}}"
            "</style></head><body>" + svg_markup + "</body></html>"
        )

        launch_kwargs: dict[str, object] = {"args": ["--no-sandbox"]}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable:
            launch_kwargs["executable_path"] = executable

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                page = await browser.new_page(viewport={"width": width, "height": height})
                await page.set_content(html, wait_until="load")
                return await page.screenshot(type="png", omit_background=True)
            finally:
                await browser.close()

    async def _image_bytes_for(
        self, course_id: str, path: str, width_px: float, height_px: float
    ) -> bytes | None:
        abs_path = self.storage.asset_abs_path(course_id, path)
        if not abs_path.exists():
            return None
        if abs_path.suffix.lower() == ".svg":
            try:
                return await self._rasterize_svg(abs_path, width_px, height_px)
            except Exception as exc:  # noqa: BLE001 - never fail the whole export over one asset
                log.warning("Could not rasterise %s for PPTX export: %s", abs_path, exc)
                return None
        return abs_path.read_bytes()

    # --- text helpers ---------------------------------------------------------

    def _add_textbox(
        self,
        slide,
        block: Block,
        paragraphs: list[tuple[str, dict]],
        *,
        default_size: float = 16.0,
        default_bold: bool = False,
        fill: str | None = None,
        force_rounded: bool = False,
    ):
        """`paragraphs` is a list of (text, overrides) pairs; overrides may set
        bold/size/italic/bullet for that one paragraph, falling back to the
        block's own BlockStyle (the same style fields the PDF/editor already
        read) for anything not overridden per-paragraph.

        Uses a real autoshape (rounded rectangle) instead of a plain
        `add_textbox` whenever the block asks for rounded corners
        (`style.border_radius`) or a fill colour is supplied - a plain
        python-pptx text box is a dedicated placeholder type with no
        line/fill/corner-rounding of its own, so getting either requires a
        genuine shape underneath it."""
        style = block.style
        rounded = force_rounded or bool(style.border_radius)
        width, height = _emu(block.layout.width), _emu(max(block.layout.height, 20))
        left, top = _emu(block.layout.x), _emu(block.layout.y)
        if rounded or fill:
            box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE, left, top, width, height)
            if fill:
                box.fill.solid()
                box.fill.fore_color.rgb = _hex_to_rgb(fill, "FFFFFF")
            else:
                box.fill.background()
            if style.border_color:
                box.line.color.rgb = _hex_to_rgb(style.border_color, "CCCCCC")
                box.line.width = Pt(style.border_width or 0.75)
            else:
                box.line.fill.background()
        else:
            box = slide.shapes.add_textbox(left, top, width, height)
        frame = box.text_frame
        frame.word_wrap = True
        color = _hex_to_rgb(style.color)
        font_name = style.font_family or _DEFAULT_FONT
        align = _align_for(style.align)

        for index, (text, overrides) in enumerate(paragraphs):
            para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            para.alignment = overrides.get("align", align)
            if style.line_height:
                para.line_spacing = style.line_height
            if overrides.get("bullet"):
                para.text = f"•  {text}"
            else:
                para.text = text
            run = para.runs[0] if para.runs else para.add_run()
            run.font.name = font_name
            run.font.size = Pt(overrides.get("size", style.font_size or default_size))
            run.font.bold = overrides.get("bold", default_bold or bool(style.font_weight and style.font_weight >= 600))
            run.font.italic = overrides.get("italic", bool(style.italic))
            run.font.color.rgb = overrides.get("color", color)
            if style.letter_spacing:
                _set_letter_spacing(run, style.letter_spacing)
        return box

    def _add_card_block(self, slide, block: Block, paragraphs: list[tuple[str, dict]]):
        """Quiz/exercise/case_study/challenge have no native OOXML or
        interactive-widget equivalent (and VBA macros are explicitly out of
        scope - a real security/compatibility risk, not a style choice) -
        this gives each a distinct, colour-coded bordered card with a small
        type label instead, so its purpose is obvious at a glance the same
        way it already is in the web editor/PDF view."""
        fill, label, accent_hex = _CARD_STYLES[block.type]
        accent = _hex_to_rgb(accent_hex)
        labelled = [(label, {"bold": True, "size": 11.0, "color": accent})] + paragraphs
        return self._add_textbox(slide, block, labelled, fill=fill, force_rounded=True)

    # --- per-block-type renderers --------------------------------------------

    async def _add_block(self, slide, document: CourseDocument, block: Block) -> None:
        content = block.content
        btype = block.type

        if btype == BlockType.HEADING:
            level = int(content.get("level") or 2)
            size = {1: 30.0, 2: 24.0, 3: 20.0, 4: 17.0}.get(level, 20.0)
            self._add_textbox(slide, block, [(content.get("text", ""), {})], default_size=size, default_bold=True)

        elif btype == BlockType.PARAGRAPH:
            self._add_textbox(slide, block, [(content.get("text", ""), {})])

        elif btype == BlockType.IMAGE:
            await self._add_image_block(slide, document, block)

        elif btype == BlockType.QUOTE:
            paras = [(content.get("text", ""), {"italic": True})]
            attribution = content.get("attribution")
            if attribution:
                paras.append((f"— {attribution}", {"italic": True, "size": (block.style.font_size or 16.0) * 0.85}))
            self._add_textbox(slide, block, paras)

        elif btype == BlockType.CALLOUT:
            title = content.get("title") or content.get("variant", "Note").replace("_", " ").title()
            self._add_callout_box(slide, block, title, [content.get("text", "")])

        elif btype == BlockType.TIP:
            self._add_callout_box(slide, block, content.get("title", "Pro Tip"), [content.get("text", "")])

        elif btype == BlockType.WARNING:
            self._add_callout_box(slide, block, content.get("title", "Common Mistake"), [content.get("text", "")])

        elif btype == BlockType.SUMMARY:
            lines = [f"✓ {t}" for t in content.get("key_takeaways", [])]
            lines += [f"→ {t}" for t in content.get("next_steps", [])]
            self._add_callout_box(slide, block, content.get("title", "Summary"), lines or [""])

        elif btype in _LIST_BLOCK_TYPES:
            title = content.get("title", "")
            items = content.get("items", [])
            paras: list[tuple[str, dict]] = []
            if title:
                paras.append((title, {"bold": True, "size": (block.style.font_size or 16.0) + 2}))
            if content.get("intro"):
                paras.append((content["intro"], {}))
            paras += [(item, {"bullet": True}) for item in items]
            self._add_textbox(slide, block, paras or [("", {})])

        elif btype == BlockType.STORY:
            paras = []
            if content.get("title"):
                paras.append((content["title"], {"bold": True}))
            paras.append((content.get("text", ""), {}))
            if content.get("takeaway"):
                paras.append((f"Takeaway: {content['takeaway']}", {"italic": True}))
            self._add_textbox(slide, block, paras)

        elif btype == BlockType.CASE_STUDY:
            paras = [(content.get("title", "Case Study"), {"bold": True})]
            for label, key in (("Context", "context"), ("Challenge", "challenge"), ("Outcome", "outcome")):
                if content.get(key):
                    paras.append((f"{label}: {content[key]}", {}))
            for action in content.get("actions", []):
                paras.append((action, {"bullet": True}))
            for lesson in content.get("lessons", []):
                paras.append((f"Lesson: {lesson}", {"bullet": True}))
            self._add_card_block(slide, block, paras)

        elif btype in (BlockType.EXERCISE, BlockType.CHALLENGE):
            paras = [(content.get("title", "Exercise"), {"bold": True}), (content.get("instructions", ""), {})]
            paras += [(step, {"bullet": True}) for step in content.get("steps", [])]
            if content.get("expected_outcome"):
                paras.append((f"Expected outcome: {content['expected_outcome']}", {"italic": True}))
            self._add_card_block(slide, block, paras)

        elif btype == BlockType.QUIZ:
            paras = [(content.get("title", "Knowledge Check"), {"bold": True})]
            for index, question in enumerate(content.get("questions", []), start=1):
                paras.append((f"{index}. {question.get('question', '')}", {"bold": True}))
                for option in question.get("options", []):
                    paras.append((option, {"bullet": True}))
                if question.get("answer"):
                    paras.append((f"Answer: {question['answer']}", {"italic": True}))
            self._add_card_block(slide, block, paras)

        elif btype in (BlockType.CODE, BlockType.CODE_CELL):
            code = content.get("code", "")
            code_color = _hex_to_rgb(_CODE_CARD_TEXT)
            lines: list[tuple[str, dict]] = [("CODE", {"bold": True, "size": 11.0, "color": code_color})]
            lines += [(line or " ", {"color": code_color}) for line in code.splitlines()] or [("", {"color": code_color})]
            box = self._add_textbox(slide, block, lines, default_size=12.0, fill=_CODE_CARD_FILL)
            for para in box.text_frame.paragraphs:
                for run in para.runs:
                    run.font.name = "Consolas"

        elif btype == BlockType.TABLE:
            self._add_table_block(slide, block)

        elif btype == BlockType.DIVIDER:
            line = slide.shapes.add_connector(
                MSO_CONNECTOR.STRAIGHT,
                _emu(block.layout.x), _emu(block.layout.y),
                _emu(block.layout.x + block.layout.width), _emu(block.layout.y),
            )
            line.line.color.rgb = _hex_to_rgb(block.style.border_color, "CCCCCC")

        else:  # pragma: no cover - exhaustive over BlockType today; a future
            # new block type falls back to a plain text box of its own preview
            # text rather than being silently dropped or crashing the export.
            preview = block.text_preview()
            if preview:
                self._add_textbox(slide, block, [(preview, {})])

    def _add_callout_box(self, slide, block: Block, title: str, lines: list[str]) -> None:
        paras: list[tuple[str, dict]] = [(title, {"bold": True, "size": (block.style.font_size or 16.0) + 1})]
        paras += [(line, {}) for line in lines]
        box = self._add_textbox(
            slide, block, paras, fill=block.style.background or "F2F2F2", force_rounded=True
        )
        box.text_frame.margin_left = Pt(10)
        box.text_frame.margin_right = Pt(10)
        box.text_frame.margin_top = Pt(6)
        box.text_frame.margin_bottom = Pt(6)

    def _add_table_block(self, slide, block: Block) -> None:
        content = block.content
        columns = content.get("columns") or []
        rows = content.get("rows") or []
        n_cols = max(1, len(columns))
        n_rows = len(rows) + (1 if columns else 0)
        if n_rows == 0:
            return
        graphic_frame = slide.shapes.add_table(
            n_rows, n_cols, _emu(block.layout.x), _emu(block.layout.y), _emu(block.layout.width), _emu(max(block.layout.height, 20))
        )
        table = graphic_frame.table
        row_offset = 0
        if columns:
            for col_index, heading in enumerate(columns):
                cell = table.cell(0, col_index)
                cell.text = str(heading)
                for para in cell.text_frame.paragraphs:
                    for run in para.runs:
                        run.font.bold = True
                        run.font.size = Pt(13)
            row_offset = 1
        for row_index, row in enumerate(rows):
            cells = row.get("cells") if isinstance(row, dict) else getattr(row, "cells", [])
            for col_index in range(n_cols):
                value = cells[col_index] if col_index < len(cells) else ""
                cell = table.cell(row_index + row_offset, col_index)
                cell.text = str(value)
                for para in cell.text_frame.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(12)
        caption = content.get("caption")
        if caption:
            caption_top = block.layout.y + block.layout.height + 4
            cap_box = slide.shapes.add_textbox(_emu(block.layout.x), _emu(caption_top), _emu(block.layout.width), _emu(20))
            cap_para = cap_box.text_frame.paragraphs[0]
            cap_para.text = caption
            run = cap_para.runs[0] if cap_para.runs else cap_para.add_run()
            run.font.italic = True
            run.font.size = Pt(11)

    async def _add_image_block(self, slide, document: CourseDocument, block: Block) -> None:
        content = block.content
        path = content.get("path")
        width_px, height_px = block.layout.width, max(block.layout.height, 20)
        image_bytes: bytes | None = None
        if path and content.get("generated"):
            image_bytes = await self._image_bytes_for(document.course_id, path, width_px, height_px)
        if image_bytes is None:
            placeholder = slide.shapes.add_textbox(_emu(block.layout.x), _emu(block.layout.y), _emu(width_px), _emu(height_px))
            placeholder.fill.solid()
            placeholder.fill.fore_color.rgb = RGBColor(0xEE, 0xEE, 0xEE)
            para = placeholder.text_frame.paragraphs[0]
            para.text = "Image not available"
            para.alignment = PP_ALIGN.CENTER
            if para.runs:
                para.runs[0].font.italic = True
                para.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
            return
        slide.shapes.add_picture(
            BytesIO(image_bytes), _emu(block.layout.x), _emu(block.layout.y), _emu(width_px), _emu(height_px)
        )
        caption = content.get("caption")
        if caption:
            caption_top = block.layout.y + height_px + 4
            cap_box = slide.shapes.add_textbox(_emu(block.layout.x), _emu(caption_top), _emu(width_px), _emu(20))
            cap_para = cap_box.text_frame.paragraphs[0]
            cap_para.text = caption
            cap_para.alignment = PP_ALIGN.CENTER
            run = cap_para.runs[0] if cap_para.runs else cap_para.add_run()
            run.font.italic = True
            run.font.size = Pt(11)

    # --- slide/page assembly -------------------------------------------------

    def _slide_size(self, document: CourseDocument) -> tuple[float, float]:
        """A single .pptx has exactly one global slide size - unlike PDF
        pages, which can each grow independently (see print.py's own
        per-page fitting), every slide here must share one canvas. Using
        only page 1's size was a real bug: a later page that grew taller
        during print-fit (the same NEAR_FIT_TOLERANCE/overflow case
        layout.py already handles for PDF) would have its own shapes
        extend past that smaller global slide's bottom edge. Taking the
        max width/height actually used by ANY page guarantees no block on
        any page can ever overflow the shared canvas - a shorter page
        just has blank space below its own content, which is a correct,
        harmless tradeoff, not a new defect."""
        if document.pages:
            width = max(page.size.width for page in document.pages)
            height = max(page.size.height for page in document.pages)
            return width, height
        from app.schemas.document import PAGE_HEIGHT, PAGE_WIDTH

        return PAGE_WIDTH, PAGE_HEIGHT

    async def _add_page(self, prs: Presentation, document: CourseDocument, page: Page) -> None:
        blank_layout = prs.slide_layouts[6]  # the standard "blank" built-in layout
        slide = prs.slides.add_slide(blank_layout)
        if page.background:
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = _hex_to_rgb(page.background, "FFFFFF")
        for block in page.blocks:
            try:
                await self._add_block(slide, document, block)
            except Exception as exc:  # noqa: BLE001 - one malformed block must
                # never take down the whole export; every other page/block
                # still needs to export successfully.
                log.warning(
                    "Skipped block %s (type=%s) on page %s during PPTX export: %s",
                    block.id, block.type, page.page_number, exc,
                )

    async def export_pptx(self, document: CourseDocument) -> bytes:
        prs = Presentation()
        width_px, height_px = self._slide_size(document)
        prs.slide_width = _emu(width_px)
        prs.slide_height = _emu(height_px)

        for page in document.pages:
            await self._add_page(prs, document, page)

        buffer = BytesIO()
        prs.save(buffer)
        data = buffer.getvalue()
        if not data:
            raise RenderError("PPTX rendering produced an empty file")
        return data

    async def export_pptx_to_disk(self, document: CourseDocument) -> Path:
        data = await self.export_pptx(document)
        path = self.storage.pptx_path(document.course_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.info("Exported %s (%s pages, %.1f KB)", path, len(document.pages), len(data) / 1024)
        return path


_service: PptxService | None = None


def get_pptx_service() -> PptxService:
    global _service
    if _service is None:
        _service = PptxService()
    return _service
