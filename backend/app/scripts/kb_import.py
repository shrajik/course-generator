"""Import the file-based knowledge library into PostgreSQL, and prove it matches.

The library lives under `data/sources/global/` (see app/services/knowledge_service.py).
This copies it into the `kb_*` tables (migration 20261103_0012) **without changing
anything a course can observe**:

* every document keeps its permanent number, so `[G2.5]` means what it always meant;
* every passage keeps its position (ordinal = old index + 1) and its exact text;
* every vector is copied bit for bit - nothing is re-embedded, so nothing is paid for;
* a document that was switched on stays Published, one that was switched off becomes
  Unpublished (new uploads, later, start as Draft);
* the original `src_...` id is kept as `public_id`, so the existing API and the course
  records that mention it keep resolving.

The files are only ever read. Re-running is safe: a document already imported is
recognised by its `public_id` and left alone.

Usage (from `backend/`):

    python -m app.scripts.kb_import --dry-run            # read the files, touch no database
    python -m app.scripts.kb_import                      # import what is missing
    python -m app.scripts.kb_import --verify             # compare database to files, change nothing
    python -m app.scripts.kb_import --report parity.json # also write the report as JSON

Exit status is non-zero when a document could not be imported or verification finds
a difference, so it can gate a cutover (docs/KB_CUTOVER_RUNBOOK.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.ingestion.retrieval import tokenize
from app.kb.service import raise_number_sequence
from app.schemas.source import SourceChunk, SourceRecord
from app.services.embedding_service import content_hash
from app.services.knowledge_service import GLOBAL_OWNER
from app.services.source_service import SourceService

log = get_logger(__name__)

_SOURCE_DIR_RE = re.compile(r"^src_[a-f0-9]{12}$")


# --- reading the files (read-only) --------------------------------------------------


@dataclass
class LegacyDoc:
    record: SourceRecord
    chunks: list[SourceChunk]
    # One entry per chunk: its vector, or None where the chunk had none.
    vectors: list[list[float] | None]
    original_key: str  # path of the original, relative to DATA_DIR

    @property
    def embedded(self) -> int:
        return sum(1 for vector in self.vectors if vector is not None)


@dataclass
class Skipped:
    source_id: str
    reason: str


def _original_key(sources: SourceService, record: SourceRecord) -> str:
    folder = sources.root / GLOBAL_OWNER / record.source_id
    found = next(iter(sorted(folder.glob("original.*"))), None)
    name = found.name if found is not None else f"original.{record.kind}"
    return (Path("sources") / GLOBAL_OWNER / record.source_id / name).as_posix()


def load_legacy(sources: SourceService) -> tuple[list[LegacyDoc], list[Skipped]]:
    """Every library document that can be imported, and why the others cannot.

    Reads `meta.json`, `chunks.json` and `vectors.bin` directly. `SourceService.get`
    is avoided on purpose: it rewrites the record of a document found stuck in
    processing, and the import must not modify the files.
    """
    folder = sources.root / GLOBAL_OWNER
    docs: list[LegacyDoc] = []
    skipped: list[Skipped] = []
    if not folder.is_dir():
        return docs, skipped
    for child in sorted(folder.iterdir()):
        if not (child.is_dir() and _SOURCE_DIR_RE.match(child.name)):
            continue
        try:
            record = SourceRecord.model_validate_json((child / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append(Skipped(child.name, "unreadable meta.json"))
            continue
        if record.global_number is None:
            skipped.append(Skipped(child.name, "no permanent number"))
        elif record.status != "ready":
            skipped.append(Skipped(child.name, f"status is '{record.status}', not ready"))
        else:
            chunks = sources.load_chunks(GLOBAL_OWNER, record.source_id)
            if not chunks:
                skipped.append(Skipped(child.name, "no passages"))
                continue
            vectors = sources._load_vectors(GLOBAL_OWNER, record.source_id, len(chunks))  # noqa: SLF001
            usable = [
                vectors[chunk.index] if vectors is not None and chunk.embedded else None for chunk in chunks
            ]
            docs.append(LegacyDoc(record, chunks, usable, _original_key(sources, record)))
    docs.sort(key=lambda doc: doc.record.global_number or 0)
    return docs, skipped


def legacy_counter(sources: SourceService) -> int:
    """The highest number ever handed out, from the persisted counter."""
    path = sources.root / GLOBAL_OWNER / "_counter.json"
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["last"])
    except (OSError, ValueError, KeyError):
        return 0


# --- turning a document into rows (pure) --------------------------------------------


def _title_for(record: SourceRecord) -> str:
    stem = record.filename.rsplit(".", 1)[0] if "." in record.filename else record.filename
    return (" ".join(stem.replace("_", " ").split()) or record.filename)[:300]


def _when(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lifecycle_for(record: SourceRecord) -> str:
    """On stays Published; off becomes Unpublished (it has been live before)."""
    return "published" if record.enabled else "unpublished"


def centroid_of(vectors: list[list[float] | None]) -> list[float] | None:
    rows = [np.asarray(v, dtype=np.float32) for v in vectors if v is not None]
    rows = [row / norm for row in rows if (norm := float(np.linalg.norm(row))) > 0]
    if not rows:
        return None
    mean = np.mean(rows, axis=0)
    return [float(x) for x in mean]


@dataclass
class DocRows:
    item: dict
    version: dict
    source: dict
    chunks: list[dict]
    version_chunks: list[dict]
    activity: dict


def build_rows(doc: LegacyDoc, *, embed_model: str) -> DocRows:
    record = doc.record
    item_id, version_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lifecycle = lifecycle_for(record)
    live = lifecycle == "published"
    created = _when(record.created_at)

    chunks: list[dict] = []
    links: list[dict] = []
    for position, (chunk, vector) in enumerate(zip(doc.chunks, doc.vectors)):
        chunk_id = uuid.uuid4()
        terms = tokenize(chunk.embed_text())
        chunks.append(
            {
                "id": chunk_id,
                "item_id": item_id,
                "ordinal": chunk.index + 1,  # [G{n}.{ordinal}] is unchanged
                "source_id": source_id,
                "heading_path": list(chunk.heading_path),
                "heading": chunk.heading,
                "page": chunk.page,
                "text": chunk.text,
                "words": chunk.words,
                "embed_hash": content_hash(chunk.embed_text()),
                "embedding": vector,
                "embed_model": embed_model if vector is not None else None,
                "tokens": sorted(set(terms)),
                "token_count": len(terms),
                "is_live": live,
            }
        )
        links.append({"version_id": version_id, "chunk_id": chunk_id, "position": position})

    return DocRows(
        item={
            "id": item_id,
            "number": record.global_number,
            "public_id": record.source_id,
            "title": _title_for(record),
            "description": record.description,
            "lifecycle_status": lifecycle,
            "origin": "migrated",
            "next_ordinal": max(chunk.index for chunk in doc.chunks) + 2,
            "centroid": centroid_of(doc.vectors),
            "created_at": created,
            "updated_at": _when(record.updated_at),
            "published_at": created if live else None,
        },
        version={
            "id": version_id,
            "item_id": item_id,
            "version_no": 1,
            "processing_status": "ready",
            "change_note": "Imported from the file library",
            "created_at": created,
            "live_from": created if live else None,
        },
        source={
            "id": source_id,
            "sha256": record.sha256,
            "filename": record.filename,
            "kind": record.kind,
            "size_bytes": record.size_bytes,
            "storage_key": doc.original_key,
            "page_count": record.page_count,
            "word_count": record.word_count,
            "outline": list(record.outline),
            "created_at": created,
        },
        chunks=chunks,
        version_chunks=links,
        activity={
            "id": uuid.uuid4(),
            "item_id": item_id,
            "action": "migrated",
            "detail": {"from": "files", "source_id": record.source_id, "chunks": len(chunks), "embedded": doc.embedded},
        },
    )


# --- comparing database to files (pure) ---------------------------------------------


@dataclass
class DbChunk:
    ordinal: int
    text: str
    heading_path: list[str]
    page: int | None
    embed_hash: str
    embedding: list[float] | None
    is_live: bool
    tokens: list[str] = field(default_factory=list)
    token_count: int = 0


@dataclass
class DbDoc:
    number: int
    public_id: str
    lifecycle_status: str
    description: str
    chunks: list[DbChunk] = field(default_factory=list)  # the live (v1) version's, by position


def compare(doc: LegacyDoc, view: DbDoc | None) -> list[str]:
    """Differences between a file document and its database copy; empty means identical
    in everything a citation, a search or a course can observe."""
    name = doc.record.source_id
    if view is None:
        return [f"{name}: not in the database"]
    problems: list[str] = []
    if view.number != doc.record.global_number:
        problems.append(f"{name}: number {view.number} != {doc.record.global_number}")
    expected = lifecycle_for(doc.record)
    if view.lifecycle_status != expected:
        problems.append(f"{name}: lifecycle {view.lifecycle_status} != {expected}")
    if view.description != doc.record.description:
        problems.append(f"{name}: description differs")
    if len(view.chunks) != len(doc.chunks):
        problems.append(f"{name}: {len(view.chunks)} passages != {len(doc.chunks)}")
        return problems
    live = expected == "published"
    for chunk, vector, db in zip(doc.chunks, doc.vectors, view.chunks):
        ref = f"G{doc.record.global_number}.{chunk.index + 1}"
        if db.ordinal != chunk.index + 1:
            problems.append(f"{ref}: ordinal {db.ordinal}")
        if db.text != chunk.text:
            problems.append(f"{ref}: text differs")
        if db.heading_path != list(chunk.heading_path) or db.page != chunk.page:
            problems.append(f"{ref}: heading or page differs")
        if db.embed_hash != content_hash(chunk.embed_text()):
            problems.append(f"{ref}: embed hash differs")
        if (db.embedding is None) != (vector is None):
            problems.append(f"{ref}: embedding presence differs")
        elif vector is not None and not np.array_equal(
            np.asarray(db.embedding, dtype=np.float32), np.asarray(vector, dtype=np.float32)
        ):
            problems.append(f"{ref}: vector is not bit-identical")
        terms = tokenize(chunk.embed_text())
        if sorted(db.tokens) != sorted(set(terms)) or db.token_count != len(terms):
            problems.append(f"{ref}: search terms differ")
        if db.is_live != live:
            problems.append(f"{ref}: is_live {db.is_live} != {live}")
    return problems


# --- the database side ---------------------------------------------------------------


@dataclass
class DocResult:
    source_id: str
    number: int | None
    outcome: str  # imported | already_imported | error
    chunks: int = 0
    embedded: int = 0
    note: str = ""


async def _import_one(session: AsyncSession, doc: LegacyDoc, embed_model: str) -> DocResult:
    record = doc.record
    result = DocResult(record.source_id, record.global_number, "imported", len(doc.chunks), doc.embedded)

    existing = await session.scalar(select(kb.KbItem.number).where(kb.KbItem.public_id == record.source_id))
    if existing is not None:
        result.outcome = "already_imported"
        return result
    holder = await session.scalar(select(kb.KbItem.public_id).where(kb.KbItem.number == record.global_number))
    if holder is not None:
        result.outcome, result.note = "error", f"number {record.global_number} already belongs to {holder}"
        return result

    rows = build_rows(doc, embed_model=embed_model)
    shared = await session.scalar(select(kb.KbSource.id).where(kb.KbSource.sha256 == record.sha256))
    if shared is not None:  # the same file already stored for another item
        rows.source["id"] = shared
        for chunk in rows.chunks:
            chunk["source_id"] = shared
    else:
        await session.execute(insert(kb.KbSource), [rows.source])

    await session.execute(insert(kb.KbItem), [rows.item])
    await session.execute(insert(kb.KbItemVersion), [rows.version])
    await session.execute(
        update(kb.KbItem)
        .where(kb.KbItem.id == rows.item["id"])
        .values(live_version_id=rows.version["id"], latest_version_id=rows.version["id"])
    )
    await session.execute(
        insert(kb.KbVersionSource),
        [{"version_id": rows.version["id"], "source_id": rows.source["id"], "position": 0}],
    )
    for start in range(0, len(rows.chunks), 200):
        await session.execute(insert(kb.KbChunk), rows.chunks[start : start + 200])
    await session.execute(insert(kb.KbVersionChunk), rows.version_chunks)
    await session.execute(insert(kb.KbActivity), [rows.activity])
    if doc.embedded < len(doc.chunks):
        result.note = f"{len(doc.chunks) - doc.embedded} passage(s) had no vector (keyword search only), as before"
    return result


async def import_documents(
    factory: async_sessionmaker[AsyncSession], docs: list[LegacyDoc], counter: int, embed_model: str
) -> list[DocResult]:
    """One transaction per document, so a failure loses only that document and a re-run
    picks up where this one stopped."""
    results: list[DocResult] = []
    for doc in docs:
        try:
            async with factory() as session, session.begin():
                results.append(await _import_one(session, doc, embed_model))
        except Exception as exc:  # noqa: BLE001 - reported per document, never hides the rest
            log.exception("Import of %s failed", doc.record.source_id)
            results.append(
                DocResult(doc.record.source_id, doc.record.global_number, "error", note=str(exc)[:300])
            )
    highest = max([counter, *(doc.record.global_number or 0 for doc in docs)])
    async with factory() as session, session.begin():
        await raise_number_sequence(session, highest)
        await session.execute(update(kb.KbEpoch).where(kb.KbEpoch.id == 1).values(epoch=kb.KbEpoch.epoch + 1, updated_at=func.now()))
    return results


async def load_db_docs(factory: async_sessionmaker[AsyncSession], public_ids: list[str]) -> dict[str, DbDoc]:
    found: dict[str, DbDoc] = {}
    async with factory() as session:
        items = (await session.execute(select(kb.KbItem).where(kb.KbItem.public_id.in_(public_ids)))).scalars().all()
        for item in items:
            chunks = (
                await session.execute(
                    select(kb.KbChunk)
                    .join(kb.KbVersionChunk, kb.KbVersionChunk.chunk_id == kb.KbChunk.id)
                    .where(kb.KbVersionChunk.version_id == item.latest_version_id)
                    .order_by(kb.KbVersionChunk.position)
                )
            ).scalars().all()
            found[item.public_id] = DbDoc(
                number=item.number,
                public_id=item.public_id,
                lifecycle_status=item.lifecycle_status,
                description=item.description,
                chunks=[
                    DbChunk(
                        c.ordinal,
                        c.text,
                        list(c.heading_path),
                        c.page,
                        c.embed_hash,
                        None if c.embedding is None else [float(x) for x in c.embedding],
                        c.is_live,
                        list(c.tokens),
                        c.token_count,
                    )
                    for c in chunks
                ],
            )
    return found


async def sequence_problem(factory: async_sessionmaker[AsyncSession], highest: int) -> str | None:
    async with factory() as session:
        row = (await session.execute(text("SELECT last_value, is_called FROM kb_item_number_seq"))).one()
    next_number = row.last_value + 1 if row.is_called else row.last_value
    if next_number <= highest:
        return f"the next item number would be {next_number}, but number {highest} has already been used"
    return None


async def verify_documents(
    factory: async_sessionmaker[AsyncSession], docs: list[LegacyDoc], counter: int
) -> list[str]:
    views = await load_db_docs(factory, [doc.record.source_id for doc in docs])
    problems: list[str] = []
    for doc in docs:
        problems.extend(compare(doc, views.get(doc.record.source_id)))
    highest = max([counter, *(doc.record.global_number or 0 for doc in docs)])
    if (extra := await sequence_problem(factory, highest)) is not None:
        problems.append(extra)
    return problems


# --- command line ---------------------------------------------------------------------


def _print_plan(docs: list[LegacyDoc], skipped: list[Skipped], counter: int) -> None:
    print(f"{len(docs)} document(s) can be imported; highest number issued so far: {max([counter, *(d.record.global_number or 0 for d in docs)])}")
    for doc in docs:
        r = doc.record
        print(f"  G{r.global_number:<4} {r.source_id}  {r.filename}  passages={len(doc.chunks)} embedded={doc.embedded} -> {lifecycle_for(r)}")
    for item in skipped:
        print(f"  skipped {item.source_id}: {item.reason}")


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    sources = SourceService(settings=settings)
    docs, skipped = load_legacy(sources)
    counter = legacy_counter(sources)
    _print_plan(docs, skipped, counter)
    report: dict = {"documents": len(docs), "skipped": [asdict(s) for s in skipped], "counter": counter}

    if args.dry_run:
        print("Dry run: no database was touched.")
        exit_code = 0
    else:
        factory = get_session_factory()
        exit_code = 0
        if not args.verify:
            results = await import_documents(factory, docs, counter, settings.embedding_model)
            report["results"] = [asdict(r) for r in results]
            for r in results:
                print(f"  {r.outcome:<17} G{r.number} {r.source_id} {r.note}".rstrip())
            if any(r.outcome == "error" for r in results):
                exit_code = 1
        problems = await verify_documents(factory, docs, counter)
        report["verification"] = {"ok": not problems, "problems": problems}
        print("Verification: " + ("every document matches the files." if not problems else f"{len(problems)} problem(s)"))
        for problem in problems[:50]:
            print(f"  PROBLEM {problem}")
        if problems:
            exit_code = 1
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="read the files and report; touch no database")
    parser.add_argument("--verify", action="store_true", help="compare the database to the files; change nothing")
    parser.add_argument("--report", help="write the report as JSON to this path")
    args = parser.parse_args(argv)
    if args.dry_run and args.verify:
        parser.error("--dry-run and --verify are different modes")
    configure_logging()
    return asyncio.run(_run(args, get_settings()))


if __name__ == "__main__":
    sys.exit(main())
