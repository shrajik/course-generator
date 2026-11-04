"""The knowledge library read from PostgreSQL (migration 20261103_0012).

Satisfies `KnowledgeBackend` (app/services/knowledge_backend.py) over the `kb_*`
tables. This is the read side only: it searches and resolves what is already there.
Writing (upload, publish, edit) arrives with the ingestion work, and until then the
file library stays the source of truth - this backend runs in *shadow* mode beside it
(app/services/knowledge_shadow.py) so its answers can be compared before anything
depends on them.

How a search works, and why it matches the file library:

* **Only live passages are ever considered.** `is_live` is true exactly for the
  passages of the live version of a published item, and the vector and keyword indexes
  are partial on it. A draft, unpublished, archived or superseded passage is not merely
  filtered out - it is not in the index, so it cannot crowd out a real result.
* **Semantic**: the nearest `pool` live passages by cosine distance (HNSW), kept if
  similarity reaches `knowledge_min_semantic`.
* **Keyword**: the database supplies the corpus statistics BM25 needs and a bounded set
  of passages sharing at least the required number of query terms; the scoring is
  `kb_ranking`, which equals the file index's formulas.
* The two rankings are fused exactly as before, and the same excerpt budget applies.

A citation `G{number}.{ordinal}` resolves to its passage whether or not that passage is
still live, because chunks are immutable and ordinals are never reused: an old citation
reaches the text it originally pointed at (until the item is purged).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db import kb_models as kb
from app.db.pgvector_check import use_iterative_scan
from app.db.session import get_session_factory
from app.ingestion.retrieval import tokenize
from app.schemas.source import SourceChunk, SourceExcerpt, SourceRecord
from app.services import kb_ranking
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.knowledge_service import GLOBAL_OWNER
from app.services.source_service import SourceLibrary

log = get_logger(__name__)

_REF_RE = re.compile(r"^G(\d+)\.(\d+)$")
# Passages sharing enough query terms that are ranked by BM25. The ones cut are those
# sharing the fewest terms, which could not have ranked among the few returned.
_KEYWORD_POOL = 500
# A passage squeezed into what is left of the budget must still be worth reading.
_MIN_COMPRESSED_CHARS = 200

_PASSAGE_COLUMNS = """
    c.id AS chunk_id, c.ordinal, c.heading, c.heading_path, c.page, c.text, c.words,
    i.number, i.public_id, i.title, i.live_version_id, s.filename, s.kind, s.size_bytes, s.sha256
"""
_PASSAGE_JOINS = """
    JOIN kb_items i ON i.id = c.item_id
    LEFT JOIN kb_sources s ON s.id = c.source_id
