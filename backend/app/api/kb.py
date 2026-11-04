"""The knowledge base admin API (PostgreSQL): organise, version, publish and inspect knowledge.

Admin only - what is published here ends up in every course (the same trust boundary as the
templates and the library). Requires the database; the older `/api/knowledge` routes keep
working beside this and, with `KB_BACKEND=postgres`, are answered from the same tables.

Lifecycle and processing are reported separately everywhere: `lifecycle` is the admin's
decision (draft, published, unpublished, archived), `processing` is what the pipeline has done.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_current_admin
from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.db.models import User
from app.kb.purge import Purger
from app.kb.queries import KbQueries
from app.kb.service import KbService
from app.kb.taxonomy import Taxonomy
from app.kb import taxonomy as taxonomy_module
from app.schemas.source import SourceExcerpt
from app.services.knowledge_pg import PostgresKnowledgeBackend, SearchScope

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])

Lifecycle = Literal["draft", "published", "unpublished", "archived"]
RelationType = Literal["related_to", "references", "derived_from", "depends_on", "supersedes", "duplicate_of"]


def _service() -> KbService:
    return KbService()


def _queries() -> KbQueries:
    return KbQueries()


def _taxonomy() -> Taxonomy:
    return Taxonomy()


async def _read(upload: UploadFile) -> tuple[str, bytes]:
    limit = get_settings().max_source_bytes
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailedError(f"'{upload.filename}' is larger than the {limit // (1024 * 1024)} MB limit.")
    return upload.filename or "upload", data


# --- models -----------------------------------------------------------------------------------


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str


class ItemSummary(BaseModel):
    id: uuid.UUID
    number: int
    public_id: str
    title: str
    description: str
    lifecycle: Lifecycle
    deleted: bool
    needs_review: bool
    origin: str
    category: CategoryRef | None
    tags: list[str]
    source_count: int
    processing: str | None
    processing_error: str | None
    unpublished_changes: bool
    updated_at: datetime


class ItemList(BaseModel):
    items: list[ItemSummary]
    total: int


class VersionInfo(BaseModel):
    id: uuid.UUID
    version_no: int
    processing_status: str
    processing_error: str | None
    warnings: list[str]
    change_note: str
    created_at: datetime
    live_from: datetime | None
    live_until: datetime | None
    is_live: bool
    passages: int
    files: list[str]
    has_text: bool


class RelationInfo(BaseModel):
    id: uuid.UUID
    relation_type: RelationType
    note: str
    source: str
    direction: Literal["out", "in"]
    other_id: uuid.UUID
    other_number: int
    other_title: str


class ItemDetail(ItemSummary):
    properties: dict[str, Any]
    suggestions: dict[str, Any]
    live_version_id: uuid.UUID | None
    latest_version_id: uuid.UUID | None
    versions: list[VersionInfo]
    relations: list[RelationInfo]


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    properties: dict[str, Any] | None = None
    category_id: uuid.UUID | None = None
    # category_id is "unchanged" unless it is named in the request, so null can mean "no category".
    tags: list[str] | None = Field(default=None, max_length=30)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The live version the admin was looking at; a different one now means someone else switched first.
    expected_live_version_id: uuid.UUID | None = None


class RelationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_item_id: uuid.UUID
    relation_type: RelationType
    note: str = Field(default="", max_length=1000)


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None
    description: str = Field(default="", max_length=1000)


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    parent_id: uuid.UUID | None = None
    approve: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)
    # Narrow the probe the way a caller can: a category (and below), all of the tags, section topics.
    category_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list, max_length=10)


class Created(BaseModel):
    id: uuid.UUID
    number: int
    public_id: str
    version_id: uuid.UUID
    version_no: int


@router.get("/status")
async def status(admin: User = Depends(get_current_admin)) -> dict[str, Any]:
    """Which store courses read (`files` or `postgres`), so the admin UI can say so."""
    settings = get_settings()
    return {"backend": settings.kb_backend if settings.use_database else "files", "database": settings.use_database}


# --- items ---------------------------------------------------------------------------------------


@router.get("/items", response_model=ItemList)
async def list_items(
    q: str | None = Query(default=None, max_length=200),
    category_id: uuid.UUID | None = None,
    tag: str | None = None,
    lifecycle: Lifecycle | None = None,
    needs_review: bool | None = None,
    deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    queries: KbQueries = Depends(_queries),
    admin: User = Depends(get_current_admin),
) -> ItemList:
    items, total = await queries.list_items(q=q, category_id=category_id, tag=tag, lifecycle=lifecycle, needs_review=needs_review, deleted=deleted, limit=limit, offset=offset)
    return ItemList(items=items, total=total)


@router.post("/items", status_code=201, response_model=Created)
async def create_item(
    files: list[UploadFile] = File(default=[]),
    body_md: str | None = Form(default=None),
    title: str | None = Form(default=None),
    description: str = Form(default=""),
    category_id: uuid.UUID | None = Form(default=None),
    service: KbService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> Created:
    """A new draft from files and/or typed text. Only the content is required: the title,
    description, tags and category are worked out and proposed for the admin to review."""
    ref = await service.create_item(
        actor_id=admin.id, files=[await _read(f) for f in files], body_md=body_md, title=title, description=description, category_id=category_id
    )
    return Created(id=ref.id, number=ref.number, public_id=ref.public_id, version_id=ref.version_id, version_no=ref.version_no)


@router.get("/items/{item_id}", response_model=ItemDetail)
async def get_item(item_id: uuid.UUID, queries: KbQueries = Depends(_queries), admin: User = Depends(get_current_admin)) -> dict:
    return await queries.item_detail(item_id)


@router.patch("/items/{item_id}", response_model=ItemDetail)
async def update_item(
    item_id: uuid.UUID,
    request: ItemUpdate,
    service: KbService = Depends(_service),
    tax: Taxonomy = Depends(_taxonomy),
    queries: KbQueries = Depends(_queries),
    admin: User = Depends(get_current_admin),
) -> dict:
    """Metadata only: no new version, nothing re-embedded."""
    fields = request.model_fields_set
    await service.update_item(
        item_id, actor_id=admin.id, title=request.title, description=request.description, properties=request.properties,
        category_id=request.category_id if "category_id" in fields else "unchanged",
    )
    if request.tags is not None:
        await tax.set_tags(item_id, request.tags, actor_id=admin.id)
    return await queries.item_detail(item_id)


@router.post("/items/{item_id}/versions", status_code=201, response_model=Created)
async def add_version(
    item_id: uuid.UUID,
    files: list[UploadFile] = File(default=[]),
    body_md: str | None = Form(default=None),
    keep_previous_sources: bool = Form(default=True),
    change_note: str = Form(default=""),
    service: KbService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> Created:
    """A new version. The item keeps serving its live version until an admin publishes this one."""
    ref = await service.add_version(
        item_id, actor_id=admin.id, files=[await _read(f) for f in files], body_md=body_md,
        keep_previous_sources=keep_previous_sources, change_note=change_note,
    )
    return Created(id=ref.id, number=ref.number, public_id=ref.public_id, version_id=ref.version_id, version_no=ref.version_no)


@router.post("/items/{item_id}/versions/{version_id}/publish")
async def publish_version(
    item_id: uuid.UUID,
    version_id: uuid.UUID,
    request: PublishRequest,
    service: KbService = Depends(_service),
    admin: User = Depends(get_current_admin),
) -> dict[str, str]:
    """Make this the live version, atomically. 409 if someone switched versions since you looked."""
    return {"result": await service.publish_version(item_id, version_id, actor_id=admin.id, expected_live_version_id=request.expected_live_version_id)}


@router.post("/items/{item_id}/versions/{version_id}/retry", status_code=202)
async def retry_version(item_id: uuid.UUID, version_id: uuid.UUID, service: KbService = Depends(_service), admin: User = Depends(get_current_admin)) -> dict[str, str]:
    await service.retry_version(version_id, actor_id=admin.id)
    return {"result": "queued"}


@router.post("/items/{item_id}/publish")
async def publish_item(item_id: uuid.UUID, service: KbService = Depends(_service), admin: User = Depends(get_current_admin)) -> dict[str, str]:
    """Publish the item's last live version, or its newest processed one if it never was live."""
    return {"result": await service.publish_item(item_id, actor_id=admin.id)}


