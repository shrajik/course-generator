"""Document endpoints: AI patch editing and PDF export."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.api.course_authorization import check_document_owner
from app.api.dependencies import get_current_user_if_db_enabled
from app.core.ids import slugify
from app.db.models import User
from app.schemas.document import (
    CourseDocument,
    RegenerateVisualsRequest,
    RegenerateVisualsResponse,
    RepairVisualsResponse,
    UpdateConceptVisualSpecRequest,
    UpdateConceptVisualSpecResponse,
)
from app.schemas.patch import AiEditRequest, AiEditResponse
from app.services.course_service import CourseService, get_course_service
from app.services.document_service import DocumentService, get_document_service

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _service() -> DocumentService:
    return get_document_service()


def _course_service() -> CourseService:
    return get_course_service()


@router.get("/{document_id}", response_model=CourseDocument)
async def get_document(
    document_id: str,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CourseDocument:
    await check_document_owner(document_id, current_user, course_service, service)
    return await service.load(document_id)


@router.put("/{document_id}", response_model=CourseDocument)
async def save_document(
    document_id: str,
    document: CourseDocument,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CourseDocument:
    """Persist manual editor changes (move/resize/type/insert/delete/style).

    The URL's `document_id` is authoritative: whatever `document_id`/`course_id`
    the body carries is discarded in favour of the server-known values, so a
    user can't redirect a save onto another user's document by editing the
    JSON their browser sends.
    """
    await check_document_owner(document_id, current_user, course_service, service)
    saved = await service.save(document_id, document)
    if current_user is not None:
        await course_service.record_activity(
            saved.course_id, str(current_user.id), current_user.email, "updated"
        )
    return saved


@router.post("/{document_id}/ai-edit", response_model=AiEditResponse)
async def ai_edit(
    document_id: str,
    request: AiEditRequest,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> AiEditResponse:
    """Ask the AI to change selected blocks. Returns the patch it produced."""
    await check_document_owner(document_id, current_user, course_service, service)
    return await service.ai_edit(document_id, request)


@router.put(
    "/{document_id}/blocks/{block_id}/visual-spec",
    response_model=UpdateConceptVisualSpecResponse,
)
async def update_concept_visual_spec(
    document_id: str,
    block_id: str,
    request: UpdateConceptVisualSpecRequest,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> UpdateConceptVisualSpecResponse:
    """Edit the text inside a concept_experience visual (an entity/step
    label or description) and re-render it deterministically - no AI call,
    the exported PDF keeps rendering this the same static way it always has.
    """
    course_id = await check_document_owner(document_id, current_user, course_service, service)
    response = await service.update_concept_visual_spec(document_id, block_id, request.spec)
    if current_user is not None:
        await course_service.record_activity(
            course_id, str(current_user.id), current_user.email, "updated"
        )
    return response


@router.post("/{document_id}/regenerate-visuals", response_model=RegenerateVisualsResponse)
async def regenerate_visuals(
    document_id: str,
    request: RegenerateVisualsRequest,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> RegenerateVisualsResponse:
    """Re-render every image block in this document with the current
    renderer/prompt code, even ones that already have an asset - for an
    existing course generated before a visual-styling change, since a
    rendered diagram/image is a static file that never updates on its own.
    `request.kinds` narrows this to specific image kinds (e.g. ["diagram"]
    to only touch deterministic, free-to-re-render diagrams, not also
    re-spend on every AI-generated illustration) - blank means everything.
    """
    course_id = await check_document_owner(document_id, current_user, course_service, service)
    response = await service.regenerate_all_visuals(document_id, kinds=request.kinds)
    if current_user is not None:
        await course_service.record_activity(
            course_id, str(current_user.id), current_user.email, "updated"
        )
    return response


@router.post("/{document_id}/repair-visual-coverage", response_model=RepairVisualsResponse)
async def repair_visual_coverage(
    document_id: str,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> RepairVisualsResponse:
    """Audit every page of this EXISTING document for the 60:40 text/visual
    target and generate+insert a relevant visual for any page that's still
    deficient - the same post-pagination repair pass a newly generated
    course already runs, available here for a course generated before this
    pass existed. Never touches a page that already meets the target;
    anything that couldn't be repaired within the bounded number of passes
    is reported by page number and reason, not silently left as-is.
    """
    course_id = await check_document_owner(document_id, current_user, course_service, service)
    response = await service.repair_visual_coverage(document_id)
    if current_user is not None:
        await course_service.record_activity(
            course_id, str(current_user.id), current_user.email, "updated"
        )
    return response


@router.post("/{document_id}/export/pdf")
async def export_pdf(
    document_id: str,
    download: bool = Query(default=True, description="Stream the PDF instead of JSON"),
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> Any:
    """Render the current Course Document JSON to PDF via HTML + Playwright."""
    await check_document_owner(document_id, current_user, course_service, service)
    path, document = await service.export_pdf(document_id)
    if current_user is not None:
        await course_service.record_activity(
            document.course_id, str(current_user.id), current_user.email, "exported"
        )
    if not download:
        return {
            "document_id": document.document_id,
            "course_id": document.course_id,
            "version": document.version,
            "pages": len(document.pages),
            "pdf_path": path.as_posix(),
            "size_bytes": path.stat().st_size,
        }
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=f"{slugify(document.course_title)}.pdf",
    )


@router.get("/{document_id}/preview", response_class=HTMLResponse)
async def preview_html(
    document_id: str,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> HTMLResponse:
    """The exact HTML the PDF is rendered from - handy while iterating on styling."""
    await check_document_owner(document_id, current_user, course_service, service)
    document = await service.load(document_id)
    html = service.pdf.render_html(document).replace(
        'src="../assets/', f'src="/api/documents/{document_id}/assets/'
    )
    return HTMLResponse(content=html)


@router.get("/{document_id}/assets/{asset_name}")
async def get_asset(
    document_id: str,
    asset_name: str,
    service: DocumentService = Depends(_service),
    course_service: CourseService = Depends(_course_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> FileResponse:
    course_id = await check_document_owner(document_id, current_user, course_service, service)
    path = (service.storage.assets_dir(course_id) / asset_name).resolve()
    assets_root = service.storage.assets_dir(course_id).resolve()
    if not str(path).startswith(str(assets_root)) or not path.exists():
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Asset '{asset_name}' not found")
    return FileResponse(path=str(path))
