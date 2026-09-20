"""Workspace-wide activity feed - every action across every course the
caller owns, newest first (the sidebar's "History" page). See
`app/api/courses.py`'s `GET /{course_id}/activity` for the single-course
version this aggregates."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_user
from app.db.models import User
from app.schemas.course import WorkspaceActivityListResponse
from app.services.course_service import CourseService, get_course_service

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _service() -> CourseService:
    return get_course_service()


@router.get("", response_model=WorkspaceActivityListResponse)
async def get_workspace_activity(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: CourseService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> WorkspaceActivityListResponse:
    activities = await service.list_activities_for_owner(
        str(current_user.id), limit=limit, offset=offset
    )
    return WorkspaceActivityListResponse(activities=activities)
