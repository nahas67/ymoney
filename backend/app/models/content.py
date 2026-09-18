"""Content domain ORM models: trends, opportunities, content, publishing,
analytics, campaigns and schedules."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class Campaign(Base, PKMixin, TimestampMixin):
    __tablename__ = "campaigns"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    target_videos: Mapped[int] = mapped_column(Integer, default=0)
    videos_per_day: Mapped[float] = mapped_column(Float, default=0)
    platforms_json: Mapped[list] = mapped_column(JSON, default=list)
    automation_level: Mapped[str] = mapped_column(String(20), default="SEMI_AUTONOMOUS")
    budget_daily_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    kpis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TrendSource(Base, PKMixin, TimestampMixin):
    __tablename__ = "trend_sources"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # google_trends|reddit|mock|custom
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Opportunity(Base, PKMixin, TimestampMixin):
    __tablename__ = "opportunities"
    __table_args__ = (
        Index("ix_opportunity_ws_score", "workspace_id", "score"),
        # dedupe discovery across concurrent cycles
        Index("uq_opportunity_ws_topic", "workspace_id", "topic", unique=True),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    topic: Mapped[str] = mapped_column(String(400), nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="unknown")
    external_ref: Mapped[str] = mapped_column(String(500), default="")
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    components_json: Mapped[dict] = mapped_column(JSON, default=dict)  # explainable scoring
    recommendation: Mapped[str] = mapped_column(String(20), default="WAIT")  # CREATE_NOW|PRODUCE|SKIP|WAIT
    # Trend lifecycle classification from emerging-trend detection.
    lifecycle: Mapped[str] = mapped_column(String(15), default="UNKNOWN")  # EMERGING|RISING|PEAK|DECLINING|EVERGREEN|UNKNOWN
    confidence: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1 scoring confidence
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    skipped_reason: Mapped[str] = mapped_column(String(300), default="")


class ContentItem(Base, PKMixin, TimestampMixin):
    __tablename__ = "content_items"
    __table_args__ = (
        Index("ix_content_ws_status", "workspace_id", "status"),
        Index("ix_content_campaign", "campaign_id"),
    )

    STATUS_FLOW = [
        "IDEA",
        "RESEARCHING",
        "STRATEGY",
        "SCRIPTING",
        "SCRIPT_READY",
        "PRODUCTION",
        "QC",
        "APPROVED",
        "SCHEDULED",
        "PUBLISHED",
        "ANALYZING",
        "LEARNED",
    ]

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    topic: Mapped[str] = mapped_column(String(400), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="IDEA", index=True)
    strategy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    research_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[list] = mapped_column(JSON, default=list)

    variants: Mapped[list[VideoVariant]] = relationship(
        back_populates="content_item", cascade="all, delete-orphan"
    )


class VideoVariant(Base, PKMixin, TimestampMixin):
    """A script/hook variation candidate for a content item."""

    __tablename__ = "video_variants"

    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(60), default="v1")
    hook: Mapped[str] = mapped_column(Text, default="")
    script: Mapped[str] = mapped_column(Text, default="")
    visual_plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)  # per-platform SEO
    predicted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

    content_item: Mapped[ContentItem] = relationship(back_populates="variants")
    video: Mapped[Video | None] = relationship(back_populates="variant", uselist=False)


class Video(Base, PKMixin, TimestampMixin):
    __tablename__ = "videos"

    variant_id: Mapped[str] = mapped_column(
        ForeignKey("video_variants.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(30))  # moneyprinterturbo | mock
    engine_task_id: Mapped[str] = mapped_column(String(80), index=True, default="")
    status: Mapped[str] = mapped_column(
        String(20), default="RENDERING", index=True
    )  # RENDERING|READY|FAILED
    progress: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str] = mapped_column(Text, default="")
    thumbnail_path: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="9:16")
    resolution: Mapped[str] = mapped_column(String(15), default="1080x1920")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")

    variant: Mapped[VideoVariant] = relationship(back_populates="video")
    quality_checks: Mapped[list[QualityCheck]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class QualityCheck(Base, PKMixin, TimestampMixin):
    __tablename__ = "quality_checks"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    overall: Mapped[float] = mapped_column(Float, default=0.0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    components_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewer: Mapped[str] = mapped_column(String(40), default="quality_agent")  # quality_agent|human
    notes: Mapped[str] = mapped_column(Text, default="")

    video: Mapped[Video] = relationship(back_populates="quality_checks")


class PublishingJob(Base, PKMixin, TimestampMixin):
    __tablename__ = "publishing_jobs"
    # ONE job row per (video, platform): retries UPDATE it instead of creating
    # duplicates. This is the backbone of idempotent publishing.
    __table_args__ = (
        Index("uq_pubjob_video_platform", "video_id", "platform", unique=True),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_post_id: Mapped[str] = mapped_column(String(200), default="")
    remote_url: Mapped[str] = mapped_column(String(500), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class PublishedPost(Base, PKMixin, TimestampMixin):
    __tablename__ = "published_posts"
    __table_args__ = (
        # A video can only ever exist once per platform — retries reuse the row.
        Index("uq_post_video_platform", "video_id", "platform", unique=True),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    video_id: Mapped[str] = mapped_column(String(36), index=True)
    publishing_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    platform: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remote_post_id: Mapped[str] = mapped_column(String(200), default="")
    remote_url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)

    metrics: Mapped[list[PostMetric]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PostMetric(Base, PKMixin, TimestampMixin):
    """Point-in-time metric snapshot for a published post."""

    __tablename__ = "post_metrics"
    __table_args__ = (Index("ix_metric_post_time", "post_id", "captured_at"),)

    post_id: Mapped[str] = mapped_column(
        ForeignKey("published_posts.id", ondelete="CASCADE"), index=True
    )
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    watch_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    avg_view_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    followers_gained: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    post: Mapped[PublishedPost] = relationship(back_populates="metrics")


class ScheduleEntry(Base, PKMixin, TimestampMixin):
    __tablename__ = "schedule_entries"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    platform: Mapped[str] = mapped_column(String(30))
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # PENDING is waiting for its run_at, DISPATCHING holds a short recovery
    # lease while the upload job is inserted, QUEUED means the durable upload
    # job exists, DONE means provider publication succeeded, and FAILED keeps
    # the entry visible for an operator retry.
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING"
    )  # PENDING|DISPATCHING|QUEUED|DONE|FAILED|CANCELLED
