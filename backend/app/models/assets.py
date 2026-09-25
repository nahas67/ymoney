"""Typed media + scene models: every byte referenced by timelines lives here.

MediaAsset is a reference row, never a blob: files stay in workspace storage
(LocalStorage / S3), the row carries identity, provenance, and technical
metadata. Scenes are first-class rows (script/broll/analytics/retention all
address the same scene id), linked to a content item and optionally to the
timeline range that realizes them.
"""

from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

ASSET_TYPES = ("video", "audio", "image", "subtitle", "generated_image",
               "generated_video", "voice", "avatar", "thumbnail", "other")
ASSET_ORIGINS = ("upload", "render", "stock", "generated", "import", "record",
                 "proxy")


class MediaAsset(Base, PKMixin, TimestampMixin):
    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_asset_ws_type", "workspace_id", "type"),
        Index("ix_asset_ws_origin", "workspace_id", "origin"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(20), default="other")  # ASSET_TYPES
    origin: Mapped[str] = mapped_column(String(20), default="upload")  # ASSET_ORIGINS
    provider: Mapped[str] = mapped_column(String(60), default="")
    # workspace-relative storage key (never absolute, never outside the ws dir)
    storage_key: Mapped[str] = mapped_column(Text, default="")
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    codec: Mapped[str] = mapped_column(String(40), default="")
    audio_codec: Mapped[str] = mapped_column(String(40), default="")
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str] = mapped_column(String(128), default="")
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Scene(Base, PKMixin, TimestampMixin):
    """One narrative unit: script segment + narration + visual intent +
    timeline range + assets + captions + performance metadata + lineage."""

    __tablename__ = "scenes"
    __table_args__ = (
        Index("ix_scene_content", "content_item_id"),
        Index("ix_scene_timeline", "timeline_id"),
        Index("ix_scene_ws_idx", "workspace_id", "index"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    chapter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)  # order within the parent
    title: Mapped[str] = mapped_column(String(200), default="")
    script_segment: Mapped[str] = mapped_column(Text, default="")
    narration: Mapped[str] = mapped_column(Text, default="")
    visual_intent: Mapped[str] = mapped_column(Text, default="")
    start_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    end_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    assets_json: Mapped[list] = mapped_column(JSON, default=list)  # [{asset_id, role}]
    captions_json: Mapped[list] = mapped_column(JSON, default=list)
    performance_json: Mapped[dict] = mapped_column(JSON, default=dict)  # retention etc.
    beats_json: Mapped[list] = mapped_column(JSON, default=list)  # visual beats (Work 03)
    parent_scene_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
