"""High-level database service wrapping repositories with Pydantic model conversion."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Blueprint, Course, Document
from app.db.repositories.blueprints import BlueprintRepository
from app.db.repositories.courses import CourseRepository
from app.db.repositories.documents import DocumentRepository
from app.db.session import get_session_factory
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import CourseInput, CourseRecord
from app.schemas.document import CourseDocument


class DatabaseService:
    """Wraps repositories to provide high-level operations using Pydantic models."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.courses = CourseRepository(session)
        self.documents = DocumentRepository(session)
        self.blueprints = BlueprintRepository(session)

    # --- Courses ----------------------------------------------------------
    async def save_course_record(self, record: CourseRecord) -> CourseRecord:
        """Create or update a course from a CourseRecord."""
        existing = await self.courses.get_by_course_id(record.course_id)
        now = datetime.now(timezone.utc)
        dumped = record.model_dump(mode="json")
        metadata = {
            key: value
            for key, value in dumped.items()
            if key
            not in {
                "course_id",
                "document_id",
                "status",
                "input",
                "template_id",
                "owner_id",
                "created_at",
                "updated_at",
            }
        }
        owner_id = uuid.UUID(record.owner_id) if record.owner_id else None
        if existing:
            existing.title = record.input.course_title
            existing.status = record.status
            existing.template_id = record.template_id
            existing.input_json = record.input.model_dump(mode="json")
            existing.metadata_json = metadata
            # Ownership is set once at creation and never overwritten by later
            # saves (e.g. a save with no owner_id set shouldn't clear it).
            if owner_id is not None:
                existing.owner_id = owner_id
            existing.updated_at = now
            course = await self.courses.update(existing)
        else:
            course = Course(
                course_id=record.course_id,
                document_id=record.document_id,
                title=record.input.course_title,
                status=record.status,
                template_id=record.template_id,
                owner_id=owner_id,
                input_json=record.input.model_dump(mode="json"),
                metadata_json=metadata,
                created_at=now,
                updated_at=now,
            )
            course = await self.courses.create(course)
        return self._to_course_record(course)

    async def load_course_record(self, course_id: str) -> CourseRecord:
        """Load a course as a CourseRecord."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        return self._to_course_record(course)

    async def list_course_records(
        self, limit: int = 100, offset: int = 0, *, owner_id: uuid.UUID | None = None
    ) -> list[CourseRecord]:
        """List courses as CourseRecords, optionally restricted to one owner."""
        courses = await self.courses.list(limit, offset, owner_id=owner_id)
        return [self._to_course_record(c) for c in courses]

    async def course_exists(self, course_id: str) -> bool:
        """Check if a course exists."""
        return await self.courses.get_by_course_id(course_id) is not None

    # --- Blueprints -------------------------------------------------------
    async def save_blueprint(self, course_id: str, blueprint: CourseBlueprint) -> None:
        """Save a blueprint for a course."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        existing = await self.blueprints.get_for_course(course.id)
        now = datetime.now(timezone.utc)
        if existing:
            existing.blueprint_json = blueprint.model_dump(mode="json")
            existing.updated_at = now
            await self.blueprints.update(existing)
        else:
            bp = Blueprint(
                course_pk=course.id,
                blueprint_json=blueprint.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            await self.blueprints.create(bp)

    async def load_blueprint(self, course_id: str) -> CourseBlueprint:
        """Load a blueprint for a course."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        blueprint = await self.blueprints.get_for_course(course.id)
        if blueprint is None:
            raise NotFoundError(
                f"Course '{course_id}' has no blueprint yet - run the planner first"
            )
        return CourseBlueprint.model_validate(blueprint.blueprint_json)

    async def has_blueprint(self, course_id: str) -> bool:
        """Check if a course has a blueprint."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            return False
        return await self.blueprints.get_for_course(course.id) is not None

    # --- Documents --------------------------------------------------------
    async def save_document(self, document: CourseDocument) -> None:
        """Save a CourseDocument."""
        course = await self.courses.get_by_course_id(document.course_id)
        if course is None:
            raise NotFoundError(f"Course '{document.course_id}' not found")
        existing = await self.documents.get_by_document_id(document.document_id)
        now = datetime.now(timezone.utc)
        if existing:
            existing.version = document.version
            existing.document_json = document.model_dump(mode="json")
            existing.updated_at = now
            await self.documents.update(existing)
        else:
            doc = Document(
                document_id=document.document_id,
                course_pk=course.id,
                version=document.version,
                document_json=document.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            await self.documents.create(doc)

    async def load_document(self, document_id: str) -> CourseDocument:
        """Load a CourseDocument by document_id."""
        document = await self.documents.get_by_document_id(document_id)
        if document is None:
            raise NotFoundError(f"Document '{document_id}' not found")
        return CourseDocument.model_validate(document.document_json)

    async def load_document_for_course(self, course_id: str) -> CourseDocument:
        """Load the CourseDocument that belongs to a course."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        document = await self.documents.get_for_course(course.id)
        if document is None:
            raise NotFoundError(
                f"Course '{course_id}' has no course document yet - run generation first"
            )
        return CourseDocument.model_validate(document.document_json)

    async def has_document(self, course_id: str) -> bool:
        """Check if a course has a document."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            return False
        return await self.documents.get_for_course(course.id) is not None

    # --- Helpers ----------------------------------------------------------
    @staticmethod
    def _to_course_record(course: Course) -> CourseRecord:
        """Convert a database Course to a CourseRecord Pydantic model."""
        return CourseRecord(
            course_id=course.course_id,
            document_id=course.document_id,
            status=course.status,
            input=CourseInput.model_validate(course.input_json),
            template_id=course.template_id,
            owner_id=str(course.owner_id) if course.owner_id else None,
            created_at=course.created_at.isoformat(),
            updated_at=course.updated_at.isoformat(),
            **(course.metadata_json or {}),
        )


@asynccontextmanager
async def get_database_service() -> AsyncIterator[DatabaseService]:
    """Open a short-lived session/transaction for a single unit of work.

    CourseService/DocumentService are long-lived singletons (and generation
    runs as a detached background task), so they cannot hold a single
    request-scoped AsyncSession the way a FastAPI `Depends` would - each
    persistence call gets its own session instead, committed on the way out.
    """
    async with get_session_factory()() as session:
        async with session.begin():
            yield DatabaseService(session)
