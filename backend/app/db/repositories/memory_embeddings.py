"""Semantic memory data access - the only place that runs a pgvector query.

Every source type (template/visual_knowledge/course_sample/generation_run)
shares this one table and this one repository instead of four near-identical
ones - see MemoryEmbedding in app/db/models.py for the storage design.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryEmbedding


class MemoryEmbeddingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, *, source_type: str, source_id: uuid.UUID) -> MemoryEmbedding | None:
        return await self.session.scalar(
            select(MemoryEmbedding).where(
                MemoryEmbedding.source_type == source_type,
                MemoryEmbedding.source_id == source_id,
            )
        )

    async def upsert(
        self,
        *,
        source_type: str,
        source_id: uuid.UUID,
        model: str,
        content_hash: str,
        embedded_text: str,
        embedding: Sequence[float],
    ) -> MemoryEmbedding:
        """Create or replace the one embedding row for `(source_type,
        source_id)`. A version row (Template) gets its own source_id, so this
        never overwrites a previous version's embedding - only the same row's
        embedding is ever replaced, which is exactly the "invalidate and
        regenerate on content change" lifecycle."""
        row = await self.get(source_type=source_type, source_id=source_id)
        now = datetime.now(timezone.utc)
        if row is None:
            row = MemoryEmbedding(
                source_type=source_type,
                source_id=source_id,
                model=model,
                content_hash=content_hash,
                embedded_text=embedded_text,
                embedding=list(embedding),
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.model = model
            row.content_hash = content_hash
            row.embedded_text = embedded_text
            row.embedding = list(embedding)
            row.updated_at = now
        await self.session.flush()
        return row

    async def delete(self, *, source_type: str, source_id: uuid.UUID) -> None:
        row = await self.get(source_type=source_type, source_id=source_id)
        if row is not None:
            await self.session.delete(row)

    async def search(
        self,
        *,
        source_type: str,
        query_vector: Sequence[float],
        limit: int,
        threshold: float,
    ) -> list[tuple[MemoryEmbedding, float]]:
        """Nearest neighbours by cosine similarity, restricted to one source
        type and to `similarity >= threshold` - weak matches are dropped in
        SQL rather than filtered in Python, so a caller that gets [] back can
        treat "no semantic results" and "fall back to keyword" as the same
        thing. `1 - cosine_distance` turns pgvector's distance (0 = identical)
        into a similarity in roughly [0, 1] (technically [-1, 1] for
        arbitrary vectors, but both our embedding sources are effectively
        non-negative-cosine text embeddings)."""
        distance = MemoryEmbedding.embedding.cosine_distance(list(query_vector))
        similarity = (1 - distance).label("similarity")
        stmt = (
            select(MemoryEmbedding, similarity)
            .where(MemoryEmbedding.source_type == source_type)
            .where(similarity >= threshold)
            .order_by(distance)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [(row, float(sim)) for row, sim in result.all()]

    async def count(self, *, source_type: str) -> int:
        return (
            await self.session.scalar(
                select(func.count()).select_from(MemoryEmbedding).where(
                    MemoryEmbedding.source_type == source_type
                )
            )
            or 0
        )

    async def get_many(
        self, *, source_type: str, source_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, MemoryEmbedding]:
        """Used by the backfill script to skip rows that already have a
        valid (matching content_hash) embedding without one query per row."""
        if not source_ids:
            return {}
        result = await self.session.scalars(
            select(MemoryEmbedding).where(
                MemoryEmbedding.source_type == source_type,
                MemoryEmbedding.source_id.in_(source_ids),
            )
        )
        return {row.source_id: row for row in result.all()}
