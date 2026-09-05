"""FastAPI dependency injection for database services."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from uuid import UUID

from fastapi import Cookie, Depends, Header

from app.core.config import get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.roles import Role
from app.core.security import verify_access_token
from app.db.models import User
from app.db.service import DatabaseService, get_database_service
from app.db.repositories.users import UserRepository
from app.db.session import get_session_factory


async def get_db_service() -> AsyncIterator[DatabaseService]:
    """Request-scoped DatabaseService, for routes that talk to Postgres directly."""
    async with get_database_service() as db:
        yield db


async def get_current_user(
    access_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> User:
    """Load the active user authenticated by the access-token cookie."""
    token = access_token or _bearer_token(authorization)
    if not token:
        raise UnauthorizedError("Authentication required")

    payload = verify_access_token(token)
    if payload is None:
        raise UnauthorizedError("Authentication required")

    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, TypeError, ValueError):
        raise UnauthorizedError("Authentication required") from None

    async with get_session_factory()() as session:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Authentication required")
        return user


async def get_current_user_if_db_enabled(
    access_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> User | None:
    """Like `get_current_user`, but only when Postgres-backed identity exists.

    Course ownership is a database concept (`courses.owner_id -> users.id`).
    With `USE_DATABASE=false` (the offline/filesystem mode used by the mock-AI
    pipeline test suite and DB-less local runs) there is no user table to
    authenticate against, so course/document routes keep their previous
    unauthenticated behaviour there and skip ownership checks entirely.
    """
    if not get_settings().use_database:
        return None
    return await get_current_user(access_token=access_token, authorization=authorization)


def require_roles(*allowed: Role) -> Callable[[User], Coroutine[None, None, User]]:
    """Build a dependency that requires the current user to hold one of `allowed`.

    Centralises role checks so individual routes never compare `user.role`
    strings inline - they just declare `Depends(require_roles(Role.MANAGER, Role.ADMIN))`.
    """

    async def dependency(current_user: User = Depends(get_current_user)) -> User:
        try:
            role = Role(current_user.role)
        except ValueError:
            role = None
        if role not in allowed:
            raise ForbiddenError("You do not have permission to perform this action")
        return current_user

    return dependency


get_current_admin = require_roles(Role.ADMIN)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token
