"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Response

from app.schemas.auth import AuthResponse, LoginRequest, RegisterRequest
from app.services.auth_service import AuthService, get_auth_service, to_user_response
from app.core.security import clear_auth_cookies, set_auth_cookies
from app.db.models import User
from app.api.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=AuthResponse)
async def register(
    request: RegisterRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    result = await service.register_user(request)
    set_auth_cookies(response, result.access_token, result.refresh_token)
    return result.response


@router.post("/login", response_model=AuthResponse)
async def login(
    request: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    result = await service.login_user(request)
    set_auth_cookies(response, result.access_token, result.refresh_token)
    return result.response


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    result = await service.refresh_user_session(refresh_token)
    set_auth_cookies(response, result.access_token, result.refresh_token)
    return result.response


@router.post("/logout")
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    service: AuthService = Depends(get_auth_service),
) -> dict[str, str]:
    await service.logout_user(refresh_token)
    clear_auth_cookies(response)
    return {"status": "ok"}


@router.get("/me", response_model=AuthResponse)
async def me(current_user: User = Depends(get_current_user)) -> AuthResponse:
    return AuthResponse(user=to_user_response(current_user))
