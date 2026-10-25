"""Uploaded course templates (DOCX or Markdown).

Any signed-in user can upload. An upload is processed into a generation
blueprint (sections, writing rules, visual rules, examples, reusable
pictures) that drives courses created with `template_id_override =
"uploaded:{id}"`. Uploads are private to the uploader (and admins) unless
shared; the uploader or an admin can share, unshare or delete one.

Separate from /admin/templates (api/templates.py), where admins edit JSON
template configurations directly.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.api.dependencies import get_current_user
from app.core.errors import ValidationFailedError
from app.db.models import User
from app.schemas.course_template_document import (
    CourseTemplateDocumentDetail,
    CourseTemplateDocumentListResponse,
    CourseTemplateDocumentSummary,
    CourseTemplateDocumentUpdate,
    CourseTemplateDocumentUploadForm,
    TemplateTypeClassification,
    TemplateTypeClassifyRequest,
)
from app.services.course_template_document_service import (
    CourseTemplateDocumentService,
    get_course_template_document_service,
)

router = APIRouter(prefix="/api/course-template-documents", tags=["course-template-documents"])


def _service() -> CourseTemplateDocumentService:
    return get_course_template_document_service()


@router.get("", response_model=CourseTemplateDocumentListResponse)
async def list_course_template_documents(
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentListResponse:
    return CourseTemplateDocumentListResponse(templates=await service.list_visible(current_user))


@router.get("/{template_id}", response_model=CourseTemplateDocumentDetail)
async def get_course_template_document(
    template_id: UUID,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentDetail:
    return await service.get(template_id, current_user)


@router.get("/{template_id}/assets/{asset_id}")
async def get_course_template_asset(
    template_id: UUID,
    asset_id: str,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """A picture extracted from the template, for the upload preview."""
    path, content_type = await service.asset_file(template_id, asset_id, current_user)
    return FileResponse(path, media_type=content_type)


@router.patch("/{template_id}", response_model=CourseTemplateDocumentSummary)
async def update_course_template_document(
    template_id: UUID,
    request: CourseTemplateDocumentUpdate,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentSummary:
    return await service.set_visibility(template_id, request.visibility, current_user)


@router.delete("/{template_id}", status_code=204)
async def delete_course_template_document(
    template_id: UUID,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> Response:
    await service.delete(template_id, current_user)
    return Response(status_code=204)


@router.post("/classify-type", response_model=TemplateTypeClassification)
async def classify_template_type(
    request: TemplateTypeClassifyRequest,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> TemplateTypeClassification:
    """Backs the hardcoded "Default Template" option: given what's been
    entered on Create Course so far, decide technical vs non_technical."""
    return await service.classify_type(request.course_title, request.target_audience)


@router.post("", status_code=201, response_model=CourseTemplateDocumentDetail)
async def upload_course_template_document(
    name: str = Form(...),
    template_type: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("private"),
    apply_design: bool = Form(False),
    file: UploadFile = File(...),
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentDetail:
    try:
        form = CourseTemplateDocumentUploadForm(
            name=name,
            template_type=template_type,
            description=description,
            visibility=visibility,
            apply_design=apply_design,
        )
    except ValidationError as exc:
        readable = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors())
        raise ValidationFailedError(readable) from exc
    return await service.upload(
        form,
        file,
        created_by=current_user.id,
        created_by_email=current_user.email,
    )
