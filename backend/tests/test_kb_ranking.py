"""The PostgreSQL backend's ranking arithmetic must equal the file index's."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from app.ingestion.index import IndexedPassage, SearchIndex
from app.ingestion.retrieval import tokenize
from app.services import kb_ranking

PASSAGES = {
    "G1.1": "Refunds are issued within fourteen days when the product is returned unused.",
    "G1.2": "Orders ship from the warehouse within two business days by courier.",
    "G1.3": "Disputed refunds are escalated to the billing team, who respond within two business days.",
    "G2.1": "Boil the pasta for ten minutes in salted water and drain it well.",
    "G2.2": "Simmer the tomato sauce slowly with garlic and fresh basil until it thickens.",
    "G3.1": "Passwords must contain at least fourteen characters and be rotated every ninety days.",
    "G3.2": "Refund requests for passwords are not a thing, but password refunds appear here too.",
}
QUERIES = [
    "how long do refunds take after the product is returned",
    "business days courier shipping",
    "rotate passwords every ninety days",
    "tomato sauce with garlic",
    "refunds",  # one term: needs only one match
    "quantum gluon refunds",  # one unknown term, one known
    "the and of",  # nothing informative
    "billing team escalation of disputed refunds",
]


def _corpus_stats(passages: dict[str, str]):
    """What PostgreSQL would report: totals, document frequencies, average length."""
    tokens = {ref: tokenize(text) for ref, text in passages.items()}
    frequency = Counter(term for terms in tokens.values() for term in set(terms))
    average = sum(len(t) for t in tokens.values()) / len(tokens)
    return tokens, dict(frequency), average


def _via_ranking(query: str, passages: dict[str, str], semantic: list[str], limit: int, **gates):
    tokens, frequency, average = _corpus_stats(passages)
    terms = kb_ranking.query_terms(query)
    # PostgreSQL would return only passages sharing a term; the scoring must cope either way.
    candidates = [kb_ranking.KeywordCandidate(ref, t) for ref, t in tokens.items() if set(t) & set(terms)]
    keyword = kb_ranking.keyword_scores(
        terms, candidates, total=len(passages), document_frequency=frequency, average_length=average,
        min_coverage=gates.get("min_coverage", 0.25), min_terms=gates.get("min_terms", 2),
    )
    return kb_ranking.fuse(keyword, semantic, limit=limit)


@pytest.mark.parametrize("query", QUERIES)
def test_keyword_ranking_equals_the_file_index(query):
    index = SearchIndex([IndexedPassage(ref, text) for ref, text in PASSAGES.items()], dimensions=4)
    expected = index.search(query, limit=5)
    got = _via_ranking(query, PASSAGES, semantic=[], limit=5)
    assert [ref for ref, _ in got] == [ref for ref, _ in expected]
    assert [round(score, 9) for _, score in got] == [round(score, 9) for _, score in expected]


@pytest.mark.parametrize("gates", [{"min_coverage": 0.0, "min_terms": 1}, {"min_coverage": 0.6, "min_terms": 3}])
def test_the_coverage_and_term_floors_behave_identically(gates):
    index = SearchIndex([IndexedPassage(ref, text) for ref, text in PASSAGES.items()], dimensions=4)
    for query in QUERIES:
        expected = index.search(query, limit=5, min_coverage=gates["min_coverage"], min_terms=gates["min_terms"])
        got = _via_ranking(query, PASSAGES, semantic=[], limit=5, **gates)
        assert [ref for ref, _ in got] == [ref for ref, _ in expected], query


def test_fusion_with_a_semantic_order_equals_the_file_index():
    rng = np.random.default_rng(3)
    vectors = {ref: rng.random(4).tolist() for ref in PASSAGES}
    index = SearchIndex([IndexedPassage(ref, text, vectors[ref]) for ref, text in PASSAGES.items()], dimensions=4)
    query_vector = rng.random(4).tolist()
    query = "refunds within two business days"
    expected = index.search(query, query_vector=query_vector, limit=4, min_semantic=-1.0)
    # The same semantic order the index computed (cosine, best first, every passage kept).
    refs = list(PASSAGES)
    unit = {r: np.asarray(v) / np.linalg.norm(v) for r, v in vectors.items()}
    q = np.asarray(query_vector) / np.linalg.norm(query_vector)
    semantic = sorted(refs, key=lambda r: -float(unit[r] @ q))[: max(4 * 4, 20)]
    got = _via_ranking(query, PASSAGES, semantic=semantic, limit=4)
    assert [ref for ref, _ in got] == [ref for ref, _ in expected]


def test_degenerate_inputs_score_nothing():
    assert kb_ranking.keyword_scores([], [], total=0, document_frequency={}, average_length=0, min_coverage=0.25, min_terms=2) == {}
    unknown = kb_ranking.keyword_scores(["zzz"], [kb_ranking.KeywordCandidate("a", ["x"])], total=1, document_frequency={}, average_length=1, min_coverage=0.0, min_terms=1)
    assert unknown == {}
    assert kb_ranking.fuse({}, [], limit=5) == []


# --- result shaping --------------------------------------------------------------------------------------


def test_a_near_copy_of_a_better_ranked_passage_is_dropped_but_a_different_one_is_kept():
    ranked = [
        ("a", "Refunds are issued within fourteen days when the product is returned unused."),
        ("b", "Refunds are issued within fourteen days when the product is returned unused!"),  # the same sentence again
        ("c", "Orders ship from the warehouse within two business days by courier."),
        ("d", ""),
    ]
    assert kb_ranking.near_duplicates_removed(ranked) == ["a", "c", "d"]
    assert kb_ranking.near_duplicates_removed(ranked, threshold=1.01) == ["a", "b", "c", "d"]  # nothing counts as a copy
    assert kb_ranking.near_duplicates_removed([]) == []


def test_compression_keeps_the_sentences_about_the_query_in_order_and_never_rewrites():
    text = (
        "Our company was founded long ago. Refunds are issued within fourteen days of a valid request. "
        "The canteen opens at nine. Disputed refunds are escalated to the billing team. The car park is full on Fridays."
    )
    terms = kb_ranking.query_terms("refund escalation billing")
    out = kb_ranking.compress(text, terms, 120)
    assert len(out) <= 120 and "Refunds are issued" in out and "billing team" in out
    assert "canteen" not in out and "founded" not in out
    assert out.index("Refunds are issued") < out.index("billing team")  # original order
    assert "..." in out  # and the gap where the canteen sentence was is marked
    original = [s.strip() for s in text.split(". ")]
    for piece in (p.strip(" .") for p in out.split("...")):
        assert any(piece in sentence for sentence in original), piece  # every kept piece is the document's own words


def test_compression_leaves_short_text_alone_and_always_returns_something_within_the_limit():
    assert kb_ranking.compress("Short text.", ["short"], 100) == "Short text."
    long_sentence = "word " * 200
    out = kb_ranking.compress(long_sentence, ["word"], 50)
    assert 0 < len(out) <= 50 and out.endswith("...")
    assert kb_ranking.compress("A. B. C. D. E. F. G. H.", ["zzz"], 12)  # no sentence matches: still the leading ones
