"""Embedding generation: EmbeddingService + the offline mock's hashing-trick
embedding. No database needed - these test the piece that calls
`AIClient.embed()` and nothing downstream of it. See test_semantic_memory.py
for the DB-backed hybrid-retrieval tests.
"""

from __future__ import annotations

import math

import pytest

from app.core.config import get_settings
from app.services.embedding_service import EmbeddingService, content_hash
from app.services.mock_ai import MockAIClient, _fake_embedding


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def test_fake_embedding_has_configured_dimension_and_is_deterministic():
    settings = get_settings()
    v1 = _fake_embedding("Electromagnetic induction basics", settings.embedding_dimensions)
    v2 = _fake_embedding("Electromagnetic induction basics", settings.embedding_dimensions)
    assert len(v1) == settings.embedding_dimensions
    assert v1 == v2


def test_fake_embedding_similarity_is_higher_for_related_text():
    """The hashing trick has no notion of synonymy, but it does capture
    lexical overlap - two texts sharing several words should cosine-score
    higher than two texts sharing none. This is what makes the offline
    semantic-retrieval tests meaningful as *pipeline mechanics* tests, even
    though they can't prove true semantic understanding (see docstring on
    mock_ai._fake_embedding)."""
    dims = 256
    a = _fake_embedding("Faraday's law and electromagnetic induction in coils", dims)
    b = _fake_embedding("An introduction to electromagnetic induction using coils", dims)
    c = _fake_embedding("A history of Renaissance oil painting techniques", dims)

    related = _cosine(a, b)
    unrelated = _cosine(a, c)
    assert related > unrelated


async def test_embedding_service_disabled_makes_zero_calls():
    settings = get_settings().model_copy(update={"enable_embeddings": False})
    ai = MockAIClient(settings)
    service = EmbeddingService(ai=ai, settings=settings)

    result = await service.embed_one("some text")

    assert result is None
    assert ai.calls == []


async def test_embedding_service_blank_text_returns_none_without_a_call():
    settings = get_settings()
    ai = MockAIClient(settings)
    service = EmbeddingService(ai=ai, settings=settings)

    assert await service.embed_one("   ") is None
    assert ai.calls == []


async def test_embedding_service_embed_one_returns_the_configured_dimension():
    settings = get_settings()
    ai = MockAIClient(settings)
    service = EmbeddingService(ai=ai, settings=settings)

    vector = await service.embed_one("Solar power fundamentals")

    assert vector is not None
    assert len(vector) == settings.embedding_dimensions
    assert len(ai.calls) == 1


async def test_embedding_service_batches_respect_configured_batch_size():
    settings = get_settings().model_copy(update={"embedding_batch_size": 2})
    ai = MockAIClient(settings)
    service = EmbeddingService(ai=ai, settings=settings)

    vectors = await service.embed_batch(["a", "b", "c", "d", "e"])

    assert len(vectors) == 5
    assert all(v is not None and len(v) == settings.embedding_dimensions for v in vectors)
    # 5 texts at batch_size=2 -> 3 embedding calls (2, 2, 1), never one big call.
    assert len(ai.calls) == 3
    assert [c["count"] for c in ai.calls] == [2, 2, 1]


async def test_embedding_service_failure_returns_none_not_raises():
    """A provider outage/bad model/etc must never propagate - callers
    (HybridRetriever, the lifecycle sync hooks) treat None exactly like
    "no semantic signal this time" and fall back to keyword retrieval."""

    class _BrokenClient(MockAIClient):
        async def embed(self, *, texts, model=None, phase="embedding"):
            raise RuntimeError("provider is down")

    settings = get_settings()
    service = EmbeddingService(ai=_BrokenClient(settings), settings=settings)

    assert await service.embed_one("some text") is None
    assert await service.embed_batch(["a", "b"]) == [None, None]


def test_content_hash_is_deterministic_and_change_sensitive():
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    h3 = content_hash("hello world!")
    assert h1 == h2
    assert h1 != h3
