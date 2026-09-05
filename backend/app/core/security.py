"""Authentication security helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash
from fastapi import Response

from app.core.config import get_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored password hash."""
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes should fail closed
        return False


def create_access_token(user_id: UUID | str, email: str, role: str) -> str:
    """Create a short-lived access token for an authenticated user."""
    settings = get_settings()
    return _create_token(
        secret=settings.jwt_secret,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        user_id=user_id,
        email=email,
        role=role,
    )


def create_refresh_token(user_id: UUID | str, email: str, role: str) -> str:
    """Create a refresh token signed with the refresh-token secret."""
    settings = get_settings()
    return _create_token(
        secret=settings.jwt_refresh_secret,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
        user_id=user_id,
        email=email,
        role=role,
    )


def verify_access_token(token: str) -> dict[str, Any] | None:
    """Verify an access token and return its claims, or None if invalid."""
    settings = get_settings()
    return _verify_token(token, settings.jwt_secret)


def verify_refresh_token(token: str) -> dict[str, Any] | None:
    """Verify a refresh token and return its claims, or None if invalid."""
    settings = get_settings()
    return _verify_token(token, settings.jwt_refresh_secret)


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Set HttpOnly auth cookies for the current response."""
    settings = get_settings()
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear HttpOnly auth cookies for the current response."""
    settings = get_settings()
    for cookie_name in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE):
        response.delete_cookie(
            key=cookie_name,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )


def _create_token(
    *,
    secret: str,
    expires_delta: timedelta,
    user_id: UUID | str,
    email: str,
    role: str,
) -> str:
    if not secret:
        raise RuntimeError("JWT secret is not configured")

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def _verify_token(token: str, secret: str) -> dict[str, Any] | None:
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        UUID(str(payload.get("sub")))
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None

    if not isinstance(payload.get("email"), str) or not isinstance(payload.get("role"), str):
        return None
    return payload
