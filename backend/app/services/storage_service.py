"""Local filesystem storage. No database, no object store.

Layout:
    data/courses/{course_id}/course.json
    data/courses/{course_id}/blueprint.json
    data/courses/{course_id}/research/chapter_01.json
    data/courses/{course_id}/chapters/chapter_01.json
    data/courses/{course_id}/document.json
    data/courses/{course_id}/assets/image_001.png
    data/courses/{course_id}/exports/course.pdf

Every read/write goes through this module so swapping in Postgres/S3 later means
reimplementing one class, not touching the AI services.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.ids import course_id_for_document, utc_now_iso
from app.core.logging import get_logger
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import CourseRecord
from app.schemas.document import CourseDocument
from app.schemas.draft import GeneratedChapter
from app.schemas.research import ChapterResearch

log = get_logger(__name__)

_CHAPTER_NUM_RE = re.compile(r"(\d+)")


def chapter_filename(chapter_id: str, chapter_number: int | None = None) -> str:
    """`chapter_1` / `chapter_01` / `ch3` -> `chapter_01.json`"""
    if chapter_number is None:
        match = _CHAPTER_NUM_RE.search(chapter_id or "")
        chapter_number = int(match.group(1)) if match else 0
    return f"chapter_{chapter_number:02d}.json"


class StorageService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- paths ------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.settings.courses_dir)

    def course_dir(self, course_id: str) -> Path:
        return self.root / course_id

    def research_dir(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "research"

    def chapters_dir(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "chapters"

    def assets_dir(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "assets"

    def exports_dir(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "exports"

    def document_path(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "document.json"

    def blueprint_path(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "blueprint.json"

    def course_path(self, course_id: str) -> Path:
        return self.course_dir(course_id) / "course.json"

    def pdf_path(self, course_id: str) -> Path:
        return self.exports_dir(course_id) / "course.pdf"

    def html_path(self, course_id: str) -> Path:
        return self.exports_dir(course_id) / "course.html"

    def ensure_course_dirs(self, course_id: str) -> None:
        for path in (
            self.course_dir(course_id),
            self.research_dir(course_id),
            self.chapters_dir(course_id),
            self.assets_dir(course_id),
            self.exports_dir(course_id),
        ):
            path.mkdir(parents=True, exist_ok=True)

    # --- primitives -------------------------------------------------------
    @staticmethod
    def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        # Atomic replace so a crash never leaves a half-written artifact.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):  # pragma: no cover
                os.unlink(tmp)
        return path

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise NotFoundError(f"File not found: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_model(self, path: Path, model: BaseModel) -> Path:
        return self.write_json(path, model.model_dump(mode="json"))

    # --- course record ----------------------------------------------------
    def save_course(self, record: CourseRecord) -> CourseRecord:
        record.updated_at = utc_now_iso()
        self.ensure_course_dirs(record.course_id)
        self.write_model(self.course_path(record.course_id), record)
        return record

    def load_course(self, course_id: str) -> CourseRecord:
        try:
            return CourseRecord.model_validate(self.read_json(self.course_path(course_id)))
        except NotFoundError as exc:
            raise NotFoundError(f"Course '{course_id}' not found") from exc

    def course_exists(self, course_id: str) -> bool:
        return self.course_path(course_id).exists()

    def list_course_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and (p / "course.json").exists()
        )

    def resolve_course_id_for_document(self, document_id: str) -> str:
        """Document ids are derived from course ids, with a scan as a fallback."""
        candidate = course_id_for_document(document_id)
        if self.course_exists(candidate):
            return candidate
        for course_id in self.list_course_ids():
            try:
                record = self.load_course(course_id)
            except Exception:  # pragma: no cover
                continue
            if record.document_id == document_id:
                return course_id
        raise NotFoundError(f"No course found for document '{document_id}'")

    # --- blueprint --------------------------------------------------------
    def save_blueprint(self, course_id: str, blueprint: CourseBlueprint) -> Path:
        return self.write_model(self.blueprint_path(course_id), blueprint)

    def load_blueprint(self, course_id: str) -> CourseBlueprint:
        try:
            return CourseBlueprint.model_validate(self.read_json(self.blueprint_path(course_id)))
        except NotFoundError as exc:
            raise NotFoundError(
                f"Course '{course_id}' has no blueprint yet - run the planner first"
            ) from exc

    def has_blueprint(self, course_id: str) -> bool:
        return self.blueprint_path(course_id).exists()

    # --- research ---------------------------------------------------------
    def research_path(self, course_id: str, chapter_id: str, chapter_number: int | None = None) -> Path:
        return self.research_dir(course_id) / chapter_filename(chapter_id, chapter_number)

    def save_research(
        self, course_id: str, research: ChapterResearch, chapter_number: int | None = None
    ) -> Path:
        return self.write_model(
            self.research_path(course_id, research.chapter_id, chapter_number), research
        )

    def load_research(
        self, course_id: str, chapter_id: str, chapter_number: int | None = None
    ) -> ChapterResearch:
        path = self.research_path(course_id, chapter_id, chapter_number)
        return ChapterResearch.model_validate(self.read_json(path))

    def has_research(
        self, course_id: str, chapter_id: str, chapter_number: int | None = None
    ) -> bool:
        return self.research_path(course_id, chapter_id, chapter_number).exists()

    # --- chapters ---------------------------------------------------------
    def chapter_path(self, course_id: str, chapter_id: str, chapter_number: int | None = None) -> Path:
        return self.chapters_dir(course_id) / chapter_filename(chapter_id, chapter_number)

    def save_chapter(self, course_id: str, chapter: GeneratedChapter) -> Path:
        return self.write_model(
            self.chapter_path(course_id, chapter.chapter_id, chapter.chapter_number), chapter
        )

    def load_chapter(
        self, course_id: str, chapter_id: str, chapter_number: int | None = None
    ) -> GeneratedChapter:
        path = self.chapter_path(course_id, chapter_id, chapter_number)
        return GeneratedChapter.model_validate(self.read_json(path))

    def has_chapter(
        self, course_id: str, chapter_id: str, chapter_number: int | None = None
    ) -> bool:
        return self.chapter_path(course_id, chapter_id, chapter_number).exists()

    def load_all_chapters(self, course_id: str) -> list[GeneratedChapter]:
        directory = self.chapters_dir(course_id)
        if not directory.exists():
            return []
        chapters: list[GeneratedChapter] = []
        for path in sorted(directory.glob("chapter_*.json")):
            try:
                chapters.append(GeneratedChapter.model_validate(self.read_json(path)))
            except Exception as exc:  # pragma: no cover
                log.warning("Skipping unreadable chapter %s: %s", path.name, exc)
        return sorted(chapters, key=lambda c: c.chapter_number)

    # --- document ---------------------------------------------------------
    def save_document(self, document: CourseDocument) -> Path:
        return self.write_model(self.document_path(document.course_id), document)

    def load_document(self, course_id: str) -> CourseDocument:
        try:
            return CourseDocument.model_validate(self.read_json(self.document_path(course_id)))
        except NotFoundError as exc:
            raise NotFoundError(
                f"Course '{course_id}' has no course document yet - run generation first"
            ) from exc

    def has_document(self, course_id: str) -> bool:
        return self.document_path(course_id).exists()

    # --- assets -----------------------------------------------------------
    def next_asset_name(self, course_id: str, extension: str = "png") -> str:
        directory = self.assets_dir(course_id)
        directory.mkdir(parents=True, exist_ok=True)
        existing = list(directory.glob(f"image_*.{extension}"))
        return f"image_{len(existing) + 1:03d}.{extension}"

    def save_asset(self, course_id: str, data: bytes, extension: str = "png") -> str:
        """Returns the course-relative path stored in the document JSON."""
        name = self.next_asset_name(course_id, extension)
        path = self.assets_dir(course_id) / name
        path.write_bytes(data)
        return f"assets/{name}"

    def asset_abs_path(self, course_id: str, relative: str) -> Path:
        return self.course_dir(course_id) / relative


_storage: StorageService | None = None


def get_storage() -> StorageService:
    global _storage
    if _storage is None:
        _storage = StorageService()
    return _storage


def reset_storage() -> None:
    """Test helper - forces the settings/data dir to be re-read."""
    global _storage
    _storage = None
