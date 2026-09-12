"""Template management API - admin-only (Permission.MANAGE_TEMPLATES_AND_KNOWLEDGE_BASE).

Read access to the *selectable* set of templates (static + DB-backed current
ones) stays where it already lived, at `GET /api/courses/templates` - this
router only adds create/edit/version/archive, which never existed before.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_admin
from app.course.templates.registry import load_template
from app.db.models import User
from app.schemas.memory import (
    TemplateCreateRequest,
    TemplateListResponse,
    TemplateRecord,
    TemplateUpdateRequest,
)
from app.schemas.template import CourseTemplate
from app.services.template_service import TemplateService, get_template_service

router = APIRouter(prefix="/admin/templates", tags=["templates"])


def _service() -> TemplateService:
    return get_template_service()


@router.get("", response_model=TemplateListResponse)
async def list_templates(
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateListResponse:
    items, total = await service.list_current(include_archived=include_archived, limit=limit, offset=offset)
    return TemplateListResponse(templates=items, total=total, limit=limit, offset=offset)


@router.post("", status_code=201, response_model=TemplateRecord)
async def create_template(
    request: TemplateCreateRequest,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateRecord:
    return await service.create(request, created_by=current_admin.id)


@router.get("/config/{template_id}", response_model=CourseTemplate)
async def get_template_config(
    template_id: str,
    current_admin: User = Depends(get_current_admin),
) -> CourseTemplate:
    """The full config for any resolvable id, static or DB-backed - used by
    the admin editor to start a new template/version from an existing one."""
    return load_template(template_id)


@router.get("/{slug}", response_model=TemplateRecord)
async def get_template(
    slug: str,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateRecord:
    """The current version of `slug`. Use /versions for the full history."""
    return await service.get_current(slug)


@router.patch("/{slug}", response_model=TemplateRecord)
async def update_template(
    slug: str,
    request: TemplateUpdateRequest,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateRecord:
    """Always creates a new version - never mutates a version a course might
    already reference. See TemplateService.update."""
    return await service.update(slug, request, created_by=current_admin.id)


@router.get("/{slug}/versions", response_model=list[TemplateRecord])
async def list_template_versions(
    slug: str,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> list[TemplateRecord]:
    return await service.list_versions(slug)


@router.post("/{slug}/archive", response_model=TemplateRecord)
async def archive_template(
    slug: str,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateRecord:
    """Hides this template family from new-course pickers. Courses that
    already reference one of its versions are completely unaffected."""
    return await service.archive(slug, archived=True)


@router.post("/{slug}/unarchive", response_model=TemplateRecord)
async def unarchive_template(
    slug: str,
    service: TemplateService = Depends(_service),
    current_admin: User = Depends(get_current_admin),
) -> TemplateRecord:
    return await service.archive(slug, archived=False)
