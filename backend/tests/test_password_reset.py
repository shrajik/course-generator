"""Forgot-password / reset-password flow - app.services.auth_service's
`request_password_reset`/`reset_password`, exercised over real HTTP.

Backed by the same TEST_DATABASE_URL-gated Postgres fixture pattern as
test_role_authorization.py.
"""

from __future__ import annotations

import os
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database(db_schema):
    os.environ["JWT_SECRET"] = "test-access-secret"
    os.environ["JWT_REFRESH_SECRET"] = "test-refresh-secret"
    reset_settings_cache()


@pytest.fixture
def reset_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("USE_DATABASE", "true")
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://app.example.com")
    reset_settings_cache()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_settings_cache()


@pytest.fixture
def captured_emails(monkeypatch):
    """Intercepts every outgoing email at the `Mailer.send` boundary - the
    same patch point `test_review_screen.py`'s own `FakeMailer` subclass
    targets, just via monkeypatch instead of a subclass since AuthService
    constructs its own `Mailer` internally rather than accepting one via
    dependency injection."""
    sent: list[tuple[str, str, str]] = []
    from app.services import notification_service

    async def fake_send(self, to: str, subject: str, body: str) -> bool:
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(notification_service.Mailer, "send", fake_send)
    return sent


def _email(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}@example.com"


def _register(client, email: str, password: str = "CourseTest123!"):
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["user"]


def _extract_token(body: str) -> str:
    match = re.search(r"reset-password\?token=([^\s]+)", body)
    assert match, f"no reset link found in email body: {body!r}"
    return match.group(1)


def test_forgot_password_for_an_unknown_email_still_returns_202(reset_client, captured_emails):
    """Anti-enumeration: an email with no account must get the exact same
    response as one that exists - see AuthService.request_password_reset's
    own docstring."""
    response = reset_client.post("/auth/forgot-password", json={"email": _email("missing")})
    assert response.status_code == 202
    assert captured_emails == []


def test_forgot_password_sends_a_working_single_use_reset_link(reset_client, captured_emails):
    email = _email("reset-flow")
    _register(reset_client, email, "OldPassword123!")

    response = reset_client.post("/auth/forgot-password", json={"email": email})
    assert response.status_code == 202
    assert len(captured_emails) == 1
    to, subject, body = captured_emails[0]
    assert to == email
    assert "reset" in subject.lower()
    token = _extract_token(body)

    reset_response = reset_client.post(
        "/auth/reset-password", json={"token": token, "password": "NewPassword456!"}
    )
    assert reset_response.status_code == 200, reset_response.text

    # The old password no longer works; the new one does.
    old_login = reset_client.post("/auth/login", json={"email": email, "password": "OldPassword123!"})
    assert old_login.status_code == 401
    new_login = reset_client.post("/auth/login", json={"email": email, "password": "NewPassword456!"})
    assert new_login.status_code == 200


def test_reset_password_token_is_single_use(reset_client, captured_emails):
    email = _email("single-use")
    _register(reset_client, email, "OldPassword123!")
    reset_client.post("/auth/forgot-password", json={"email": email})
    token = _extract_token(captured_emails[0][2])

    first = reset_client.post("/auth/reset-password", json={"token": token, "password": "NewPassword456!"})
    assert first.status_code == 200

    second = reset_client.post("/auth/reset-password", json={"token": token, "password": "AnotherPassword789!"})
    assert second.status_code == 401


def test_reset_password_rejects_a_garbage_token(reset_client):
    response = reset_client.post(
        "/auth/reset-password", json={"token": "not-a-real-token", "password": "NewPassword456!"}
    )
    assert response.status_code == 401


def test_reset_password_revokes_existing_sessions(reset_client, captured_emails):
    """A password reset must end any session that existed before it -
    otherwise a stolen session survives the very action meant to lock it
    out (see AuthService.reset_password's own comment)."""
    email = _email("session-revoke")
    _register(reset_client, email, "OldPassword123!")  # this login's cookies are still set on reset_client

    reset_client.post("/auth/forgot-password", json={"email": email})
    token = _extract_token(captured_emails[0][2])
    reset_client.post("/auth/reset-password", json={"token": token, "password": "NewPassword456!"})

    # The refresh cookie from the pre-reset session must no longer work.
    refresh_response = reset_client.post("/auth/refresh")
    assert refresh_response.status_code == 401


def test_forgot_password_cooldown_prevents_a_second_email_within_the_window(reset_client, captured_emails):
    email = _email("cooldown")
    _register(reset_client, email, "OldPassword123!")

    reset_client.post("/auth/forgot-password", json={"email": email})
    reset_client.post("/auth/forgot-password", json={"email": email})

    assert len(captured_emails) == 1
