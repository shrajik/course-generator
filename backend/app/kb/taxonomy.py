"""How admins organise knowledge: categories, tags and relationships between items.

Categories are the admin's own tree, independent of course generation. Tags are free
labels, case-insensitive. Relationships are typed edges between items; storing them is
all that happens here - retrieval's use of them is in app/services/knowledge_pg.py.

Every operation keeps the structure sound: a category cannot become its own ancestor,
the same slug cannot appear twice among siblings, an item cannot supersede itself or
(through a chain) its own ancestor.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.kb.service import _actor, attach_tags, bump_epoch, log_activity

_UNCHANGED = object()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:100] or "category"


class Taxonomy:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._factory = session_factory

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    # --- categories ------------------------------------------------------------------------
    async def create_category(self, name: str, *, parent_id: uuid.UUID | None = None, description: str = "") -> uuid.UUID:
        name = " ".join(name.split())[:200]
        if not name:
            raise ValidationFailedError("A category needs a name.")
        category_id = uuid.uuid4()
        async with self.factory() as session:
            try:
                async with session.begin():
                    if parent_id is not None and await session.get(kb.KbCategory, parent_id) is None:
                        raise NotFoundError("Parent category not found")
                    await session.execute(
                        insert(kb.KbCategory),
                        [{"id": category_id, "parent_id": parent_id, "name": name, "slug": slugify(name), "description": description[:1000]}],
                    )
            except IntegrityError as exc:
                raise ConflictError(f"There is already a category called '{name}' here.") from exc
        return category_id

    async def update_category(
        self,
        category_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        parent_id: object = _UNCHANGED,
        approve: bool = False,
    ) -> None:
        values: dict = {"updated_at": func.now()}
        if name is not None:
            clean = " ".join(name.split())[:200]
            if not clean:
                raise ValidationFailedError("A category needs a name.")
            values.update(name=clean, slug=slugify(clean))
        if description is not None:
            values["description"] = description[:1000]
        if approve:
            values["is_suggested"] = False
        async with self.factory() as session:
            try:
                async with session.begin():
                    if await session.get(kb.KbCategory, category_id) is None:
                        raise NotFoundError("Category not found")
                    if parent_id is not _UNCHANGED:
                        if parent_id is not None and parent_id in await self._descendants(session, category_id, include_self=True):
                            raise ConflictError("A category cannot be moved under itself.")
                        values["parent_id"] = parent_id
                    await session.execute(update(kb.KbCategory).where(kb.KbCategory.id == category_id).values(**values))
            except IntegrityError as exc:
                raise ConflictError("A sibling category already has that name.") from exc

    async def archive_category(self, category_id: uuid.UUID) -> None:
        """Hide a category. Its items keep it until an admin moves them; it must have no live sub-categories."""
        async with self.factory() as session, session.begin():
            if await session.get(kb.KbCategory, category_id) is None:
                raise NotFoundError("Category not found")
            children = await session.scalar(select(func.count()).select_from(kb.KbCategory).where(kb.KbCategory.parent_id == category_id, kb.KbCategory.archived_at.is_(None)))
            if children:
                raise ConflictError("Archive or move the sub-categories first.")
            await session.execute(update(kb.KbCategory).where(kb.KbCategory.id == category_id).values(archived_at=func.now()))

    @staticmethod
    async def _descendants(session: AsyncSession, category_id: uuid.UUID, *, include_self: bool) -> set[uuid.UUID]:
        rows = await session.execute(
            text(
                "WITH RECURSIVE tree AS (SELECT id FROM kb_categories WHERE id = :id "
                "UNION SELECT c.id FROM kb_categories c JOIN tree t ON c.parent_id = t.id) SELECT id FROM tree"
            ),
            {"id": category_id},
        )
        found = {row.id for row in rows}
        return found if include_self else found - {category_id}

    # --- tags ---------------------------------------------------------------------------------
    async def set_tags(self, item_id: uuid.UUID, names: list[str], *, actor_id: object = None) -> None:
        """Replace an item's tags with `names` (an admin's choice, so recorded as theirs)."""
        async with self.factory() as session, session.begin():
            if await session.get(kb.KbItem, item_id) is None:
                raise NotFoundError("Knowledge item not found")
            await session.execute(delete(kb.KbItemTag).where(kb.KbItemTag.item_id == item_id))
            await attach_tags(session, item_id, names, "admin")
            # Tags the classifier proposed and the admin replaced must not linger as empty labels.
            await session.execute(delete(kb.KbTag).where(~select(kb.KbItemTag.tag_id).where(kb.KbItemTag.tag_id == kb.KbTag.id).exists()))
            await bump_epoch(session)
            await log_activity(session, item_id, _actor(actor_id), "tags_changed", tags=names)

    # --- relationships ----------------------------------------------------------------------------
    async def add_relation(self, from_item: uuid.UUID, to_item: uuid.UUID, relation_type: str, *, note: str = "", actor_id: object = None, source: str = "admin") -> uuid.UUID:
        if relation_type not in kb.RELATION_TYPES:
            raise ValidationFailedError(f"Unknown relationship '{relation_type}'.")
        if from_item == to_item:
            raise ValidationFailedError("An item cannot be related to itself.")
        relation_id = uuid.uuid4()
        async with self.factory() as session:
            try:
                async with session.begin():
                    for item_id in (from_item, to_item):
                        item = await session.get(kb.KbItem, item_id)
                        if item is None or item.purged_at is not None:
                            raise NotFoundError("Knowledge item not found")
                    if relation_type == "supersedes" and await self._supersedes_chain_reaches(session, to_item, from_item):
                        raise ConflictError("That would make an item supersede itself through a chain.")
                    await session.execute(
                        insert(kb.KbRelation),
                        [{"id": relation_id, "from_item_id": from_item, "to_item_id": to_item, "relation_type": relation_type,
                          "note": note[:1000], "source": source, "created_by": _actor(actor_id)}],
                    )
                    await bump_epoch(session)  # supersedes / duplicate_of change what retrieval returns
                    await log_activity(session, from_item, _actor(actor_id), "relation_added", type=relation_type, to=str(to_item))
            except IntegrityError as exc:
                raise ConflictError("That relationship already exists.") from exc
        return relation_id

    async def remove_relation(self, relation_id: uuid.UUID, *, actor_id: object = None) -> None:
        async with self.factory() as session, session.begin():
            relation = await session.get(kb.KbRelation, relation_id)
            if relation is None:
                raise NotFoundError("Relationship not found")
            await session.execute(delete(kb.KbRelation).where(kb.KbRelation.id == relation_id))
            await bump_epoch(session)
            await log_activity(session, relation.from_item_id, _actor(actor_id), "relation_removed", type=relation.relation_type, to=str(relation.to_item_id))

    @staticmethod
    async def _supersedes_chain_reaches(session: AsyncSession, start: uuid.UUID, target: uuid.UUID) -> bool:
        """Whether following 'supersedes' edges from `start` reaches `target`."""
        row = await session.execute(
            text(
                "WITH RECURSIVE chain AS (SELECT to_item_id AS id FROM kb_relations WHERE from_item_id = :start AND relation_type = 'supersedes' "
                "UNION SELECT r.to_item_id FROM kb_relations r JOIN chain c ON r.from_item_id = c.id WHERE r.relation_type = 'supersedes') "
                "SELECT 1 FROM chain WHERE id = :target LIMIT 1"
            ),
            {"start": start, "target": target},
        )
        return row.first() is not None
