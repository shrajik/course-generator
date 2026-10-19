"""Source material endpoints: upload, status, passages and removal.

Every call is scoped to the signed-in user: a source belongs to whoever
uploaded it and is invisible (a plain 404) to everyone else.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.dependencies import get_current_user_if_db_enabled
from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.db.models import User
from app.schemas.source import SourceChunk, SourceRecord
from app.services.source_service import SourceService, get_source_service, owner_key_for

router = APIRouter(prefix="/api/sources", tags=["sources"])


def _service() -> SourceService:
    return get_source_service()


def _owner(user: User | None) -> str:
    return owner_key_for(str(user.id) if user is not None else None)


@router.post("", status_code=201, response_model=SourceRecord)
async def upload_source(
    file: UploadFile = File(...),
    service: SourceService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> SourceRecord:
    """Upload a PDF, Word, text or Markdown file. Returns at once; the file is
    processed in the background, so poll `GET /api/sources/{id}` for its status."""
    limit = get_settings().max_source_bytes
    # Read one byte past the limit so an oversized upload is refused without
    # holding all of it in memory.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailedError(f"The file is larger than the {limit // (1024 * 1024)} MB limit.")
    return service.upload(_owner(user), file.filename or "upload", data)


@router.get("", response_model=list[SourceRecord])
async def list_sources(
    service: SourceService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> list[SourceRecord]:
    return service.list_sources(_owner(user))


@router.get("/{source_id}", response_model=SourceRecord)
async def get_source(
    source_id: str,
    service: SourceService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> SourceRecord:
    return service.get(_owner(user), source_id)


@router.get("/{source_id}/chunks", response_model=list[SourceChunk])
async def get_source_chunks(
    source_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    service: SourceService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> list[SourceChunk]:
    """The passages the document was split into, for inspection."""
    owner = _owner(user)
    service.get(owner, source_id)  # 404 unless it is the caller's
    return service.load_chunks(owner, source_id)[offset : offset + limit]


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    service: SourceService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> None:
    service.delete(_owner(user), source_id)
