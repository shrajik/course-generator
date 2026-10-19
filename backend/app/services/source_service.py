"""Uploaded source material: store, ingest and retrieve.

A source lives on disk under `data/sources/{owner}/{source_id}/`:

    original.<ext>   the file exactly as uploaded
    meta.json        the SourceRecord (status, counts, outline, error)
    chunks.json      the chunks (heading path, page, text)
    vectors.bin      one float32 vector per chunk, in chunk order

Like the rest of file storage (app/services/storage_service.py) this is one
class to reimplement when the platform moves to Blob Storage and a search
index; callers only see `SourceService`.

Ingestion runs in the background after the upload returns, moving the record
through uploaded -> parsing -> chunking -> embedding -> ready (or failed with a
message the person can act on). Embedding is best-effort: if it is unavailable
the source is still `ready` and retrieval is keyword-only for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from array import array
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.ids import new_id, utc_now_iso
from app.core.logging import get_logger
from app.ingestion.chunker import chunk_document
from app.ingestion.parsers import IngestionError, parse
from app.ingestion.retrieval import Candidate, rank
from app.schemas.source import (
    IN_PROGRESS,
    SourceChunk,
    SourceExcerpt,
    SourceRecord,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service

log = get_logger(__name__)

LOCAL_OWNER = "local"
_SOURCE_ID_RE = re.compile(r"^src_[a-f0-9]{12}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_EXTENSIONS = {"pdf": "pdf", "docx": "docx", "txt": "txt", "md": "md", "markdown": "md"}
# A source still "in progress" with no task running for this long was cut off
# (a restart); it is reported as failed rather than spinning forever.
_STALE_AFTER_SECONDS = 600
_PARSE_TIMEOUT_SECONDS = 180


def owner_key_for(owner_id: str | None) -> str:
    return owner_id or LOCAL_OWNER


def _safe_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f]", "", base)
    return (base or "upload")[:200]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


atomic_write = _atomic_write


class SourceService:
    def __init__(
        self, settings: Settings | None = None, embeddings: EmbeddingService | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.embeddings = embeddings or get_embedding_service()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # --- paths ----------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.settings.data_dir) / "sources"

    def _dir(self, owner_key: str, source_id: str) -> Path:
        if not _OWNER_RE.match(owner_key) or not _SOURCE_ID_RE.match(source_id):
            raise NotFoundError("Source not found")
        return self.root / owner_key / source_id

    # --- records --------------------------------------------------------------
    def _write_record(self, record: SourceRecord) -> None:
        record.updated_at = utc_now_iso()
        path = self._dir(record.owner_key, record.source_id) / "meta.json"
        _atomic_write(path, record.model_dump_json(indent=1).encode("utf-8"))

    def get(self, owner_key: str, source_id: str) -> SourceRecord:
        path = self._dir(owner_key, source_id) / "meta.json"
        if not path.exists():
            raise NotFoundError("Source not found")
        record = SourceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if record.status in IN_PROGRESS and source_id not in self._tasks:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(record.updated_at)).total_seconds()
            if age > _STALE_AFTER_SECONDS:
                record.status = "failed"
                record.error = "Processing was interrupted. Upload the file again."
                record.message = ""
                self._write_record(record)
        return record

    def list_sources(self, owner_key: str) -> list[SourceRecord]:
        folder = self.root / owner_key
        if not _OWNER_RE.match(owner_key) or not folder.is_dir():
            return []
        records: list[SourceRecord] = []
        for child in folder.iterdir():
            if child.is_dir() and _SOURCE_ID_RE.match(child.name):
                try:
                    records.append(self.get(owner_key, child.name))
                except Exception:  # noqa: BLE001 - one unreadable record must not hide the rest
                    log.warning("Unreadable source record %s", child)
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def delete(self, owner_key: str, source_id: str) -> None:
        folder = self._dir(owner_key, source_id)
        if not folder.exists():
            raise NotFoundError("Source not found")
        task = self._tasks.pop(source_id, None)
        if task is not None:
            task.cancel()
        for child in folder.iterdir():
            child.unlink(missing_ok=True)
        folder.rmdir()

    # --- upload ---------------------------------------------------------------
    def _validate_upload(self, filename: str, data: bytes) -> str:
        if not data:
            raise ValidationFailedError("The file is empty.")
        if len(data) > self.settings.max_source_bytes:
            limit = self.settings.max_source_bytes // (1024 * 1024)
            raise ValidationFailedError(f"The file is larger than the {limit} MB limit.")
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        kind = _EXTENSIONS.get(extension)
        if kind is None:
            raise ValidationFailedError("Unsupported file type. Upload a PDF, Word (.docx), text or Markdown file.")
        # The extension is a claim; check the content agrees with it.
        if kind == "pdf" and not data.lstrip()[:5].startswith(b"%PDF"):
            raise ValidationFailedError("The file is not a PDF.")
        if kind == "docx" and not data.startswith(b"PK"):
            raise ValidationFailedError("The file is not a Word (.docx) document.")
        if kind in {"txt", "md"} and b"\x00" in data[:4096]:
            raise ValidationFailedError("The file is binary, not text.")
        return kind

    def upload(
        self, owner_key: str, filename: str, data: bytes, *, extra: dict | None = None
    ) -> SourceRecord:
        """Store the file and start ingesting it in the background.

        `extra` sets further record fields before the first save, so the
        background task never sees (and overwrites) a record without them.
        """
        name = _safe_filename(filename)
        kind = self._validate_upload(name, data)
        digest = hashlib.sha256(data).hexdigest()

        # The same file again is not processed (or paid for) again.
        for existing in self.list_sources(owner_key):
            if existing.sha256 == digest and existing.status != "failed":
                return existing

        source_id = new_id("src")
        now = utc_now_iso()
        record = SourceRecord(
            source_id=source_id,
            owner_key=owner_key,
            filename=name,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=len(data),
            sha256=digest,
            message="Waiting to start",
            created_at=now,
            updated_at=now,
            **(extra or {}),
        )
        folder = self._dir(owner_key, source_id)
        _atomic_write(folder / f"original.{kind}", data)
        self._write_record(record)
        self._tasks[source_id] = asyncio.get_running_loop().create_task(
            self._ingest(owner_key, source_id, kind, data)
        )
        self._tasks[source_id].add_done_callback(lambda _t, sid=source_id: self._tasks.pop(sid, None))
        return record

    # --- ingestion ------------------------------------------------------------
    def _set(self, record: SourceRecord, status: str, message: str = "") -> None:
        record.status = status  # type: ignore[assignment]
        record.message = message
        self._write_record(record)

    async def _ingest(self, owner_key: str, source_id: str, kind: str, data: bytes) -> None:
        record = self.get(owner_key, source_id)
        folder = self._dir(owner_key, source_id)
        try:
            self._set(record, "parsing", "Reading the document")
            try:
                parsed = await asyncio.wait_for(asyncio.to_thread(parse, kind, data), _PARSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise IngestionError("Reading the document took too long.") from exc
            record.page_count = parsed.page_count
            record.word_count = parsed.word_count
            record.outline = parsed.outline()

            self._set(record, "chunking", "Splitting into searchable passages")
            chunks = await asyncio.to_thread(chunk_document, parsed)
            if not chunks:
                raise IngestionError("No usable text was found in the document.")
            record.chunk_count = len(chunks)

            self._set(record, "embedding", f"Indexing {len(chunks)} passages")
            vectors = await self.embeddings.embed_batch([chunk.embed_text() for chunk in chunks])
            dims = self.settings.embedding_dimensions
            packed = array("f")
            for chunk, vector in zip(chunks, vectors):
                if vector is not None and len(vector) == dims:
                    chunk.embedded = True
                    packed.extend(vector)
                else:
                    packed.extend([0.0] * dims)
            record.embedded_chunks = sum(1 for chunk in chunks if chunk.embedded)

            payload = json.dumps([chunk.model_dump() for chunk in chunks], ensure_ascii=False)
            _atomic_write(folder / "chunks.json", payload.encode("utf-8"))
            _atomic_write(folder / "vectors.bin", packed.tobytes())
            self._set(record, "ready", "")
            log.info(
                "Ingested %s: %s chunks (%s embedded)", source_id, record.chunk_count, record.embedded_chunks
            )
        except IngestionError as exc:
            record.error = str(exc)
            self._set(record, "failed", "")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never leave a source spinning
            log.exception("Ingestion failed for %s", source_id)
            record.error = "The document could not be processed."
            self._set(record, "failed", "")

    async def wait(self, source_id: str) -> None:
        """Wait for a source's ingestion to finish (used by tests and scripts)."""
        task = self._tasks.get(source_id)
        if task is not None:
            await asyncio.shield(task)

    # --- reading --------------------------------------------------------------
    def load_chunks(self, owner_key: str, source_id: str) -> list[SourceChunk]:
        path = self._dir(owner_key, source_id) / "chunks.json"
        if not path.exists():
            return []
        return [SourceChunk.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def _load_vectors(self, owner_key: str, source_id: str, count: int) -> list[list[float]] | None:
        path = self._dir(owner_key, source_id) / "vectors.bin"
        dims = self.settings.embedding_dimensions
        if not path.exists():
            return None
        raw = array("f")
        raw.frombytes(path.read_bytes())
        if len(raw) != count * dims:
            return None  # written for a different embedding size; keyword-only
        return [list(raw[i * dims : (i + 1) * dims]) for i in range(count)]

    # --- courses --------------------------------------------------------------
    def validate_for_course(self, owner_key: str, source_ids: list[str]) -> list[SourceRecord]:
        """The sources a new course may use: all exist, are the caller's, and are ready."""
        unique = list(dict.fromkeys(source_ids))
        if len(unique) > self.settings.max_sources_per_course:
            raise ValidationFailedError(
                f"A course can use at most {self.settings.max_sources_per_course} source documents."
            )
        records: list[SourceRecord] = []
        for source_id in unique:
            record = self.get(owner_key, source_id)  # NotFoundError for a missing or foreign id
            if record.status == "failed":
                raise ConflictError(f"'{record.filename}' could not be processed: {record.error}")
            if record.status != "ready":
                raise ConflictError(f"'{record.filename}' is still being processed. Wait until it is ready.")
            records.append(record)
        return records

    def load_library(self, owner_key: str, source_ids: list[str]) -> "SourceLibrary":
        """Every passage of the course's ready sources, in memory, by reference.

        References are stable for a course: `S{n}.{m}` is passage m of the n-th
        source in the course's own list. A source that is gone or not ready
        simply contributes nothing (its references stay reserved, so the other
        sources keep their numbers).
        """
        passages: dict[str, tuple[SourceRecord, SourceChunk]] = {}
        candidates: list[Candidate] = []
        for position, source_id in enumerate(dict.fromkeys(source_ids), start=1):
            try:
                record = self.get(owner_key, source_id)
            except NotFoundError:
                log.warning("Source %s is no longer available", source_id)
                continue
            if record.status != "ready":
                continue
            chunks = self.load_chunks(owner_key, source_id)
            vectors = self._load_vectors(owner_key, source_id, len(chunks))
            for chunk in chunks:
                ref = f"S{position}.{chunk.index + 1}"
                vector = vectors[chunk.index] if vectors is not None and chunk.embedded else None
                candidates.append(Candidate(key=ref, text=chunk.embed_text(), vector=vector))
                passages[ref] = (record, chunk)
        return SourceLibrary(passages=passages, candidates=candidates)

    def course_sources(self, owner_key: str, source_ids: list[str]) -> list[tuple[int, SourceRecord]]:
        """A course's sources with the number its references use (`S{n}`).
        One that is gone is skipped but keeps its number reserved."""
        found: list[tuple[int, SourceRecord]] = []
        for position, source_id in enumerate(dict.fromkeys(source_ids), start=1):
            try:
                found.append((position, self.get(owner_key, source_id)))
            except NotFoundError:
                continue
        return found

    def passages_of(
        self, owner_key: str, source_ids: list[str], number: int, offset: int = 0, limit: int = 50
    ) -> list[SourceExcerpt]:
        """The passages of source `S{number}`, in reading order."""
        library = self.load_library(owner_key, source_ids)
        prefix = f"S{number}."
        refs = [ref for ref in library.passages if ref.startswith(prefix)]
        return [e for e in (library.excerpt(ref) for ref in refs[offset : offset + limit]) if e is not None]

    def passages_by_refs(self, owner_key: str, source_ids: list[str], refs: list[str]) -> list[SourceExcerpt]:
        library = self.load_library(owner_key, source_ids)
        found = (library.excerpt(ref) for ref in dict.fromkeys(refs))
        return [e for e in found if e is not None]

    async def excerpts_for(
        self,
        owner_key: str,
        source_ids: list[str],
        query: str,
        *,
        limit: int | None = None,
        char_budget: int | None = None,
    ) -> list[SourceExcerpt]:
        """The passages of the course's sources that best answer `query`.

        References are stable for a course: `S{n}.{m}` is passage m of the n-th
        source in the course's own list.
        """
        limit = limit or self.settings.source_excerpts_per_chapter
        char_budget = char_budget or self.settings.source_excerpt_chars
        library = self.load_library(owner_key, source_ids)
        if not library.passages:
            return []

        query_vector = await self.embeddings.embed_one(query)
        excerpts: list[SourceExcerpt] = []
        used = 0
        for ref, score in rank(query, library.candidates, query_vector=query_vector, limit=limit):
            record, chunk = library.passages[ref]
            if used + len(chunk.text) > char_budget and excerpts:
                break
            used += len(chunk.text)
            excerpts.append(
                SourceExcerpt(
                    ref=ref,
                    source_id=record.source_id,
                    filename=record.filename,
                    heading=chunk.heading,
                    page=chunk.page,
                    text=chunk.text,
                    score=round(score, 5),
                )
            )
        return excerpts


class SourceLibrary:
    """The passages of one course's sources, held in memory for ranking and lookup."""

    def __init__(
        self,
        passages: dict[str, tuple[SourceRecord, SourceChunk]],
        candidates: list[Candidate],
    ) -> None:
        self.passages = passages
        self.candidates = candidates

    def excerpt(self, ref: str, score: float = 0.0) -> SourceExcerpt | None:
        """The passage behind a reference, or None if there is no such passage."""
        found = self.passages.get(ref)
        if found is None:
            return None
        record, chunk = found
        return SourceExcerpt(
            ref=ref,
            source_id=record.source_id,
            filename=record.filename,
            heading=chunk.heading,
            page=chunk.page,
            text=chunk.text,
            score=score,
        )


_service: SourceService | None = None


def get_source_service() -> SourceService:
    global _service
    if _service is None:
        _service = SourceService()
    return _service


def reset_source_service() -> None:
    global _service
    _service = None
