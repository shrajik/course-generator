"""Uploaded course template documents - any authenticated user can upload,
browse or select. Separate from /admin/templates (the JSONB pipeline-schema
templates in api/templates.py, still admin-only) - these are prose reference
documents (DOCX or Markdown, normalized to Markdown) offered on Create
Course, not wired into generation yet.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import ValidationError

from app.api.dependencies import get_current_user
from app.core.errors import ValidationFailedError
from app.db.models import User
from app.schemas.course_template_document import (
    CourseTemplateDocumentDetail,
    CourseTemplateDocumentListResponse,
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
    return CourseTemplateDocumentListResponse(templates=await service.list_active())


@router.get("/{template_id}", response_model=CourseTemplateDocumentDetail)
async def get_course_template_document(
    template_id: UUID,
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentDetail:
    return await service.get(template_id)


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
    file: UploadFile = File(...),
    service: CourseTemplateDocumentService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseTemplateDocumentDetail:
    try:
        form = CourseTemplateDocumentUploadForm(
            name=name, template_type=template_type, description=description
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
