"""Keyword scoring and rank fusion for the PostgreSQL knowledge backend.

`app/ingestion/index.py` scores keyword matches (BM25 with a coverage floor) from an
in-memory index of every passage. PostgreSQL does not hold such an index in the
application, so the database supplies what BM25 needs - how many passages there are,
how many contain each query term, the average passage length - plus a bounded set of
candidate passages, and this module does the arithmetic. The formulas are the file
index's, line for line, so the same corpus and query give the same ranking
(tests/test_kb_ranking.py checks exactly that against `SearchIndex`).

Pure functions: no database, no model calls.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.retrieval import B, K1, RRF_K, tokenize


@dataclass(frozen=True)
class KeywordCandidate:
    key: str
    # Every term occurrence of the passage (not the distinct set): term frequency and
    # length both come from it.
    tokens: Sequence[str]


def query_terms(query: str) -> list[str]:
    """The distinct search terms of a query, in a stable order."""
    return sorted(set(tokenize(query)))


def needed_terms(terms: Sequence[str], min_terms: int) -> int:
    """How many query terms a passage must contain: one shared word, even a fairly rare
    one, is a coincidence on a long query."""
    return min(min_terms, len(terms))


def keyword_scores(
    terms: Sequence[str],
    candidates: Sequence[KeywordCandidate],
    *,
    total: int,
    document_frequency: dict[str, int],
    average_length: float,
    min_coverage: float,
    min_terms: int,
) -> dict[str, float]:
    """BM25 scores of the candidates that cover enough of the query.

    `total` is the number of passages in the whole corpus and `document_frequency[term]`
    how many contain `term`: the statistics the file index derived from its postings.
    A term no passage contains has no weight and cannot count towards coverage.
    """
    unique = set(terms)
    average = average_length if average_length > 0 else 1.0
    idf: dict[str, float] = {}
    weight_of_query = 0.0
    for term in unique:
        frequency = document_frequency.get(term, 0)
        if frequency <= 0:
            continue
        idf[term] = math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
        weight_of_query += idf[term]
    if weight_of_query <= 0:
        return {}

    scores: dict[str, float] = defaultdict(float)
    matched: dict[str, float] = defaultdict(float)
    matched_terms: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        counts = Counter(candidate.tokens)
        length = len(candidate.tokens)
        for term, weight in idf.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            matched[candidate.key] += weight
            matched_terms[candidate.key] += 1
            scores[candidate.key] += weight * frequency * (K1 + 1) / (frequency + K1 * (1 - B + B * length / average))

    needed = needed_terms(list(unique), min_terms)
    return {
        key: score
        for key, score in scores.items()
        if matched[key] / weight_of_query >= min_coverage and matched_terms[key] >= needed
    }


def fuse(
    keyword: dict[str, float],
    semantic_order: Sequence[str],
    *,
    limit: int,
) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion of the keyword ranking and the semantic one, best first."""
    keyword_order = sorted(keyword, key=lambda key: keyword[key], reverse=True)
    fused: dict[str, float] = defaultdict(float)
    for order in (keyword_order, semantic_order):
        for position, key in enumerate(order):
            fused[key] += 1.0 / (RRF_K + position + 1)
    return sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]


# --- result shaping (used by the PostgreSQL backend's optional v2 features) ----------------------


def near_duplicates_removed(ranked: Sequence[tuple[str, str]], *, threshold: float = 0.9) -> list[str]:
    """Keys of the passages worth keeping, best first: a passage whose terms overlap an
    already-kept, better-ranked one by `threshold` or more (Jaccard) says the same thing again
    and only costs tokens. `ranked` is (key, text) in rank order."""
    kept: list[tuple[str, set[str]]] = []
    for key, text in ranked:
        terms = set(tokenize(text))
        if any(terms and other and len(terms & other) / len(terms | other) >= threshold for _, other in kept):
            continue
        kept.append((key, terms))
    return [key for key, _ in kept]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def compress(text: str, terms: Sequence[str], max_chars: int) -> str:
    """`text` cut to at most `max_chars` by keeping the sentences that share the most query terms
    (original order, "..." where sentences were left out). Extractive: nothing is rewritten, so a
    reader never meets a sentence the document did not contain. Text that already fits is untouched."""
    if len(text) <= max_chars:
        return text
    sentences = [s for s in _SENTENCE_END.split(text.strip()) if s]
    wanted = set(terms)
    scored = sorted(
        range(len(sentences)),
        key=lambda i: (-len(wanted & set(tokenize(sentences[i]))), i),
    )
    chosen: set[int] = set()
    used = 0
    for index in scored:
        cost = len(sentences[index]) + 1
        if used + cost <= max_chars - 4 or not chosen:  # always keep at least the best sentence
            chosen.add(index)
            used += cost
    pieces: list[str] = []
    previous = -2
    for index in sorted(chosen):
        if index != previous + 1 and pieces:
            pieces.append("...")
        pieces.append(sentences[index])
        previous = index
    result = " ".join(pieces)
    return result if len(result) <= max_chars else result[: max_chars - 3].rstrip() + "..."
