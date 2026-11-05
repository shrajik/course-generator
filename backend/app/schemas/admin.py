"""Admin API response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.roles import Role


UserRole = Literal[Role.AUTHOR.value, Role.EDITOR_REVIEWER.value, Role.MANAGER.value, Role.ADMIN.value]


class AdminDashboardResponse(BaseModel):
    total_users: int
    total_courses: int
    system_status: str


class AdminUserResponse(BaseModel):
    id: UUID
    email: str
    role: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class UpdateUserRoleRequest(BaseModel):
    role: UserRole = Field(description="The role to assign to the user.")


class UpdateUserStatusRequest(BaseModel):
    is_active: bool = Field(description="Whether the user account is active.")


class AdminCourseResponse(BaseModel):
    id: UUID
    course_id: str
    document_id: str
    title: str
    status: str
    template_id: str
    owner: str | None = None
    target_audience: str | None = None
    language: str | None = None
    tone: str | None = None
    toc_count: int = 0
    chapters_count: int = 0
    has_blueprint: bool = False
    has_document: bool = False
    last_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # Additive reflection of CourseRecord.quality_snapshot.outcome (see its
    # docstring in app.schemas.course) - distinct from `status`, which never
    # changes because of a quality finding. `None` when no snapshot exists
    # (never generated, or evaluation itself failed at that time).
    quality_outcome: Literal["passed", "failed"] | None = None
    run_state: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AdminCourseListResponse(BaseModel):
    courses: list[AdminCourseResponse]
    total: int
    limit: int
    offset: int
    statuses: list[str] = Field(default_factory=list)


class ModelUsageRecord(BaseModel):
    """One real AI call made while generating a course - the admin-facing
    shape of `app.core.metrics.CallRecord`, persisted into a
    `GenerationRun.payload_json["model_usage"]` row at the end of a run (see
    CourseService._finalise). MOM Phase 2.2 - AI Model Transparency.

    `provider`/`used_fallback` distinguish "which Azure resource/deployment
    actually answered" and "was this the originally-requested model or the
    configured fallback" from the bare model name alone. A deterministic
    stage (an SVG diagram, a concept_experience HTML render) never appears
    here at all - it never calls a model, so there's nothing to log; it is
    not represented as a fabricated "deterministic" entry, it is simply
    absent, same as the real CallRecord list this is read from."""

    kind: str
    purpose: str
    model: str
    provider: str = ""
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    failed: bool = False
    used_fallback: bool = False
    timestamp: str = ""


class AdminModelUsageResponse(BaseModel):
    course_id: str
    job_id: str | None = None
    run_state: str | None = None
    run_created_at: datetime | None = None
    calls: list[ModelUsageRecord] = Field(default_factory=list)
