"""Document data access operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, document: Document) -> Document:
        self.session.add(document)
        await self.session.flush()
        return document

    async def get_by_document_id(self, document_id: str) -> Document | None:
        return await self.session.scalar(select(Document).where(Document.document_id == document_id))

    async def update(self, document: Document) -> Document:
        await self.session.merge(document)
        await self.session.flush()
        return document

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self.session.get(Document, document_id)

    async def get_for_course(self, course_pk: uuid.UUID) -> Document | None:
        return await self.session.scalar(
            select(Document).where(Document.course_pk == course_pk)
        )