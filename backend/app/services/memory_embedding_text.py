"""Deterministic "what to embed" text for each of the four memory sources.

Kept as plain functions (no state, no I/O) so both the lifecycle hooks
(memory_embedding_sync) and the backfill script build embedding text the
exact same way - a row is "stale" precisely when hashing this output no
longer matches its stored content_hash.
"""

from __future__ import annotations

from app.db.models import CourseSample, GenerationRun, Template, VisualKnowledge
from app.schemas.template import CourseTemplate

# GenerationRun.payload_json can list every chapter in a long course - only a
# few titles are embedded, never the full payload (which may also contain
# large warning/error text).
MAX_GENERATION_RUN_ITEMS = 5


def _join(parts: list[str]) -> str:
    return " | ".join(p.strip() for p in parts if p and p.strip())


def template_text(row: Template) -> str:
    config = CourseTemplate.model_validate(row.template_json)
    return _join(
        [
            row.name,
            row.kind,
            row.description,
            config.writer_guidance,
            "; ".join(config.review_focus),
        ]
    )


def visual_text(row: VisualKnowledge) -> str:
    return _join(
        [
            row.diagram_kind or row.kind,
            row.topic,
            row.description,
            ", ".join(row.tags),
        ]
    )


def sample_text(row: CourseSample) -> str:
    return _join([row.title, row.topic, row.description, ", ".join(row.tags)])


def generation_run_text(course_title: str, run: GenerationRun) -> str:
    payload = run.payload_json or {}
    generated = [str(c) for c in (payload.get("chapters_generated") or [])]
    failed = [str(c) for c in (payload.get("chapters_failed") or [])]
    parts = [
        course_title,
        run.state,
        f"{len(generated)} chapter(s) generated",
    ]
    if failed:
        parts.append(f"{len(failed)} failed")
    parts.extend(generated[:MAX_GENERATION_RUN_ITEMS])
    return _join(parts)
