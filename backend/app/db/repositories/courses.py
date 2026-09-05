"""Course data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course


class CourseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, course: Course) -> Course:
        self.session.add(course)
        await self.session.flush()
        return course

    async def get_by_course_id(self, course_id: str) -> Course | None:
        return await self.session.scalar(select(Course).where(Course.course_id == course_id))

    def _filtered_query(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        owner_id: uuid.UUID | None = None,
    ) -> Select[tuple[Course]]:
        query = select(Course)
        if search:
            term = search.strip()
            if term:
                clauses = [
                    Course.title.ilike(f"%{term}%"),
                    Course.course_id.ilike(f"%{term}%"),
                    Course.document_id.ilike(f"%{term}%"),
                    Course.metadata_json["owner"].as_string().ilike(f"%{term}%"),
                ]
                try:
                    clauses.append(Course.id == uuid.UUID(term))
                except ValueError:
                    pass
                query = query.where(or_(*clauses))
        if status:
            query = query.where(Course.status == status)
        if owner:
            owner_term = owner.strip()
            if owner_term:
                query = query.where(Course.metadata_json["owner"].as_string().ilike(f"%{owner_term}%"))
        if owner_id is not None:
            # Real per-user ownership (courses.owner_id), distinct from the
            # free-text `owner` display filter above used by the admin UI.
            query = query.where(Course.owner_id == owner_id)
        return query

    async def count(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        owner_id: uuid.UUID | None = None,
    ) -> int:
        query = self._filtered_query(
            search=search, status=status, owner=owner, owner_id=owner_id
        ).subquery()
        return await self.session.scalar(select(func.count()).select_from(query)) or 0

    async def list(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        search: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        owner_id: uuid.UUID | None = None,
    ) -> Sequence[Course]:
        result = await self.session.scalars(
            self._filtered_query(search=search, status=status, owner=owner, owner_id=owner_id)
            .order_by(Course.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()

    async def distinct_statuses(self) -> Sequence[str]:
        result = await self.session.scalars(
            select(Course.status).distinct().order_by(Course.status)
        )
        return result.all()

    async def delete(self, course: Course) -> None:
        await self.session.delete(course)

    async def update(self, course: Course) -> Course:
        await self.session.merge(course)
        await self.session.flush()
        return course

    async def get_by_id(self, course_id: uuid.UUID) -> Course | None:
        return await self.session.get(Course, course_id)
