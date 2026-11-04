"""Creating, versioning and publishing knowledge items in PostgreSQL.

Everything that changes what an admin sees or what retrieval can return goes through
here, one transaction per operation:

* `create_item` / `add_version` store the files, create the item or version in
  `queued` processing state and enqueue the ingestion job. They never make anything
  live: a new item is a draft, a new version of a published item is only a candidate.
* `publish_version` is the only way content becomes live. It locks the item row, so
  publishing, unpublishing, archiving and deleting cannot interleave, and it switches
  the item's passages in one transaction: a search sees all of the old version or all
  of the new one, never a mixture and never nothing. A stale view (someone else
  switched versions meanwhile) is a 409, not an overwrite.
* `set_lifecycle` applies the rules in app/kb/lifecycle.py and, whenever the result is
  "not live", switches the item's passages off in the same transaction.
* `check_invariants` reports any disagreement between lifecycle and `is_live`.

The rules themselves (what may follow what) live in app/kb/lifecycle.py; this applies
them to the database.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.kb import classify, lifecycle
from app.services.source_service import SourceService, atomic_write

@dataclass(frozen=True)
class ItemRef:
    id: uuid.UUID
    number: int
    public_id: str
    version_id: uuid.UUID
    version_no: int


def _actor(actor_id: object) -> uuid.UUID | None:
    """The acting user's id as a UUID, or None for anything that is not one (a system action,
    or an auth layer whose ids are not UUIDs): history records who when it can, never fails for it."""
    if isinstance(actor_id, uuid.UUID):
        return actor_id
    try:
        return uuid.UUID(str(actor_id))
    except (ValueError, AttributeError, TypeError):
        return None


def new_public_id() -> str:
    return f"src_{uuid.uuid4().hex[:12]}"


def storage_key_for(sha256: str, kind: str) -> str:
    return f"kb/{sha256[:2]}/{sha256}.{kind}"


async def raise_number_sequence(session: AsyncSession, highest: int) -> None:
    """Move the item-number sequence up to at least `highest`; never down."""
    await session.execute(
        text(
            "SELECT setval('kb_item_number_seq', GREATEST(:highest, "
            "(SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM kb_item_number_seq)))"
        ),
        {"highest": max(highest, 1)},
    )


async def bump_epoch(session: AsyncSession) -> None:
    """Tell every worker's caches that what is retrievable may have changed."""
    await session.execute(update(kb.KbEpoch).where(kb.KbEpoch.id == 1).values(epoch=kb.KbEpoch.epoch + 1, updated_at=func.now()))


async def log_activity(session: AsyncSession, item_id: uuid.UUID, actor_id: uuid.UUID | None, action: str, **detail: object) -> None:
    actor_id = _actor(actor_id)
    await session.execute(
        insert(kb.KbActivity),
        [{"id": uuid.uuid4(), "item_id": item_id, "actor_id": actor_id, "action": action, "detail": detail}],
    )


async def move_category(session: AsyncSession, current: uuid.UUID | None, target: uuid.UUID | None, vector: list[float] | None) -> None:
    """Keep both categories' centroids (the mean vector of their items) right when an item
    moves from `current` to `target`."""
    if current == target:
        return
    if target is not None and await session.get(kb.KbCategory, target) is None:
        raise NotFoundError("Category not found")
    if vector is None:
        return
    for category_id, remove in ((current, True), (target, False)):
        if category_id is None:
            continue
        category = await session.get(kb.KbCategory, category_id)
        centroid = None if category.centroid is None else [float(x) for x in category.centroid]
        centroid, count = classify.update_centroid(centroid, category.centroid_count, vector, remove=remove)
        await session.execute(update(kb.KbCategory).where(kb.KbCategory.id == category_id).values(centroid=centroid, centroid_count=count))


async def attach_tags(session: AsyncSession, item_id: uuid.UUID, names: list[str], source: str) -> None:
    """Add tags to an item (creating any that do not exist; names match case-insensitively)."""
    for name in dict.fromkeys(" ".join(n.split()) for n in names):
        key = name.lower()
        if not name or len(name) > 64:
            continue
        tag_id = await session.scalar(select(kb.KbTag.id).where(kb.KbTag.name_key == key))
        if tag_id is None:
            tag_id = uuid.uuid4()
            await session.execute(insert(kb.KbTag), [{"id": tag_id, "name": name, "name_key": key}])
        exists = await session.scalar(select(kb.KbItemTag.tag_id).where(kb.KbItemTag.item_id == item_id, kb.KbItemTag.tag_id == tag_id))
        if exists is None:
            await session.execute(insert(kb.KbItemTag), [{"item_id": item_id, "tag_id": tag_id, "source": source}])


