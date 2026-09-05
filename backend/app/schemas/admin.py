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
