"""Chapter-by-chapter research orchestration with on-disk caching.

Research is the slowest and most expensive phase, so results are cached as JSON
per chapter and reused unless `force=True`.
"""

from __future__ import annotations

import asyncio

from app.agents.research import ResearchAgent
from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import phase as metrics_phase
from app.course.templates.registry import load_template
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.research import ChapterResearch
from app.services.openai_service import AIClient, get_ai_client
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


class ResearchService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()
        self.agent = ResearchAgent(ai or get_ai_client(), self.settings)

    async def research_chapter(
        self,
        *,
        course_id: str,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        course_input: CourseInput,
        force: bool = False,
        mode: str | None = None,
    ) -> ChapterResearch:
        if not force and self.storage.has_research(course_id, chapter.id, chapter.order):
            log.info("Reusing cached research for %s/%s", course_id, chapter.id)
            return self.storage.load_research(course_id, chapter.id, chapter.order)

        template = load_template(blueprint.template_id or course_input.template_id)
        research = await self.agent.research_chapter(
            blueprint=blueprint,
            chapter=chapter,
            template=template,
            course_input=course_input,
            mode=mode,
        )
        self.storage.save_research(course_id, research, chapter.order)
        return research

    async def research_chapters(
        self,
        *,
        course_id: str,
        blueprint: CourseBlueprint,
        chapters: list[BlueprintChapter],
        course_input: CourseInput,
        force: bool = False,
        mode: str | None = None,
        deep_chapter_ids: set[str] | None = None,
        on_done: "callable | None" = None,
    ) -> dict[str, ChapterResearch]:
        """Research chapters concurrently, bounded by the research limiter."""
        limiter = get_limiter("research", self.settings.concurrency_for("research"))
        deep_ids = deep_chapter_ids or set()

        async def worker(chapter: BlueprintChapter) -> tuple[str, ChapterResearch | None]:
            async with limiter.slot():
                try:
                    result = await self.research_chapter(
                        course_id=course_id,
                        blueprint=blueprint,
                        chapter=chapter,
                        course_input=course_input,
                        force=force,
                        # Deep Research is opt-in per chapter: it costs minutes each.
                        mode="deep" if chapter.id in deep_ids else mode,
                    )
                    if on_done:
                        on_done(chapter.id)
                    return chapter.id, result
                except Exception as exc:  # noqa: BLE001
                    log.warning("Research failed for %s: %s", chapter.id, exc)
                    return chapter.id, None

        with metrics_phase("research"):
            results = await asyncio.gather(*(worker(chapter) for chapter in chapters))
        return {chapter_id: research for chapter_id, research in results if research is not None}
