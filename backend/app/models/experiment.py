"""Experiment ORM model (Work 06 Lane B: creative experiments).

Owned exclusively by Lane B. Lane A owns the other performance models;
this module is intentionally standalone so neither lane blocks the other.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

EXPERIMENT_KINDS = (
    "HOOK",
    "TITLE",
    "THUMBNAIL",
    "VOICE",
    "CAPTION_STYLE",
    "VIDEO_DURATION",
    "CTA",
    "MUSIC",
    "BROLL_DENSITY",
    "POSTING_TIME",
)

EXPERIMENT_STATUSES = (
    "DRAFT",
    "RUNNING",
    "INSUFFICIENT_DATA",
    "COMPLETED",
    "INCONCLUSIVE",
    "CANCELLED",
)


class Experiment(Base, PKMixin, TimestampMixin):
    """A creative A/B test: one control arm vs one or more variant arms."""

    __tablename__ = "experiments"
    # Production authority is the migration, and 0020 declares ONE composite
    # index ``ix_experiments_ws_status ON (workspace_id, status)``. The ORM
    # previously declared two separate single-column indexes (``index=True`` on
    # each field), so a ``create_all`` database and a migration-built database
    # had genuinely different index sets -- and only the migration's version can
    # serve the workspace-scoped, status-filtered query this table exists for.
    __table_args__ = (
        Index("ix_experiments_ws_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(20), default="HOOK", index=True)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    control_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {variant_ref}
    variants_json: Mapped[list] = mapped_column(JSON, default=list)  # [{variant_ref, descriptor}]
    platform: Mapped[str] = mapped_column(String(30), default="")
    primary_metric: Mapped[str] = mapped_column(String(60), default="views")
    secondary_metrics: Mapped[list] = mapped_column(JSON, default=list)
    minimum_sample: Mapped[int] = mapped_column(Integer, default=60)
    # DRAFT|RUNNING|INSUFFICIENT_DATA|COMPLETED|INCONCLUSIVE|CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(40), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
