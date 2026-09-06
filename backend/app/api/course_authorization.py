"""Per-user course ownership, shared by every course and document endpoint.

Centralised here so each route calls one function instead of comparing
`record.owner_id` inline - see `get_current_user_if_db_enabled` in
`dependencies.py` for why `current_user` can be None (offline/filesystem mode
has no user identity, so ownership is not enforced there).

Two access levels:
- `user_can_access_course` (view/edit): owner, admin, or an editor/reviewer
  looking at a course that's actually been submitted (not a private draft).
  This is what `get_owned_course`/`check_document_owner` enforce, so it also
  covers the editor and document-save routes - a reviewer can open and edit a
  submitted course the same way its author can.
- `user_owns_course` (author-only actions): owner or admin, full stop - used
  for actions that must stay with the author, like submitting for review.
  Approve/request-changes don't use either of these; they gate on role alone
  (`require_roles(Role.EDITOR_REVIEWER, Role.ADMIN)`) since a reviewer must be
  able to act on courses they don't own.
"""

from __future__ import annotations

from app.core.errors import ForbiddenError
from app.core.roles import Role
from app.db.models import User
from app.schemas.course import CourseRecord
from app.services.course_service import CourseService
from app.services.document_service import DocumentService


def user_owns_course(user: User, record: CourseRecord) -> bool:
    """Strict: the author (or admin) only - used for author-only actions
    like submitting a course for review."""
    if user.role == Role.ADMIN.value:
        return True
    return record.owner_id is not None and record.owner_id == str(user.id)


def user_can_access_course(user: User, record: CourseRecord) -> bool:
    """Broader: owner, admin, or an editor/reviewer viewing/editing a course
    that has actually been submitted (not a private, unsubmitted draft)."""
    if user_owns_course(user, record):
        return True
    if user.role == Role.EDITOR_REVIEWER.value and record.review_status != "draft":
        return True
    return False


async def get_owned_course(
    course_id: str, current_user: User | None, service: CourseService
) -> CourseRecord:
    """Load a course, raising ForbiddenError if `current_user` can't access it
    (see `user_can_access_course`). Returns the record so callers don't have
    to fetch it twice."""
    record = await service.get_course_record(course_id)
    if current_user is not None and not user_can_access_course(current_user, record):
        raise ForbiddenError("You do not have access to this course")
    return record


async def get_course_for_author_action(
    course_id: str, current_user: User | None, service: CourseService
) -> CourseRecord:
    """Like `get_owned_course`, but for actions only the author (or admin) may
    take - e.g. submitting a course for review. A reviewer can view a
    submitted course without being allowed to resubmit it."""
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
    """Resolve a document_id to its course_id, enforcing access. Returns course_id."""
    course_id = document_service.resolve_course_id(document_id)
    await get_owned_course(course_id, current_user, course_service)
    return course_id
