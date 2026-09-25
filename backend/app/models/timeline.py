"""Canonical editorial timeline: the single source of truth for long videos,
shorts, the manual editor, the AI Creative Director, and the render engine.

A timeline DESCRIBES a render; it never replaces the VideoEngine interface.
Tracks shape (see engine/timeline.py)::

    {"tracks": [{"id", "kind", "name", "clips":
        [{"id", "name", "start", "duration", "source": {}, "effects": []}]}],
     ...}

Track kinds: video | broll | avatar | text | caption | voice | music | sfx.
"""

from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class ContentTimeline(Base, PKMixin, TimestampMixin):
    __tablename__ = "content_timelines"
    __table_args__ = (
        Index("ix_timeline_ws", "workspace_id"),
        Index("ix_timeline_content", "content_item_id"),
        Index("ix_timeline_video", "video_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="main")
    fps: Mapped[float] = mapped_column(Float, default=30.0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tracks_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # undo/redo state primitive: each saved version points at its parent.
    parent_timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
