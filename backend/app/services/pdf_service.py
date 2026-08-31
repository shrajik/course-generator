"""PDF export: Course Document JSON -> HTML -> CSS -> Playwright -> PDF.

No AI involvement. The HTML is written next to the PDF so relative asset paths
resolve and so the exact markup that produced a PDF can be inspected.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.errors import RenderError
from app.core.logging import get_logger
from app.course.templates.registry import load_template
from app.render.html_renderer import render_document_html
from app.schemas.document import PAGE_HEIGHT, PAGE_WIDTH, CourseDocument
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


class PdfService:
    def __init__(
        self, storage: StorageService | None = None, settings: Settings | None = None
    ) -> None:
        self.storage = storage or get_storage()
        self.settings = settings or get_settings()

    def render_html(self, document: CourseDocument) -> str:
        template = load_template(document.template_id)
        return render_document_html(document, template, asset_prefix="../")

    def write_html(self, document: CourseDocument) -> Path:
        path = self.storage.html_path(document.course_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_html(document), encoding="utf-8")
        return path

    async def export_pdf(self, document: CourseDocument) -> Path:
        html_path = self.write_html(document)
        pdf_path = self.storage.pdf_path(document.course_id)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RenderError(
                "Playwright is not installed - run `pip install playwright` and "
                "`playwright install chromium`"
            ) from exc

        launch_kwargs: dict[str, object] = {"args": ["--no-sandbox", "--font-render-hinting=none"]}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable:
            launch_kwargs["executable_path"] = executable

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs)
                try:
                    page = await browser.new_page(
                        viewport={"width": int(PAGE_WIDTH), "height": int(PAGE_HEIGHT)}
                    )
                    await page.goto(html_path.resolve().as_uri(), wait_until="load")
                    await page.emulate_media(media="print")
                    await page.pdf(
                        path=str(pdf_path),
                        width=f"{PAGE_WIDTH}px",
                        height=f"{PAGE_HEIGHT}px",
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    )
                finally:
                    await browser.close()
        except RenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RenderError(f"PDF rendering failed: {exc}") from exc

        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            raise RenderError("PDF rendering produced an empty file")

        log.info(
            "Exported %s (%s pages, %.1f KB)",
            pdf_path,
            len(document.pages),
            pdf_path.stat().st_size / 1024,
        )
        return pdf_path
