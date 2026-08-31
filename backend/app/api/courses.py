"""Course endpoints. Thin layer: validate, delegate, serialise."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.errors import NotFoundError
from app.course.templates.registry import available_templates, load_template
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import (
    CourseInput,
    CourseRecord,
    CreateCourseRequest,
    GenerateRequest,
    GenerateResponse,
    ImproveTocRequest,
    ImproveTocResponse,
)
from app.schemas.document import CourseDocument
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
) -> CourseRecord:
    """Create a course and (by default) run the planner to produce a blueprint."""
    course_input = CourseInput.model_validate(request.model_dump(exclude={"run_planner"}))
    return await service.create_course(course_input, run_planner=request.run_planner)


@router.post("/improve-toc", response_model=ImproveTocResponse)
async def improve_toc(
    request: ImproveTocRequest,
    service: CourseService = Depends(_service),
) -> ImproveTocResponse:
    """Propose an improved table of contents. Nothing is saved or applied."""
    return await service.improve_toc(request)


@router.get("", response_model=dict)
@router.get("/", response_model=dict, include_in_schema=False)
async def list_courses(storage: StorageService = Depends(_storage)) -> dict[str, Any]:
    courses = []
    for course_id in storage.list_course_ids():
        try:
            record = storage.load_course(course_id)
        except Exception:  # pragma: no cover
            continue
        courses.append(
            {
                "course_id": record.course_id,
                "document_id": record.document_id,
                "course_title": record.input.course_title,
                "template_id": record.template_id,
                "status": record.status,
                "updated_at": record.updated_at,
            }
        )
    return {"courses": courses, "count": len(courses)}


@router.get("/{course_id}")
async def get_course(
    course_id: str,
    include_blueprint: bool = Query(default=True),
    storage: StorageService = Depends(_storage),
) -> dict[str, Any]:
    record = storage.load_course(course_id)
    payload: dict[str, Any] = {"course": record.model_dump(mode="json")}
    if include_blueprint and storage.has_blueprint(course_id):
        payload["blueprint"] = storage.load_blueprint(course_id).model_dump(mode="json")
    payload["artifacts"] = {
        "blueprint": storage.has_blueprint(course_id),
        # True as soon as the first chapter is assembled, so the editor can be
        # opened while later chapters are still being written.
        "document": storage.has_document(course_id),
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
    course_id: str, storage: StorageService = Depends(_storage)
) -> CourseBlueprint:
    return storage.load_blueprint(course_id)


@router.post("/{course_id}/generate", response_model=GenerateResponse)
async def generate_course(
    course_id: str,
    request: GenerateRequest | None = None,
    service: CourseService = Depends(_service),
) -> GenerateResponse:
    """Research + write + review + assemble.

    Backgrounded by default: returns a job id immediately and the caller polls
    `GET /api/courses/{course_id}`. Pass `{"mode": "sync"}` to block until the run
    finishes, `chapter_ids` to regenerate part of a course, or `resume: true` to
    continue an interrupted run.
    """
    return await service.start_generation(course_id, request or GenerateRequest())


@router.get("/{course_id}/run")
async def get_run_state(
    course_id: str, service: CourseService = Depends(_service)
) -> dict[str, Any]:
    """Progress, ETA and timings for the current or last generation run."""
    run = service.job_state(course_id)
    return {"course_id": course_id, "run": run.model_dump(mode="json") if run else None}


@router.get("/{course_id}/document", response_model=CourseDocument)
async def get_course_document(
    course_id: str, storage: StorageService = Depends(_storage)
) -> CourseDocument:
    """The source of truth for this course."""
    return storage.load_document(course_id)


@router.get("/{course_id}/chapters/{chapter_id}")
async def get_chapter(
    course_id: str, chapter_id: str, storage: StorageService = Depends(_storage)
) -> dict[str, Any]:
    blueprint = storage.load_blueprint(course_id)
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
    course_id: str, chapter_id: str, storage: StorageService = Depends(_storage)
) -> dict[str, Any]:
    blueprint = storage.load_blueprint(course_id)
    chapter = blueprint.chapter_by_id(chapter_id)
    if chapter is None:
        raise NotFoundError(f"Chapter '{chapter_id}' is not part of this course")
    return storage.load_research(course_id, chapter_id, chapter.order).model_dump(mode="json")


@router.get("/{course_id}/template")
async def get_course_template(
    course_id: str, storage: StorageService = Depends(_storage)
) -> dict[str, Any]:
    record = storage.load_course(course_id)
    return load_template(record.template_id).model_dump(mode="json")
