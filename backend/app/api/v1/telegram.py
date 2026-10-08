"""Telegram integration management endpoints.

The bot token itself is managed through Settings → Connections
(provider_settings key telegram.bot_token); these endpoints handle pairing,
link status and control-plane tests, all scoped by workspace role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db import get_db
from app.models import TelegramLink, Workspace
from app.services import telegram_service as tg
from app.services.auth_service import get_current_user, require_workspace_role

router = APIRouter(prefix="/workspaces/{workspace_id}/telegram", tags=["telegram"])


def _viewer():
    return Depends(require_workspace_role("viewer"))


def _admin():
    return Depends(require_workspace_role("admin"))


def _link_dict(link: TelegramLink) -> dict:
    return {
        "id": link.id,
        "chat_id": link.chat_id,
        "chat_title": link.chat_title,
        "active": link.active,
        "settings": link.settings_json or {},
        "linked_at": link.created_at.isoformat() + "Z" if link.created_at else None,
    }


@router.get("/status", summary="Telegram integration status for this workspace")
def status(
    ws: Workspace = _viewer(),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    token, token_src = tg.get_bot_token()
    links = db.scalars(
        select(TelegramLink).where(TelegramLink.workspace_id == ws.id)
    ).all()
    return {
        "bot_configured": bool(token),
        "token_source": token_src,
        "links": [_link_dict(l) for l in links],
        "linked": any(l.active for l in links),
    }


@router.post("/pairing-code", summary="Generate a one-time pairing code (15 min TTL)")
def pairing_code(
    ws: Workspace = _admin(),
    user=Depends(get_current_user),
):
    return tg.create_pairing_code(ws.id, user.id)


@router.delete("/links/{link_id}", summary="Unlink a chat")
def unlink(link_id: str, ws: Workspace = _admin(), db=Depends(get_db)):
    link = db.get(TelegramLink, link_id)
    if not link or link.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="link not found")
    db.delete(link)
    return {"unlinked": True}


@router.post("/links/{link_id}/toggle", summary="Enable/disable a chat link")
def toggle(link_id: str, ws: Workspace = _admin(), db=Depends(get_db)):
    link = db.get(TelegramLink, link_id)
    if not link or link.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="link not found")
    link.active = not link.active
    db.commit()
    return {"id": link.id, "active": link.active}


class TestBody(BaseModel):
    message: str = ""


class TelegramTestOut(BaseModel):
    """Result of `POST /telegram/test`, declared from the handler source.

    The success shape is `{"sent": <chat count>}` (`test_send` returns it
    directly). This contract is hand-written rather than observed because
    observing it sends a REAL Telegram message -- a live external side effect
    the contract harness must never trigger. A reviewed declaration beats an
    unobservable inference; inventing coverage by firing the route would be
    the fabrication.
    """

    sent: int


@router.post("/test", summary="Send a test message to all linked chats", response_model=TelegramTestOut)
async def test_send(body: TestBody, ws: Workspace = _admin()):
    if not tg.bot_configured():
        raise HTTPException(
            status_code=409,
            detail="bot token not configured — add it in Settings → Connections (key: telegram.bot_token)",
        )
    sent = tg.notify_workspaces(
        ws.id,
        body.message or "✅ <b>YMONEY</b> test message — Telegram remote control is live.",
    )
    if sent == 0:
        raise HTTPException(status_code=409, detail="no active linked chats — pair one first")
    return {"sent": sent}
