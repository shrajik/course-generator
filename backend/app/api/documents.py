"""Document endpoints: AI patch editing and PDF export."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.core.ids import slugify
from app.schemas.document import CourseDocument
from app.schemas.patch import AiEditRequest, AiEditResponse
from app.services.document_service import DocumentService, get_document_service

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _service() -> DocumentService:
    return get_document_service()


@router.get("/{document_id}", response_model=CourseDocument)
async def get_document(
    document_id: str, service: DocumentService = Depends(_service)
) -> CourseDocument:
    return service.load(document_id)


@router.post("/{document_id}/ai-edit", response_model=AiEditResponse)
async def ai_edit(
    document_id: str,
    request: AiEditRequest,
    service: DocumentService = Depends(_service),
) -> AiEditResponse:
    """Ask the AI to change selected blocks. Returns the patch it produced."""
    return await service.ai_edit(document_id, request)


@router.post("/{document_id}/export/pdf")
async def export_pdf(
    document_id: str,
    download: bool = Query(default=True, description="Stream the PDF instead of JSON"),
    service: DocumentService = Depends(_service),
) -> Any:
    """Render the current Course Document JSON to PDF via HTML + Playwright."""
    path, document = await service.export_pdf(document_id)
    if not download:
        return {
            "document_id": document.document_id,
            "course_id": document.course_id,
            "version": document.version,
            "pages": len(document.pages),
            "pdf_path": str(path),
            "size_bytes": path.stat().st_size,
        }
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=f"{slugify(document.course_title)}.pdf",
    )


@router.get("/{document_id}/preview", response_class=HTMLResponse)
async def preview_html(
    document_id: str, service: DocumentService = Depends(_service)
) -> HTMLResponse:
    """The exact HTML the PDF is rendered from - handy while iterating on styling."""
    document = service.load(document_id)
    html = service.pdf.render_html(document).replace(
        'src="../assets/', f'src="/api/documents/{document_id}/assets/'
    )
    return HTMLResponse(content=html)


@router.get("/{document_id}/assets/{asset_name}")
async def get_asset(
    document_id: str, asset_name: str, service: DocumentService = Depends(_service)
) -> FileResponse:
    course_id = service.storage.resolve_course_id_for_document(document_id)
    path = (service.storage.assets_dir(course_id) / asset_name).resolve()
    assets_root = service.storage.assets_dir(course_id).resolve()
    if not str(path).startswith(str(assets_root)) or not path.exists():
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Asset '{asset_name}' not found")
    return FileResponse(path=str(path))
