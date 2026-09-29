"""Performance intelligence persistence (Work 06, Lane A).

Three tables, all workspace-scoped with CASCADE deletes:

- ``retention_points``: granular audience-retention samples. One row per
  (post/short, checkpoint). Checkpoints are coarse-standard
  (``1s``/``3s``/``25%``/``50%``/``75%``/``100%``) or raw seconds
  (``"12.5"``). Providers ship no retention data today, so most rows come
  from test fixtures or future provider backfills — never fabricated.
- ``creative_features``: observable creative attributes per content item
  (Lane B's extractor writes here; Lane A/C read).
- ``performance_observations``: evidence-backed metric facts about any
  subject (post/short/variant/campaign/chapter/scene) with the evidence
  payload inline so decisions never depend on recomputation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class RetentionPoint(Base, PKMixin, TimestampMixin):
    """One audience-retention sample for a post/short at a checkpoint."""

    __tablename__ = "retention_points"
    __table_args__ = (
        Index("ix_retention_ws_post", "workspace_id", "post_id"),
        Index("ix_retention_ws_short", "workspace_id", "short_content_id"),
        Index("ix_retention_ws_campaign", "workspace_id", "campaign_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    post_id: Mapped[str | None] = mapped_column(
        ForeignKey("published_posts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    short_content_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # "1s" | "3s" | "25%" | "50%" | "75%" | "100%" | seconds ("12.5")
    checkpoint: Mapped[str] = mapped_column(String(20), default="")
    value: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1 fraction watching
    source: Mapped[str] = mapped_column(String(40), default="")  # provider|coarse_proxy|fixture
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CreativeFeature(Base, PKMixin, TimestampMixin):
    """Observable creative attributes for one content item (Lane B writes)."""

    __tablename__ = "creative_features"
    __table_args__ = (
        Index("ix_creative_features_ws", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PerformanceObservation(Base, PKMixin, TimestampMixin):
    """One evidence-backed metric fact about any subject."""

    __tablename__ = "performance_observations"
    __table_args__ = (
        Index("ix_perf_obs_ws_subject", "workspace_id", "subject_type", "subject_id"),
        Index("ix_perf_obs_ws_metric", "workspace_id", "metric"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # post | short | variant | campaign | chapter | scene
    subject_type: Mapped[str] = mapped_column(String(20), default="")
    subject_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    metric: Mapped[str] = mapped_column(String(60), default="")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    platform: Mapped[str] = mapped_column(String(30), default="")
    scope_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = ["CreativeFeature", "PerformanceObservation", "RetentionPoint"]
