"""Image generation for `image` blocks.

The writer decides *where* a visual helps and supplies the prompt; this service
turns those prompts into asset files under `data/courses/{course_id}/assets/`
and writes the relative path back into the Course Document. A block marked
`kind == "diagram"` is delegated to `DiagramService` first (a structured, on-
theme SVG); everything else - and any diagram that doesn't work out - gets a
raster illustration from the image model, as before.
"""

from __future__ import annotations

import asyncio

from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import phase as metrics_phase
from app.schemas.blocks import BlockType, merge_content
from app.schemas.document import Block, CourseDocument
from app.schemas.template import CourseTemplate
from app.services.diagram_service import DiagramService
from app.services.openai_service import AIClient, get_ai_client
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

MAX_PROMPT_CHARS = 3800


class ImageService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.storage = storage or get_storage()
        self.settings = settings or get_settings()
        self.diagrams = DiagramService(self.ai, self.storage, self.settings)

    # --- prompt -----------------------------------------------------------
    @staticmethod
    def build_prompt(block: Block, template: CourseTemplate, course_title: str) -> str:
        content = block.content
        parts = [
            content.get("prompt") or content.get("purpose") or content.get("caption") or "",
            f"Educational illustration for the course '{course_title}'.",
            template.image_guidance,
            "Do not include watermarks, signatures or lorem ipsum text.",
        ]
        return "\n".join(part for part in parts if part)[:MAX_PROMPT_CHARS]

    @staticmethod
    def _dimensions(data: bytes) -> tuple[int | None, int | None]:
        """Intrinsic size so the layout engine can reserve the right box."""
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                return image.width, image.height
        except Exception:  # pragma: no cover - never block on metadata
            return None, None

    # --- generation -------------------------------------------------------
    async def generate_for_block(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
    ) -> bool:
        """Diagram blocks try the structured/SVG path first and fall back to
        the raster illustration path on any failure - a block must never end
        up with nothing just because the diagram-specific path had trouble."""
        if block.content.get("kind") == "diagram" and self.settings.enable_diagram_generation:
            ok = await self.diagrams.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
        return await self._generate_illustration(
            course_id=course_id, block=block, template=template, course_title=course_title
        )

    async def _generate_illustration(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
    ) -> bool:
        prompt = self.build_prompt(block, template, course_title)
        if not prompt.strip():
            log.warning("Image block %s has no prompt - skipped", block.id)
            return False
        try:
            data = await self.ai.image(prompt=prompt, size=self.settings.image_size)
        except Exception as exc:  # noqa: BLE001 - a failed image must not fail the course
            log.warning("Image generation failed for %s: %s", block.id, exc)
            block.content = merge_content(
                block.type, block.content, {"error": str(exc)[:300], "generated": False}
            )
            return False
        relative = self.storage.save_asset(course_id, data)
        width, height = self._dimensions(data)
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                "prompt": prompt,
                "width": width,
                "height": height,
            },
        )
        log.info("Generated %s for block %s", relative, block.id)
        return True

    async def generate_missing(
        self,
        *,
        document: CourseDocument,
        template: CourseTemplate,
        only_block_ids: list[str] | None = None,
        force: bool = False,
    ) -> int:
        """Generate every image the document is still missing. Returns the count."""
        targets: list[Block] = []
        wanted = set(only_block_ids) if only_block_ids else None
        for _, block in document.iter_blocks():
            if block.type is not BlockType.IMAGE:
                continue
            if wanted is not None and block.id not in wanted:
                continue
            if block.content.get("path") and not force:
                continue
            targets.append(block)

        if not targets:
            return 0

        limiter = get_limiter("image", self.settings.concurrency_for("image"))

        async def worker(block: Block) -> bool:
            async with limiter.slot():
                return await self.generate_for_block(
                    course_id=document.course_id,
                    block=block,
                    template=template,
                    course_title=document.course_title,
                )

        with metrics_phase("images"):
            results = await asyncio.gather(*(worker(block) for block in targets))
        generated = sum(1 for ok in results if ok)
        log.info("Generated %s/%s images for %s", generated, len(targets), document.course_id)
        return generated
