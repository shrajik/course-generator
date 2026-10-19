"""The signed-in user's notification inbox."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.dependencies import get_current_user_if_db_enabled
from app.core.errors import NotFoundError
from app.db.models import User
from app.services.notification_service import (
    Notification,
    NotificationService,
    get_notification_service,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationList(BaseModel):
    notifications: list[Notification]
    unread: int


def _service() -> NotificationService:
    return get_notification_service()


def _owner(user: User | None) -> str:
    # Without a user table there is one anonymous inbox, like the rest of that mode.
    return str(user.id) if user is not None else "local"


@router.get("", response_model=NotificationList)
async def list_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    service: NotificationService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> NotificationList:
    items, unread = service.list_for(_owner(user), limit)
    return NotificationList(notifications=items, unread=unread)


@router.post("/read-all")
async def read_all(
    service: NotificationService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> dict[str, int]:
    return {"marked": service.mark_all_read(_owner(user))}


@router.post("/{notification_id}/read", status_code=204)
async def read_one(
    notification_id: str,
    service: NotificationService = Depends(_service),
    user: User | None = Depends(get_current_user_if_db_enabled),
) -> None:
    if not service.mark_read(_owner(user), notification_id):
        raise NotFoundError("Notification not found")
