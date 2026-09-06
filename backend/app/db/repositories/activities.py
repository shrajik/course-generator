"""Course activity (audit log) data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CourseActivity


class CourseActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, activity: CourseActivity) -> CourseActivity:
        self.session.add(activity)
        await self.session.flush()
        return activity

    async def list_for_course(
        self, course_pk: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> Sequence[CourseActivity]:
        result = await self.session.scalars(
            select(CourseActivity)
            .where(CourseActivity.course_pk == course_pk)
            .order_by(CourseActivity.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()
