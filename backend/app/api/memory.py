"""AI memory layer inspection API - lets an admin see exactly what context a
generation stage would receive, and why (Phase 6's boundaries made visible),
without running any generation. Admin-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_admin
from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.db.models import User
from app.db.repositories.course_samples import CourseSampleRepository
from app.db.repositories.generation_runs import GenerationRunRepository
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.repositories.templates import TemplateRepository
from app.db.repositories.visual_knowledge import VisualKnowledgeRepository
from app.db.session import get_session_factory
from app.scripts.backfill_embeddings import BACKFILL_FUNCS
from app.schemas.memory import (
    EmbeddingBackfillRequest,
    EmbeddingBackfillResponse,
    EmbeddingBackfillSourceResult,
    EmbeddingSourceStatus,
    EmbeddingStatusResponse,
    MemoryPreviewRequest,
    MemoryPreviewResponse,
)
from app.services.memory_service import MemoryService, get_memory_service

router = APIRouter(prefix="/admin/memory", tags=["memory"])


def _service() -> MemoryService:
    return get_memory_service()


@router.post("/preview", response_model=MemoryPreviewResponse)
async def preview_memory(
    request: MemoryPreviewRequest,
    service: MemoryService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> MemoryPreviewResponse:
    template = load_template(request.template)
    context = await service.build_context(
        stage=request.stage,
        course_title=request.course_title,
        template=template,
        chapter_title=request.chapter_title,
    )
    return MemoryPreviewResponse(context=context, rendered=context.render())


@router.get("/embeddings/status", response_model=EmbeddingStatusResponse)
async def embedding_status(current_admin: User = Depends(get_current_admin)) -> EmbeddingStatusResponse:
    """How much of each memory source has a semantic embedding right now -
    lets an admin see whether a backfill is needed without SSH-ing in to run
    the CLI script directly."""
    settings = get_settings()
    async with get_session_factory()() as session:
        embeddings = MemoryEmbeddingRepository(session)
        sources = [
            EmbeddingSourceStatus(
                source_type="template",
                total_rows=await TemplateRepository(session).count_all(),
                embedded_rows=await embeddings.count(source_type="template"),
            ),
            EmbeddingSourceStatus(
                source_type="visual_knowledge",
                total_rows=await VisualKnowledgeRepository(session).count(),
                embedded_rows=await embeddings.count(source_type="visual_knowledge"),
            ),
            EmbeddingSourceStatus(
                source_type="course_sample",
                total_rows=await CourseSampleRepository(session).count(),
                embedded_rows=await embeddings.count(source_type="course_sample"),
            ),
            EmbeddingSourceStatus(
                source_type="generation_run",
                total_rows=await GenerationRunRepository(session).count_all(),
                embedded_rows=await embeddings.count(source_type="generation_run"),
            ),
        ]
    return EmbeddingStatusResponse(
        enabled=settings.enable_embeddings,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        similarity_threshold=settings.semantic_similarity_threshold,
        keyword_weight=settings.keyword_score_weight,
        semantic_weight=settings.semantic_score_weight,
        sources=sources,
    )


@router.post("/embeddings/backfill", response_model=EmbeddingBackfillResponse)
async def trigger_backfill(
    request: EmbeddingBackfillRequest, current_admin: User = Depends(get_current_admin)
) -> EmbeddingBackfillResponse:
    """Runs the same logic as `python -m app.scripts.backfill_embeddings`,
    inline, for an admin-triggered top-up. For a large first-time backfill on
    a big dataset, prefer running the CLI script directly (bounded
    concurrency, but this HTTP request blocks until every page is done)."""
    results = [
        EmbeddingBackfillSourceResult(
            source_type=source_type,
            scanned=report.scanned,
            embedded=report.embedded,
            skipped_valid=report.skipped_valid,
            skipped_blank=report.skipped_blank,
            failed=report.failed,
        )
        for source_type in request.source_types
        for report in [
            await BACKFILL_FUNCS[source_type](dry_run=request.dry_run, concurrency=request.concurrency)
        ]
    ]
    return EmbeddingBackfillResponse(dry_run=request.dry_run, results=results)
