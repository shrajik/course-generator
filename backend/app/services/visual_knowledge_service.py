"""Visual knowledge: a registry over *existing* generated assets.

This deliberately does not duplicate any asset repository that might exist
elsewhere in the app - there isn't one; images/diagrams are generated
per-course by ImageService/DiagramService and live under
`data/courses/{course_id}/assets/`. This service only adds a searchable,
cross-course index on top of that (path + tags/topic/description/approval),
reusing StorageService to validate an asset actually exists before it's
registered, and the existing `/api/documents/{id}/assets/{name}` route
(ownership-checked) to serve the bytes - no new asset storage, no new
asset-serving route.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.ids import document_id_for_course
from app.db.models import VisualKnowledge
from app.db.repositories.visual_knowledge import VisualKnowledgeRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    VisualKnowledgeCreateRequest,
    VisualKnowledgeEntry,
    VisualKnowledgeUpdateRequest,
)
from app.services.memory_embedding_sync import delete_embedding, sync_embedding
from app.services.memory_embedding_text import visual_text
from app.services.storage_service import StorageService, get_storage


class VisualKnowledgeService:
    def __init__(self, storage: StorageService | None = None) -> None:
        self.storage = storage or get_storage()

    async def register(
        self, request: VisualKnowledgeCreateRequest, *, created_by: uuid.UUID | None = None
    ) -> VisualKnowledgeEntry:
        if request.course_id:
            abs_path = self.storage.asset_abs_path(request.course_id, request.asset_path)
            if not abs_path.exists():
                raise ValidationFailedError(
                    f"No asset at '{request.asset_path}' for course '{request.course_id}'"
                )
        now = datetime.now(timezone.utc)
        async with get_session_factory()() as session:
            async with session.begin():
                repo = VisualKnowledgeRepository(session)
                row = VisualKnowledge(
                    course_id=request.course_id,
                    asset_path=request.asset_path,
                    kind=request.kind,
                    diagram_kind=request.diagram_kind,
                    topic=request.topic,
                    description=request.description,
                    tags=request.tags,
                    approved=False,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                await repo.create(row)
                entry = self._to_entry(row)
        await sync_embedding("visual_knowledge", row.id, visual_text(row))
        return entry

    async def update(
        self, entry_id: uuid.UUID, request: VisualKnowledgeUpdateRequest
    ) -> VisualKnowledgeEntry:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = VisualKnowledgeRepository(session)
                row = await repo.get_by_id(entry_id)
                if row is None:
                    raise NotFoundError("Visual not found")
                if request.topic is not None:
                    row.topic = request.topic
                if request.description is not None:
                    row.description = request.description
                if request.tags is not None:
                    row.tags = request.tags
                if request.approved is not None:
                    row.approved = request.approved
                row.updated_at = datetime.now(timezone.utc)
                await repo.update(row)
                entry = self._to_entry(row)
        await sync_embedding("visual_knowledge", row.id, visual_text(row))
        return entry

    async def get(self, entry_id: uuid.UUID) -> VisualKnowledgeEntry:
        async with get_session_factory()() as session:
            row = await VisualKnowledgeRepository(session).get_by_id(entry_id)
            if row is None:
                raise NotFoundError("Visual not found")
            return self._to_entry(row)

    async def delete(self, entry_id: uuid.UUID) -> None:
        """Removes the registry row only - the underlying asset file (still
        owned by its course) is never touched."""
        async with get_session_factory()() as session:
            async with session.begin():
                repo = VisualKnowledgeRepository(session)
                row = await repo.get_by_id(entry_id)
                if row is None:
                    raise NotFoundError("Visual not found")
                await repo.delete(row)
        await delete_embedding("visual_knowledge", entry_id)

    async def list(
        self,
        *,
        search: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        approved_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[VisualKnowledgeEntry], int]:
        filters = {"search": search, "kind": kind, "tags": tags, "approved_only": approved_only}
        async with get_session_factory()() as session:
            repo = VisualKnowledgeRepository(session)
            rows = await repo.list(limit=limit, offset=offset, **filters)
            total = await repo.count(**filters)
            return [self._to_entry(row) for row in rows], total

    @staticmethod
    def _to_entry(row: VisualKnowledge) -> VisualKnowledgeEntry:
        asset_url = None
        if row.course_id:
            name = row.asset_path.rsplit("/", 1)[-1]
            asset_url = f"/api/documents/{document_id_for_course(row.course_id)}/assets/{name}"
        return VisualKnowledgeEntry(
            id=row.id,
            course_id=row.course_id,
            asset_path=row.asset_path,
            asset_url=asset_url,
            kind=row.kind,
            diagram_kind=row.diagram_kind,
            topic=row.topic,
            description=row.description,
            tags=row.tags,
            approved=row.approved,
            created_by=str(row.created_by) if row.created_by else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


_service: VisualKnowledgeService | None = None


def get_visual_knowledge_service() -> VisualKnowledgeService:
    global _service
    if _service is None:
        _service = VisualKnowledgeService()
    return _service
