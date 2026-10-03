"""Campaign derivation ORM models (Work 04 Lane A).

One long-form master ContentItem derives N diverse Shorts, each with
per-platform variants and a publishing plan. All rows are workspace-scoped.
"""

from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

PLAN_STATUSES = ("DRAFT", "RUNNING", "READY", "FAILED", "COMPLETE")
#: Work 14 adds AWAITING_HANDOFF: media prepared, a human still has to publish
#: it. It is deliberately NOT PUBLISHED, so nothing downstream can mistake a
#: Snapchat handoff for a live post.
VARIANT_STATUSES = ("DRAFT", "READY", "SCHEDULED", "PUBLISHED", "FAILED",
                    "AWAITING_HANDOFF")


class CampaignPlan(Base, PKMixin, TimestampMixin):
    """Derivation plan: master video -> N shorts -> platform variants."""

    __tablename__ = "campaign_plans"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), unique=True, index=True
    )
    master_content_id: Mapped[str] = mapped_column(String(36), index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    target_platforms: Mapped[list] = mapped_column(JSON, default=list)
    desired_shorts: Mapped[int] = mapped_column(Integer, default=8)
    duration_min: Mapped[float] = mapped_column(Float, default=20.0)
    duration_max: Mapped[float] = mapped_column(Float, default=55.0)
    diversity_config: Mapped[dict] = mapped_column(JSON, default=dict)
    posting_window: Mapped[dict] = mapped_column(JSON, default=dict)
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    # DRAFT|RUNNING|READY|FAILED|COMPLETE
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    progress_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")


class PlatformVariant(Base, PKMixin, TimestampMixin):
    """One platform cut of a derived short (Shorts/TikTok/Reels)."""

    __tablename__ = "platform_variants"
    __table_args__ = (
        Index("uq_variant_short_platform", "short_content_id", "platform", unique=True),
        Index("ix_variant_campaign", "campaign_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    short_content_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), index=True)
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="9:16")
    timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cover_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    safe_zone_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # DRAFT|READY|SCHEDULED|PUBLISHED|FAILED
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    publishing_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    published_post_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class PublishingPlan(Base, PKMixin, TimestampMixin):
    """Ordered publish schedule for a campaign's platform variants."""

    __tablename__ = "publishing_plans"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    # [{variant_id, platform, planned_at ISO, priority, depends_on,
    #   approval_state, publication_state}]
    items_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
