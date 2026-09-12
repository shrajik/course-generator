"""One common Reviewer for both templates.

Deterministic template checks run locally (cheap, reliable); judgement calls -
accuracy, clarity, tone, repetition - go to the model. Both results are merged
into a single ChapterReview.

Two performance decisions live here:
* the model receives a text-only projection of the chapter rather than the full
  block JSON, because input size is latency;
* after a parallel run, one extra call checks the whole course for the failure
  mode parallel writing can introduce (repetition and broken transitions).
"""

from __future__ import annotations

from app.agents import prompts
from app.agents.prompts import ContinuityContext
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.review import ChapterReview, ContinuityReport
from app.schemas.template import CourseTemplate
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)


class ReviewerAgent:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()

    async def review_chapter(
        self,
        *,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        blocks: list[Block],
        continuity: ContinuityContext,
    ) -> ChapterReview:
        payload = [block.model_dump(mode="json") for block in blocks]
        memory = await self.memory.build_context(
            stage="reviewer",
            course_title=blueprint.course_title,
            chapter_title=chapter.title,
            template=template,
        )
        review = await self.ai.structured(
            schema=ChapterReview,
            system=prompts.REVIEWER_SYSTEM,
            user=prompts.reviewer_user(
                blueprint,
                chapter,
                template,
                payload,
                course_input=course_input,
                continuity=continuity,
                limit=self.settings.reviewer_context_chars,
                memory=memory.render(),
            ),
            model=self.settings.reviewer_model,
            purpose=f"reviewer:{chapter.id}",
            phase="reviewer",
        )
        review.reviewed_at = utc_now_iso()
        self._apply_structural_checks(review, template, blocks)
        # Keep the reviewer honest: structural gaps always block approval.
        if review.missing_required_blocks:
            review.approved = False
        log.info(
            "Reviewed %s: approved=%s score=%.1f issues=%s",
            chapter.id,
            review.approved,
            review.scores.overall(),
            len(review.issues),
        )
        return review

    async def review_continuity(
        self,
        *,
        blueprint: CourseBlueprint,
        summaries: list[tuple[str, str, str]],
    ) -> ContinuityReport:
        """One call over the whole course after the chapters are written."""
        report = await self.ai.structured(
            schema=ContinuityReport,
            system=prompts.CONTINUITY_SYSTEM,
            user=prompts.continuity_user(blueprint, summaries),
            model=self.settings.reviewer_model,
            purpose="continuity",
            phase="reviewer",
        )
        report.reviewed_at = utc_now_iso()
        log.info(
            "Continuity pass: approved=%s issues=%s", report.approved, len(report.issues)
        )
        return report

    @staticmethod
    def _apply_structural_checks(
        review: ChapterReview, template: CourseTemplate, blocks: list[Block]
    ) -> None:
        present_types = {block.type for block in blocks}
        present_sections = {block.meta.section_key for block in blocks if block.meta.section_key}

        missing: list[str] = list(review.missing_required_blocks)
        for block_type in template.required_block_types:
            if block_type not in present_types:
                missing.append(f"block type '{block_type.value}'")
        for section in template.required_sections():
            allowed = set(section.block_types) or set(BlockType)
            if section.key not in present_sections and not (allowed & present_types):
                missing.append(f"section '{section.key}'")
        # Deduplicate while preserving order.
        review.missing_required_blocks = list(dict.fromkeys(missing))
