"""Uploaded course template document data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
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
