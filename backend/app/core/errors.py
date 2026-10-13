"""Domain errors mapped to HTTP responses in main.py."""

from __future__ import annotations


class CourseCreatorError(Exception):
    """Base class for all application errors."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(CourseCreatorError):
    status_code = 404
    code = "not_found"


class ValidationFailedError(CourseCreatorError):
    status_code = 422
    code = "validation_failed"


class ConflictError(CourseCreatorError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(CourseCreatorError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(CourseCreatorError):
    status_code = 403
    code = "forbidden"


class AIServiceError(CourseCreatorError):
    status_code = 502
    code = "ai_service_error"


class RenderError(CourseCreatorError):
    status_code = 500
    code = "render_error"


class CodeExecutionUnavailableError(CourseCreatorError):
    """The sandboxed executor cannot be reached or declined to run the code.

    Distinct from a failing *program*: a program that crashes still gets a
    normal result with status "error". This means no verdict was obtained.
    """

    status_code = 503
    code = "code_execution_unavailable"


class IncompleteChapterInventoryError(ConflictError):
    """Raised by `submit_for_review`/`approve_course` (and their override
    paths) when a course's blueprint-expected chapter set
    (`CourseRecord.chapters`) is not fully present and loadable on disk -
    distinct from `QualityApprovalBlockedError` (a complete course with a
    real quality finding) and from `QualityEvaluationFailedError` (the
    report computation itself crashing). See
    `StorageService.verify_chapter_inventory`."""

    code = "incomplete_chapter_inventory"


class QualityApprovalBlockedError(ConflictError):
    """Raised by `approve_course` when the course's quality report contains
    at least one hard failure whose `verification_status` is not
    "unverified" (see the approval predicate in
    app.services.quality_framework) - a confirmed or verification-failed
    blocker, never a merely-unverified reviewer-model opinion."""

    code = "quality_approval_blocked"


class QualityEvaluationFailedError(CourseCreatorError):
    """Raised when the quality-report computation itself crashed while
    evaluating a course for approval - distinct from a complete report that
    contains a real finding. Never implies the course is clean or that any
    specific finding is true or false."""

    status_code = 500
    code = "quality_evaluation_failed"
