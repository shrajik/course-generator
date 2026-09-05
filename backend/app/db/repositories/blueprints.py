"""Blueprint data access operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Blueprint


class BlueprintRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, blueprint: Blueprint) -> Blueprint:
        self.session.add(blueprint)
        await self.session.flush()
        return blueprint

    async def get_for_course(self, course_pk: object) -> Blueprint | None:
        return await self.session.scalar(select(Blueprint).where(Blueprint.course_pk == course_pk))

    async def update(self, blueprint: Blueprint) -> Blueprint:
        await self.session.merge(blueprint)
        await self.session.flush()
        return blueprint