@router.get("/items/{item_id}/chunks")
async def item_chunks(
    item_id: uuid.UUID,
    version_id: uuid.UUID | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    queries: KbQueries = Depends(_queries),
    admin: User = Depends(get_current_admin),
) -> list[dict]:
    return await queries.chunks(item_id, version_id=version_id, offset=offset, limit=limit)


@router.get("/items/{item_id}/activity")
async def item_activity(item_id: uuid.UUID, queries: KbQueries = Depends(_queries), admin: User = Depends(get_current_admin)) -> list[dict]:
    return await queries.activity(item_id)


# --- relationships --------------------------------------------------------------------------------------


@router.post("/items/{item_id}/relations", status_code=201)
async def add_relation(item_id: uuid.UUID, request: RelationCreate, tax: Taxonomy = Depends(_taxonomy), admin: User = Depends(get_current_admin)) -> dict[str, uuid.UUID]:
    return {"id": await tax.add_relation(item_id, request.to_item_id, request.relation_type, note=request.note, actor_id=admin.id)}


@router.delete("/relations/{relation_id}", status_code=204)
async def remove_relation(relation_id: uuid.UUID, tax: Taxonomy = Depends(_taxonomy), admin: User = Depends(get_current_admin)) -> None:
    await tax.remove_relation(relation_id, actor_id=admin.id)