"""


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _ref(number: int, ordinal: int) -> str:
    return f"G{number}.{ordinal}"


@dataclass
class _Passage:
    chunk_id: object
    ref: str
    number: int
    ordinal: int
    public_id: str
    filename: str
    heading: str
    heading_path: list[str]
    page: int | None
    text: str
    words: int
    version_id: object
    kind: str
    size_bytes: int
    sha256: str

    @classmethod
    def from_row(cls, row) -> "_Passage":
        return cls(
            chunk_id=row.chunk_id,
            ref=_ref(row.number, row.ordinal),
            number=row.number,
            ordinal=row.ordinal,
            public_id=row.public_id,
            # The file the passage came from; for knowledge typed in, the item's title.
            filename=row.filename or row.title,
            heading=row.heading,
            heading_path=list(row.heading_path or []),
            page=row.page,
            text=row.text,
            words=row.words,
            version_id=row.live_version_id,
            kind=row.kind or "txt",
            size_bytes=row.size_bytes or 0,
            sha256=row.sha256 or "",
        )

    def excerpt(self, score: float = 0.0) -> SourceExcerpt:
        return SourceExcerpt(
            ref=self.ref,
            source_id=self.public_id,
            filename=self.filename,
            heading=self.heading,
            page=self.page,
            text=self.text,
            score=score,
        )

    def embed_text(self) -> str:
        return f"{self.heading}\n{self.text}" if self.heading else self.text


@dataclass(frozen=True)
class SearchScope:
    """Optional narrowing of a search: only items in a category (and everything beneath it), carrying
    all of the tags, and/or whose passages are labelled with one of the topics."""

    category_id: uuid.UUID | None = None
    tags: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self.category_id is None and not self.tags and not self.topics


_SUPERSEDED = (
    "NOT EXISTS (SELECT 1 FROM kb_relations r JOIN kb_items f ON f.id = r.from_item_id "
    "WHERE r.to_item_id = {alias}.item_id AND r.relation_type = 'supersedes' "
    "AND f.lifecycle_status = 'published' AND f.deleted_at IS NULL AND f.purged_at IS NULL)"
)


class PostgresKnowledgeBackend:
    def __init__(
        self,
        settings: Settings | None = None,
        embeddings: EmbeddingService | None = None,
        session_factory: Callable[[], async_sessionmaker[AsyncSession]] | None = None,
        *,
        log_retrievals: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.embeddings = embeddings or get_embedding_service()
        self._factory = session_factory or get_session_factory
        # Off for a shadow run: what it returns is never offered to a course.
        self.log_retrievals = log_retrievals
        self._stats_cache: tuple[int, int, float] | None = None  # (epoch, passages, average length)
        self._results: OrderedDict[tuple, tuple[int, list[SourceExcerpt], list[_Passage]]] = OrderedDict()

    # --- corpus state -----------------------------------------------------------------
    @staticmethod
    async def _epoch(session: AsyncSession) -> int:
        """Moves whenever anything retrievable changes; what cached answers are checked against."""
        return int((await session.execute(text("SELECT epoch FROM kb_epoch WHERE id = 1"))).scalar() or 0)

    async def _stats(self, session: AsyncSession, epoch: int) -> tuple[int, float]:
        cached = self._stats_cache
        if cached is not None and cached[0] == epoch:
            return cached[1], cached[2]
        row = (
            await session.execute(text("SELECT count(*) AS n, coalesce(avg(token_count), 0) AS average FROM kb_chunks WHERE is_live"))
        ).one()
        self._stats_cache = (epoch, int(row.n), float(row.average))
        return int(row.n), float(row.average)

    # --- the contract -----------------------------------------------------------
    async def ahas_documents(self) -> bool:
        if not self.settings.enable_global_knowledge:
            return False
        async with self._factory()() as session:
            return bool((await session.execute(text("SELECT EXISTS (SELECT 1 FROM kb_chunks WHERE is_live)"))).scalar())

    async def asearch(
        self,
        query: str,
        *,
        limit: int | None = None,
        char_budget: int | None = None,
        trace: tuple[str, str] | None = None,
        scope: SearchScope | None = None,
    ) -> list[SourceExcerpt]:
        if not self.settings.enable_global_knowledge or not query.strip():
            return []
        limit = limit or self.settings.knowledge_excerpts_per_chapter
        char_budget = char_budget or self.settings.knowledge_excerpt_chars
        scope = scope or SearchScope()
        key = (" ".join(query.lower().split()), limit, char_budget, scope)

        async with self._factory()() as session:
            cached = self._cache_get(key, await self._epoch(session))
        if cached is not None:
            excerpts, chosen = cached
        else:
            excerpts, chosen, epoch = await self._search(query, limit, char_budget, scope)
            self._cache_put(key, epoch, excerpts, chosen)
        if trace is not None and chosen and self.log_retrievals:
            await self._log_retrieval(query, chosen, trace)
        return excerpts

    # --- the search itself ------------------------------------------------------
    async def _search(self, query: str, limit: int, char_budget: int, scope: SearchScope) -> tuple[list[SourceExcerpt], list[_Passage], int]:
        vector = await self.embeddings.embed_one(query)
        async with self._factory()() as session:
            epoch = await self._epoch(session)
            total, average = await self._stats(session, epoch)
            if total == 0:
                return [], [], epoch
            allowed = await self._allowed_items(session, scope)
            if allowed is not None and not allowed:
                return [], [], epoch
            narrowing = self._narrowing(allowed, scope.topics)
            semantic = await self._semantic(session, vector, limit, narrowing, filtered=not scope.empty)
            keyword, keyword_passages = await self._keyword(session, query, total, average, narrowing)
            take = limit * 2 if self.settings.knowledge_dedupe else limit
            ranked = kb_ranking.fuse(keyword, [p.ref for p in semantic], limit=take)
            wanted = {ref for ref, _ in ranked}
            passages = {p.ref: p for p in [*semantic, *keyword_passages] if p.ref in wanted}
            if self.settings.knowledge_graph_rules:
                ranked = await self._collapse_duplicates(session, ranked, passages)
            if self.settings.knowledge_dedupe:
                keep = set(kb_ranking.near_duplicates_removed([(ref, passages[ref].text) for ref, _ in ranked if ref in passages]))
                ranked = [pair for pair in ranked if pair[0] in keep]
            ranked = ranked[:limit]
            if self.settings.knowledge_related_expansion and ranked and vector is not None:
                ranked = await self._expand_related(session, ranked, passages, vector, allowed)
        excerpts, chosen = self._within_budget(ranked, passages, char_budget, kb_ranking.query_terms(query))
        return excerpts, chosen, epoch

    async def _allowed_items(self, session: AsyncSession, scope: SearchScope) -> list[uuid.UUID] | None:
        """The published items inside the scope's category and carrying all its tags; None = no narrowing."""
        if scope.category_id is None and not scope.tags:
            return None
        where, params = ["i.lifecycle_status = 'published'", "i.deleted_at IS NULL", "i.purged_at IS NULL"], {}
        if scope.category_id is not None:
            where.append(
                "i.category_id IN (WITH RECURSIVE tree AS (SELECT id FROM kb_categories WHERE id = :category "
                "UNION SELECT c2.id FROM kb_categories c2 JOIN tree t ON c2.parent_id = t.id) SELECT id FROM tree)"
            )
            params["category"] = scope.category_id
        if scope.tags:
            where.append(
                "(SELECT count(DISTINCT t.name_key) FROM kb_item_tags it JOIN kb_tags t ON t.id = it.tag_id "
                "WHERE it.item_id = i.id AND t.name_key = ANY(CAST(:tags AS text[]))) = :tag_count"
            )
            wanted = sorted({t.strip().lower() for t in scope.tags})
            params.update(tags=wanted, tag_count=len(wanted))
        rows = await session.execute(text(f"SELECT i.id FROM kb_items i WHERE {' AND '.join(where)}"), params)
        return [row.id for row in rows]

    def _narrowing(self, allowed: list[uuid.UUID] | None, topics: tuple[str, ...]) -> tuple[str, dict]:
        """SQL conditions (and their parameters) every candidate passage must satisfy beyond being live.
        `{alias}` is filled in with the query's own name for the passage table."""
        parts: list[str] = []
        params: dict = {}
        if self.settings.knowledge_graph_rules:
            parts.append(_SUPERSEDED)
        if allowed is not None:
            parts.append("{alias}.item_id = ANY(CAST(:allowed AS uuid[]))")
            params["allowed"] = allowed
        if topics:
            parts.append("{alias}.topics && CAST(:topics AS text[])")
            params["topics"] = list(topics)
        return "".join(f" AND {part}" for part in parts), params

    async def _semantic(
        self, session: AsyncSession, vector: list[float] | None, limit: int, narrowing: tuple[str, dict], *, filtered: bool
    ) -> list[_Passage]:
        if vector is None or len(vector) != self.settings.embedding_dimensions:
            return []
        if filtered:
            await use_iterative_scan(session)
        conditions, extra = narrowing
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH near AS (
                        SELECT k.id, 1 - (k.embedding <=> CAST(:q AS vector)) AS similarity
                        FROM kb_chunks k
                        WHERE k.is_live AND k.embedding IS NOT NULL {conditions.replace("{alias}", "k")}
                        ORDER BY k.embedding <=> CAST(:q AS vector)
                        LIMIT :pool
                    )
                    SELECT {_PASSAGE_COLUMNS}, near.similarity
                    FROM near JOIN kb_chunks c ON c.id = near.id {_PASSAGE_JOINS}
                    WHERE near.similarity >= :minimum
                    ORDER BY near.similarity DESC
                    """
                ),
                {"q": _vector_literal(vector), "pool": max(limit * 4, 20), "minimum": self.settings.knowledge_min_semantic, **extra},
            )
        ).all()
        return [_Passage.from_row(row) for row in rows]

    async def _keyword(
        self, session: AsyncSession, query: str, total: int, average: float, narrowing: tuple[str, dict]
    ) -> tuple[dict[str, float], list[_Passage]]:
        terms = kb_ranking.query_terms(query)
        if not terms:
            return {}, []
        # Corpus-wide statistics, whatever the scope: BM25's idea of how rare a word is does not
        # change because the search was narrowed.
        frequency_rows = (
            await session.execute(
                text(
                    """
                    SELECT t.term, count(c.id) AS frequency
                    FROM unnest(CAST(:terms AS text[])) AS t(term)
                    LEFT JOIN kb_chunks c ON c.is_live AND c.tokens @> ARRAY[t.term]
                    GROUP BY t.term
                    """
                ),
                {"terms": terms},
            )
        ).all()
        frequency = {row.term: int(row.frequency) for row in frequency_rows}
        if not any(frequency.values()):
            return {}, []
        conditions, extra = narrowing
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_PASSAGE_COLUMNS}
                    FROM kb_chunks c {_PASSAGE_JOINS}
                    JOIN LATERAL (
                        SELECT count(*) AS shared
                        FROM unnest(CAST(:terms AS text[])) AS t(term)
                        WHERE c.tokens @> ARRAY[t.term]
                    ) m ON true
                    WHERE c.is_live AND c.tokens && CAST(:terms AS text[]) AND m.shared >= :needed {conditions.replace("{alias}", "c")}
                    ORDER BY m.shared DESC
                    LIMIT :pool
                    """
                ),
                {"terms": terms, "needed": kb_ranking.needed_terms(terms, 2), "pool": _KEYWORD_POOL, **extra},
            )
        ).all()
        passages = [_Passage.from_row(row) for row in rows]
        scores = kb_ranking.keyword_scores(
            terms,
            [kb_ranking.KeywordCandidate(p.ref, tokenize(p.embed_text())) for p in passages],
            total=total,
            document_frequency=frequency,
            average_length=average,
            min_coverage=self.settings.knowledge_min_coverage,
            min_terms=2,
        )
        return scores, passages

    # --- relationships ----------------------------------------------------------
    @staticmethod
    async def _collapse_duplicates(session: AsyncSession, ranked: list[tuple[str, float]], passages: dict[str, _Passage]) -> list[tuple[str, float]]:
        """Of two items where one is marked `duplicate_of` the other, offer only the original."""
        numbers = sorted({passages[ref].number for ref, _ in ranked if ref in passages})
        if len(numbers) < 2:
            return ranked
        rows = await session.execute(
            text(
                "SELECT f.number AS duplicate FROM kb_relations r JOIN kb_items f ON f.id = r.from_item_id JOIN kb_items t ON t.id = r.to_item_id "
                "WHERE r.relation_type = 'duplicate_of' AND f.number = ANY(CAST(:n AS int[])) AND t.number = ANY(CAST(:n AS int[]))"
            ),
            {"n": numbers},
        )
        duplicates = {row.duplicate for row in rows}
        return [pair for pair in ranked if pair[0] not in passages or passages[pair[0]].number not in duplicates]

    async def _expand_related(
        self,
        session: AsyncSession,
        ranked: list[tuple[str, float]],
        passages: dict[str, _Passage],
        vector: list[float],
        allowed: list[uuid.UUID] | None,
    ) -> list[tuple[str, float]]:
        """After the ranked passages, add some from items directly related to the best match - only
        those themselves relevant to the query (the same similarity floor as any semantic hit)."""
        best = passages.get(ranked[0][0])
        if best is None:
            return ranked
        narrow = "AND c.item_id = ANY(CAST(:allowed AS uuid[]))" if allowed is not None else ""
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH related AS (
                        SELECT o.id FROM kb_items i
                        JOIN kb_relations r ON r.from_item_id = i.id OR (r.to_item_id = i.id AND r.relation_type = 'related_to')
                        JOIN kb_items o ON o.id = CASE WHEN r.from_item_id = i.id THEN r.to_item_id ELSE r.from_item_id END
                        WHERE i.number = :n AND r.relation_type IN ('related_to', 'references', 'depends_on')
                          AND o.lifecycle_status = 'published' AND o.deleted_at IS NULL AND o.purged_at IS NULL
                    )
                    SELECT {_PASSAGE_COLUMNS}, 1 - (c.embedding <=> CAST(:q AS vector)) AS similarity
                    FROM kb_chunks c {_PASSAGE_JOINS}
                    WHERE c.is_live AND c.embedding IS NOT NULL AND c.item_id IN (SELECT id FROM related) {narrow}
                    ORDER BY c.embedding <=> CAST(:q AS vector) LIMIT :extra
                    """
                ),
                {"n": best.number, "q": _vector_literal(vector), "extra": self.settings.knowledge_related_expansion + len(ranked), **({"allowed": allowed} if allowed is not None else {})},
            )
        ).all()
        present = {ref for ref, _ in ranked}
        floor = min(score for _, score in ranked) / 2  # always below every ranked passage
        added = 0
        for row in rows:
            passage = _Passage.from_row(row)
            if row.similarity < self.settings.knowledge_min_semantic or passage.ref in present:
                continue
            if added >= self.settings.knowledge_related_expansion:
                break
            passages[passage.ref] = passage
            ranked = [*ranked, (passage.ref, floor)]
            added += 1
        return ranked

    # --- the answer ---------------------------------------------------------------
    def _within_budget(
        self, ranked: list[tuple[str, float]], passages: dict[str, _Passage], char_budget: int, terms: list[str]
    ) -> tuple[list[SourceExcerpt], list[_Passage]]:
        excerpts: list[SourceExcerpt] = []
        chosen: list[_Passage] = []
        used = 0
        for ref, score in ranked:
            passage = passages.get(ref)
            if passage is None:
                continue
            excerpt = passage.excerpt(round(score, 5))
            if used + len(excerpt.text) > char_budget and excerpts:
                remaining = char_budget - used
                if not self.settings.knowledge_compress or remaining < _MIN_COMPRESSED_CHARS:
                    break
                excerpt = excerpt.model_copy(update={"text": kb_ranking.compress(excerpt.text, terms, remaining)})
            used += len(excerpt.text)
            excerpts.append(excerpt)
            chosen.append(passage)
        return excerpts, chosen

    # --- the result cache ----------------------------------------------------------
    def _cache_get(self, key: tuple, epoch: int) -> tuple[list[SourceExcerpt], list[_Passage]] | None:
        entry = self._results.get(key)
        if entry is None or entry[0] != epoch:
            return None
        self._results.move_to_end(key)
        return [e.model_copy() for e in entry[1]], entry[2]

    def _cache_put(self, key: tuple, epoch: int, excerpts: list[SourceExcerpt], chosen: list[_Passage]) -> None:
        if self.settings.knowledge_cache_size <= 0:
            return
        self._results[key] = (epoch, [e.model_copy() for e in excerpts], chosen)
        while len(self._results) > self.settings.knowledge_cache_size:
            self._results.popitem(last=False)

    async def alibrary(self, extra_refs: list[str] | None = None) -> SourceLibrary:
        """Every live passage's text, plus the passages behind `extra_refs` even when they
        are no longer live, so an old citation can be checked against what it cited.
        Text only: no vectors are read."""
        async with self._factory()() as session:
            rows = (
                await session.execute(
                    text(f"SELECT {_PASSAGE_COLUMNS} FROM kb_chunks c {_PASSAGE_JOINS} WHERE c.is_live ORDER BY i.number, c.ordinal")
                )
            ).all()
            passages = [_Passage.from_row(row) for row in rows]
            have = {p.ref for p in passages}
            missing = [ref for ref in dict.fromkeys(extra_refs or []) if ref not in have]
            if missing:
                passages.extend(await self._by_refs(session, missing))
        return _library_of(passages)

    async def apassages_by_refs(self, refs: list[str], *, live_only: bool = False) -> list[SourceExcerpt]:
        async with self._factory()() as session:
            found = {p.ref: p for p in await self._by_refs(session, list(dict.fromkeys(refs)), live_only=live_only)}
        return [found[ref].excerpt() for ref in dict.fromkeys(refs) if ref in found]

    async def _by_refs(self, session: AsyncSession, refs: list[str], *, live_only: bool = False) -> list[_Passage]:
        pairs = [(int(m.group(1)), int(m.group(2))) for ref in refs if (m := _REF_RE.match(ref))]
        if not pairs:
            return []
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_PASSAGE_COLUMNS}
                    FROM unnest(CAST(:numbers AS int[]), CAST(:ordinals AS int[])) AS w(number, ordinal)
                    JOIN kb_items i ON i.number = w.number
                    JOIN kb_chunks c ON c.item_id = i.id AND c.ordinal = w.ordinal
                    LEFT JOIN kb_sources s ON s.id = c.source_id
                    WHERE i.purged_at IS NULL {"AND c.is_live" if live_only else ""}
                    """
                ),
                {"numbers": [n for n, _ in pairs], "ordinals": [o for _, o in pairs]},
            )
        ).all()
        return [_Passage.from_row(row) for row in rows]

    # --- the retrieval log ------------------------------------------------------
    async def _log_retrieval(self, query: str, chosen: list[_Passage], trace: tuple[str, str]) -> None:
        """Best effort: a failure here must never fail the search that produced the result."""
        course_id, chapter_id = trace
        digest = hashlib.sha256(" ".join(query.lower().split()).encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        try:
            async with self._factory()() as session, session.begin():
                await session.execute(
                    insert(kb.KbRetrievalLog),
                    [
                        {
                            "retrieved_at": now,
                            "item_number": p.number,
                            "ordinal": p.ordinal,
                            "chunk_id": p.chunk_id,
                            "version_id": p.version_id,
                            "course_id": course_id[:64],
                            "chapter_id": chapter_id[:64],
                            "query_hash": digest,
                        }
                        for p in chosen
                    ],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record the knowledge retrieval for %s/%s: %s", course_id, chapter_id, exc)


def _library_of(passages: list[_Passage]) -> SourceLibrary:
    """A `SourceLibrary` (what the fact-check audit reads) from database passages."""
    now = datetime.now(timezone.utc).isoformat()
    records: dict[str, SourceRecord] = {}
    held: dict[str, tuple[SourceRecord, SourceChunk]] = {}
    for p in passages:
        record = records.get(p.public_id)
        if record is None:
            record = records[p.public_id] = SourceRecord(
                source_id=p.public_id,
                owner_key=GLOBAL_OWNER,
                filename=p.filename,
                kind=p.kind if p.kind in {"pdf", "docx", "txt", "md"} else "txt",  # type: ignore[arg-type]
                size_bytes=p.size_bytes,
                sha256=p.sha256,
                status="ready",
                global_number=p.number,
                created_at=now,
                updated_at=now,
            )
        held[p.ref] = (
            record,
            SourceChunk(index=p.ordinal - 1, heading_path=p.heading_path, page=p.page, text=p.text, words=p.words),
        )
    # No candidates: the audit reads passage text only, and vectors are never loaded.
    return SourceLibrary(passages=held, candidates=[])
