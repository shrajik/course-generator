"""Template data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, template: Template) -> Template:
        self.session.add(template)
        await self.session.flush()
        return template

    async def get_by_template_id(self, template_id: str) -> Template | None:
        return await self.session.scalar(
            select(Template).where(Template.template_id == template_id)
        )

    async def get_current_for_slug(self, slug: str) -> Template | None:
        return await self.session.scalar(
            select(Template).where(Template.slug == slug, Template.is_current.is_(True))
        )

    async def list_versions(self, slug: str) -> Sequence[Template]:
        result = await self.session.scalars(
            select(Template).where(Template.slug == slug).order_by(Template.version.desc())
        )
        return result.all()

    def _current_query(self, *, include_archived: bool = False) -> Select[tuple[Template]]:
        query = select(Template).where(Template.is_current.is_(True))
        if not include_archived:
            query = query.where(Template.is_archived.is_(False))
        return query

    async def list_current(self, *, include_archived: bool = False) -> Sequence[Template]:
        result = await self.session.scalars(
            self._current_query(include_archived=include_archived).order_by(Template.name)
        )
        return result.all()

    async def clear_current(self, slug: str) -> None:
        """Unset `is_current` on every existing version of `slug` before a
        new version takes over - keeps "exactly one current row per slug"
        an invariant the service enforces rather than the DB."""
        rows = await self.list_versions(slug)
        for row in rows:
            row.is_current = False
        if rows:
            await self.session.flush()

    async def update(self, template: Template) -> Template:
        await self.session.merge(template)
        await self.session.flush()
        return template

    async def get_by_id(self, pk: uuid.UUID) -> Template | None:
        return await self.session.get(Template, pk)

    async def list_all(self, limit: int = 200, offset: int = 0) -> Sequence[Template]:
        """Every version of every template, current or not - used by the
        embedding backfill script, which (unlike retrieval) needs to reach
        every row so each version gets its own independent embedding."""
        result = await self.session.scalars(
            select(Template).order_by(Template.created_at).limit(limit).offset(offset)
        )
        return result.all()

    async def count_all(self) -> int:
        return await self.session.scalar(select(func.count()).select_from(Template)) or 0
