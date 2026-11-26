"""Authentication service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.roles import DEFAULT_ROLE, Role
from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_reset_token,
    hash_password,
    hash_reset_token,
    verify_refresh_token,
    verify_password,
)
from app.db.models import AuthSession, PasswordResetToken, User
from app.db.repositories.users import UserRepository, normalize_email
from app.db.session import get_session_factory
from app.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UserResponse,
)
from app.services.notification_service import Mailer

# A short cooldown, not a security control - account enumeration is already
# prevented by `request_password_reset` always returning the same outcome
# regardless of whether the email exists (see that method's own docstring).
# This exists only to stop a user who double-clicks "resend" (or an
# automated retry) from getting several reset emails for one real request.
_PASSWORD_RESET_COOLDOWN_MINUTES = 2


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
            role=Role.ADMIN.value
            if email == normalize_email(settings.initial_admin_email)
            else DEFAULT_ROLE.value,
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

    async def request_password_reset(self, request: ForgotPasswordRequest) -> None:
        """Always completes the same way regardless of whether the email
        belongs to a real, active account - never raises, never returns a
        signal the caller could use to distinguish "sent" from "no such
        account". This is a deliberate anti-enumeration measure: nothing
        else in this codebase addresses account enumeration today, but a
        forgot-password endpoint is exactly the classic place that leaks
        it (a different response for a known vs. unknown email), so this
        is the one place it's worth being careful about from the start."""
        user = await self.users.get_by_email(request.email)
        if user is None or not user.is_active:
            return

        now = datetime.now(timezone.utc)
        cooldown_floor = now - timedelta(minutes=_PASSWORD_RESET_COOLDOWN_MINUTES)
        active_tokens = await self.users.list_active_password_reset_tokens(user.id)
        if any(token.created_at > cooldown_floor for token in active_tokens):
            return  # a request this recent is already in flight - don't re-send

        settings = get_settings()
        raw_token = generate_reset_token()
        await self.users.create_password_reset_token(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_reset_token(raw_token),
                expires_at=now + timedelta(minutes=settings.password_reset_token_expire_minutes),
                created_at=now,
            )
        )
        await self._send_reset_email(user, raw_token, settings=settings)

    async def reset_password(self, request: ResetPasswordRequest) -> None:
        reset_token = await self.users.get_unused_password_reset_token(hash_reset_token(request.token))
        now = datetime.now(timezone.utc)
        if reset_token is None or reset_token.expires_at <= now:
            raise UnauthorizedError("This reset link is invalid or has expired")

        user = await self.users.get_by_id(reset_token.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("This reset link is invalid or has expired")

        user.password_hash = hash_password(request.password)
        user.updated_at = now
        await self.users.update(user)
        # Single-use: marked BEFORE anything else could still fail, so a
        # retried/duplicate request after a real success can never succeed
        # twice. (See `get_unused_password_reset_token`'s own query - a
        # second attempt with the same token no longer finds this row.)
        await self.users.mark_password_reset_token_used(reset_token)
        # Security measure: resetting the password must invalidate every
        # existing session - if an attacker had a stolen session (or the
        # account owner is resetting because they suspect exactly that),
        # the reset itself has to end it, not just block future logins.
        for auth_session in await self.users.list_active_auth_sessions(user.id):
            await self.users.revoke_auth_session(auth_session)

    async def _send_reset_email(self, user: User, raw_token: str, *, settings: Settings) -> None:
        base = settings.public_app_url.rstrip("/")
        link = f"{base}/reset-password?token={raw_token}"
        await Mailer(settings).send(
            user.email,
            "Reset your password",
            "We received a request to reset your password. Click the link below to choose a new "
            f"one. This link expires in {settings.password_reset_token_expire_minutes} minutes and "
            f"can only be used once:\n\n{link}\n\n"
            "If you didn't request this, you can safely ignore this email - your password won't be "
            "changed.",
        )

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
