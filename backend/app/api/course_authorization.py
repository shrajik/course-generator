"""Per-user course ownership, shared by every course and document endpoint.

Centralised here so each route calls one function instead of comparing
`record.owner_id` inline - see `get_current_user_if_db_enabled` in
`dependencies.py` for why `current_user` can be None (offline/filesystem mode
has no user identity, so ownership is not enforced there).
"""

from __future__ import annotations

from app.core.errors import ForbiddenError
from app.core.roles import Role
from app.db.models import User
from app.schemas.course import CourseRecord
from app.services.course_service import CourseService
from app.services.document_service import DocumentService


def user_owns_course(user: User, record: CourseRecord) -> bool:
    if user.role == Role.ADMIN.value:
        return True
    return record.owner_id is not None and record.owner_id == str(user.id)


async def get_owned_course(
    course_id: str, current_user: User | None, service: CourseService
) -> CourseRecord:
    """Load a course, raising ForbiddenError if `current_user` isn't its owner
    or an admin. Returns the record so callers don't have to fetch it twice."""
    record = await service.get_course_record(course_id)
    if current_user is not None and not user_owns_course(current_user, record):
        raise ForbiddenError("You do not have access to this course")
    return record


async def check_document_owner(
    document_id: str,
    current_user: User | None,
    course_service: CourseService,
    document_service: DocumentService,
) -> str:
    """Resolve a document_id to its course_id, enforcing ownership. Returns course_id."""
    course_id = document_service.resolve_course_id(document_id)
    await get_owned_course(course_id, current_user, course_service)
    return course_id
