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
