"""Permanently removing an item's content, and warning first what that breaks.

Deleting an item only moves it to the trash; nothing is lost and a citation to it still
resolves. Purging is the one irreversible step, so it is separate, admin-only and off until
`KB_PURGE_ENABLED` (it stays off while the cutover rollback window is open, because legacy
originals are used in place). Before it, `impact` lists the courses that cite the item.

What purge removes: passages and their vectors, version text, the item's files (when no other
item uses them), its relationships, tags and pending jobs. What it keeps: the item's row, so its
number can never be handed out again, and its activity history. A citation to a purged item can
no longer be resolved - the same outcome as a document that was deleted from the old file library.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.kb import lifecycle
from app.kb.service import _actor, bump_epoch, log_activity


class Purger:
    def __init__(self, settings: Settings | None = None, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self.settings = settings or get_settings()
        self._factory = session_factory

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    async def impact(self, item_id: uuid.UUID) -> dict:
        """Who cites this item. Three sources, because each covers what the others miss: the retrieval
        log (exact, but only since it was introduced), the sources each chapter recorded, and a scan of
        the stored course documents for `[G<number>.` (the only one that sees older courses)."""
        async with self.factory() as session:
            item = await session.get(kb.KbItem, item_id)
            if item is None or item.purged_at is not None:
                raise NotFoundError("Knowledge item not found")
            logged = (
                await session.execute(
                    text("SELECT DISTINCT course_id FROM kb_retrieval_log WHERE item_number = :n AND course_id IS NOT NULL"), {"n": item.number}
                )
            ).scalars().all()
            documents = (
                await session.execute(
                    text(
                        "SELECT c.course_id FROM documents d JOIN courses c ON c.id = d.course_pk "
                        "WHERE d.document_json::text LIKE :pattern"
                    ),
                    {"pattern": f"%[G{item.number}.%"},
                )
            ).scalars().all()
        courses = sorted({*logged, *documents})
        return {
            "number": item.number,
            "title": item.title,
            "courses": courses,
            "from_retrieval_log": sorted(logged),
            "from_document_text": sorted(documents),
            "warning": (
                f"{len(courses)} course(s) cite G{item.number}. After removal those citations can no longer be shown or checked."
                if courses
                else f"No course is known to cite G{item.number}."
            ),
        }

    async def purge(self, item_id: uuid.UUID, *, confirm_number: int, actor_id: object = None) -> dict:
        """Permanently remove the item's content. `confirm_number` must be the item's number, typed by
        the admin after reading `impact`."""
        if not self.settings.kb_purge_enabled:
            raise ConflictError("Permanent removal is not enabled yet.")
        actor = _actor(actor_id)
        orphaned_files: list[str] = []
        async with self.factory() as session, session.begin():
            item = (await session.execute(select(kb.KbItem).where(kb.KbItem.id == item_id).with_for_update())).scalar_one_or_none()
            if item is None:
                raise NotFoundError("Knowledge item not found")
            lifecycle.apply(
                lifecycle.ItemState(item.lifecycle_status, deleted=item.deleted_at is not None, purged=item.purged_at is not None),
                "purge", purge_allowed=True,
            )
            if confirm_number != item.number:
                raise ValidationFailedError(f"Type the item's number ({item.number}) to confirm.")
            versions = select(kb.KbItemVersion.id).where(kb.KbItemVersion.item_id == item_id)
            source_ids = set((await session.scalars(select(kb.KbVersionSource.source_id).where(kb.KbVersionSource.version_id.in_(versions)))).all())
            await session.execute(delete(kb.KbChunk).where(kb.KbChunk.item_id == item_id))  # links, vectors and entity ties go with them
            await session.execute(delete(kb.KbVersionSource).where(kb.KbVersionSource.version_id.in_(versions)))
            await session.execute(delete(kb.KbJob).where(kb.KbJob.version_id.in_(versions)))
            await session.execute(delete(kb.KbItemTag).where(kb.KbItemTag.item_id == item_id))
            await session.execute(delete(kb.KbItemEntity).where(kb.KbItemEntity.item_id == item_id))
            await session.execute(delete(kb.KbRelation).where((kb.KbRelation.from_item_id == item_id) | (kb.KbRelation.to_item_id == item_id)))
            await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.item_id == item_id).values(body_md=None))
            for source_id in source_ids:  # a file another item still uses stays
                if not await session.scalar(select(func.count()).select_from(kb.KbVersionSource).where(kb.KbVersionSource.source_id == source_id)):
                    source = await session.get(kb.KbSource, source_id)
                    orphaned_files.append(source.storage_key)
                    await session.delete(source)
            await session.execute(
                update(kb.KbItem).where(kb.KbItem.id == item_id).values(
                    purged_at=func.now(), purged_by=actor, description="", suggestions={}, properties={}, centroid=None, needs_review=False, updated_at=func.now()
                )
            )
            await bump_epoch(session)
            await log_activity(session, item_id, actor, "purged", number=item.number, files=len(orphaned_files))
        for key in orphaned_files:  # after the commit: a file is only deleted once the rows are gone
            path = Path(self.settings.data_dir) / key
            if path.is_file() and not key.startswith("sources/global/"):  # legacy originals stay until the rollback window closes
                os.unlink(path)
        return {"purged": item.number, "files_removed": len([k for k in orphaned_files if not k.startswith("sources/global/")])}
