"""Creative Director audit rows (Work 08 Lane B): NL → typed commands.

One row per parse / preview / apply / rejection, append-only:
  * ``commands_json``  — the typed CreativeCommand dicts (the payload audited),
  * ``change_set_json`` — preview diff (change lines, estimates, manifest hash),
  * ``status``         — parsed | previewed | applied | rejected | undone | stale,
  * ``parent_version`` — the timeline version this action was based on, so a
    later apply can detect that the timeline moved (stale preview → 409),
  * ``result_json``    — apply result (previous/new timeline id, version, hash)
    which is what ``undo`` uses to restore via the parent-pointer system.

Nothing here executes: rows are an immutable audit trail of inert data.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# parsed | previewed | applied | rejected | undone | stale
CREATIVE_STATUSES: tuple[str, ...] = (
    "parsed", "previewed", "applied", "rejected", "undone", "stale",
)


class CreativeCommandRow(Base, PKMixin, TimestampMixin):
    __tablename__ = "creative_commands"
    __table_args__ = (
        Index("ix_creative_ws_status", "workspace_id", "status"),
        Index("ix_creative_ws_timeline", "workspace_id", "timeline_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    timeline_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    actor: Mapped[str] = mapped_column(String(60), default="user")
    source_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    text_input: Mapped[str] = mapped_column(Text, default="")
    commands_json: Mapped[list] = mapped_column(JSON, default=list)
    change_set_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="parsed")
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)


__all__ = ["CREATIVE_STATUSES", "CreativeCommandRow"]
