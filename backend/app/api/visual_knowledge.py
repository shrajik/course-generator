"""Visual knowledge registry API.

Admin-only (`Permission.MANAGE_TEMPLATES_AND_KNOWLEDGE_BASE` in
app.core.roles - already defined, never wired up before this). This is a
system-level knowledge base curated by admins, not per-user content: the AI
memory layer (app.services.memory_service) reads approved entries for every
user's generation regardless of who registered/approved them, but only an
admin can add or approve one - the same trust boundary as template management.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_admin
from app.db.models import User
from app.schemas.memory import (
    VisualKnowledgeCreateRequest,
    VisualKnowledgeEntry,
    VisualKnowledgeListResponse,
    VisualKnowledgeUpdateRequest,
)
from app.services.visual_knowledge_service import VisualKnowledgeService, get_visual_knowledge_service

router = APIRouter(prefix="/api/visual-knowledge", tags=["visual-knowledge"])


def _service() -> VisualKnowledgeService:
    return get_visual_knowledge_service()


@router.get("", response_model=VisualKnowledgeListResponse)
async def list_visual_knowledge(
    search: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None),
    tags: list[str] | None = Query(default=None),
    approved_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: VisualKnowledgeService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> VisualKnowledgeListResponse:
    items, total = await service.list(
        search=search, kind=kind, tags=tags, approved_only=approved_only, limit=limit, offset=offset
    )
    return VisualKnowledgeListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", status_code=201, response_model=VisualKnowledgeEntry)
async def register_visual_knowledge(
    request: VisualKnowledgeCreateRequest,
    service: VisualKnowledgeService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> VisualKnowledgeEntry:
    return await service.register(request, created_by=current_admin.id)


@router.get("/{entry_id}", response_model=VisualKnowledgeEntry)
async def get_visual_knowledge(
    entry_id: UUID,
    service: VisualKnowledgeService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> VisualKnowledgeEntry:
    return await service.get(entry_id)


@router.patch("/{entry_id}", response_model=VisualKnowledgeEntry)
async def update_visual_knowledge(
    entry_id: UUID,
    request: VisualKnowledgeUpdateRequest,
    service: VisualKnowledgeService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> VisualKnowledgeEntry:
    return await service.update(entry_id, request)


@router.delete("/{entry_id}", status_code=204)
async def delete_visual_knowledge(
    entry_id: UUID,
    service: VisualKnowledgeService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> None:
    """Removes the registry entry only - the underlying asset file (still
    owned by its course) is never touched."""
    await service.delete(entry_id)