class KbService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._factory = session_factory
        self._validator = SourceService(settings=self.settings)

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    @property
    def data_dir(self) -> Path:
        return Path(self.settings.data_dir)

    # --- creating ----------------------------------------------------------------------
    def _store_file(self, filename: str, data: bytes) -> dict:
        """Validate and write one uploaded file; returns the `kb_sources` row to insert."""
        name = re.sub(r"[\x00-\x1f]", "", filename.replace("\\", "/").rsplit("/", 1)[-1]).strip()[:200] or "upload"
        kind = self._validator._validate_upload(name, data)  # noqa: SLF001 - the one validation rule set
        sha = hashlib.sha256(data).hexdigest()
        key = storage_key_for(sha, kind)
        atomic_write(self.data_dir / key, data)
        return {"sha256": sha, "filename": name, "kind": kind, "size_bytes": len(data), "storage_key": key}

    async def _attach_sources(self, session: AsyncSession, version_id: uuid.UUID, files: list[dict], start: int = 0) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        for offset, row in enumerate(files):
            existing = await session.scalar(select(kb.KbSource.id).where(kb.KbSource.sha256 == row["sha256"]))
            if existing is None:
                existing = uuid.uuid4()
                await session.execute(insert(kb.KbSource), [{"id": existing, **row}])
            ids.append(existing)
            await session.execute(insert(kb.KbVersionSource), [{"version_id": version_id, "source_id": existing, "position": start + offset}])
        return ids

    async def create_item(
        self,
        *,
        actor_id: uuid.UUID | None,
        files: list[tuple[str, bytes]] | None = None,
        body_md: str | None = None,
        title: str | None = None,
        description: str = "",
        category_id: uuid.UUID | None = None,
        allow_duplicate: bool = False,
    ) -> ItemRef:
        """A new draft item from one or more files and/or text written directly.

        Only the content is required. With no `title` the processing stage proposes one
        (until then the first file's name stands in, and `properties.title_auto` records
        that the admin never chose it)."""
        actor_id = _actor(actor_id)
        files = files or []
        body = (body_md or "").strip()
        if not files and not body:
            raise ValidationFailedError("Add a file or some text.")
        rows = [self._store_file(name, data) for name, data in files]
        if len({r["sha256"] for r in rows}) != len(rows):
            raise ValidationFailedError("The same file was added twice.")

        async with self.factory() as session, session.begin():
            if not allow_duplicate:
                for row in rows:
                    holder = await session.scalar(
                        select(kb.KbItem.public_id)
                        .join(kb.KbItemVersion, kb.KbItemVersion.item_id == kb.KbItem.id)
                        .join(kb.KbVersionSource, kb.KbVersionSource.version_id == kb.KbItemVersion.id)
                        .join(kb.KbSource, kb.KbSource.id == kb.KbVersionSource.source_id)
                        .where(kb.KbSource.sha256 == row["sha256"], kb.KbItem.deleted_at.is_(None), kb.KbItem.purged_at.is_(None))
                        .limit(1)
                    )
                    if holder is not None:
                        raise ConflictError(f"'{row['filename']}' is already in the knowledge base.", details={"public_id": holder})
            item_id, version_id = uuid.uuid4(), uuid.uuid4()
            chosen = " ".join((title or "").split())[:300]
            fallback = classify.suggest_title(rows[0]["filename"] if rows else "", [], body)
            number = await session.scalar(select(func.nextval("kb_item_number_seq")))
            await session.execute(
                insert(kb.KbItem),
                [{
                    "id": item_id, "number": number, "public_id": new_public_id(), "title": chosen or fallback,
                    "description": " ".join(description.split())[:2000], "lifecycle_status": "draft",
                    "origin": "upload" if rows else "manual", "category_id": category_id, "created_by": actor_id,
                    "properties": {} if chosen else {"title_auto": True},
                }],
            )
            await session.execute(
                insert(kb.KbItemVersion),
                [{
                    "id": version_id, "item_id": item_id, "version_no": 1, "processing_status": "queued",
                    "body_md": body or None, "content_hash": hashlib.sha256(body.encode()).hexdigest() if body else None,
                    "change_note": "Created", "created_by": actor_id,
                }],
            )
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item_id).values(latest_version_id=version_id))
            await self._attach_sources(session, version_id, rows)
            await self._enqueue(session, version_id)
            await log_activity(session, item_id, actor_id, "created", files=[r["filename"] for r in rows], typed=bool(body))
            public_id = await session.scalar(select(kb.KbItem.public_id).where(kb.KbItem.id == item_id))
            return ItemRef(item_id, number, public_id, version_id, 1)

    async def add_version(
        self,
        item_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        files: list[tuple[str, bytes]] | None = None,
        body_md: str | None = None,
        keep_previous_sources: bool = True,
        change_note: str = "",
    ) -> ItemRef:
        """A new version: the previous version's files (unless `keep_previous_sources` is
        false) plus any new ones, and the new text if given, else the previous text. The
        item keeps serving its live version until an admin publishes this one."""
        actor_id = _actor(actor_id)
        rows = [self._store_file(name, data) for name, data in (files or [])]
        async with self.factory() as session, session.begin():
            item = await self._lock(session, item_id)
            if item.purged_at is not None or item.deleted_at is not None:
                raise ConflictError("This item has been deleted.")
            previous = await session.get(kb.KbItemVersion, item.latest_version_id)
            if previous is not None and previous.processing_status not in ("ready", "failed"):
                raise ConflictError("The previous version is still being processed.")
            version_no = (await session.scalar(select(func.max(kb.KbItemVersion.version_no)).where(kb.KbItemVersion.item_id == item_id))) + 1
            body = body_md if body_md is not None else (previous.body_md if previous is not None else None)
            body = (body or "").strip() or None
            version_id = uuid.uuid4()
            await session.execute(
                insert(kb.KbItemVersion),
                [{
                    "id": version_id, "item_id": item_id, "version_no": version_no, "processing_status": "queued",
                    "body_md": body, "content_hash": hashlib.sha256(body.encode()).hexdigest() if body else None,
                    "change_note": change_note[:1000], "created_by": actor_id,
                }],
            )
            kept: list[uuid.UUID] = []
            if keep_previous_sources and previous is not None:
                kept = list((await session.scalars(
                    select(kb.KbVersionSource.source_id).where(kb.KbVersionSource.version_id == previous.id).order_by(kb.KbVersionSource.position)
                )).all())
                for position, source_id in enumerate(kept):
                    await session.execute(insert(kb.KbVersionSource), [{"version_id": version_id, "source_id": source_id, "position": position}])
            added = await self._attach_sources(session, version_id, rows, start=len(kept))
            if not kept and not added and not body:
                raise ValidationFailedError("A version needs at least a file or some text.")
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item_id).values(latest_version_id=version_id, updated_at=func.now()))
            await self._enqueue(session, version_id)
            await log_activity(session, item_id, actor_id, "version_added", version=version_no, files=[r["filename"] for r in rows])
            return ItemRef(item_id, item.number, item.public_id, version_id, version_no)

    async def _enqueue(self, session: AsyncSession, version_id: uuid.UUID) -> None:
        await session.execute(
            insert(kb.KbJob),
            [{"id": uuid.uuid4(), "version_id": version_id, "kind": "ingest", "status": "queued", "max_attempts": self.settings.kb_job_max_attempts}],
        )

    async def retry_version(self, version_id: uuid.UUID, *, actor_id: uuid.UUID | None) -> None:
        """An admin retries a failed or dead version: fresh job, attempts reset."""
        async with self.factory() as session, session.begin():
            version = await session.get(kb.KbItemVersion, version_id)
            if version is None:
                raise NotFoundError("Version not found")
            if version.processing_status != "failed":
                raise ConflictError("Only a failed version can be retried.")
            await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == version_id).values(processing_status="queued", processing_error=None))
            await session.execute(update(kb.KbJob).where(kb.KbJob.version_id == version_id, kb.KbJob.status.in_(("failed", "dead"))).values(status="queued", attempts=0, run_after=func.now(), last_error=None, locked_by=None, updated_at=func.now()))
            await log_activity(session, version.item_id, actor_id, "retry", version=version.version_no)

    # --- locking and state ---------------------------------------------------------------
    async def _lock(self, session: AsyncSession, item_id: uuid.UUID) -> kb.KbItem:
        item = (await session.execute(select(kb.KbItem).where(kb.KbItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None:
            raise NotFoundError("Knowledge item not found")
        return item

    @staticmethod
    def _state(item: kb.KbItem) -> lifecycle.ItemState:
        return lifecycle.ItemState(
            item.lifecycle_status,
            deleted=item.deleted_at is not None,
            purged=item.purged_at is not None,
            has_live_version=item.live_version_id is not None,
        )

    # --- publishing ------------------------------------------------------------------------
    async def publish_version(
        self,
        item_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        expected_live_version_id: uuid.UUID | None,
    ) -> str:
        """Make `version_id` the item's live version, atomically. Returns what happened:
        `noop`, `publish` (it was not live) or `switch` (it replaced another version)."""
        async with self.factory() as session, session.begin():
            item = await self._lock(session, item_id)
            version = await session.get(kb.KbItemVersion, version_id)
            if version is None:
                raise NotFoundError("Version not found")
            chunk_ids = set((await session.scalars(select(kb.KbVersionChunk.chunk_id).where(kb.KbVersionChunk.version_id == version_id))).all())
            result = lifecycle.check_publish_version(
                self._state(item),
                version_id=version_id,
                version_item_id=version.item_id,
                item_id=item_id,
                version_ready=version.processing_status == "ready",
                version_chunk_count=len(chunk_ids),
                live_version_id=item.live_version_id,
                expected_live_version_id=expected_live_version_id,
            )
            if result == "noop":
                return result
            live_now = set((await session.scalars(select(kb.KbChunk.id).where(kb.KbChunk.item_id == item_id, kb.KbChunk.is_live))).all())
            switch_off, switch_on = lifecycle.liveness_changes(live_now, chunk_ids)
            if switch_off:
                await session.execute(update(kb.KbChunk).where(kb.KbChunk.id.in_(switch_off)).values(is_live=False, retired_at=func.now()))
            if switch_on:
                await session.execute(update(kb.KbChunk).where(kb.KbChunk.id.in_(switch_on)).values(is_live=True, retired_at=None))
            if item.live_version_id is not None and item.live_version_id != version_id:
                await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == item.live_version_id, kb.KbItemVersion.live_until.is_(None)).values(live_until=func.now()))
            await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == version_id).values(live_from=func.now(), live_until=None))
            await session.execute(
                update(kb.KbItem).where(kb.KbItem.id == item_id).values(
                    live_version_id=version_id, lifecycle_status="published", needs_review=False,
                    published_at=func.coalesce(kb.KbItem.published_at, func.now()), updated_at=func.now(),
                )
            )
            await bump_epoch(session)
            await log_activity(session, item_id, actor_id, "published" if result == "publish" else "version_switched", version=version.version_no, previous=str(item.live_version_id) if item.live_version_id else None)
            return result

    async def publish_item(self, item_id: uuid.UUID, *, actor_id: uuid.UUID | None) -> str:
        """Publish an item as the admin sees it: its last live version if it had one, else
        its newest processed one. (The legacy on/off toggle maps here.)"""
        async with self.factory() as session:
            item = await session.get(kb.KbItem, item_id)
            if item is None:
                raise NotFoundError("Knowledge item not found")
            candidate = item.live_version_id
            if candidate is None:
                candidate = await session.scalar(
                    select(kb.KbItemVersion.id)
                    .where(kb.KbItemVersion.item_id == item_id, kb.KbItemVersion.processing_status == "ready")
                    .order_by(kb.KbItemVersion.version_no.desc()).limit(1)
                )
            if candidate is None:
                raise ConflictError("The item has no fully processed version to publish yet.")
            expected = item.live_version_id
        return await self.publish_version(item_id, candidate, actor_id=actor_id, expected_live_version_id=expected)

    async def set_lifecycle(self, item_id: uuid.UUID, action: lifecycle.Action, *, actor_id: uuid.UUID | None) -> kb.KbItem:
        """unpublish / archive / restore_archived / delete / restore_deleted. (Publishing goes
        through `publish_version`/`publish_item`; purge is not enabled yet.)"""
        if action == "publish":
            raise ValueError("publish goes through publish_version or publish_item")
        actor_id = _actor(actor_id)
        async with self.factory() as session, session.begin():
            item = await self._lock(session, item_id)
            outcome = lifecycle.apply(self._state(item), action, purge_allowed=False)  # purging is app/kb/purge.py's
            values: dict = {"lifecycle_status": outcome.lifecycle, "updated_at": func.now()}
            if action == "delete":
                values.update(deleted_at=func.now(), deleted_by=actor_id)
            elif action == "restore_deleted":
                values.update(deleted_at=None, deleted_by=None)
            elif action == "archive":
                values.update(archived_at=func.now())
            elif action == "restore_archived":
                values.update(archived_at=None)
            if not outcome.live:
                await session.execute(update(kb.KbChunk).where(kb.KbChunk.item_id == item_id, kb.KbChunk.is_live).values(is_live=False))
                if item.live_version_id is not None and item.lifecycle_status == "published":
                    await session.execute(
                        update(kb.KbItemVersion)
                        .where(kb.KbItemVersion.id == item.live_version_id, kb.KbItemVersion.live_until.is_(None))
                        .values(live_until=func.now())
                    )
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item_id).values(**values))
            await bump_epoch(session)
            await log_activity(session, item_id, actor_id, action, was=item.lifecycle_status)
        async with self.factory() as session:
            return await session.get(kb.KbItem, item_id)

    # --- admin edits that need no new version -------------------------------------------------
    async def update_item(
        self,
        item_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        title: str | None = None,
        description: str | None = None,
        properties: dict | None = None,
        category_id: uuid.UUID | None | str = "unchanged",
    ) -> None:
        """Metadata only: no new version, nothing re-embedded. Choosing a title takes it out
        of the automatic ones; moving category keeps both categories' centroids right."""
        async with self.factory() as session, session.begin():
            item = await self._lock(session, item_id)
            if item.purged_at is not None:
                raise ConflictError("This item has been permanently removed.")
            values: dict = {"updated_at": func.now()}
            merged = dict(item.properties or {})
            if title is not None:
                clean = " ".join(title.split())[:300]
                if not clean:
                    raise ValidationFailedError("The title cannot be empty.")
                values["title"] = clean
                merged.pop("title_auto", None)
            if description is not None:
                values["description"] = " ".join(description.split())[:2000]
            if properties is not None:
                merged = {**properties, **({"title_auto": True} if merged.get("title_auto") else {})}
            values["properties"] = merged
            if category_id != "unchanged":
                await self._move_category(session, item, category_id)
                values["category_id"] = category_id
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item_id).values(**values))
            await bump_epoch(session)  # a title, category or tag change alters filtered results and cached answers
            await log_activity(session, item_id, actor_id, "edited", fields=[k for k in ("title", "description", "properties", "category_id") if k in values])

    async def _move_category(self, session: AsyncSession, item: kb.KbItem, target: uuid.UUID | None) -> None:
        vector = None if item.centroid is None else [float(x) for x in item.centroid]
        await move_category(session, item.category_id, target, vector)

    # --- numbering ------------------------------------------------------------------------------
    async def ensure_number_sequence(self) -> int:
        """Make the next item number higher than every number already issued (live items, trashed
        ones and the stubs of purged ones). Run at startup: a restored backup can rewind a sequence,
        and a number must never be handed out twice. Never lowers the sequence. Returns the next number."""
        async with self.factory() as session, session.begin():
            highest = await session.scalar(select(func.coalesce(func.max(kb.KbItem.number), 0))) or 0
            await raise_number_sequence(session, highest)
            return highest + 1

    # --- integrity ----------------------------------------------------------------------------
    async def check_invariants(self) -> list[str]:
        """Disagreements between lifecycle and `is_live`. Empty means consistent."""
        problems: list[str] = []
        async with self.factory() as session:
            checks = {
                "passages are live although their item is not published, or is deleted": """
                    SELECT count(*) FROM kb_chunks c JOIN kb_items i ON i.id = c.item_id
                    WHERE c.is_live AND NOT (i.lifecycle_status = 'published' AND i.deleted_at IS NULL AND i.purged_at IS NULL)""",
                "passages are live although they are not in their item's live version": """
                    SELECT count(*) FROM kb_chunks c JOIN kb_items i ON i.id = c.item_id
                    WHERE c.is_live AND NOT EXISTS (SELECT 1 FROM kb_version_chunks vc WHERE vc.chunk_id = c.id AND vc.version_id = i.live_version_id)""",
                "passages of a published item's live version are not live": """
                    SELECT count(*) FROM kb_items i JOIN kb_version_chunks vc ON vc.version_id = i.live_version_id JOIN kb_chunks c ON c.id = vc.chunk_id
                    WHERE i.lifecycle_status = 'published' AND i.deleted_at IS NULL AND i.purged_at IS NULL AND NOT c.is_live""",
                "published items have no live version": """
                    SELECT count(*) FROM kb_items WHERE lifecycle_status = 'published' AND live_version_id IS NULL""",
            }
            for message, sql in checks.items():
                count = (await session.execute(text(sql))).scalar() or 0
                if count:
                    problems.append(f"{count} {message}")
        return problems
