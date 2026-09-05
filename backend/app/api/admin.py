"""Admin-only endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import get_args
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_admin
from app.core.errors import ForbiddenError, NotFoundError
from app.db.models import Course, User
from app.db.repositories.courses import CourseRepository
from app.db.repositories.users import UserRepository
from app.db.session import get_session_factory
from app.schemas.admin import (
    AdminCourseListResponse,
    AdminCourseResponse,
    AdminDashboardResponse,
    AdminUserListResponse,
    AdminUserResponse,
    UpdateUserStatusRequest,
    UpdateUserRoleRequest,
    UserRole,
)
from app.schemas.course import CourseStatus

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/health")
async def admin_health(current_admin: User = Depends(get_current_admin)) -> dict[str, str]:
    return {"status": "ok"}


@router.get("/dashboard", response_model=AdminDashboardResponse)
async def admin_dashboard(
    current_admin: User = Depends(get_current_admin),
) -> AdminDashboardResponse:
    async with get_session_factory()() as session:
        users = UserRepository(session)
        courses = CourseRepository(session)
        return AdminDashboardResponse(
            total_users=await users.count(),
            total_courses=await courses.count(),
            system_status="ok",
        )


@router.get("/users", response_model=AdminUserListResponse)
async def admin_users(
    search: str | None = Query(default=None, max_length=320),
    role: UserRole | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    is_verified: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
) -> AdminUserListResponse:
    async with get_session_factory()() as session:
        users = UserRepository(session)
        filters = {
            "search": search,
            "role": role,
            "is_active": is_active,
            "is_verified": is_verified,
        }
        user_rows = await users.list(limit=limit, offset=offset, **filters)
        return AdminUserListResponse(
            users=[_to_admin_user_response(user) for user in user_rows],
            total=await users.count(**filters),
            limit=limit,
            offset=offset,
        )


@router.get("/users/{user_id}", response_model=AdminUserResponse)
async def admin_user_detail(
    user_id: UUID,
    current_admin: User = Depends(get_current_admin),
) -> AdminUserResponse:
    async with get_session_factory()() as session:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found")
        return _to_admin_user_response(user)


@router.patch("/users/{user_id}/role", response_model=AdminUserResponse)
async def update_admin_user_role(
    user_id: UUID,
    request: UpdateUserRoleRequest,
    current_admin: User = Depends(get_current_admin),
) -> AdminUserResponse:
    if user_id == current_admin.id:
        raise ForbiddenError("Admins cannot change their own role")

    async with get_session_factory()() as session:
        async with session.begin():
            users = UserRepository(session)
            user = await users.get_by_id(user_id)
            if user is None:
                raise NotFoundError("User not found")
            user.role = request.role
            user.updated_at = datetime.now(timezone.utc)
            updated = await users.update(user)
            return _to_admin_user_response(updated)


@router.patch("/users/{user_id}/status", response_model=AdminUserResponse)
async def update_admin_user_status(
    user_id: UUID,
    request: UpdateUserStatusRequest,
    current_admin: User = Depends(get_current_admin),
) -> AdminUserResponse:
    if user_id == current_admin.id:
        raise ForbiddenError("Admins cannot deactivate themselves")

    async with get_session_factory()() as session:
        async with session.begin():
            users = UserRepository(session)
            user = await users.get_by_id(user_id)
            if user is None:
                raise NotFoundError("User not found")
            user.is_active = request.is_active
            user.updated_at = datetime.now(timezone.utc)
            updated = await users.update(user)
            return _to_admin_user_response(updated)


@router.get("/courses", response_model=AdminCourseListResponse)
async def admin_courses(
    search: str | None = Query(default=None, max_length=320),
    status: CourseStatus | None = Query(default=None),
    owner: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
) -> AdminCourseListResponse:
    async with get_session_factory()() as session:
        courses = CourseRepository(session)
        filters = {
            "search": search,
            "status": status,
            "owner": owner,
        }
        course_rows = await courses.list(limit=limit, offset=offset, **filters)
        statuses = sorted(set(get_args(CourseStatus)) | set(await courses.distinct_statuses()))
        return AdminCourseListResponse(
            courses=[_to_admin_course_response(course) for course in course_rows],
            total=await courses.count(**filters),
            limit=limit,
            offset=offset,
            statuses=statuses,
        )


@router.get("/courses/{course_id}", response_model=AdminCourseResponse)
async def admin_course_detail(
    course_id: UUID,
    current_admin: User = Depends(get_current_admin),
) -> AdminCourseResponse:
    async with get_session_factory()() as session:
        course = await CourseRepository(session).get_by_id(course_id)
        if course is None:
            raise NotFoundError("Course not found")
        return _to_admin_course_response(course)


def _to_admin_user_response(user: User) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _to_admin_course_response(course: Course) -> AdminCourseResponse:
    metadata = course.metadata_json or {}
    input_data = course.input_json or {}
    owner = None
    raw_owner = metadata.get("owner")
    if isinstance(raw_owner, str) and raw_owner:
        owner = raw_owner
    run = metadata.get("run")
    run_state = run.get("state") if isinstance(run, dict) and isinstance(run.get("state"), str) else None
    chapters = metadata.get("chapters")
    warnings = metadata.get("warnings")
    return AdminCourseResponse(
        id=course.id,
        course_id=course.course_id,
        document_id=course.document_id,
        title=course.title,
        status=course.status,
        template_id=course.template_id,
        owner=owner,
        target_audience=input_data.get("target_audience") if isinstance(input_data, dict) else None,
        language=input_data.get("language") if isinstance(input_data, dict) else None,
        tone=input_data.get("tone") if isinstance(input_data, dict) else None,
        toc_count=len(input_data.get("toc", [])) if isinstance(input_data, dict) else 0,
        chapters_count=len(chapters) if isinstance(chapters, list) else 0,
        has_blueprint=bool(metadata.get("has_blueprint")),
        has_document=bool(metadata.get("has_document")),
        last_error=metadata.get("last_error") if isinstance(metadata.get("last_error"), str) else None,
        warnings=warnings if isinstance(warnings, list) else [],
        run_state=run_state,
        metadata=metadata,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )
