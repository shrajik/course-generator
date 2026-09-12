"""AI memory layer: bounded, best-effort retrieval across templates, visual
knowledge, course samples and generation history.

Retrieval is hybrid: the original keyword path (Postgres ILIKE + tag
overlap, mirroring `CourseRepository._filtered_query`) is unchanged, and a
semantic path (embeddings + pgvector, see HybridRetriever/EmbeddingService)
is merged in on top of it. The semantic half is entirely best-effort - no
API key, a disabled `enable_embeddings` flag, a provider outage, or no
pgvector extension all just mean every `HybridRetriever.rank()` call below
falls back to the keyword candidates alone, because a semantic hit only ever
*adds* to the candidate pool `rank()` already has from `keyword_items`.

This is never a hard dependency: `build_context()` catches every exception
and, when `settings.enable_memory_retrieval` is off, returns an empty
`MemoryContext` immediately. An empty context renders to "" (see
MemoryContext.render), so a prompt built with memory disabled or unavailable
is byte-identical to one built without this feature at all.
"""

from __future__ import annotations

import uuid
from typing import Literal

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models import CourseSample, VisualKnowledge
from app.db.repositories.course_samples import CourseSampleRepository
from app.db.repositories.generation_runs import GenerationRunRepository
from app.db.repositories.visual_knowledge import VisualKnowledgeRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    MAX_HISTORY_NOTES,
    MAX_NOTE_CHARS,
    MAX_SAMPLE_NOTES,
    MAX_VISUAL_NOTES,
    MemoryContext,
)
from app.schemas.template import CourseTemplate
from app.services.course_sample_service import CourseSampleService, get_course_sample_service
from app.services.hybrid_retriever import OVERSAMPLE, HybridRetriever
from app.services.visual_knowledge_service import VisualKnowledgeService, get_visual_knowledge_service

log = get_logger(__name__)

Stage = Literal["planner", "writer", "reviewer", "visual"]


