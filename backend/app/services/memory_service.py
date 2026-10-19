"""AI memory layer: bounded, best-effort retrieval across templates and
generation history.

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
from app.db.repositories.generation_runs import GenerationRunRepository
from app.db.session import get_session_factory
from app.schemas.memory import MAX_HISTORY_NOTES, MAX_NOTE_CHARS, MemoryContext
from app.schemas.template import CourseTemplate
from app.services.hybrid_retriever import OVERSAMPLE, HybridRetriever

log = get_logger(__name__)

Stage = Literal["planner", "writer", "reviewer", "visual"]


class MemoryService:
    def __init__(
        self,
        settings: Settings | None = None,
        hybrid: HybridRetriever | None = None,
    ) -> None:
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

        Templates and history are both DB-backed, so this is a
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

        # Planner: the structural shape of the template.
        if stage == "planner":
            if template is not None:
                context.template_notes = _clip(template.writer_guidance)

        # Reviewer: template requirements + a quality signal from history
        # (the reviewer never invents new content).
        elif stage == "reviewer":
            if template is not None and template.review_focus:
                context.template_notes = _clip("; ".join(template.review_focus))
            await self._add_history_notes(context, query, exclude_course_id, retriever)

        # The writer and visual stages have no memory source of their own.
        return context

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
