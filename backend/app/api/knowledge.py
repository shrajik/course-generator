"""The global knowledge library: admin-only management and a search probe.

Documents uploaded here are searched by every course while its chapters are
written (see app/services/knowledge_service.py). Only an admin may add, switch
off or remove one - the same trust boundary as templates and the visual
knowledge base - because what is in the library ends up in everyone's courses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_current_admin
from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.db.models import User
from app.schemas.source import SourceChunk, SourceExcerpt, SourceRecord
from app.services.knowledge_service import KnowledgeService, get_knowledge_service

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _service() -> KnowledgeService:
    return get_knowledge_service()


class KnowledgeList(BaseModel):
    documents: list[SourceRecord]
    stats: dict[str, int]


class KnowledgeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=500)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


@router.post("", status_code=201, response_model=SourceRecord)
async def upload_document(
    file: UploadFile = File(...),
    description: str = Form(default=""),
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> SourceRecord:
    """Add a PDF, Word, text or Markdown document to the library. Returns at
    once; it is processed in the background, so poll the list for its status."""
    limit = get_settings().max_source_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailedError(f"The file is larger than the {limit // (1024 * 1024)} MB limit.")
    return service.upload(file.filename or "upload", data, description=description)


@router.get("", response_model=KnowledgeList)
async def list_documents(
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> KnowledgeList:
    return KnowledgeList(documents=service.list_documents(), stats=service.stats())


@router.post("/search", response_model=list[SourceExcerpt])
async def search_library(
    request: SearchRequest,
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> list[SourceExcerpt]:
    """Try a query against the library, exactly as a chapter's search would: the
    passages a course would be offered, with the references it would cite."""
    return await service.search(request.query, limit=request.limit)


@router.get("/{source_id}", response_model=SourceRecord)
async def get_document(
    source_id: str,
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> SourceRecord:
    return service.get(source_id)


@router.get("/{source_id}/chunks", response_model=list[SourceChunk])
async def get_document_chunks(
    source_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> list[SourceChunk]:
    return service.chunks(source_id, offset, limit)


@router.patch("/{source_id}", response_model=SourceRecord)
async def update_document(
    source_id: str,
    request: KnowledgeUpdate,
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> SourceRecord:
    """Switch a document on or off for search, or change its description."""
    return service.update(source_id, enabled=request.enabled, description=request.description)


@router.delete("/{source_id}", status_code=204)
async def delete_document(
    source_id: str,
    service: KnowledgeService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> None:
    service.delete(source_id)
