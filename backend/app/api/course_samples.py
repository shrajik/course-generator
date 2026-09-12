"""Course sample registry API - admin-only, same trust boundary as templates
and visual knowledge (Permission.MANAGE_TEMPLATES_AND_KNOWLEDGE_BASE).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_admin
from app.db.models import User
from app.schemas.memory import (
    CourseSampleCreateRequest,
    CourseSampleEntry,
    CourseSampleListResponse,
    CourseSampleUpdateRequest,
)
from app.services.course_sample_service import CourseSampleService, get_course_sample_service

router = APIRouter(prefix="/api/course-samples", tags=["course-samples"])


def _service() -> CourseSampleService:
    return get_course_sample_service()


@router.get("", response_model=CourseSampleListResponse)
async def list_course_samples(
    search: str | None = Query(default=None, max_length=200),
    tags: list[str] | None = Query(default=None),
    approved_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: CourseSampleService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> CourseSampleListResponse:
    items, total = await service.list(
        search=search, tags=tags, approved_only=approved_only, limit=limit, offset=offset
    )
    return CourseSampleListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", status_code=201, response_model=CourseSampleEntry)
async def register_course_sample(
    request: CourseSampleCreateRequest,
    service: CourseSampleService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> CourseSampleEntry:
    return await service.register(request, created_by=current_admin.id)


@router.get("/{entry_id}", response_model=CourseSampleEntry)
async def get_course_sample(
    entry_id: UUID,
    service: CourseSampleService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> CourseSampleEntry:
    return await service.get(entry_id)


@router.patch("/{entry_id}", response_model=CourseSampleEntry)
async def update_course_sample(
    entry_id: UUID,
    request: CourseSampleUpdateRequest,
    service: CourseSampleService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> CourseSampleEntry:
    return await service.update(entry_id, request)


@router.delete("/{entry_id}", status_code=204)
async def delete_course_sample(
    entry_id: UUID,
    service: CourseSampleService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> None:
    await service.delete(entry_id)
