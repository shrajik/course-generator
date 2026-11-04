"""The knowledge library as the rest of the app sees it, backed by PostgreSQL.

`PostgresKnowledgeService` is what `get_knowledge_service()` returns when
`KB_BACKEND=postgres`. It is the PostgreSQL search backend plus the operations the
existing admin API (`/api/knowledge`) offers, answered from the `kb_*` tables in the
same shapes (`SourceRecord`, `SourceChunk`) so that API and its page keep working.

How the old vocabulary maps:

* `source_id`      -> the item's `public_id` (`src_...`; migrated items keep theirs)
* `global_number`  -> the item's permanent `number`
* `status`         -> the newest version's *processing* status
* `enabled`        -> the item is Published (and not deleted); switching it on publishes,
                      off unpublishes
* upload           -> a new **draft** item (new uploads are not searchable until an admin
                      publishes them; the old behaviour was to be searchable at once)
* delete           -> the item goes to the trash (a soft delete; numbers are never reused)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.db import kb_models as kb
from app.kb.service import KbService
from app.schemas.source import SourceChunk, SourceRecord
from app.services.knowledge_pg import PostgresKnowledgeBackend

_STATUS = {
    "queued": "uploaded",
    "parsing": "parsing",
    "chunking": "chunking",
    "embedding": "embedding",
    "classifying": "embedding",
    "ready": "ready",
    "failed": "failed",
}
_IN_PROGRESS = ("queued", "parsing", "chunking", "embedding", "classifying")


def _iso(value: datetime | None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


class PostgresKnowledgeService(PostgresKnowledgeBackend):
    def __init__(self, settings: Settings | None = None, **kwargs) -> None:
        super().__init__(settings, **kwargs)
        self.kb = KbService(settings=self.settings, session_factory=kwargs.get("session_factory") and kwargs["session_factory"]())

    # --- records ------------------------------------------------------------------------
    async def _record(self, session, item: kb.KbItem) -> SourceRecord:
        version = await session.get(kb.KbItemVersion, item.latest_version_id) if item.latest_version_id else None
        source = None
        if version is not None:
            source = (
                await session.execute(
                    select(kb.KbSource).join(kb.KbVersionSource, kb.KbVersionSource.source_id == kb.KbSource.id)
                    .where(kb.KbVersionSource.version_id == version.id).order_by(kb.KbVersionSource.position).limit(1)
                )
            ).scalar_one_or_none()
        chunk_count = embedded = 0
        if version is not None:
            row = (
                await session.execute(
                    select(func.count(), func.count(kb.KbChunk.embedding))
                    .select_from(kb.KbVersionChunk).join(kb.KbChunk, kb.KbChunk.id == kb.KbVersionChunk.chunk_id)
                    .where(kb.KbVersionChunk.version_id == version.id)
                )
            ).one()
            chunk_count, embedded = int(row[0]), int(row[1])
        processing = version.processing_status if version is not None else "queued"
        return SourceRecord(
            source_id=item.public_id,
            owner_key="global",
            filename=source.filename if source is not None else item.title,
            kind=source.kind if source is not None else "md",  # type: ignore[arg-type]
            size_bytes=source.size_bytes if source is not None else 0,
            sha256=source.sha256 if source is not None else "",
            status=_STATUS[processing],  # type: ignore[arg-type]
            message="",
            error=version.processing_error if version is not None else None,
            page_count=source.page_count if source is not None else None,
            word_count=source.word_count if source is not None else 0,
            chunk_count=chunk_count,
            embedded_chunks=embedded,
            outline=list(source.outline) if source is not None and source.outline else [],
            global_number=item.number,
            enabled=item.lifecycle_status == "published" and item.deleted_at is None,
            description=item.description,
            created_at=_iso(item.created_at),
            updated_at=_iso(item.updated_at),
        )

    async def _item_by_public_id(self, session, source_id: str) -> kb.KbItem:
        item = (
            await session.execute(select(kb.KbItem).where(kb.KbItem.public_id == source_id, kb.KbItem.deleted_at.is_(None), kb.KbItem.purged_at.is_(None)))
        ).scalar_one_or_none()
        if item is None:
            raise NotFoundError("Source not found")
        return item

    # --- what /api/knowledge offers (async) --------------------------------------------------
    async def aupload(self, filename: str, data: bytes, *, description: str = "", actor_id: uuid.UUID | None = None) -> SourceRecord:
        try:
            ref = await self.kb.create_item(actor_id=actor_id, files=[(filename, data)], description=description)
        except ConflictError as clash:
            public_id = clash.details.get("public_id")
            if public_id is None:
                raise
            async with self._factory()() as session:
                existing = await self._item_by_public_id(session, public_id)
                record = await self._record(session, existing)
            if record.status != "failed":
                return record  # the same file again is not added twice, and keeps its number
            ref = await self.kb.create_item(actor_id=actor_id, files=[(filename, data)], description=description, allow_duplicate=True)
        async with self._factory()() as session:
            return await self._record(session, await self._item_by_public_id(session, ref.public_id))

    async def alist_documents(self) -> list[SourceRecord]:
        async with self._factory()() as session:
            items = (await session.execute(select(kb.KbItem).where(kb.KbItem.deleted_at.is_(None), kb.KbItem.purged_at.is_(None)).order_by(kb.KbItem.number))).scalars().all()
            return [await self._record(session, item) for item in items]

    async def aget(self, source_id: str) -> SourceRecord:
        async with self._factory()() as session:
            return await self._record(session, await self._item_by_public_id(session, source_id))

    async def aupdate(self, source_id: str, *, enabled: bool | None = None, description: str | None = None, actor_id: uuid.UUID | None = None) -> SourceRecord:
        async with self._factory()() as session:
            item = await self._item_by_public_id(session, source_id)
            version = await session.get(kb.KbItemVersion, item.latest_version_id)
            if version is not None and version.processing_status in _IN_PROGRESS:
                raise ConflictError("The document is still being processed. Try again when it is ready.")
            item_id, published = item.id, item.lifecycle_status == "published"
        if description is not None:
            await self.kb.update_item(item_id, actor_id=actor_id, description=description)
        if enabled is True and not published:
            await self.kb.publish_item(item_id, actor_id=actor_id)
        elif enabled is False and published:
            await self.kb.set_lifecycle(item_id, "unpublish", actor_id=actor_id)
        return await self.aget(source_id)

    async def adelete(self, source_id: str, *, actor_id: uuid.UUID | None = None) -> None:
        async with self._factory()() as session:
            item_id = (await self._item_by_public_id(session, source_id)).id
        await self.kb.set_lifecycle(item_id, "delete", actor_id=actor_id)

    async def achunks(self, source_id: str, offset: int = 0, limit: int = 50) -> list[SourceChunk]:
        async with self._factory()() as session:
            item = await self._item_by_public_id(session, source_id)
            rows = (
                await session.execute(
                    select(kb.KbChunk).join(kb.KbVersionChunk, kb.KbVersionChunk.chunk_id == kb.KbChunk.id)
                    .where(kb.KbVersionChunk.version_id == item.latest_version_id)
                    .order_by(kb.KbVersionChunk.position).offset(offset).limit(limit)
                )
            ).scalars().all()
            return [
                SourceChunk(index=c.ordinal - 1, heading_path=list(c.heading_path), page=c.page, text=c.text, words=c.words, embedded=c.embedding is not None)
                for c in rows
            ]

    async def astats(self) -> dict[str, int]:
        async with self._factory()() as session:
            documents = await session.scalar(select(func.count()).select_from(kb.KbItem).where(kb.KbItem.deleted_at.is_(None), kb.KbItem.purged_at.is_(None))) or 0
            searchable = await session.scalar(select(func.count()).select_from(kb.KbItem).where(kb.KbItem.lifecycle_status == "published", kb.KbItem.deleted_at.is_(None), kb.KbItem.purged_at.is_(None))) or 0
            passages = await session.scalar(select(func.count()).select_from(kb.KbChunk).where(kb.KbChunk.is_live)) or 0
            processing = await session.scalar(
                select(func.count()).select_from(kb.KbItem).join(kb.KbItemVersion, kb.KbItemVersion.id == kb.KbItem.latest_version_id)
                .where(kb.KbItem.deleted_at.is_(None), kb.KbItemVersion.processing_status.in_(_IN_PROGRESS))
            ) or 0
        return {"documents": documents, "searchable": searchable, "passages": passages, "processing": processing}

    async def asearch(self, query, *, limit=None, char_budget=None, trace=None):  # noqa: D102 - the contract's
        return await super().asearch(query, limit=limit, char_budget=char_budget, trace=trace)
