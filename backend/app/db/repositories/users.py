"""User data access operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        normalized = normalize_email(email)
        return await self.session.scalar(select(User).where(User.email == normalized))

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    def _filtered_query(
        self,
        *,
        search: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
    ) -> Select[tuple[User]]:
        query = select(User)
        if search:
            term = search.strip()
            if term:
                clauses = [User.email.ilike(f"%{term}%")]
                try:
                    clauses.append(User.id == uuid.UUID(term))
                except ValueError:
                    pass
                query = query.where(or_(*clauses))
        if role:
            query = query.where(User.role == role)
        if is_active is not None:
            query = query.where(User.is_active.is_(is_active))
        if is_verified is not None:
            query = query.where(User.is_verified.is_(is_verified))
        return query

    async def count(
        self,
        *,
        search: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
    ) -> int:
        query = self._filtered_query(
            search=search,
            role=role,
            is_active=is_active,
            is_verified=is_verified,
        ).subquery()
        return await self.session.scalar(select(func.count()).select_from(query)) or 0

    async def list(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        search: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
    ) -> Sequence[User]:
        result = await self.session.scalars(
            self._filtered_query(
                search=search,
                role=role,
                is_active=is_active,
                is_verified=is_verified,
            )
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()

    async def create(self, user: User) -> User:
        user.email = normalize_email(user.email)
        self.session.add(user)
        await self.session.flush()
        return user

    async def update(self, user: User) -> User:
        await self.session.merge(user)
        await self.session.flush()
        return user

    async def create_auth_session(self, auth_session: AuthSession) -> AuthSession:
        self.session.add(auth_session)
        await self.session.flush()
        return auth_session

    async def list_active_auth_sessions(self, user_id: uuid.UUID) -> Sequence[AuthSession]:
        result = await self.session.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        )
        return result.all()

    async def revoke_auth_session(self, auth_session: AuthSession) -> AuthSession:
        auth_session.revoked_at = datetime.now(timezone.utc)
        await self.session.flush()
        return auth_session
