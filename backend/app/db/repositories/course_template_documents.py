"""Uploaded course template document data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CourseTemplateDocument


class CourseTemplateDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, document: CourseTemplateDocument) -> CourseTemplateDocument:
        self.session.add(document)
        await self.session.flush()
        return document

    async def get_by_id(self, pk: uuid.UUID) -> CourseTemplateDocument | None:
        return await self.session.get(CourseTemplateDocument, pk)

    async def list_active(self) -> Sequence[CourseTemplateDocument]:
        result = await self.session.scalars(
            select(CourseTemplateDocument)
            .where(CourseTemplateDocument.is_active.is_(True))
            .order_by(CourseTemplateDocument.created_at.desc())
        )
        return result.all()

    async def list_visible(
        self, *, user_id: uuid.UUID | None, is_admin: bool
    ) -> Sequence[CourseTemplateDocument]:
        """Active templates this user may see: shared ones, their own, and
        (for an admin) everyone's."""
        query = select(CourseTemplateDocument).where(CourseTemplateDocument.is_active.is_(True))
        if not is_admin:
            conditions = [CourseTemplateDocument.visibility == "shared"]
            if user_id is not None:
                conditions.append(CourseTemplateDocument.created_by == user_id)
            query = query.where(or_(*conditions))
        result = await self.session.scalars(query.order_by(CourseTemplateDocument.created_at.desc()))
        return result.all()

    async def update(self, document: CourseTemplateDocument) -> CourseTemplateDocument:
        await self.session.merge(document)
        await self.session.flush()
        return document
