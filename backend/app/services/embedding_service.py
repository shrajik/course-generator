"""Embedding generation: the only place that calls `AIClient.embed()`.

Every method here is best-effort and never raises - a provider outage, a
missing API key, or `enable_embeddings=false` all just mean "no vector this
time", which callers (HybridRetriever) treat as "fall back to keyword
search", never as a reason to fail course generation.
"""

from __future__ import annotations

import hashlib

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

# Bounds the text actually sent to the embedding API - a memory item's
# description/content can be arbitrarily long, but the embedded
# *representation* should stay short and focused (see the `*_text()`
# builders in each memory source service).
MAX_EMBED_CHARS = 4000


def content_hash(text: str) -> str:
    """Stable fingerprint of embedded text, used to detect "source content
    changed since this embedding was made" without comparing vectors."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingService:
    def __init__(self, ai: AIClient | None = None, settings: Settings | None = None) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.enable_embeddings

    async def embed_one(self, text: str) -> list[float] | None:
        """None on any failure, on disabled embeddings, or on blank input."""
        text = (text or "").strip()
        if not self.enabled or not text:
            return None
        try:
            vectors = await self.ai.embed(
                texts=[text[:MAX_EMBED_CHARS]], model=self.settings.embedding_model
            )
            return vectors[0] if vectors else None
        except Exception as exc:  # noqa: BLE001 - embedding must never break the caller
            log.warning("Embedding generation failed: %s", exc)
            return None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Batched (respecting `embedding_batch_size`); one entry per input,
        in order. A batch that fails yields None for just that batch's
        entries - one bad chunk never loses embeddings the rest of the call
        could still produce."""
        if not self.enabled:
            return [None] * len(texts)
        clipped = [(t or "")[:MAX_EMBED_CHARS] for t in texts]
        results: list[list[float] | None] = []
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(clipped), batch_size):
            chunk = clipped[start : start + batch_size]
            blank_mask = [not t.strip() for t in chunk]
            non_blank = [t for t, blank in zip(chunk, blank_mask) if not blank]
            if not non_blank:
                results.extend([None] * len(chunk))
                continue
            try:
                vectors = await self.ai.embed(texts=non_blank, model=self.settings.embedding_model)
            except Exception as exc:  # noqa: BLE001
                log.warning("Batch embedding failed for %s text(s): %s", len(non_blank), exc)
                results.extend([None] * len(chunk))
                continue
            vector_iter = iter(vectors)
            for blank in blank_mask:
                results.append(None if blank else next(vector_iter, None))
        return results


_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _service
    if _service is None:
        _service = EmbeddingService()
    return _service


def reset_embedding_service() -> None:
    """Test hook - mirrors reset_* in the other services."""
    global _service
    _service = None