# --- categories and tags ------------------------------------------------------------------------------------


@router.get("/categories")
async def list_categories(queries: KbQueries = Depends(_queries), admin: User = Depends(get_current_admin)) -> list[dict]:
    return await queries.categories()


@router.post("/categories", status_code=201)
async def create_category(request: CategoryCreate, tax: Taxonomy = Depends(_taxonomy), admin: User = Depends(get_current_admin)) -> dict[str, uuid.UUID]:
    return {"id": await tax.create_category(request.name, parent_id=request.parent_id, description=request.description)}


@router.patch("/categories/{category_id}")
async def update_category(category_id: uuid.UUID, request: CategoryUpdate, tax: Taxonomy = Depends(_taxonomy), admin: User = Depends(get_current_admin)) -> dict[str, str]:
    parent = request.parent_id if "parent_id" in request.model_fields_set else taxonomy_module._UNCHANGED  # noqa: SLF001
    await tax.update_category(category_id, name=request.name, description=request.description, parent_id=parent, approve=request.approve)
    return {"result": "updated"}


@router.delete("/categories/{category_id}", status_code=204)
async def archive_category(category_id: uuid.UUID, tax: Taxonomy = Depends(_taxonomy), admin: User = Depends(get_current_admin)) -> None:
    await tax.archive_category(category_id)


@router.get("/tags")
async def list_tags(queries: KbQueries = Depends(_queries), admin: User = Depends(get_current_admin)) -> list[dict]:
    return await queries.tags()


# --- try it, and trace a citation ----------------------------------------------------------------------------


@router.post("/search", response_model=list[SourceExcerpt])
async def search(request: SearchRequest, admin: User = Depends(get_current_admin)) -> list[SourceExcerpt]:
    """Ask the library as a chapter would: what a course would be offered, with its citations."""
    # Always the PostgreSQL library: this API manages those tables, whatever KB_BACKEND serves courses.
    scope = SearchScope(category_id=request.category_id, tags=tuple(request.tags), topics=tuple(request.topics))
    return await PostgresKnowledgeBackend(log_retrievals=False).asearch(request.query, limit=request.limit, scope=scope)


@router.get("/citations/{ref}")
async def resolve_citation(
    ref: str,
    course_id: str | None = Query(default=None, max_length=64),
    at: datetime | None = None,
    queries: KbQueries = Depends(_queries),
    admin: User = Depends(get_current_admin),
) -> dict:
    """What a citation says (exactly, even if replaced since) and whether it was live when the course
    was generated: from the retrieval log with `course_id`, else from `at`, else unknown."""
    return await queries.resolve_citation(ref, course_id=course_id, at=at)


@router.get("/items/{item_id}/purge-impact")
async def purge_impact(item_id: uuid.UUID, admin: User = Depends(get_current_admin)) -> dict:
    """Which courses cite this item - shown before it is permanently removed."""
    return await Purger().impact(item_id)


class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_number: int


@router.post("/items/{item_id}/purge")
async def purge_item(item_id: uuid.UUID, request: PurgeRequest, admin: User = Depends(get_current_admin)) -> dict:
    """Permanently remove a trashed item's content. Irreversible; off until KB_PURGE_ENABLED."""
    return await Purger().purge(item_id, confirm_number=request.confirm_number, actor_id=admin.id)


# Registered last: `/items/{id}/{action}` matches any single word after an item id, so it must come after
# every more specific POST route (versions, relations, publish) or it would shadow them.
_ACTIONS = {"unpublish": "unpublish", "archive": "archive", "restore": "restore_archived", "delete": "delete", "undelete": "restore_deleted"}


@router.post("/items/{item_id}/{action}", response_model=ItemSummary)
async def change_lifecycle(
    item_id: uuid.UUID,
    action: Literal["unpublish", "archive", "restore", "delete", "undelete"],
    service: KbService = Depends(_service),
    queries: KbQueries = Depends(_queries),
    admin: User = Depends(get_current_admin),
) -> dict:
    await service.set_lifecycle(item_id, _ACTIONS[action], actor_id=admin.id)  # type: ignore[arg-type]
    return (await queries.item_detail(item_id))


__all__ = ["router"]
