"""Course samples: mark an existing, already-generated course as a reusable
generation reference. Never copies content - only `course_pk`/`course_id`
are stored; the sample's actual chapters/blocks are read fresh from the
existing Course/Document rows at retrieval time (see MemoryService).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.errors import ConflictError, NotFoundError
from app.db.models import CourseSample
from app.db.repositories.course_samples import CourseSampleRepository
from app.db.repositories.courses import CourseRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    CourseSampleCreateRequest,
    CourseSampleEntry,
    CourseSampleUpdateRequest,
)
from app.services.memory_embedding_sync import delete_embedding, sync_embedding
from app.services.memory_embedding_text import sample_text


class CourseSampleService:
    async def register(
        self, request: CourseSampleCreateRequest, *, created_by: uuid.UUID | None = None
    ) -> CourseSampleEntry:
        now = datetime.now(timezone.utc)
        async with get_session_factory()() as session:
            async with session.begin():
                courses = CourseRepository(session)
                course = await courses.get_by_course_id(request.course_id)
                if course is None:
                    raise NotFoundError(f"Course '{request.course_id}' not found")
                repo = CourseSampleRepository(session)
                if await repo.get_by_course_pk(course.id) is not None:
                    raise ConflictError(f"Course '{request.course_id}' is already a sample")
                row = CourseSample(
                    course_pk=course.id,
                    course_id=course.course_id,
                    title=request.title or course.title,
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
        await sync_embedding("course_sample", row.id, sample_text(row))
        return entry

    async def update(
        self, entry_id: uuid.UUID, request: CourseSampleUpdateRequest
    ) -> CourseSampleEntry:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = CourseSampleRepository(session)
                row = await repo.get_by_id(entry_id)
                if row is None:
                    raise NotFoundError("Sample not found")
                if request.title is not None:
                    row.title = request.title
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
        # Content may have changed (title/topic/description/tags) - always
        # regenerate rather than trying to detect which fields actually
        # affect the embedded text.
        await sync_embedding("course_sample", row.id, sample_text(row))
        return entry

    async def get(self, entry_id: uuid.UUID) -> CourseSampleEntry:
        async with get_session_factory()() as session:
            row = await CourseSampleRepository(session).get_by_id(entry_id)
            if row is None:
                raise NotFoundError("Sample not found")
            return self._to_entry(row)

    async def delete(self, entry_id: uuid.UUID) -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = CourseSampleRepository(session)
                row = await repo.get_by_id(entry_id)
                if row is None:
                    raise NotFoundError("Sample not found")
                await repo.delete(row)
        await delete_embedding("course_sample", entry_id)

    async def list(
        self,
        *,
        search: str | None = None,
        tags: list[str] | None = None,
        approved_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CourseSampleEntry], int]:
        filters = {"search": search, "tags": tags, "approved_only": approved_only}
        async with get_session_factory()() as session:
            repo = CourseSampleRepository(session)
            rows = await repo.list(limit=limit, offset=offset, **filters)
            total = await repo.count(**filters)
            return [self._to_entry(row) for row in rows], total

    @staticmethod
    def _to_entry(row: CourseSample) -> CourseSampleEntry:
        return CourseSampleEntry(
            id=row.id,
            course_id=row.course_id,
            title=row.title,
            topic=row.topic,
            description=row.description,
            tags=row.tags,
            approved=row.approved,
            created_by=str(row.created_by) if row.created_by else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


_service: CourseSampleService | None = None


def get_course_sample_service() -> CourseSampleService:
    global _service
    if _service is None:
        _service = CourseSampleService()
    return _service
