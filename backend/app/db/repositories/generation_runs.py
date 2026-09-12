"""Generation run (per-attempt history) data access operations.

`generation_runs` was created by the very first migration but never wired up
to a repository - `CourseRecord.run` only ever tracked the *latest* attempt,
overwritten every time. This repository finishes that table's original job:
one row per generation attempt, so history survives being overwritten.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course, GenerationRun


class GenerationRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, run: GenerationRun) -> GenerationRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_by_job_id(self, job_id: str) -> GenerationRun | None:
        return await self.session.scalar(
            select(GenerationRun).where(GenerationRun.job_id == job_id)
        )

    async def list_for_course(
        self, course_pk: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> Sequence[GenerationRun]:
        result = await self.session.scalars(
            select(GenerationRun)
            .where(GenerationRun.course_pk == course_pk)
            .order_by(GenerationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()

    async def list_recent_successful(
        self, *, exclude_course_pk: uuid.UUID | None = None, limit: int = 20
    ) -> Sequence[GenerationRun]:
        """Recent completed runs across all courses - the raw material for
        cross-course generation-history memory (see MemoryService)."""
        query = select(GenerationRun).where(GenerationRun.state == "done")
        if exclude_course_pk is not None:
            query = query.where(GenerationRun.course_pk != exclude_course_pk)
        result = await self.session.scalars(
            query.order_by(GenerationRun.created_at.desc()).limit(limit)
        )
        return result.all()

    async def list_recent_successful_for_topic(
        self, search: str, *, exclude_course_id: str | None = None, limit: int = 5
    ) -> Sequence[tuple[GenerationRun, Course]]:
        """Completed runs on courses whose title resembles `search` - the
        keyword-matched (no embeddings) equivalent of "similar past courses",
        for cross-course generation-history memory (see MemoryService)."""
        term = search.strip()
        if not term:
            return []
        query = (
            select(GenerationRun, Course)
            .join(Course, Course.id == GenerationRun.course_pk)
            .where(GenerationRun.state == "done", Course.title.ilike(f"%{term}%"))
        )
        if exclude_course_id:
            query = query.where(Course.course_id != exclude_course_id)
        result = await self.session.execute(
            query.order_by(GenerationRun.created_at.desc()).limit(limit)
        )
        return list(result.all())

    async def update(self, run: GenerationRun) -> GenerationRun:
        await self.session.merge(run)
        await self.session.flush()
        return run

    async def get_by_id(self, pk: uuid.UUID) -> GenerationRun | None:
        return await self.session.get(GenerationRun, pk)

    async def get_with_course_by_ids(
        self, run_ids: Sequence[uuid.UUID], *, exclude_course_id: str | None = None
    ) -> dict[uuid.UUID, tuple[GenerationRun, Course]]:
        """Resolve semantic search hits (bare run ids) back into the same
        (run, course) shape `list_recent_successful_for_topic` returns -
        re-applies the same "done" + exclude filters so a semantic hit on a
        failed run or the excluded course is dropped just like the keyword
        path drops it."""
        if not run_ids:
            return {}
        query = (
            select(GenerationRun, Course)
            .join(Course, Course.id == GenerationRun.course_pk)
            .where(GenerationRun.id.in_(run_ids), GenerationRun.state == "done")
        )
        if exclude_course_id:
            query = query.where(Course.course_id != exclude_course_id)
        result = await self.session.execute(query)
        return {run.id: (run, course) for run, course in result.all()}

    async def list_all_with_course(
        self, limit: int = 200, offset: int = 0
    ) -> list[tuple[GenerationRun, Course]]:
        """Every run, any state - used by the embedding backfill script."""
        result = await self.session.execute(
            select(GenerationRun, Course)
            .join(Course, Course.id == GenerationRun.course_pk)
            .order_by(GenerationRun.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.all())

    async def count_all(self) -> int:
        return await self.session.scalar(select(func.count()).select_from(GenerationRun)) or 0
