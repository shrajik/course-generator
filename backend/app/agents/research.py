"""Phase 2 - Deep Research, one chapter at a time.

Two passes per chapter:
1. `research()` gathers grounded notes (web search / Deep Research API).
2. `structured()` converts those notes into a validated ChapterResearch artifact.

Splitting it this way keeps the grounded text available in `raw_notes` for audit
and stops the model from having to search and format in a single shot.
"""

from __future__ import annotations

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.research import ChapterResearch, Reference, ResearchPayload
from app.schemas.template import CourseTemplate
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)


class ResearchAgent:
    def __init__(self, ai: AIClient | None = None, settings: Settings | None = None) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()

    async def research_chapter(
        self,
        *,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        mode: str | None = None,
    ) -> ChapterResearch:
        research_mode = mode or self.settings.research_mode
        deep = research_mode == "deep"

        # One call that searches *and* returns the structured artifact when the
        # model supports it; otherwise the client reports no structured payload
        # and we shape the notes in a second call as before.
        notes = await self.ai.research(
            prompt=prompts.research_user(
                blueprint, chapter, template, course_input=course_input
            ),
            deep=deep,
            system=prompts.RESEARCH_SYSTEM,
            schema=ResearchPayload if self.settings.single_call_research else None,
            phase="research",
        )

        payload: ResearchPayload | None = None
        if notes.structured is not None:
            try:
                payload = ResearchPayload.model_validate(notes.structured)
            except Exception as exc:  # noqa: BLE001 - fall back to the second call
                log.info("Single-call research payload invalid for %s: %s", chapter.id, exc)

        if payload is None:
            payload = await self.ai.structured(
                schema=ResearchPayload,
                system=prompts.RESEARCH_STRUCTURE_SYSTEM,
                user=prompts.research_structure_user(chapter.title, notes.text, template),
                model=self.settings.research_model,
                purpose=f"research_structure:{chapter.id}",
                phase="research",
            )
        payload = self._merge_citations(payload, notes.citations)

        research = ChapterResearch(
            chapter_id=chapter.id,
            chapter_title=chapter.title,
            mode=notes.mode or research_mode,
            model=notes.model,
            generated_at=utc_now_iso(),
            raw_notes=notes.text,
            payload=payload,
        )
        log.info(
            "Researched %s (%s): %s concepts, %s references",
            chapter.id,
            research.mode,
            len(payload.core_concepts),
            len(payload.references),
        )
        return research

    @staticmethod
    def _merge_citations(
        payload: ResearchPayload, citations: list[dict[str, str]]
    ) -> ResearchPayload:
        """Trust the API's citation annotations over anything the model retyped."""
        known = {ref.url for ref in payload.references if ref.url}
        for citation in citations:
            url = citation.get("url", "")
            if url and url not in known:
                payload.references.append(
                    Reference(title=citation.get("title", "") or url, url=url)
                )
                known.add(url)
        return payload
