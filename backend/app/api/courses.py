"""Course endpoints. Thin layer: validate, delegate, serialise."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.course_authorization import get_course_for_author_action, get_owned_course
from app.api.dependencies import get_current_user, get_current_user_if_db_enabled, require_roles
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.generation_stream import get_stream_hub
from app.core.roles import Role
from app.course.templates.registry import available_templates, load_template
from app.db.models import User
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import (
    ApproveCourseRequest,
    CourseActivityListResponse,
    CourseInput,
    CourseRecord,
    CourseReviewResponse,
    CreateCourseRequest,
    GenerateRequest,
    GenerateResponse,
    ImproveTocRequest,
    ImproveTocResponse,
    RequestChangesRequest,
)
from app.schemas.document import CourseDocument
from app.schemas.memory import GenerationRunListResponse
from app.services.course_service import CourseService, get_course_service
from app.services.storage_service import StorageService, get_storage

router = APIRouter(prefix="/api/courses", tags=["courses"])


def _service() -> CourseService:
    return get_course_service()


def _storage() -> StorageService:
    return get_storage()


@router.get("/templates")
async def list_templates() -> dict[str, Any]:
    """The two template configurations, for the future frontend picker."""
    return {
        "templates": [
            {
                "template_id": template.template_id,
                "kind": template.kind,
                "name": template.name,
                "description": template.description,
                "sections": [
                    {"key": s.key, "label": s.label, "required": s.required}
                    for s in template.sections
                ],
                "required_block_types": [bt.value for bt in template.required_block_types],
            }
            for template in available_templates()
        ]
    }


@router.post("", status_code=201, response_model=CourseRecord)
@router.post("/", status_code=201, response_model=CourseRecord, include_in_schema=False)
async def create_course(
    request: CreateCourseRequest,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CourseRecord:
    """Create a course and (by default) run the planner to produce a blueprint.

    Ownership is always taken from the authenticated session, never from the
    request body - `CreateCourseRequest` has no owner field to spoof.
    """
    course_input = CourseInput.model_validate(request.model_dump(exclude={"run_planner"}))
    if request.template_id_override:
        known_ids = {t.template_id for t in available_templates()}
        if request.template_id_override not in known_ids:
            raise ValidationFailedError(
                f"Unknown template_id_override '{request.template_id_override}'",
                details={"available": sorted(known_ids)},
            )
    owner_id = str(current_user.id) if current_user is not None else None
    record = await service.create_course(course_input, run_planner=request.run_planner, owner_id=owner_id)
    if current_user is not None:
        await service.record_activity(record.course_id, owner_id, current_user.email, "created")
    return record


@router.post("/improve-toc", response_model=ImproveTocResponse)
async def improve_toc(
    request: ImproveTocRequest,
    service: CourseService = Depends(_service),
) -> ImproveTocResponse:
    """Propose an improved table of contents. Nothing is saved or applied."""
    return await service.improve_toc(request)


@router.get("", response_model=dict)
@router.get("/", response_model=dict, include_in_schema=False)
async def list_courses(
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    """Every course for admins/offline mode; an editor/reviewer's queue
    (every submitted course, any author) if they hold that role; only the
    caller's own otherwise."""
    owner_id = None
    review_statuses = None
    if current_user is not None:
        if current_user.role == Role.EDITOR_REVIEWER.value:
            review_statuses = ["in_review", "changes_requested", "approved"]
        elif current_user.role != Role.ADMIN.value:
            owner_id = str(current_user.id)
    records = await service.list_courses(owner_id=owner_id, review_statuses=review_statuses)
    courses = [
        {
            "course_id": record.course_id,
            "document_id": record.document_id,
            "course_title": record.input.course_title,
            "template_id": record.template_id,
            "status": record.status,
            "review_status": record.review_status,
            "updated_at": record.updated_at,
        }
        for record in records
    ]
    return {"courses": courses, "count": len(courses)}


