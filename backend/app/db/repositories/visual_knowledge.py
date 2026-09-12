"""Visual knowledge (reusable visuals registry) data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VisualKnowledge


class VisualKnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, entry: VisualKnowledge) -> VisualKnowledge:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def get_by_id(self, pk: uuid.UUID) -> VisualKnowledge | None:
        return await self.session.get(VisualKnowledge, pk)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> Sequence[VisualKnowledge]:
        if not ids:
            return []
        result = await self.session.scalars(select(VisualKnowledge).where(VisualKnowledge.id.in_(ids)))
        return result.all()

    def _filtered_query(
        self,
        *,
        search: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        approved_only: bool = False,
    ) -> Select[tuple[VisualKnowledge]]:
        query = select(VisualKnowledge)
        if search:
            term = search.strip()
            if term:
                query = query.where(
                    or_(
                        VisualKnowledge.topic.ilike(f"%{term}%"),
                        VisualKnowledge.description.ilike(f"%{term}%"),
                    )
                )
        if kind:
            query = query.where(VisualKnowledge.kind == kind)
        if tags:
            # Any overlap between the requested tags and the row's tags.
            query = query.where(VisualKnowledge.tags.overlap(tags))
        if approved_only:
            query = query.where(VisualKnowledge.approved.is_(True))
        return query

    async def count(self, **filters: object) -> int:
        query = self._filtered_query(**filters).subquery()  # type: ignore[arg-type]
        return await self.session.scalar(select(func.count()).select_from(query)) or 0

    async def list(
        self, limit: int = 50, offset: int = 0, **filters: object
    ) -> Sequence[VisualKnowledge]:
        result = await self.session.scalars(
            self._filtered_query(**filters)  # type: ignore[arg-type]
            .order_by(VisualKnowledge.approved.desc(), VisualKnowledge.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()

    async def update(self, entry: VisualKnowledge) -> VisualKnowledge:
        await self.session.merge(entry)
        await self.session.flush()
        return entry

    async def delete(self, entry: VisualKnowledge) -> None:
        await self.session.delete(entry)