class MemoryService:
    def __init__(
        self,
        samples: CourseSampleService | None = None,
        visuals: VisualKnowledgeService | None = None,
        settings: Settings | None = None,
        hybrid: HybridRetriever | None = None,
    ) -> None:
        self.samples = samples or get_course_sample_service()
        self.visuals = visuals or get_visual_knowledge_service()
        self.settings = settings or get_settings()
        # Fresh instance per build_context() call (see below) - its query
        # embedding cache should live no longer than one generation stage.
        self._hybrid_factory = hybrid

    async def build_context(
        self,
        *,
        stage: Stage,
        course_title: str,
        template: CourseTemplate | None = None,
        chapter_title: str = "",
        exclude_course_id: str | None = None,
    ) -> MemoryContext:
        """The single entry point every agent calls. Never raises.

        Templates/samples/visuals/history are all DB-backed, so this is a
        no-op (not just a soft failure) in filesystem-only mode - it must
        never attempt a database connection when `USE_DATABASE=false` says
        there isn't one to use, even if one happens to be reachable.
        """
        if not self.settings.enable_memory_retrieval or not self.settings.use_database:
            return MemoryContext()
        try:
            return await self._build_context(
                stage=stage,
                course_title=course_title,
                template=template,
                chapter_title=chapter_title,
                exclude_course_id=exclude_course_id,
            )
        except Exception as exc:  # noqa: BLE001 - memory must never break generation
            log.warning("Memory retrieval failed for stage=%s: %s", stage, exc)
            return MemoryContext()

    async def _build_context(
        self,
        *,
        stage: Stage,
        course_title: str,
        template: CourseTemplate | None,
        chapter_title: str,
        exclude_course_id: str | None,
    ) -> MemoryContext:
        query = chapter_title or course_title
        context = MemoryContext()
        # One retriever per build_context() call (= one pipeline stage): its
        # query-embedding cache is shared by every _add_*_notes call below
        # (they all embed the same `query`) but never outlives this call, so
        # it can't grow unbounded on the long-lived MemoryService singleton.
        # Tests may inject a shared instance via the constructor instead.
        retriever = self._hybrid_factory or HybridRetriever(settings=self.settings)

        # Planner: the structural shape of the template plus how similar
        # courses were organised - not writing style, not visuals.
        if stage == "planner":
            if template is not None:
                context.template_notes = _clip(template.writer_guidance)
            await self._add_sample_notes(context, query, exclude_course_id, retriever)

        # Writer: style/content references for the specific chapter, plus
        # relevant existing visuals so the writer knows a diagram-worthy
        # illustration may already exist for a similar topic.
        elif stage == "writer":
            await self._add_sample_notes(context, query, exclude_course_id, retriever)
            await self._add_visual_notes(context, query, retriever)

        # Reviewer: template requirements + a quality signal from history,
        # not samples or visuals (the reviewer never invents new content).
        elif stage == "reviewer":
            if template is not None and template.review_focus:
                context.template_notes = _clip("; ".join(template.review_focus))
            await self._add_history_notes(context, query, exclude_course_id, retriever)

        # Visual generation: only visual knowledge is relevant.
        elif stage == "visual":
            await self._add_visual_notes(context, query, retriever)

        return context

    async def _add_sample_notes(
        self,
        context: MemoryContext,
        query: str,
        exclude_course_id: str | None,
        retriever: HybridRetriever,
    ) -> None:
        items, _ = await self.samples.list(
            search=query, approved_only=True, limit=MAX_SAMPLE_NOTES * OVERSAMPLE
        )
        items = [s for s in items if not (exclude_course_id and s.course_id == exclude_course_id)]
        ranked = await retriever.rank(
            source_type="course_sample",
            query=query,
            limit=MAX_SAMPLE_NOTES,
            keyword_items=items,
            id_of=lambda sample: sample.id,
            semantic_lookup=lambda ids: self._lookup_samples(ids, exclude_course_id),
        )
        for sample in ranked:
            note = f"'{sample.title}'"
            if sample.topic:
                note += f" ({sample.topic})"
            if sample.description:
                note += f" - {sample.description}"
            context.sample_notes.append(_clip(note))
            context.sources.append(f"course_sample:{sample.id}")

    async def _lookup_samples(
        self, ids: list[uuid.UUID], exclude_course_id: str | None
    ) -> dict[uuid.UUID, CourseSample]:
        if not ids:
            return {}
        async with get_session_factory()() as session:
            rows = await CourseSampleRepository(session).get_many(ids)
        result = {}
        for row in rows:
            if not row.approved:
                continue
            if exclude_course_id and row.course_id == exclude_course_id:
                continue
            result[row.id] = self.samples._to_entry(row)  # noqa: SLF001 - same module family
        return result

    async def _add_visual_notes(
        self, context: MemoryContext, query: str, retriever: HybridRetriever
    ) -> None:
        items, _ = await self.visuals.list(
            search=query, approved_only=True, limit=MAX_VISUAL_NOTES * OVERSAMPLE
        )
        ranked = await retriever.rank(
            source_type="visual_knowledge",
            query=query,
            limit=MAX_VISUAL_NOTES,
            keyword_items=items,
            id_of=lambda visual: visual.id,
            semantic_lookup=self._lookup_visuals,
        )
        for visual in ranked:
            label = visual.diagram_kind or visual.kind
            note = f"a {label} visual"
            if visual.topic:
                note += f" about '{visual.topic}'"
            if visual.description:
                note += f": {visual.description}"
            context.visual_notes.append(_clip(note))
            context.sources.append(f"visual_knowledge:{visual.id}")

    async def _lookup_visuals(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, VisualKnowledge]:
        if not ids:
            return {}
        async with get_session_factory()() as session:
            rows = await VisualKnowledgeRepository(session).get_many(ids)
        return {row.id: self.visuals._to_entry(row) for row in rows if row.approved}  # noqa: SLF001

    async def _add_history_notes(
        self,
        context: MemoryContext,
        query: str,
        exclude_course_id: str | None,
        retriever: HybridRetriever,
    ) -> None:
        if not self.settings.use_database:
            return
        async with get_session_factory()() as session:
            rows = await GenerationRunRepository(session).list_recent_successful_for_topic(
                query, exclude_course_id=exclude_course_id, limit=MAX_HISTORY_NOTES * OVERSAMPLE
            )
        ranked = await retriever.rank(
            source_type="generation_run",
            query=query,
            limit=MAX_HISTORY_NOTES,
            keyword_items=list(rows),
            id_of=lambda pair: pair[0].id,
            semantic_lookup=lambda ids: self._lookup_history(ids, exclude_course_id),
        )
        for run, course in ranked:
            payload = run.payload_json or {}
            generated = len(payload.get("chapters_generated") or [])
            failed = len(payload.get("chapters_failed") or [])
            note = f"a similar course ('{course.title}') completed with {generated} chapter(s) generated"
            if failed:
                note += f" and {failed} failed"
            context.history_notes.append(_clip(note))
            context.sources.append(f"generation_run:{run.id}")

    async def _lookup_history(
        self, ids: list[uuid.UUID], exclude_course_id: str | None
    ):
        if not ids:
            return {}
        async with get_session_factory()() as session:
            return await GenerationRunRepository(session).get_with_course_by_ids(
                ids, exclude_course_id=exclude_course_id
            )


def _clip(text: str) -> str:
    return (text or "").strip()[:MAX_NOTE_CHARS]


_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    global _service
    if _service is None:
        _service = MemoryService()
    return _service