@router.get("/{course_id}")
async def get_course(
    course_id: str,
    include_blueprint: bool = Query(default=True),
    service: CourseService = Depends(_service),
    storage: StorageService = Depends(_storage),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    record = await get_owned_course(course_id, current_user, service)
    has_blueprint = await service.has_blueprint(course_id)
    payload: dict[str, Any] = {"course": record.model_dump(mode="json")}
    if include_blueprint and has_blueprint:
        payload["blueprint"] = (await service.get_blueprint(course_id)).model_dump(mode="json")
    payload["artifacts"] = {
        "blueprint": has_blueprint,
        # True as soon as the first chapter is assembled, so the editor can be
        # opened while later chapters are still being written.
        "document": await service.has_document(course_id),
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "blocks": len(chapter.blocks),
                "revisions": chapter.revisions,
                "reviewed": chapter.review is not None,
            }
            for chapter in storage.load_all_chapters(course_id)
        ],
        "research": sorted(
            path.name for path in storage.research_dir(course_id).glob("*.json")
        )
        if storage.research_dir(course_id).exists()
        else [],
        "pdf": record.pdf_path,
    }
    return payload


@router.get("/{course_id}/blueprint", response_model=CourseBlueprint)
async def get_blueprint(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CourseBlueprint:
    await get_owned_course(course_id, current_user, service)
    return await service.get_blueprint(course_id)


@router.post("/{course_id}/generate", response_model=GenerateResponse)
async def generate_course(
    course_id: str,
    request: GenerateRequest | None = None,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> GenerateResponse:
    """Research + write + review + assemble.

    Backgrounded by default: returns a job id immediately and the caller polls
    `GET /api/courses/{course_id}`. Pass `{"mode": "sync"}` to block until the run
    finishes, `chapter_ids` to regenerate part of a course, or `resume: true` to
    continue an interrupted run.

    Author (or admin) only - regenerating content via the AI pipeline is an
    authoring action, not the kind of in-place content edit a reviewer is
    allowed to make on a submitted course (see `get_owned_course` vs
    `get_course_for_author_action` in course_authorization.py).
    """
    await get_course_for_author_action(course_id, current_user, service)
    return await service.start_generation(course_id, request or GenerateRequest())


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/{course_id}/generate/stream")
async def stream_generation(
    course_id: str,
    request: Request,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> StreamingResponse:
    """Real model output as the writer/reviser actually produce it - see
    `app/core/generation_stream.py`. Same ownership rule as every other read
    on this course; not a second generation pipeline, just a window onto the
    one that `POST /generate` already started.
    """
    await get_owned_course(course_id, current_user, service)
    hub = get_stream_hub()

    async def events():
        queue = hub.subscribe(course_id)
        try:
            # Replay whatever's currently in flight so a fresh tab (or a
            # reconnect after a drop) doesn't start blank - this is the whole
            # answer to "refresh shouldn't restart or lose the stream".
            for state in hub.snapshot(course_id):
                yield _sse(state.to_event())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(course_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{course_id}/run")
async def get_run_state(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    """Progress, ETA and timings for the current or last generation run."""
    await get_owned_course(course_id, current_user, service)
    run = await service.job_state(course_id)
    return {"course_id": course_id, "run": run.model_dump(mode="json") if run else None}


@router.get("/{course_id}/document", response_model=CourseDocument)
async def get_course_document(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CourseDocument:
    """The source of truth for this course."""
    await get_owned_course(course_id, current_user, service)
    return await service.load_document(course_id)


@router.get("/{course_id}/chapters/{chapter_id}")
async def get_chapter(
    course_id: str,
    chapter_id: str,
    service: CourseService = Depends(_service),
    storage: StorageService = Depends(_storage),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    await get_owned_course(course_id, current_user, service)
    blueprint = await service.get_blueprint(course_id)
    chapter = blueprint.chapter_by_id(chapter_id)
    if chapter is None:
        raise NotFoundError(f"Chapter '{chapter_id}' is not part of this course")
    return {
        "chapter": storage.load_chapter(course_id, chapter_id, chapter.order).model_dump(
            mode="json"
        ),
        "research_available": storage.has_research(course_id, chapter_id, chapter.order),
    }


@router.get("/{course_id}/chapters/{chapter_id}/research")
async def get_chapter_research(
    course_id: str,
    chapter_id: str,
    service: CourseService = Depends(_service),
    storage: StorageService = Depends(_storage),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    await get_owned_course(course_id, current_user, service)
    blueprint = await service.get_blueprint(course_id)
    chapter = blueprint.chapter_by_id(chapter_id)
    if chapter is None:
        raise NotFoundError(f"Chapter '{chapter_id}' is not part of this course")
    return storage.load_research(course_id, chapter_id, chapter.order).model_dump(mode="json")


@router.get("/{course_id}/template")
async def get_course_template(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, Any]:
    record = await get_owned_course(course_id, current_user, service)
    return load_template(record.template_id).model_dump(mode="json")


# --- review/approval workflow ------------------------------------------------
# draft -> in_review -> approved
#                     -> changes_requested -> in_review (resubmit) -> ...
# These endpoints always require real authentication (unlike the routes
# above, which stay open in offline/filesystem mode for backward
# compatibility) - there is no meaningful review workflow without a user/role
# system to enforce it against.


def _to_review_response(record: CourseRecord) -> CourseReviewResponse:
    return CourseReviewResponse(
        course_id=record.course_id,
        owner_id=record.owner_id,
        review_status=record.review_status,
        review_comment=record.review_comment,
        reviewer_id=record.reviewer_id,
        reviewed_at=record.reviewed_at,
        history=record.review_history,
    )


@router.post("/{course_id}/submit-for-review", response_model=CourseReviewResponse)
async def submit_course_for_review(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseReviewResponse:
    """Author (or admin) only - a reviewer can view a submitted course but
    doesn't get to resubmit someone else's."""
    await get_course_for_author_action(course_id, current_user, service)
    record = await service.submit_for_review(course_id, str(current_user.id))
    await service.record_activity(
        course_id, str(current_user.id), current_user.email, "submitted_for_review"
    )
    return _to_review_response(record)


@router.get("/{course_id}/review", response_model=CourseReviewResponse)
async def get_course_review(
    course_id: str,
    service: CourseService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseReviewResponse:
    """Owner, admin, or an editor/reviewer looking at a submitted course."""
    record = await get_owned_course(course_id, current_user, service)
    return _to_review_response(record)


@router.post("/{course_id}/approve", response_model=CourseReviewResponse)
async def approve_course(
    course_id: str,
    request: ApproveCourseRequest | None = None,
    service: CourseService = Depends(_service),
    current_user: User = Depends(require_roles(Role.EDITOR_REVIEWER, Role.ADMIN)),
) -> CourseReviewResponse:
    """Editor/reviewer or admin only - never the course's own author. The
    course must actually be `in_review`; the service enforces that."""
    comment = request.comment if request else None
    record = await service.approve_course(course_id, str(current_user.id), comment)
    await service.record_activity(
        course_id, str(current_user.id), current_user.email, "approved", comment
    )
    return _to_review_response(record)


@router.post("/{course_id}/request-changes", response_model=CourseReviewResponse)
async def request_course_changes(
    course_id: str,
    request: RequestChangesRequest,
    service: CourseService = Depends(_service),
    current_user: User = Depends(require_roles(Role.EDITOR_REVIEWER, Role.ADMIN)),
) -> CourseReviewResponse:
    """Editor/reviewer or admin only. `comment` is required by the request
    schema (min_length=1), so an empty reason is a 422 before this ever runs."""
    record = await service.request_changes(course_id, str(current_user.id), request.comment)
    await service.record_activity(
        course_id, str(current_user.id), current_user.email, "changes_requested", request.comment
    )
    return _to_review_response(record)


# --- activity log -------------------------------------------------------------
# Owner or admin only, per the requirement - deliberately not extended to the
# reviewer read-access carve-out used elsewhere, since a course's activity
# history is more sensitive than its content.


@router.get("/{course_id}/activity", response_model=CourseActivityListResponse)
async def get_course_activity(
    course_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: CourseService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> CourseActivityListResponse:
    await get_course_for_author_action(course_id, current_user, service)
    activities = await service.list_activities(course_id, limit=limit, offset=offset)
    return CourseActivityListResponse(course_id=course_id, activities=activities)


# --- generation history --------------------------------------------------------
# One row per generation attempt (see GenerationRun/generation_runs) - unlike
# `run` on the course record (only the latest attempt), this survives being
# overwritten by a later run. Same owner-or-admin boundary as activity.


@router.get("/{course_id}/runs", response_model=GenerationRunListResponse)
async def get_course_generation_runs(
    course_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: CourseService = Depends(_service),
    current_user: User = Depends(get_current_user),
) -> GenerationRunListResponse:
    await get_course_for_author_action(course_id, current_user, service)
    runs = await service.list_generation_runs(course_id, limit=limit, offset=offset)
    return GenerationRunListResponse(course_id=course_id, runs=runs)
