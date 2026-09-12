"""Backfill memory_embeddings for existing rows.

Deliberately a separate, explicitly-invoked script rather than anything run
at startup - embedding an existing database is a potentially slow, API-cost-
incurring operation that an operator should choose to run (and can re-run
safely: already-valid embeddings are skipped, so this is idempotent).

Usage:

    python -m app.scripts.backfill_embeddings                # backfill everything
    python -m app.scripts.backfill_embeddings --dry-run       # report only, no writes/API calls
    python -m app.scripts.backfill_embeddings --source template,course_sample
    python -m app.scripts.backfill_embeddings --concurrency 8 --batch-size 200

A row is "valid" (skipped) when a memory_embeddings row already exists for it
AND its content_hash matches the current text built from the row right now -
so editing a sample/visual/template's text and re-running this script
re-embeds only what actually changed. A row with no embedding at all (created
before this feature, or created while embeddings were failing/disabled) is
always backfilled.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from app.core.logging import configure_logging, get_logger
from app.db.models import CourseSample, GenerationRun, Template, VisualKnowledge
from app.db.repositories.course_samples import CourseSampleRepository
from app.db.repositories.generation_runs import GenerationRunRepository
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.repositories.templates import TemplateRepository
from app.db.repositories.visual_knowledge import VisualKnowledgeRepository
from app.db.session import get_session_factory
from app.services.embedding_service import content_hash, get_embedding_service
from app.services.memory_embedding_text import (
    generation_run_text,
    sample_text,
    template_text,
    visual_text,
)

log = get_logger(__name__)

PAGE_SIZE = 100
ALL_SOURCE_TYPES = ("template", "visual_knowledge", "course_sample", "generation_run")
# One retry beyond the first attempt - a transient provider hiccup shouldn't
# make an operator re-run the whole script; a persistent failure (bad model
# name, no key) still fails fast rather than retrying forever.
MAX_ATTEMPTS = 2


class BackfillReport:
    def __init__(self) -> None:
        self.scanned = 0
        self.embedded = 0
        self.skipped_valid = 0
        self.skipped_blank = 0
        self.failed = 0

    def add(self, other: "BackfillReport") -> None:
        self.scanned += other.scanned
        self.embedded += other.embedded
        self.skipped_valid += other.skipped_valid
        self.skipped_blank += other.skipped_blank
        self.failed += other.failed

    def line(self, source_type: str) -> str:
        return (
            f"{source_type:16s} scanned={self.scanned:5d} embedded={self.embedded:5d} "
            f"skipped_valid={self.skipped_valid:5d} skipped_blank={self.skipped_blank:5d} "
            f"failed={self.failed:5d}"
        )


async def _embed_rows(
    source_type: str,
    rows: list[tuple],  # (source_id, text)
    *,
    dry_run: bool,
    concurrency: int,
) -> BackfillReport:
    report = BackfillReport()
    if not rows:
        return report

    service = get_embedding_service()
    ids = [source_id for source_id, _ in rows]
    async with get_session_factory()() as session:
        existing = await MemoryEmbeddingRepository(session).get_many(
            source_type=source_type, source_ids=ids
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def handle(source_id, text: str) -> None:
        report.scanned += 1
        text = (text or "").strip()
        if not text:
            report.skipped_blank += 1
            return
        expected_hash = content_hash(text)
        current = existing.get(source_id)
        if current is not None and current.content_hash == expected_hash:
            report.skipped_valid += 1
            return
        if dry_run:
            report.embedded += 1
            return

        async with semaphore:
            vector = None
            for attempt in range(MAX_ATTEMPTS):
                vector = await service.embed_one(text)
                if vector is not None:
                    break
                log.warning(
                    "Backfill: embed attempt %s/%s failed for %s:%s",
                    attempt + 1, MAX_ATTEMPTS, source_type, source_id,
                )
            if vector is None:
                report.failed += 1
                return
            try:
                async with get_session_factory()() as session:
                    async with session.begin():
                        await MemoryEmbeddingRepository(session).upsert(
                            source_type=source_type,
                            source_id=source_id,
                            model=service.settings.embedding_model,
                            content_hash=expected_hash,
                            embedded_text=text,
                            embedding=vector,
                        )
                report.embedded += 1
            except Exception as exc:  # noqa: BLE001 - one row's failure must not stop the run
                report.failed += 1
                log.warning("Backfill: could not store embedding for %s:%s: %s", source_type, source_id, exc)

    await asyncio.gather(*(handle(source_id, text) for source_id, text in rows))
    return report


async def _paginate(fetch_page, build_rows) -> list:
    """`fetch_page(limit, offset)` -> ORM rows; `build_rows` turns a page of
    ORM rows into (source_id, text) tuples. Pages until a short page ends it."""
    rows: list = []
    offset = 0
    while True:
        page = await fetch_page(PAGE_SIZE, offset)
        if not page:
            break
        rows.extend(build_rows(page))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


async def backfill_templates(*, dry_run: bool, concurrency: int) -> BackfillReport:
    async def fetch_page(limit: int, offset: int) -> list[Template]:
        async with get_session_factory()() as session:
            return list(await TemplateRepository(session).list_all(limit=limit, offset=offset))

    rows = await _paginate(fetch_page, lambda page: [(row.id, template_text(row)) for row in page])
    return await _embed_rows("template", rows, dry_run=dry_run, concurrency=concurrency)


async def backfill_visual_knowledge(*, dry_run: bool, concurrency: int) -> BackfillReport:
    async def fetch_page(limit: int, offset: int) -> list[VisualKnowledge]:
        async with get_session_factory()() as session:
            return list(await VisualKnowledgeRepository(session).list(limit=limit, offset=offset))

    rows = await _paginate(fetch_page, lambda page: [(row.id, visual_text(row)) for row in page])
    return await _embed_rows("visual_knowledge", rows, dry_run=dry_run, concurrency=concurrency)


async def backfill_course_samples(*, dry_run: bool, concurrency: int) -> BackfillReport:
    async def fetch_page(limit: int, offset: int) -> list[CourseSample]:
        async with get_session_factory()() as session:
            return list(await CourseSampleRepository(session).list(limit=limit, offset=offset))

    rows = await _paginate(fetch_page, lambda page: [(row.id, sample_text(row)) for row in page])
    return await _embed_rows("course_sample", rows, dry_run=dry_run, concurrency=concurrency)


async def backfill_generation_runs(*, dry_run: bool, concurrency: int) -> BackfillReport:
    async def fetch_page(limit: int, offset: int) -> list[tuple[GenerationRun, object]]:
        async with get_session_factory()() as session:
            return await GenerationRunRepository(session).list_all_with_course(limit=limit, offset=offset)

    rows = await _paginate(
        fetch_page,
        lambda page: [(run.id, generation_run_text(course.title, run)) for run, course in page],
    )
    return await _embed_rows("generation_run", rows, dry_run=dry_run, concurrency=concurrency)


BACKFILL_FUNCS = {
    "template": backfill_templates,
    "visual_knowledge": backfill_visual_knowledge,
    "course_sample": backfill_course_samples,
    "generation_run": backfill_generation_runs,
}


async def run(*, source_types: list[str], dry_run: bool, concurrency: int) -> int:
    service = get_embedding_service()
    if not service.enabled:
        log.warning(
            "enable_embeddings is false (or no API key configured) - nothing to backfill. "
            "Set ENABLE_EMBEDDINGS=true and OPENAI_API_KEY before running this script."
        )
        return 0

    started = time.perf_counter()
    total = BackfillReport()
    for source_type in source_types:
        report = await BACKFILL_FUNCS[source_type](dry_run=dry_run, concurrency=concurrency)
        total.add(report)
        print(report.line(source_type))  # noqa: T201 - this is a CLI script

    elapsed = time.perf_counter() - started
    mode = "DRY RUN - " if dry_run else ""
    print(f"{mode}{total.line('TOTAL')} in {elapsed:.1f}s")  # noqa: T201
    return 1 if total.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill semantic memory embeddings.")
    parser.add_argument(
        "--source",
        default=",".join(ALL_SOURCE_TYPES),
        help=f"Comma-separated source types to backfill, from {ALL_SOURCE_TYPES}.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be embedded, write nothing.")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent embedding calls.")
    args = parser.parse_args()

    source_types = [s.strip() for s in args.source.split(",") if s.strip()]
    unknown = set(source_types) - set(ALL_SOURCE_TYPES)
    if unknown:
        parser.error(f"Unknown --source value(s): {sorted(unknown)} (choose from {ALL_SOURCE_TYPES})")

    configure_logging()
    exit_code = asyncio.run(run(source_types=source_types, dry_run=args.dry_run, concurrency=args.concurrency))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
