"""Notifications: an in-app inbox per user, plus an optional email.

HLD "Notification Service": generation completion, review requests, approval
decisions and failures reach the people concerned, and delivery is decoupled
from the workflow - a failed notification must never fail the action that
caused it, so every public method here swallows and logs its own errors.

Each user's inbox is one JSON file under `data/notifications/`, like the rest
of file storage. Email goes out over SMTP only when a host is configured; with
none it is simply skipped, and the inbox is still written.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import smtplib
import tempfile
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.core.ids import new_id, utc_now_iso
from app.core.logging import get_logger

log = get_logger(__name__)

NotificationKind = Literal[
    "generation_done", "review_requested", "course_approved", "changes_requested"
]
MAX_PER_USER = 200
_USER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_lock = threading.Lock()


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: str
    kind: NotificationKind
    title: str
    message: str = ""
    course_id: str = ""
    # An app path the notification opens, e.g. /review/doc_123.
    link: str = ""
    created_at: str
    read: bool = False


@dataclass(frozen=True)
class Recipient:
    user_id: str
    email: str | None = None


class Mailer:
    """Sends a plain-text email over SMTP, or does nothing when none is configured."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.smtp_host.strip())

    def _send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.smtp_from or self.settings.smtp_user
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as server:
            if self.settings.smtp_use_tls:
                server.starttls()
            if self.settings.smtp_user:
                server.login(self.settings.smtp_user, self.settings.smtp_password)
            server.send_message(message)

    async def send(self, to: str, subject: str, body: str) -> bool:
        if not self.enabled or not to:
            return False
        try:
            await asyncio.to_thread(self._send, to, subject, body)
            return True
        except Exception as exc:  # noqa: BLE001 - email trouble never blocks the workflow
            log.warning("Email to %s failed: %s", to, exc)
            return False


class NotificationService:
    def __init__(self, settings: Settings | None = None, mailer: Mailer | None = None) -> None:
        self.settings = settings or get_settings()
        self.mailer = mailer or Mailer(self.settings)

    # --- the inbox ------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.settings.data_dir) / "notifications"

    def _path(self, user_id: str) -> Path | None:
        return self.root / f"{user_id}.json" if _USER_RE.match(user_id or "") else None

    def _read(self, user_id: str) -> list[Notification]:
        path = self._path(user_id)
        if path is None or not path.exists():
            return []
        try:
            return [Notification.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
        except (ValueError, OSError):
            log.warning("Unreadable notification inbox for %s", user_id)
            return []

    def _write(self, user_id: str, items: list[Notification]) -> None:
        path = self._path(user_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump([item.model_dump() for item in items[:MAX_PER_USER]], file)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def add(
        self,
        user_id: str,
        kind: NotificationKind,
        title: str,
        message: str = "",
        *,
        course_id: str = "",
        link: str = "",
    ) -> Notification | None:
        item = Notification(
            notification_id=new_id("ntf"), kind=kind, title=title, message=message,
            course_id=course_id, link=link, created_at=utc_now_iso(),
        )
        try:
            with _lock:
                self._write(user_id, [item, *self._read(user_id)])
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store a notification for %s: %s", user_id, exc)
            return None
        return item

    def list_for(self, user_id: str, limit: int = 50) -> tuple[list[Notification], int]:
        items = self._read(user_id)
        return items[:limit], sum(1 for item in items if not item.read)

    def mark_read(self, user_id: str, notification_id: str) -> bool:
        with _lock:
            items = self._read(user_id)
            found = False
            for item in items:
                if item.notification_id == notification_id and not item.read:
                    item.read, found = True, True
            if found:
                self._write(user_id, items)
        return found

    def mark_all_read(self, user_id: str) -> int:
        with _lock:
            items = self._read(user_id)
            changed = sum(1 for item in items if not item.read)
            for item in items:
                item.read = True
            if changed:
                self._write(user_id, items)
        return changed

    # --- who to tell ----------------------------------------------------------
    async def recipient(self, user_id: str | None) -> Recipient | None:
        """A user, with their email when the user table can supply it."""
        if not user_id:
            return None
        email: str | None = None
        if self.settings.use_database:
            try:
                import uuid

                from app.db.repositories.users import UserRepository
                from app.db.session import get_session_factory

                async with get_session_factory()() as session:
                    user = await UserRepository(session).get_by_id(uuid.UUID(user_id))
                    email = user.email if user is not None else None
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not look up user %s: %s", user_id, exc)
        return Recipient(user_id, email)

    async def reviewers(self) -> list[Recipient]:
        """Everyone who can review a submitted course. None without a user table."""
        if not self.settings.use_database:
            return []
        try:
            from app.core.roles import Role
            from app.db.repositories.users import UserRepository
            from app.db.session import get_session_factory

            async with get_session_factory()() as session:
                users = await UserRepository(session).list(
                    limit=500, role=Role.EDITOR_REVIEWER.value, is_active=True
                )
                return [Recipient(str(user.id), user.email) for user in users]
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not look up reviewers: %s", exc)
            return []

    # --- telling them ---------------------------------------------------------
    async def notify(
        self,
        recipients: list[Recipient],
        kind: NotificationKind,
        title: str,
        message: str = "",
        *,
        course_id: str = "",
        link: str = "",
    ) -> int:
        """Add to each inbox and email where possible. Never raises."""
        if not self.settings.notifications_enabled:
            return 0
        delivered = 0
        for person in recipients:
            try:
                if self.add(person.user_id, kind, title, message, course_id=course_id, link=link):
                    delivered += 1
                if person.email:
                    base = self.settings.public_app_url.rstrip("/")
                    where = f"\n\nOpen: {base}{link}" if base and link else ""
                    await self.mailer.send(person.email, title, f"{message}{where}".strip())
            except Exception as exc:  # noqa: BLE001
                log.warning("Notification to %s failed: %s", person.user_id, exc)
        return delivered


_service: NotificationService | None = None


def get_notification_service() -> NotificationService:
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


def reset_notification_service() -> None:
    global _service
    _service = None
