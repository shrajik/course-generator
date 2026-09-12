"""Embedding lifecycle hooks: create-on-new-item / invalidate-and-regenerate-
on-change / delete-on-hard-delete, for the four memory source services.

This is the only bridge between "a source row changed" and the
memory_embeddings table - services call `sync_embedding()`/`delete_embedding()`
after their own commit, never touching MemoryEmbeddingRepository or pgvector
directly. Both functions are best-effort and never raise: a failed embedding
write must never fail the create/update/delete it's attached to, it just
means this row keeps falling back to keyword retrieval until the next
backfill run.
"""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.db.repositories.memory_embeddings import MemoryEmbeddingRepository
from app.db.session import get_session_factory
from app.services.embedding_service import content_hash, get_embedding_service

log = get_logger(__name__)


async def sync_embedding(source_type: str, source_id: uuid.UUID, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    service = get_embedding_service()
    if not service.enabled:
        return
    try:
        vector = await service.embed_one(text)
        if vector is None:
            return
        async with get_session_factory()() as session:
            async with session.begin():
                await MemoryEmbeddingRepository(session).upsert(
                    source_type=source_type,
                    source_id=source_id,
                    model=service.settings.embedding_model,
                    content_hash=content_hash(text),
                    embedded_text=text,
                    embedding=vector,
                )
    except Exception as exc:  # noqa: BLE001 - embedding must never break the caller
        log.warning("Could not sync embedding for %s:%s: %s", source_type, source_id, exc)


async def delete_embedding(source_type: str, source_id: uuid.UUID) -> None:
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                await MemoryEmbeddingRepository(session).delete(
                    source_type=source_type, source_id=source_id
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not delete embedding for %s:%s: %s", source_type, source_id, exc)
