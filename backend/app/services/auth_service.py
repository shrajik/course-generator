"""Authentication service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_refresh_token,
    verify_password,
)
from app.db.models import AuthSession, User
from app.db.repositories.users import UserRepository, normalize_email
from app.db.session import get_session_factory
from app.schemas.auth import AuthResponse, LoginRequest, RegisterRequest, UserResponse


@dataclass(frozen=True)
class AuthResult:
    response: AuthResponse
    access_token: str
    refresh_token: str


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def register_user(self, request: RegisterRequest) -> AuthResult:
        email = normalize_email(request.email)
        if await self.users.get_by_email(email):
            raise ConflictError("A user with this email already exists")

        settings = get_settings()
        now = datetime.now(timezone.utc)
        user = User(
            email=email,
            password_hash=hash_password(request.password),
            role="admin" if email == normalize_email(settings.initial_admin_email) else "user",
            is_active=True,
            is_verified=False,
            created_at=now,
            updated_at=now,
        )
        user = await self.users.create(user)
        return await self._authenticated_response(user)

    async def login_user(self, request: LoginRequest) -> AuthResult:
        user = await self.users.get_by_email(request.email)
        if user is None or not verify_password(request.password, user.password_hash):
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        return await self._authenticated_response(user)

    async def refresh_user_session(self, refresh_token: str | None) -> AuthResult:
        if not refresh_token:
            raise UnauthorizedError("Authentication required")

        payload = verify_refresh_token(refresh_token)
        if payload is None:
            raise UnauthorizedError("Invalid refresh token")

        try:
            user_id = UUID(str(payload["sub"]))
        except (KeyError, TypeError, ValueError):
            raise UnauthorizedError("Invalid refresh token") from None

        user = await self.users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Invalid refresh token")

        auth_session = await self._find_active_refresh_session(user.id, refresh_token)
        if auth_session is None:
            raise UnauthorizedError("Invalid refresh token")

        now = datetime.now(timezone.utc)
        if auth_session.expires_at <= now:
            await self.users.revoke_auth_session(auth_session)
            raise UnauthorizedError("Invalid refresh token")

        await self.users.revoke_auth_session(auth_session)
        return await self._authenticated_response(user)

    async def logout_user(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return

        payload = verify_refresh_token(refresh_token)
        if payload is None:
            return

        try:
            user_id = UUID(str(payload["sub"]))
        except (KeyError, TypeError, ValueError):
            return

        auth_session = await self._find_active_refresh_session(user_id, refresh_token)
        if auth_session is not None:
            await self.users.revoke_auth_session(auth_session)

    async def _authenticated_response(self, user: User) -> AuthResult:
        settings = get_settings()
        access_token = create_access_token(user.id, user.email, user.role)
        refresh_token = create_refresh_token(user.id, user.email, user.role)
        now = datetime.now(timezone.utc)
        await self.users.create_auth_session(
            AuthSession(
                user_id=user.id,
                refresh_token_hash=hash_password(refresh_token),
                expires_at=now + timedelta(days=settings.refresh_token_expire_days),
                created_at=now,
            )
        )
        return AuthResult(
            response=AuthResponse(
                user=to_user_response(user),
            ),
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def _find_active_refresh_session(
        self, user_id: UUID, refresh_token: str
    ) -> AuthSession | None:
        for auth_session in await self.users.list_active_auth_sessions(user_id):
            if verify_password(refresh_token, auth_session.refresh_token_hash):
                return auth_session
        return None


def to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def get_auth_service() -> AsyncIterator[AuthService]:
    async with get_session_factory()() as session:
        async with session.begin():
            yield AuthService(session)
