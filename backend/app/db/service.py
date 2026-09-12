"""High-level database service wrapping repositories with Pydantic model conversion."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models import Blueprint, Course, CourseActivity, Document, GenerationRun
from app.db.repositories.activities import CourseActivityRepository
from app.db.repositories.blueprints import BlueprintRepository
from app.db.repositories.courses import CourseRepository
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.generation_runs import GenerationRunRepository
from app.db.session import get_session_factory
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import CourseActivityEntry, CourseInput, CourseRecord, TocItem
from app.schemas.document import CourseDocument
from app.schemas.memory import GenerationRunEntry

log = get_logger(__name__)

# `CourseRecord` fields that live in their own dedicated `courses` columns
# (see `Course` in app/db/models.py) rather than the catch-all `metadata_json`
# column - every other `CourseRecord` field (chapters, run, warnings, ...)
# goes into `metadata_json`. Both writers (`save_course_record` below and
# `app.commands.import_filesystem_data`) and the reader (`_to_course_record`)
# share this one constant instead of each maintaining their own copy of the
# list, which is what let `import_filesystem_data.py` drift out of sync and
# leak `owner_id`/review fields into `metadata_json` - colliding with the
# same fields passed explicitly in `_to_course_record` and crashing
# `GET /api/courses` for every course, not just the imported one.
COURSE_RECORD_COLUMN_FIELDS = frozenset(
    {
        "course_id",
        "document_id",
        "status",
        "input",
        "template_id",
        "owner_id",
        "review_status",
        "review_comment",
        "reviewer_id",
        "reviewed_at",
        "created_at",
        "updated_at",
    }
)

# Placeholders used only when a persisted `input_json` is missing or has an
# invalid value for one of CourseInput's required fields (see
# `_coerce_course_input`). They make a legacy row representable without
# inventing anything that looks like real course content.
_LEGACY_TOC_PLACEHOLDER = [{"title": "Untitled chapter"}]
_LEGACY_TARGET_AUDIENCE_PLACEHOLDER = "Not specified (legacy record)"
_LEGACY_TEMPLATE_PLACEHOLDER = "technical"


def _safe_str_list(value: object) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _coerce_course_input(raw: object, course_id: str) -> CourseInput:
    """Hydrate a persisted `input_json` blob into a `CourseInput`.

    `CourseInput` is deliberately strict (`extra="forbid"`, required fields)
    because it also validates *new* course creation. Older or externally
    edited rows can predate a required field or carry a since-removed key,
    which would otherwise 500 every read that touches that row (including
    `GET /api/courses`, which fails the whole list for one bad course). On
    failure here we keep every field that still validates and backfill only
    what's missing/invalid with explicit placeholders, so the row stays
    listable without silently inventing real course content.
    """
    try:
        return CourseInput.model_validate(raw)
    except ValidationError as exc:
        log.warning(
            "Course %s has a legacy/invalid input_json; reconstructing with "
            "placeholders for missing or invalid fields: %s",
            course_id,
            exc,
        )
        source = raw if isinstance(raw, dict) else {}

        title = source.get("course_title")
        if not isinstance(title, str) or not (1 <= len(title) <= 300):
            title = "(untitled course)"

        toc_payload = list(_LEGACY_TOC_PLACEHOLDER)
        toc = source.get("toc")
        if isinstance(toc, list) and toc:
            try:
                toc_payload = [TocItem.model_validate(item).model_dump() for item in toc]
            except ValidationError:
                pass

        audience = source.get("target_audience")
        if not isinstance(audience, str) or not (1 <= len(audience) <= 2000):
            audience = _LEGACY_TARGET_AUDIENCE_PLACEHOLDER

        template = source.get("template")
        if template not in ("technical", "non_technical"):
            template = _LEGACY_TEMPLATE_PLACEHOLDER

        fallback = {
            "course_title": title,
            "toc": toc_payload,
            "target_audience": audience,
            "template": template,
            "dos": _safe_str_list(source.get("dos")),
            "donts": _safe_str_list(source.get("donts")),
            "language": source.get("language") if isinstance(source.get("language"), str) else "en",
            "tone": source.get("tone") if isinstance(source.get("tone"), str) else "",
        }
        return CourseInput.model_validate(fallback)


class DatabaseService:
    """Wraps repositories to provide high-level operations using Pydantic models."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.courses = CourseRepository(session)
        self.documents = DocumentRepository(session)
        self.blueprints = BlueprintRepository(session)
        self.activities = CourseActivityRepository(session)
        self.generation_runs = GenerationRunRepository(session)

    # --- Courses ----------------------------------------------------------
    async def save_course_record(self, record: CourseRecord) -> CourseRecord:
        """Create or update a course from a CourseRecord."""
        existing = await self.courses.get_by_course_id(record.course_id)
        now = datetime.now(timezone.utc)
        dumped = record.model_dump(mode="json")
        metadata = {
            key: value for key, value in dumped.items() if key not in COURSE_RECORD_COLUMN_FIELDS
        }
        owner_id = uuid.UUID(record.owner_id) if record.owner_id else None
        reviewer_id = uuid.UUID(record.reviewer_id) if record.reviewer_id else None
        reviewed_at = (
            datetime.fromisoformat(record.reviewed_at) if record.reviewed_at else None
        )
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
            # Review fields DO need to be clearable (e.g. resubmitting clears
            # the previous reviewer's comment), so always take the record's
            # current value rather than guarding on non-None like owner_id.
            existing.review_status = record.review_status
            existing.review_comment = record.review_comment
            existing.reviewer_id = reviewer_id
            existing.reviewed_at = reviewed_at
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
                review_status=record.review_status,
                review_comment=record.review_comment,
                reviewer_id=reviewer_id,
                reviewed_at=reviewed_at,
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
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        owner_id: uuid.UUID | None = None,
        review_statuses: list[str] | None = None,
    ) -> list[CourseRecord]:
        """List courses as CourseRecords, optionally restricted to one owner
        and/or to a set of review statuses (the reviewer's queue)."""
        courses = await self.courses.list(
            limit, offset, owner_id=owner_id, review_statuses=review_statuses
        )
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

    # --- Activity log -------------------------------------------------------
    async def record_activity(
        self,
        course_id: str,
        user_id: uuid.UUID | None,
        user_email: str | None,
        action: str,
        message: str | None = None,
    ) -> None:
        """Best-effort: if the course is gone there's nothing to attach the
        entry to, so this quietly no-ops rather than failing the caller's
        already-succeeded action."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            return
        await self.activities.create(
            CourseActivity(
                course_pk=course.id,
                user_id=user_id,
                user_email=user_email,
                action=action,
                message=message,
                created_at=datetime.now(timezone.utc),
            )
        )

    async def list_activities(
        self, course_id: str, limit: int = 50, offset: int = 0
    ) -> list[CourseActivityEntry]:
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        rows = await self.activities.list_for_course(course.id, limit, offset)
        return [self._to_activity_entry(row) for row in rows]

    # --- Generation history ------------------------------------------------
    # `generation_runs` existed since the very first migration but was never
    # written to - `CourseRecord.run` only ever tracked the latest attempt.
    # This records one row per attempt so history survives being overwritten.
    async def record_generation_run(
        self,
        course_id: str,
        *,
        job_id: str,
        state: str,
        payload: dict,
        error: str | None = None,
    ) -> tuple[GenerationRun, str] | None:
        """Best-effort: a missing course or a duplicate job_id (a retried
        finalise on the same run) must not break the generation it's meant
        to be recording. Returns the (run, course_title) pair so the caller
        can sync its memory embedding once this session has committed -
        never None just because it's an update rather than an insert."""
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            return None
        now = datetime.now(timezone.utc)
        existing = await self.generation_runs.get_by_job_id(job_id)
        if existing is not None:
            existing.state = state
            existing.payload_json = payload
            existing.error = error
            existing.updated_at = now
            run = await self.generation_runs.update(existing)
            return run, course.title
        run = await self.generation_runs.create(
            GenerationRun(
                course_pk=course.id,
                job_id=job_id,
                state=state,
                payload_json=payload,
                error=error,
                created_at=now,
                updated_at=now,
            )
        )
        return run, course.title

    async def list_generation_runs(
        self, course_id: str, limit: int = 20, offset: int = 0
    ) -> list[GenerationRunEntry]:
        course = await self.courses.get_by_course_id(course_id)
        if course is None:
            raise NotFoundError(f"Course '{course_id}' not found")
        rows = await self.generation_runs.list_for_course(course.id, limit, offset)
        return [self._to_generation_run_entry(row) for row in rows]

    @staticmethod
    def _to_generation_run_entry(row: GenerationRun) -> GenerationRunEntry:
        payload = row.payload_json or {}
        return GenerationRunEntry(
            id=row.id,
            job_id=row.job_id,
            state=row.state,
            chapters_generated=payload.get("chapters_generated") or [],
            chapters_failed=payload.get("chapters_failed") or [],
            error=row.error,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_activity_entry(row: CourseActivity) -> CourseActivityEntry:
        return CourseActivityEntry(
            id=str(row.id),
            action=row.action,
            user_id=str(row.user_id) if row.user_id else None,
            user_email=row.user_email,
            message=row.message,
            created_at=row.created_at.isoformat(),
        )

    # --- Helpers ----------------------------------------------------------
    @staticmethod
    def _to_course_record(course: Course) -> CourseRecord:
        """Convert a database Course to a CourseRecord Pydantic model."""
        # A row written before a writer's exclusion list matched
        # COURSE_RECORD_COLUMN_FIELDS (e.g. an older import_filesystem_data.py)
        # can have `owner_id`/review fields duplicated inside `metadata_json`
        # itself. Drop anything that collides with a field already supplied
        # explicitly below instead of letting `CourseRecord(...)` raise
        # "got multiple values" for it - the explicit, column-backed value
        # always wins over whatever a legacy `metadata_json` also has.
        metadata = {
            key: value
            for key, value in (course.metadata_json or {}).items()
            if key not in COURSE_RECORD_COLUMN_FIELDS
        }
        return CourseRecord(
            course_id=course.course_id,
            document_id=course.document_id,
            status=course.status,
            input=_coerce_course_input(course.input_json, course.course_id),
            template_id=course.template_id,
            owner_id=str(course.owner_id) if course.owner_id else None,
            review_status=course.review_status,
            review_comment=course.review_comment,
            reviewer_id=str(course.reviewer_id) if course.reviewer_id else None,
            reviewed_at=course.reviewed_at.isoformat() if course.reviewed_at else None,
            created_at=course.created_at.isoformat(),
            updated_at=course.updated_at.isoformat(),
            **metadata,
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
