"""Integration/remote-control link models (e.g. Telegram)."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class TelegramLink(Base, PKMixin, TimestampMixin):
    """A Telegram chat linked to a workspace for remote control + alerts.

    Pairing: the UI generates a short-lived one-time code; the user sends
    /start <code> to the YMONEY bot in Telegram; the poller exchanges it for
    a permanent chat binding. One link per chat; many chats per workspace.
    """

    __tablename__ = "telegram_links"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    # Optional thread/topic inside the chat (supergroup topics)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_title: Mapped[str] = mapped_column(String(200), default="")
    linked_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-link notification filters (JSON): {"events": bool, "daily_digest": bool}
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)  # type: ignore[assignment]
