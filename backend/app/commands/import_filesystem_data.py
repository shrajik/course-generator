"""Import existing filesystem course artifacts into PostgreSQL.

Usage from the backend directory:
    python -m app.commands.import_filesystem_data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import reset_settings_cache
from app.core.ids import document_id_for_course
from app.db.engine import dispose_engine
from app.db.models import Blueprint, Course, Document
from app.db.service import COURSE_RECORD_COLUMN_FIELDS
from app.db.session import get_session_factory
from app.schemas.blueprint import CourseBlueprint
from app.schemas.course import CourseRecord
from app.schemas.document import CourseDocument


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _course_values(record: CourseRecord) -> dict[str, Any]:
    dumped = record.model_dump(mode="json")
    # Excludes the same fields as DatabaseService.save_course_record - both
    # writers share COURSE_RECORD_COLUMN_FIELDS so they can't drift apart
    # again (they previously did: this importer used a shorter, stale list
    # that left owner_id/review_* in metadata_json instead of their own
    # columns, which then collided with those same fields read back
    # explicitly in _to_course_record and crashed GET /api/courses for every
    # course whenever an imported one was in the list).
    metadata = {key: value for key, value in dumped.items() if key not in COURSE_RECORD_COLUMN_FIELDS}
    return {
        "course_id": record.course_id,
        "document_id": record.document_id,
        "title": record.input.course_title,
        "status": record.status,
        "template_id": record.template_id,
        "owner_id": uuid.UUID(record.owner_id) if record.owner_id else None,
        "review_status": record.review_status,
        "review_comment": record.review_comment,
        "reviewer_id": uuid.UUID(record.reviewer_id) if record.reviewer_id else None,
        "reviewed_at": _timestamp(record.reviewed_at) if record.reviewed_at else None,
        "input_json": record.input.model_dump(mode="json"),
        "metadata_json": metadata,
        "created_at": _timestamp(record.created_at),
        "updated_at": _timestamp(record.updated_at),
    }


async def import_courses(data_dir: Path) -> dict[str, int]:
    courses_dir = data_dir / "courses"
    report = {"imported": 0, "updated": 0, "skipped": 0, "blueprints": 0, "documents": 0}
    if not courses_dir.exists():
        print(f"SKIP: courses directory does not exist: {courses_dir}")
        return report

    session_factory = get_session_factory()
    for course_dir in sorted(path for path in courses_dir.iterdir() if path.is_dir()):
        course_path = course_dir / "course.json"
        if not course_path.exists():
            report["skipped"] += 1
            print(f"SKIP: {course_dir.name}: course.json is missing")
            continue
        try:
            record = CourseRecord.model_validate(_read_json(course_path))
        except Exception as exc:
            report["skipped"] += 1
            print(f"SKIP: {course_dir.name}: invalid course.json ({exc})")
            continue

        expected_document_id = document_id_for_course(record.course_id)
        if record.document_id != expected_document_id:
            print(
                f"WARN: {record.course_id}: document_id {record.document_id!r} "
                f"does not match derived value {expected_document_id!r}; preserving file value"
            )

        try:
            async with session_factory() as session:
                async with session.begin():
                    course = await session.scalar(select(Course).where(Course.course_id == record.course_id))
                    was_existing = course is not None
                    if course is None:
                        course = Course(**_course_values(record))
                        session.add(course)
                        await session.flush()
                    else:
                        for key, value in _course_values(record).items():
                            setattr(course, key, value)
                    report["updated" if was_existing else "imported"] += 1

                    blueprint_path = course_dir / "blueprint.json"
                    if blueprint_path.exists():
                        try:
                            blueprint_payload = _read_json(blueprint_path)
                            CourseBlueprint.model_validate(blueprint_payload)
                            existing_blueprint = await session.scalar(
                                select(Blueprint).where(Blueprint.course_pk == course.id)
                            )
                            if existing_blueprint is None:
                                session.add(Blueprint(course_pk=course.id, blueprint_json=blueprint_payload,
                                    created_at=_timestamp(record.created_at), updated_at=_timestamp(record.updated_at)))
                            else:
                                existing_blueprint.blueprint_json = blueprint_payload
                                existing_blueprint.updated_at = _timestamp(record.updated_at)
                            report["blueprints"] += 1
                        except Exception as exc:
                            print(f"WARN: {record.course_id}: blueprint skipped ({exc})")
                    else:
                        print(f"INFO: {record.course_id}: blueprint.json is missing")

                    document_path = course_dir / "document.json"
                    if document_path.exists():
                        try:
                            document_payload = _read_json(document_path)
                            document = CourseDocument.model_validate(document_payload)
                            if document.course_id != record.course_id or document.document_id != record.document_id:
                                raise ValueError("document course_id/document_id does not match course.json")
                            existing_document = await session.scalar(
                                select(Document).where(Document.document_id == record.document_id)
                            )
                            if existing_document is None:
                                session.add(Document(
                                    document_id=document.document_id,
                                    course_pk=course.id,
                                    version=document.version,
                                    document_json=document_payload,
                                    created_at=_timestamp(document.created_at),
                                    updated_at=_timestamp(document.updated_at),
                                ))
                            else:
                                existing_document.version = document.version
                                existing_document.document_json = document_payload
                                existing_document.updated_at = _timestamp(document.updated_at)
                            report["documents"] += 1
                        except Exception as exc:
                            print(f"WARN: {record.course_id}: document skipped ({exc})")
                    else:
                        print(f"INFO: {record.course_id}: document.json is missing")
        except Exception as exc:
            report["skipped"] += 1
            print(f"ERROR: {record.course_id}: transaction rolled back ({exc})")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
        reset_settings_cache()
    data_dir = args.data_dir or Path(os.getenv("DATA_DIR", "data"))
    try:
        report = asyncio.run(import_courses(data_dir))
        print(f"DONE: {json.dumps(report, sort_keys=True)}")
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    main()