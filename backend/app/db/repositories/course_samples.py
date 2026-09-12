"""Course sample (generation-reference) data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CourseSample


class CourseSampleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, sample: CourseSample) -> CourseSample:
        self.session.add(sample)
        await self.session.flush()
        return sample

    async def get_by_id(self, pk: uuid.UUID) -> CourseSample | None:
        return await self.session.get(CourseSample, pk)

    async def get_by_course_pk(self, course_pk: uuid.UUID) -> CourseSample | None:
        return await self.session.scalar(
            select(CourseSample).where(CourseSample.course_pk == course_pk)
        )

    async def get_many(self, ids: Sequence[uuid.UUID]) -> Sequence[CourseSample]:
        if not ids:
            return []
        result = await self.session.scalars(select(CourseSample).where(CourseSample.id.in_(ids)))
        return result.all()

    def _filtered_query(
        self,
        *,
        search: str | None = None,
        tags: list[str] | None = None,
        approved_only: bool = False,
    ) -> Select[tuple[CourseSample]]:
        query = select(CourseSample)
        if search:
            term = search.strip()
            if term:
                query = query.where(
                    or_(
                        CourseSample.title.ilike(f"%{term}%"),
                        CourseSample.topic.ilike(f"%{term}%"),
                        CourseSample.description.ilike(f"%{term}%"),
                    )
                )
        if tags:
            query = query.where(CourseSample.tags.overlap(tags))
        if approved_only:
            query = query.where(CourseSample.approved.is_(True))
        return query

    async def count(self, **filters: object) -> int:
        query = self._filtered_query(**filters).subquery()  # type: ignore[arg-type]
        return await self.session.scalar(select(func.count()).select_from(query)) or 0

    async def list(self, limit: int = 50, offset: int = 0, **filters: object) -> Sequence[CourseSample]:
        result = await self.session.scalars(
            self._filtered_query(**filters)  # type: ignore[arg-type]
            .order_by(CourseSample.approved.desc(), CourseSample.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()

    async def update(self, sample: CourseSample) -> CourseSample:
        await self.session.merge(sample)
        await self.session.flush()
        return sample

    async def delete(self, sample: CourseSample) -> None:
        await self.session.delete(sample)
