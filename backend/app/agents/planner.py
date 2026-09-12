"""Phase 1 - Course Planner (and the standalone TOC improver)."""

from __future__ import annotations

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.templates.registry import load_template
from app.schemas.blueprint import (
    BlueprintChapter,
    ChapterSection,
    CourseBlueprint,
    PlannedSummaries,
    PlannerOutput,
)
from app.schemas.course import CourseInput, ImproveTocRequest, ImproveTocResponse, TocItem
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)


class PlannerAgent:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()

    # --- blueprint --------------------------------------------------------
    async def plan(self, course_input: CourseInput) -> CourseBlueprint:
        template = load_template(course_input.template_id)
        memory = await self.memory.build_context(
            stage="planner", course_title=course_input.course_title, template=template
        )
        output = await self.ai.structured(
            schema=PlannerOutput,
            system=prompts.PLANNER_SYSTEM,
            user=prompts.planner_user(course_input, template, memory=memory.render()),
            model=self.settings.planner_model,
            purpose="planner",
            phase="planner",
        )
        chapters = self._reconcile_chapters(course_input, output)
        blueprint = CourseBlueprint(
            course_title=course_input.course_title,
            audience=course_input.target_audience,
            template_id=course_input.template_id,
            course_summary=output.course_summary,
            learning_objectives=output.learning_objectives,
            prerequisites=output.prerequisites,
            tone=course_input.tone or output.tone,
            dos=course_input.dos,
            donts=course_input.donts,
            chapters=chapters,
            critique=output.critique,
            generated_at=utc_now_iso(),
        )
        log.info(
            "Planned '%s' with %s chapters (template %s)",
            blueprint.course_title,
            len(blueprint.chapters),
            blueprint.template_id,
        )
        return blueprint

    @staticmethod
    def _reconcile_chapters(
        course_input: CourseInput, output: PlannerOutput
    ) -> list[BlueprintChapter]:
        """The user's TOC is authoritative: one chapter per TOC entry, in order.

        Proposed structural changes stay in `critique`, never applied silently.
        """
        planned = list(output.chapters)
        by_title = {c.title.strip().lower(): c for c in planned if c.title}
        chapters: list[BlueprintChapter] = []

        for index, toc_item in enumerate(course_input.toc, start=1):
            match = by_title.pop(toc_item.title.strip().lower(), None)
            if match is None and index - 1 < len(planned):
                candidate = planned[index - 1]
                # Only borrow positionally if that chapter wasn't matched by title.
                if candidate.title.strip().lower() in by_title:
                    match = by_title.pop(candidate.title.strip().lower())
            chapter = (match or BlueprintChapter()).model_copy(deep=True)
            chapter.id = f"chapter_{index}"
            chapter.order = index
            chapter.title = toc_item.title
            if not chapter.sections and toc_item.sections:
                chapter.sections = [ChapterSection(title=s) for s in toc_item.sections]
            if not chapter.objective:
                chapter.objective = f"Understand and apply the core ideas of {toc_item.title}."
            if not chapter.research_questions:
                chapter.research_questions = [
                    f"What must a learner know about {toc_item.title}?",
                    f"What mistakes are common with {toc_item.title}?",
                ]
            if not chapter.estimated_words:
                chapter.estimated_words = 1400
            chapters.append(chapter)
        return chapters

    async def ensure_planned_summaries(
        self, blueprint: CourseBlueprint, course_input: CourseInput
    ) -> bool:
        """Guarantee every chapter has a planned summary.

        This is what makes parallel writing possible: each chapter is told what
        the others will cover, so nothing has to wait for a neighbour to finish.
        The planner usually fills `chapter.summary` already, so this normally
        costs nothing; when it doesn't, one cheap call fills the gaps.

        Returns True if a model call was made.
        """
        missing = [chapter for chapter in blueprint.chapters if not chapter.summary.strip()]
        if not missing:
            return False

        template = load_template(blueprint.template_id or course_input.template_id)
        try:
            planned = await self.ai.structured(
                schema=PlannedSummaries,
                system=prompts.SUMMARIES_SYSTEM,
                user=prompts.summaries_user(blueprint, template, course_input),
                model=self.settings.planner_model,
                purpose="planned_summaries",
                phase="planner",
            )
        except Exception as exc:  # noqa: BLE001 - fall back to a derived summary
            log.warning("Planned summaries call failed (%s); deriving from the blueprint", exc)
            planned = PlannedSummaries()

        by_id = {item.chapter_id: item.summary for item in planned.summaries if item.chapter_id}
        for chapter in missing:
            chapter.summary = by_id.get(chapter.id, "").strip() or self._derived_summary(chapter)
        return bool(planned.summaries)

    @staticmethod
    def _derived_summary(chapter: BlueprintChapter) -> str:
        sections = ", ".join(section.title for section in chapter.sections)
        parts = [chapter.objective or f"Covers {chapter.title}."]
        if sections:
            parts.append(f"Sections: {sections}.")
        if chapter.key_concepts:
            parts.append(f"Key concepts: {', '.join(chapter.key_concepts)}.")
        return " ".join(parts)

    # --- TOC improvement --------------------------------------------------
    async def improve_toc(self, request: ImproveTocRequest) -> ImproveTocResponse:
        template = load_template(request.template)
        response = await self.ai.structured(
            schema=ImproveTocResponse,
            system=prompts.TOC_SYSTEM,
            user=prompts.toc_user(request, template),
            model=self.settings.planner_model,
            purpose="improve_toc",
            phase="planner",
        )
        if not response.suggested_toc:
            # Never hand back an empty TOC - fall back to what the user gave us.
            response.suggested_toc = [TocItem(title=item.title) for item in request.toc]
            response.reasoning = (
                response.reasoning or "No improvements proposed; original order retained."
            )
        return response
