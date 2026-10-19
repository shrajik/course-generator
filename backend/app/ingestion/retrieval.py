"""Rank source chunks for a query: keyword (BM25) plus semantic (cosine).

The two rankings are merged by reciprocal-rank fusion, which needs no tuning
between their different score scales. When no vectors are available - the
embedding provider is down, or embeddings are switched off - ranking is
keyword-only, exactly as the rest of the platform's hybrid retrieval degrades
(see app/services/hybrid_retriever.py).

Pure functions over in-memory data: the caller loads the chunks and vectors.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'+#.-]*[a-z0-9+#]|[a-z0-9]")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how i if in into is it its of on or "
    "that the their then there these they this to was were what when where which who will "
    "with you your we our not can do does did so than too very".split()
)

K1 = 1.5
B = 0.75
RRF_K = 60


def _stem(token: str) -> str:
    """A light suffix-stripper so "refunds", "refunded" and "refunding" meet.

    Deliberately crude (no dictionary): it only has to give a query and a
    passage the same form for the same word, and short or symbol-bearing
    tokens ("c++", "v2.1") are left alone.
    """
    if len(token) <= 4 or not token.isalpha():
        return token
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            if suffix == "s" and token.endswith("ss"):
                return token
            return token[: -len(suffix)] + replacement
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True)
class Candidate:
    """One chunk to rank. `key` identifies it to the caller."""

    key: str
    text: str
    vector: Sequence[float] | None = None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def bm25_scores(query: str, candidates: Sequence[Candidate]) -> dict[str, float]:
    """BM25 over the candidates only. Chunks sharing no query term are omitted."""
    terms = set(tokenize(query))
    if not terms or not candidates:
        return {}
    documents = {c.key: tokenize(c.text) for c in candidates}
    average = sum(len(tokens) for tokens in documents.values()) / max(len(documents), 1) or 1.0
    document_frequency = Counter(term for tokens in documents.values() for term in terms & set(tokens))
    total = len(documents)

    scores: dict[str, float] = {}
    for key, tokens in documents.items():
        counts = Counter(tokens)
        score = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += idf * frequency * (K1 + 1) / (frequency + K1 * (1 - B + B * len(tokens) / average))
        if score > 0:
            scores[key] = score
    return scores


def rank(
    query: str,
    candidates: Sequence[Candidate],
    *,
    query_vector: Sequence[float] | None = None,
    limit: int = 6,
    min_semantic: float = 0.15,
) -> list[tuple[str, float]]:
    """Best `limit` (key, score) pairs, highest first. Empty when nothing matches."""
    keyword = bm25_scores(query, candidates)
    keyword_order = sorted(keyword, key=lambda key: keyword[key], reverse=True)

    semantic_order: list[str] = []
    if query_vector is not None:
        similarity = {
            c.key: cosine(query_vector, c.vector)
            for c in candidates
            if c.vector is not None
        }
        semantic_order = [
            key
            for key in sorted(similarity, key=lambda key: similarity[key], reverse=True)
            if similarity[key] >= min_semantic
        ]

    fused: dict[str, float] = {}
    for order in (keyword_order, semantic_order):
        for position, key in enumerate(order):
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + position + 1)
    ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]
