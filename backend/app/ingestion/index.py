"""A reusable search index over many passages: keyword (BM25) plus semantic.

`app.ingestion.retrieval.rank` re-tokenises every passage on every query, which
is right for one course's few sources but not for a global library that every
chapter of every course searches. This builds the index once - term postings
for keyword search and a normalised vector matrix for semantic search - and
answers each query without touching passages that cannot match.

The two rankings are merged by reciprocal-rank fusion, as in `rank`, and the
semantic side only contributes passages that are genuinely similar
(`min_semantic`): a library holds documents about many subjects, and a course
on one of them must not be handed passages from another just because they were
the least dissimilar.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from app.ingestion.retrieval import B, K1, RRF_K, tokenize


@dataclass(frozen=True)
class IndexedPassage:
    ref: str
    text: str
    vector: Sequence[float] | None = None


class SearchIndex:
    def __init__(self, passages: Sequence[IndexedPassage], dimensions: int) -> None:
        self.refs = [p.ref for p in passages]
        self.dimensions = dimensions
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._lengths: list[int] = []
        for position, passage in enumerate(passages):
            tokens = tokenize(passage.text)
            self._lengths.append(len(tokens))
            for token, count in Counter(tokens).items():
                self._postings[token].append((position, count))
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0

        # One row per passage; rows without a vector stay zero and never match.
        self._matrix = np.zeros((len(passages), dimensions), dtype=np.float32)
        for position, passage in enumerate(passages):
            if passage.vector is not None and len(passage.vector) == dimensions:
                row = np.asarray(passage.vector, dtype=np.float32)
                norm = float(np.linalg.norm(row))
                if norm > 0:
                    self._matrix[position] = row / norm

    def __len__(self) -> int:
        return len(self.refs)

    # --- the two halves -------------------------------------------------------
    def _keyword(self, query: str, min_coverage: float, min_terms: int) -> dict[int, float]:
        """BM25 scores for passages that cover enough of the query.

        Coverage is the share of the query's informative words (weighted by how
        rare they are) a passage contains. Without this floor a passage that
        shares one common word with a long query would be ranked, and a library
        spanning many subjects would hand a course material from another one.
        """
        terms = set(tokenize(query))
        total = len(self.refs)
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, float] = defaultdict(float)
        matched_terms: dict[int, int] = defaultdict(int)
        weight_of_query = 0.0
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            weight_of_query += idf
            for position, frequency in postings:
                length = self._lengths[position]
                matched[position] += idf
                matched_terms[position] += 1
                scores[position] += (
                    idf * frequency * (K1 + 1) / (frequency + K1 * (1 - B + B * length / self._average))
                )
        if weight_of_query <= 0:
            return {}
        # A long query needs a passage matching at least `min_terms` of its
        # words: one shared word (even a fairly rare one) is a coincidence.
        needed = min(min_terms, len(terms))
        return {
            p: s
            for p, s in scores.items()
            if matched[p] / weight_of_query >= min_coverage and matched_terms[p] >= needed
        }

    def _semantic(self, vector: Sequence[float], minimum: float, pool: int) -> list[int]:
        if not len(self.refs) or len(vector) != self.dimensions:
            return []
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm == 0:
            return []
        similarity = self._matrix @ (query / norm)
        candidates = np.argsort(-similarity)[:pool]
        return [int(i) for i in candidates if similarity[i] >= minimum]

    # --- the query ------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        query_vector: Sequence[float] | None = None,
        limit: int = 5,
        min_semantic: float = 0.3,
        min_coverage: float = 0.25,
        min_terms: int = 2,
    ) -> list[tuple[str, float]]:
        """Best `limit` (ref, score) pairs, highest first. Empty when nothing matches."""
        if not self.refs:
            return []
        keyword = self._keyword(query, min_coverage, min_terms)
        keyword_order = sorted(keyword, key=lambda position: keyword[position], reverse=True)
        semantic_order = (
            self._semantic(query_vector, min_semantic, max(limit * 4, 20)) if query_vector is not None else []
        )
        fused: dict[int, float] = defaultdict(float)
        for order in (keyword_order, semantic_order):
            for rank_position, position in enumerate(order):
                fused[position] += 1.0 / (RRF_K + rank_position + 1)
        ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [(self.refs[position], score) for position, score in ranked]
