"""Combine existing keyword (ILIKE) results with semantic (pgvector) results
into one ranked, deduplicated list, per the architecture:

    MemoryService -> HybridRetriever -> {keyword .list(), SemanticRetriever}
                                              SemanticRetriever -> EmbeddingService -> pgvector

`HybridRetriever` doesn't know about templates/samples/visuals/runs
specifically - callers pass in the keyword candidates they already fetch
(unchanged) plus a small lookup function for turning a semantic hit's
source_id into the same kind of item. This is the one place scoring happens,
so the four call sites in MemoryService don't each reimplement it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.session import get_session_factory
from app.services.embedding_service import EmbeddingService, get_embedding_service

log = get_logger(__name__)

T = TypeVar("T")

# Candidate pool fetched before ranking/limiting - keeps a merely-decent
# keyword hit from silently crowding out a better semantic one (or vice
# versa) just because it happened to be fetched first and filled the quota.
OVERSAMPLE = 3


@dataclass
class _Candidate(Generic[T]):
    item: T
    keyword_hit: bool
    similarity: float | None = None


class HybridRetriever:
    def __init__(
        self,
        embeddings: EmbeddingService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.embeddings = embeddings or get_embedding_service()
        self.settings = settings or get_settings()
        # Per-instance (per-request/per-stage, see MemoryService) cache so a
        # planner+writer call in the same pipeline run never embeds the same
        # query text twice.
        self._query_cache: dict[str, list[float] | None] = {}

    async def _embed_query(self, query: str) -> list[float] | None:
        key = (query or "").strip().lower()
        if not key:
            return None
        if key not in self._query_cache:
            self._query_cache[key] = await self.embeddings.embed_one(query)
        return self._query_cache[key]

    async def rank(
        self,
        *,
        source_type: str,
        query: str,
        limit: int,
        keyword_items: Sequence[T],
        id_of: Callable[[T], uuid.UUID],
        semantic_lookup: Callable[[list[uuid.UUID]], Awaitable[dict[uuid.UUID, T]]],
    ) -> list[T]:
        """Rank keyword + semantic candidates for one source type and return
        at most `limit` items, highest score first.

        Score = keyword_weight * (1 if it was an ILIKE hit) +
                semantic_weight * cosine_similarity (only when the item also
                has a semantic hit above the configured threshold). An item
                found by only one method still competes on that method's
                term alone - neither method can fully hide the other's
                results, since both surface here as bare weighted scores in
                one merged dict, not as two separately-truncated lists.
        """
        candidates: dict[str, _Candidate[T]] = {}
        for item in keyword_items:
            candidates[str(id_of(item))] = _Candidate(item=item, keyword_hit=True)

        vector = None
        try:
            vector = await self._embed_query(query)
        except Exception as exc:  # noqa: BLE001 - a query-embedding failure must not cost the keyword results above
            log.warning("Query embedding failed for %s: %s", source_type, exc)

        if vector is not None:
            try:
                async with get_session_factory()() as session:
                    hits = await MemoryEmbeddingRepository(session).search(
                        source_type=source_type,
                        query_vector=vector,
                        limit=max(limit, 1) * OVERSAMPLE,
                        threshold=self.settings.semantic_similarity_threshold,
                    )
            except Exception as exc:  # noqa: BLE001 - semantic search is never load-bearing
                log.warning("Semantic search failed for %s: %s", source_type, exc)
                hits = []

            missing_ids = [row.source_id for row, _ in hits if str(row.source_id) not in candidates]
            fetched: dict[uuid.UUID, T] = {}
            if missing_ids:
                try:
                    fetched = await semantic_lookup(missing_ids)
                except Exception as exc:  # noqa: BLE001 - same reasoning as above
                    log.warning("Semantic result lookup failed for %s: %s", source_type, exc)
            for row, similarity in hits:
                key = str(row.source_id)
                if key in candidates:
                    candidates[key].similarity = similarity
                elif row.source_id in fetched:
                    candidates[key] = _Candidate(
                        item=fetched[row.source_id], keyword_hit=False, similarity=similarity
                    )

        def score(candidate: _Candidate[T]) -> float:
            value = 0.0
            if candidate.keyword_hit:
                value += self.settings.keyword_score_weight
            if candidate.similarity is not None:
                value += self.settings.semantic_score_weight * candidate.similarity
            return value

        ranked = sorted(candidates.values(), key=score, reverse=True)
        return [c.item for c in ranked[:limit]]
