"""The global knowledge library: documents every course can draw on.

Admins upload reference documents (policies, manuals, standards, house
knowledge) once. From then on every course - whoever writes it, whatever its
subject - searches them while its chapters are written, and uses what matches.
It is the platform's shared memory, as opposed to a course's own source
documents (app/services/source_service.py), which only that course sees.

Storage and ingestion are the source pipeline's: the library is simply the
`global` owner of that store, so uploading, reading, splitting into passages
and embedding are one implementation. What this module adds is what makes it
global:

* a permanent number per document, so a citation like `[G2.5]` (document 2,
  passage 5) means the same passage in every course, even after other
  documents are added or removed - numbers are never reused;
* an on/off switch per document, so material can be withdrawn from search
  without being deleted;
* one cached search index over all enabled documents, rebuilt when any of them
  changes, because every chapter of every course queries it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.ingestion.index import IndexedPassage, SearchIndex
from app.schemas.source import SourceChunk, SourceExcerpt, SourceRecord
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.source_service import (
    Candidate,
    SourceLibrary,
    SourceService,
    atomic_write,
    get_source_service,
)

log = get_logger(__name__)

GLOBAL_OWNER = "global"
# How long a built index is trusted before the document list is checked again.
_FINGERPRINT_TTL_SECONDS = 2.0
_counter_lock = threading.Lock()


@dataclass
class _Built:
    fingerprint: tuple
    checked_at: float
    index: SearchIndex
    library: SourceLibrary


class KnowledgeService:
    def __init__(
        self,
        settings: Settings | None = None,
        sources: SourceService | None = None,
        embeddings: EmbeddingService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sources = sources or get_source_service()
        self.embeddings = embeddings or get_embedding_service()
        self._built: _Built | None = None

    # --- documents ------------------------------------------------------------
    @property
    def _counter_path(self) -> Path:
        return self.sources.root / GLOBAL_OWNER / "_counter.json"

    def _next_number(self) -> int:
        """The next document number. Persisted, so a deleted document's number
        is never handed to a new one."""
        with _counter_lock:
            used = max((r.global_number or 0 for r in self.list_documents()), default=0)
            try:
                stored = int(json.loads(self._counter_path.read_text(encoding="utf-8"))["last"])
            except (OSError, ValueError, KeyError):
                stored = 0
            number = max(used, stored) + 1
            atomic_write(self._counter_path, json.dumps({"last": number}).encode("utf-8"))
            return number

    def upload(self, filename: str, data: bytes, *, description: str = "") -> SourceRecord:
        """Add a document to the library and start processing it."""
        digest = hashlib.sha256(data).hexdigest()
        # The same file again is not added twice, and keeps its number.
        for existing in self.list_documents():
            if existing.sha256 == digest and existing.status != "failed":
                return existing
        # The number is fixed before processing starts: the background task
        # keeps its own copy of the record and rewrites it as it goes.
        record = self.sources.upload(
            GLOBAL_OWNER,
            filename,
            data,
            extra={
                "global_number": self._next_number(),
                "description": " ".join(description.split())[:500],
            },
        )
        self._built = None
        return record

    def list_documents(self) -> list[SourceRecord]:
        return sorted(
            self.sources.list_sources(GLOBAL_OWNER), key=lambda r: r.global_number or 10**9
        )

    def get(self, source_id: str) -> SourceRecord:
        return self.sources.get(GLOBAL_OWNER, source_id)

    def update(
        self, source_id: str, *, enabled: bool | None = None, description: str | None = None
    ) -> SourceRecord:
        record = self.get(source_id)
        if record.status not in {"ready", "failed"}:
            raise ConflictError("The document is still being processed. Try again when it is ready.")
        if enabled is not None:
            record.enabled = enabled
        if description is not None:
            record.description = " ".join(description.split())[:500]
        self.sources._write_record(record)  # noqa: SLF001
        self._built = None
        return record

    def delete(self, source_id: str) -> None:
        self.sources.delete(GLOBAL_OWNER, source_id)
        self._built = None

    def chunks(self, source_id: str, offset: int = 0, limit: int = 50) -> list[SourceChunk]:
        self.get(source_id)  # NotFoundError unless it exists
        return self.sources.load_chunks(GLOBAL_OWNER, source_id)[offset : offset + limit]

    def stats(self) -> dict[str, int]:
        documents = self.list_documents()
        searchable = [r for r in documents if r.status == "ready" and r.enabled]
        return {
            "documents": len(documents),
            "searchable": len(searchable),
            "passages": sum(r.chunk_count for r in searchable),
            "processing": sum(1 for r in documents if r.status not in {"ready", "failed"}),
        }

    # --- the cached index -----------------------------------------------------
    def _fingerprint(self) -> tuple:
        return tuple(
            (r.source_id, r.updated_at, r.enabled, r.status, r.global_number)
            for r in self.list_documents()
            if r.status == "ready"
        )

    def _build(self, fingerprint: tuple) -> _Built:
        passages: dict[str, tuple[SourceRecord, SourceChunk]] = {}
        candidates: list[Candidate] = []
        indexed: list[IndexedPassage] = []
        dims = self.settings.embedding_dimensions
        for record in self.list_documents():
            if record.status != "ready" or not record.enabled or record.global_number is None:
                continue
            chunks = self.sources.load_chunks(GLOBAL_OWNER, record.source_id)
            vectors = self.sources._load_vectors(GLOBAL_OWNER, record.source_id, len(chunks))  # noqa: SLF001
            for chunk in chunks:
                ref = f"G{record.global_number}.{chunk.index + 1}"
                vector = vectors[chunk.index] if vectors is not None and chunk.embedded else None
                passages[ref] = (record, chunk)
                candidates.append(Candidate(key=ref, text=chunk.embed_text(), vector=vector))
                indexed.append(IndexedPassage(ref, chunk.embed_text(), vector))
        log.info("Knowledge index built: %s passages from %s documents", len(indexed), len({r.source_id for r, _ in passages.values()}))
        return _Built(fingerprint, time.monotonic(), SearchIndex(indexed, dims), SourceLibrary(passages, candidates))

    def _current(self) -> _Built:
        now = time.monotonic()
        built = self._built
        if built is not None and now - built.checked_at < _FINGERPRINT_TTL_SECONDS:
            return built
        fingerprint = self._fingerprint()
        if built is not None and built.fingerprint == fingerprint:
            built.checked_at = now
            return built
        self._built = self._build(fingerprint)
        return self._built

    def library(self) -> SourceLibrary:
        """Every passage of every searchable document, by `G` reference."""
        return self._current().library

    def has_documents(self) -> bool:
        return bool(self.settings.enable_global_knowledge and len(self._current().index))

    # --- searching ------------------------------------------------------------
    async def search(
        self, query: str, *, limit: int | None = None, char_budget: int | None = None
    ) -> list[SourceExcerpt]:
        """The passages of the library that best answer `query`; [] when nothing
        is relevant enough, which is the usual case for most courses' chapters."""
        if not self.settings.enable_global_knowledge or not query.strip():
            return []
        built = self._current()
        if not len(built.index):
            return []
        limit = limit or self.settings.knowledge_excerpts_per_chapter
        char_budget = char_budget or self.settings.knowledge_excerpt_chars
        vector = await self.embeddings.embed_one(query)
        excerpts: list[SourceExcerpt] = []
        used = 0
        for ref, score in built.index.search(
            query,
            query_vector=vector,
            limit=limit,
            min_semantic=self.settings.knowledge_min_semantic,
            min_coverage=self.settings.knowledge_min_coverage,
        ):
            excerpt = built.library.excerpt(ref, round(score, 5))
            if excerpt is None:
                continue
            if used + len(excerpt.text) > char_budget and excerpts:
                break
            used += len(excerpt.text)
            excerpts.append(excerpt)
        return excerpts

    def passages_by_refs(self, refs: list[str]) -> list[SourceExcerpt]:
        library = self.library()
        found = (library.excerpt(ref) for ref in dict.fromkeys(refs))
        return [e for e in found if e is not None]


_service: KnowledgeService | None = None


def get_knowledge_service() -> KnowledgeService:
    global _service
    if _service is None:
        _service = KnowledgeService()
    return _service


def reset_knowledge_service() -> None:
    global _service
    _service = None


__all__ = ["GLOBAL_OWNER", "KnowledgeService", "get_knowledge_service", "reset_knowledge_service", "NotFoundError"]
