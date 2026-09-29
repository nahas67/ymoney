"""Notifications API (Work 11 Lane L) -- contracts §10.

    GET  /workspaces/{workspace_id}/notifications          own rows only
    POST /workspaces/{workspace_id}/notifications/read-all
    POST /workspaces/{workspace_id}/notifications/{notification_id}/read

**Ownership is not a filter, it is the only query.** Every read and
write is scoped to ``user.id`` inside the dependency, so there is no
"id of somebody else's notification" surface at all: a foreign id
matches no row and answers 404, and no response ever contains another
user's inbox.

There is no route to CREATE a notification: inbox rows are written by
``services/notifications.py`` from ledger events (contracts §6 final
decision). The only verbs here are read + mark-read, which is the only
mutation an append-only inbox needs.

Error policy: short details; a generic 500 logged on ``ymoney.collab``
that echoes nothing back.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, Workspace
from app.services import notifications as notifications_service
from app.services.auth_service import get_current_user, require_workspace_role

notifications_router = APIRouter(
    prefix="/workspaces/{workspace_id}/notifications", tags=["notifications"]
)
logger = logging.getLogger("ymoney.collab")


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
            logger.exception(
                "notifications route failed: %s", getattr(fn, "__name__", fn)
            )
            raise HTTPException(status_code=500, detail="internal error") from None

    return run


@notifications_router.get("", summary="My notifications (newest first)")
@_guard
def list_my_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(
        default=notifications_service.DEFAULT_LIMIT,
        ge=1,
        le=notifications_service.MAX_LIMIT,
    ),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = notifications_service.list_notifications(
        db, ws.id, user.id, unread_only=unread_only, limit=limit
    )
    return {
        "items": items,
        "count": len(items),
        "unread": notifications_service.unread_count(db, ws.id, user.id),
        "limit": limit,
    }


# Declared BEFORE the {notification_id} route so the literal path can
# never be captured as an id (FastAPI matches in registration order).
@notifications_router.post("/read-all", summary="Mark every unread notification read")
@_guard
def read_all_notifications(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    updated = notifications_service.mark_read(db, ws.id, user.id, all=True)
    db.commit()
    return {"ok": True, "updated": updated,
            "unread": notifications_service.unread_count(db, ws.id, user.id)}


@notifications_router.post(
    "/{notification_id}/read", summary="Mark one notification read"
)
@_guard
def read_notification(
    notification_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # The filter includes user_id, so a foreign id is not "mine" and the
    # 404 below is the honest answer: it does not exist for this caller.
    # Marking an already-read notification is idempotent (200, 0 changed).
    if not notifications_service.notification_exists(db, ws.id, user.id, notification_id):
        raise HTTPException(status_code=404, detail="notification not found")
    updated = notifications_service.mark_read(db, ws.id, user.id, notification_id)
    db.commit()
    return {"ok": True, "updated": updated,
            "unread": notifications_service.unread_count(db, ws.id, user.id)}


__all__ = ["notifications_router"]
