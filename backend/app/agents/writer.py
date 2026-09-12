"""Phase 3 - Chapter Writer.

One chapter per model call. Context is deliberately bounded: blueprint,
audience, do's/don'ts, template, chapter outline, chapter research and the
*summaries* of the neighbouring chapters (never their full text).

In parallel mode those neighbour summaries come from the blueprint's plan rather
than from chapters already written, which is what removes the serial dependency
between chapters. `ContinuityContext` carries the topic boundaries that keep the
chapters from overlapping.
"""

from __future__ import annotations

from app.agents import prompts
from app.agents.prompts import ContinuityContext
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.blocks.normalizer import build_block, normalize_draft_blocks
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.draft import BlockRevision, ChapterDraft, DraftBlock, GeneratedChapter
from app.schemas.research import ChapterResearch
from app.schemas.review import ChapterReview
from app.schemas.template import CourseTemplate
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, StreamSink, get_ai_client

log = get_logger(__name__)


class WriterAgent:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()

    async def write_chapter(
        self,
        *,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        research: ChapterResearch | None,
        continuity: ContinuityContext,
        revision_notes: str = "",
        on_delta: StreamSink | None = None,
    ) -> tuple[list[Block], str]:
        """Returns (validated blocks, compact chapter summary)."""
        memory = await self.memory.build_context(
            stage="writer",
            course_title=blueprint.course_title,
            chapter_title=chapter.title,
            template=template,
        )
        draft = await self.ai.structured(
            schema=ChapterDraft,
            system=prompts.WRITER_SYSTEM,
            user=prompts.writer_user(
                blueprint,
                chapter,
                template,
                research,
                continuity,
                course_input=course_input,
                revision_notes=revision_notes,
                research_chars=self.settings.research_context_chars,
                memory=memory.render(),
            ),
            model=self.settings.writer_model,
            purpose=f"writer:{chapter.id}",
            phase="writer",
            on_delta=on_delta,
        )

        drafts = self._ensure_title_heading(draft.blocks, chapter.title)
        blocks = normalize_draft_blocks(
            drafts,
            template=template,
            chapter_id=chapter.id,
            chapter_number=chapter.order,
        )
        if not blocks:
            raise ValueError(f"Writer produced no usable blocks for {chapter.id}")

        summary = draft.chapter_summary or self._fallback_summary(chapter, blocks)
        log.info("Wrote %s: %s blocks", chapter.id, len(blocks))
        return blocks, summary

    async def revise_chapter(
        self,
        *,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        research: ChapterResearch | None,
        continuity: ContinuityContext,
        review: ChapterReview,
        blocks: list[Block] | None = None,
        summary: str = "",
        on_delta: StreamSink | None = None,
    ) -> tuple[list[Block], str]:
        """Fix a reviewed chapter.

        Prefers a surgical rewrite of just the offending blocks - a full rewrite
        costs another whole writer call and is the single most expensive thing the
        pipeline can do.
        """
        if self.settings.surgical_revision and blocks:
            indices = review.offending_indices()
            if indices and not review.missing_required_blocks:
                revised = await self._revise_blocks(
                    chapter=chapter,
                    template=template,
                    course_input=course_input,
                    research=research,
                    review=review,
                    blocks=blocks,
                    indices=indices,
                    on_delta=on_delta,
                )
                if revised is not None:
                    return revised[0], revised[1] or summary

        # Structure is missing or the targeted fix failed: rewrite the chapter.
        return await self.write_chapter(
            blueprint=blueprint,
            chapter=chapter,
            template=template,
            course_input=course_input,
            research=research,
            continuity=continuity,
            revision_notes=self.review_to_notes(review),
            on_delta=on_delta,
        )

    async def _revise_blocks(
        self,
        *,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        research: ChapterResearch | None,
        review: ChapterReview,
        blocks: list[Block],
        indices: list[int],
        on_delta: StreamSink | None = None,
    ) -> tuple[list[Block], str] | None:
        payload = [block.model_dump(mode="json") for block in blocks]
        try:
            revision = await self.ai.structured(
                schema=BlockRevision,
                system=prompts.REVISION_SYSTEM,
                user=prompts.revision_user(
                    chapter,
                    template,
                    payload,
                    indices,
                    review,
                    course_input=course_input,
                    research=research,
                ),
                model=self.settings.writer_model,
                purpose=f"revise:{chapter.id}",
                phase="writer",
                on_delta=on_delta,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to a full rewrite
            log.warning("Surgical revision failed for %s: %s", chapter.id, exc)
            return None

        updated = list(blocks)
        applied = 0
        for replacement in revision.replacements:
            index = replacement.index
            if not (0 <= index < len(updated)):
                continue
            block = build_block(
                replacement.block,
                template=template,
                chapter_id=chapter.id,
                chapter_number=chapter.order,
                origin="edited",
            )
            if block is None:
                continue
            block.id = updated[index].id  # keep ids stable
            updated[index] = block
            applied += 1

        if applied == 0:
            log.info("Surgical revision produced nothing usable for %s", chapter.id)
            return None
        log.info("Revised %s block(s) in %s surgically", applied, chapter.id)
        return updated, revision.chapter_summary

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def review_to_notes(review: ChapterReview) -> str:
        lines: list[str] = []
        for issue in review.issues:
            if issue.severity in {"blocker", "major"}:
                lines.append(
                    f"[{issue.severity}/{issue.category}] block {issue.block_index}: "
                    f"{issue.description} -> {issue.suggestion}"
                )
        for missing in review.missing_required_blocks:
            lines.append(f"[blocker/template] missing required block or section: {missing}")
        for violation in review.donts_violations:
            lines.append(f"[blocker/donts] violates a Don't: {violation}")
        for violation in review.dos_violations:
            lines.append(f"[major/dos] does not honour a Do: {violation}")
        for note in review.repetition_notes:
            lines.append(f"[major/repetition] {note}")
        return "\n".join(lines) or review.summary

    @staticmethod
    def _ensure_title_heading(drafts: list[DraftBlock], title: str) -> list[DraftBlock]:
        if drafts and drafts[0].type is BlockType.HEADING:
            first = drafts[0]
            if not first.text:
                drafts[0] = first.model_copy(update={"text": title, "level": 1})
            elif first.level not in (1, 2):
                drafts[0] = first.model_copy(update={"level": 1})
            return drafts
        return [
            DraftBlock(type=BlockType.HEADING, text=title, level=1, section_key="introduction")
        ] + drafts

    @staticmethod
    def _fallback_summary(chapter: BlueprintChapter, blocks: list[Block]) -> str:
        types = ", ".join(sorted({b.type.value for b in blocks}))
        return f"{chapter.title}: {chapter.objective} Covered via {types}."


def to_generated_chapter(
    *,
    chapter: BlueprintChapter,
    blocks: list[Block],
    summary: str,
    review: ChapterReview | None,
    revisions: int,
    model: str,
) -> GeneratedChapter:
    return GeneratedChapter(
        chapter_id=chapter.id,
        chapter_number=chapter.order,
        title=chapter.title,
        summary=summary,
        blocks=[block.model_dump(mode="json") for block in blocks],
        review=review.model_dump(mode="json") if review else None,
        revisions=revisions,
        model=model,
        generated_at=utc_now_iso(),
    )
