"""Course activity (audit log) data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course, CourseActivity


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

    async def list_for_owner(
        self, owner_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> Sequence[tuple[CourseActivity, str, str, str]]:
        """Every activity entry across every course this user owns, newest
        first - the workspace-wide feed (see `list_for_course` for the
        single-course version). Joined against `courses` (rather than a
        second query per row) so each result also carries the course's own
        `course_id`/`document_id`/`title`, which the caller needs to link
        each entry back to the course (the editor route is keyed by
        `document_id`, not `course_id`) it happened in."""
        result = await self.session.execute(
            select(CourseActivity, Course.course_id, Course.document_id, Course.title)
            .join(Course, Course.id == CourseActivity.course_pk)
            .where(Course.owner_id == owner_id)
            .order_by(CourseActivity.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()
