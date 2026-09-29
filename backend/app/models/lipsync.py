"""Lip-sync job ORM model (Work 07 Lane B: replaceable lip-sync layer).

Owned exclusively by Lane B (Work 07). One row per submitted lip-sync job;
heavy inference runs in an isolated worker (`engine.lipsync.worker`), so this
row is the durable record of status, progress, failure and GPU cost.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED | TIMEOUT
LIPSYNC_STATUSES = (
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
)


class LipSyncJob(Base, PKMixin, TimestampMixin):
    """A single lip-sync render request driven by the local worker queue."""

    __tablename__ = "lipsync_jobs"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="unavailable", index=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    result_asset_ref: Mapped[str] = mapped_column(String(1024), default="")
    cost_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # inputs + adapter linkage (beyond the minimal contract: needed to run)
    video_ref: Mapped[str] = mapped_column(String(1024), default="")
    audio_ref: Mapped[str] = mapped_column(String(1024), default="")
    opts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    adapter_job_id: Mapped[str] = mapped_column(String(80), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = ["LIPSYNC_STATUSES", "LipSyncJob"]
