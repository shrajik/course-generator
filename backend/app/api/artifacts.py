"""Artifact memory (Memory 2) API: the review queue, approvals, versions, links and citations.

Reviewers (and admins) read and review; approval is the existing SME permission, the same roles that
approve a course. Approving locks the version for good - the database refuses any later change - so
the only way to change approved work is a new version, which needs its own approval.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import require_roles
from app.artifacts.queries import ArtifactQueries
from app.artifacts.retrieval import ArtifactExcerpt, ArtifactMemory
from app.artifacts.service import ArtifactService
from app.core.roles import Role
from app.db.models import User

router = APIRouter(prefix="/api/artifacts", tags=["artifact-memory"])

ArtifactType = Literal["chapter", "visual", "questions", "assessment", "activity", "case_study", "other"]
ReviewStatus = Literal["pending_review", "approved", "changes_requested"]
LinkType = Literal["belongs_to_course", "contains_chapter", "references_knowledge", "derived_from", "related_to", "supersedes", "contains_visual", "contains_questions"]

can_read = require_roles(Role.EDITOR_REVIEWER, Role.MANAGER, Role.ADMIN)
can_review = require_roles(Role.EDITOR_REVIEWER, Role.ADMIN)
can_manage = require_roles(Role.ADMIN)


def _service() -> ArtifactService:
    return ArtifactService()


def _queries() -> ArtifactQueries:
    return ArtifactQueries()


class ReviewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=2000)


class ChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=2000)


class NewVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]
    title: str | None = Field(default=None, max_length=300)
    asset: dict[str, Any] | None = None
    change_note: str = Field(default="", max_length=1000)


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link_type: LinkType
    to_artifact_id: uuid.UUID | None = None
    course_id: str | None = Field(default=None, max_length=64)
    kb_number: int | None = Field(default=None, ge=1)
    kb_ordinal: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=1000)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    types: list[ArtifactType] = Field(default_factory=list, max_length=7)
    course_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=5, ge=1, le=20)


@router.get("")
async def list_artifacts(
    artifact_type: ArtifactType | None = None,
    course_id: str | None = None,
    review_status: ReviewStatus | None = Query(default=None, description="The newest version's review state; `pending_review` is the reviewer's queue."),
    status: Literal["active", "archived"] | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    queries: ArtifactQueries = Depends(_queries),
    user: User = Depends(can_read),
) -> dict[str, Any]:
    items, total = await queries.list(artifact_type=artifact_type, course_id=course_id, review_status=review_status, status=status, q=q, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/counts")
async def counts(queries: ArtifactQueries = Depends(_queries), user: User = Depends(can_read)) -> dict[str, int]:
    return await queries.counts()


@router.get("/citations/{ref}")
async def resolve_citation(ref: str, include_unapproved: bool = False, user: User = Depends(can_read)) -> dict[str, Any]:
    """What `A7.2` (artifact 7, version 2) is: content, approver, provenance, and whether it is still current.
    Unapproved versions resolve only with `include_unapproved`, for reviewers looking at what they were sent."""
    return await ArtifactMemory(log_retrievals=False).resolve(ref, include_unapproved=include_unapproved)


@router.post("/search", response_model=list[ArtifactExcerpt])
async def search(request: SearchRequest, user: User = Depends(can_read)) -> list[ArtifactExcerpt]:
    """Ask artifact memory as a chapter would: what would be offered for reuse (approved, active versions only)."""
    return await ArtifactMemory(log_retrievals=False).search(request.query, types=tuple(request.types), course_id=request.course_id, limit=request.limit)


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: uuid.UUID, queries: ArtifactQueries = Depends(_queries), user: User = Depends(can_read)) -> dict[str, Any]:
    return await queries.detail(artifact_id)


@router.post("/versions/{version_id}/approve")
async def approve_version(version_id: uuid.UUID, request: ReviewNote, service: ArtifactService = Depends(_service), user: User = Depends(can_review)) -> dict[str, str]:
    """SME approval of one artifact version: it is locked, indexed and becomes what future courses can reuse."""
    await service.approve_version(version_id, user.id, request.comment)
    return {"result": "approved"}


@router.post("/versions/{version_id}/request-changes")
async def request_changes(version_id: uuid.UUID, request: ChangesRequest, service: ArtifactService = Depends(_service), user: User = Depends(can_review)) -> dict[str, str]:
    await service.request_changes_version(version_id, user.id, request.comment)
    return {"result": "changes_requested"}


@router.post("/{artifact_id}/versions", status_code=201)
async def new_version(artifact_id: uuid.UUID, request: NewVersion, service: ArtifactService = Depends(_service), user: User = Depends(can_review)) -> dict[str, uuid.UUID]:
    """Change an artifact. Approved work is never edited: this creates a new version that needs its own approval."""
    return {"version_id": await service.new_version(artifact_id, request.content, actor_id=user.id, title=request.title, asset=request.asset, change_note=request.change_note)}


@router.post("/{artifact_id}/archive")
async def archive(artifact_id: uuid.UUID, service: ArtifactService = Depends(_service), user: User = Depends(can_manage)) -> dict[str, str]:
    await service.set_archived(artifact_id, True, actor_id=user.id)
    return {"result": "archived"}


@router.post("/{artifact_id}/restore")
async def restore(artifact_id: uuid.UUID, service: ArtifactService = Depends(_service), user: User = Depends(can_manage)) -> dict[str, str]:
    await service.set_archived(artifact_id, False, actor_id=user.id)
    return {"result": "restored"}


@router.post("/{artifact_id}/links", status_code=201)
async def add_link(artifact_id: uuid.UUID, request: LinkCreate, service: ArtifactService = Depends(_service), user: User = Depends(can_manage)) -> dict[str, str]:
    """Connect an artifact to another artifact, a course, or a knowledge passage (by citation number: the
    two memories are linked, never joined)."""
    await service.add_link(artifact_id, request.link_type, to_artifact=request.to_artifact_id, course_id=request.course_id,
                           kb_number=request.kb_number, kb_ordinal=request.kb_ordinal, note=request.note, actor_id=user.id)
    return {"result": "linked"}


@router.delete("/links/{link_id}", status_code=204)
async def remove_link(link_id: uuid.UUID, service: ArtifactService = Depends(_service), user: User = Depends(can_manage)) -> None:
    await service.remove_link(link_id)


@router.post("/index-missing")
async def index_missing(service: ArtifactService = Depends(_service), user: User = Depends(can_manage)) -> dict[str, int]:
    """Embed approved versions that were approved while embedding was unavailable."""
    return {"indexed": await service.index_missing()}
