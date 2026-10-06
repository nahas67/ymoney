"""Content domain ORM models: trends, opportunities, content, publishing,
analytics, campaigns and schedules."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
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
        # 0032's own index: the planner reads opportunities by basis.
        Index("ix_opportunity_basis", "workspace_id", "basis"),
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
    virality: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100 breakout potential (informational)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    skipped_reason: Mapped[str] = mapped_column(String(300), default="")

    # -- Work 15 §2/§3: planning provenance -------------------------------
    # How this opportunity was derived. OBSERVED = seen in real evidence;
    # INFERRED = derived by scoring/clustering; RECOMMENDED = an AI suggestion
    # with no measurement. A RECOMMENDED row may never be scheduled as demand.
    #
    # Work 16 §1: ``nullable=True`` on every column 0032 bolted on. 0032's
    # ``add_column_if_missing`` DDL carries no ``NOT NULL``, so a deployment
    # that took the migration path has these NULLABLE while ``create_all``
    # made them NOT NULL -- and ``nullable=False`` here is a claim the
    # deployed schema does not honour. Loosening is the safe direction: it
    # cannot make a write fail that succeeds today. ``default=`` is
    # untouched, so ORM writes still supply every value.
    basis: Mapped[str] = mapped_column(String(20), default="INFERRED",
                                         nullable=True)
    angle: Mapped[str] = mapped_column(String(400), default="",
                                         nullable=True)
    audience: Mapped[str] = mapped_column(String(200), default="",
                                           nullable=True)
    platforms_json: Mapped[list] = mapped_column(JSON, default=list,
                                                nullable=True)
    format: Mapped[str] = mapped_column(String(40), default="",
                                        nullable=True)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list,
                                                nullable=True)
    brand_fit: Mapped[float] = mapped_column(Float, default=0.0,
                                           nullable=True)
    freshness: Mapped[str] = mapped_column(String(12), default="",
                                           nullable=True)
    competition_json: Mapped[dict] = mapped_column(JSON, default=dict,
                                                  nullable=True)
    estimated_effort_hours: Mapped[float] = mapped_column(Float, default=0.0,
                                                        nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0,
                                                   nullable=True)
    priority_inputs_json: Mapped[dict] = mapped_column(JSON, default=dict,
                                                     nullable=True)
    # Work 15 §10: NEW | RELATED | DUPLICATE | SATURATED
    dedupe_verdict: Mapped[str] = mapped_column(String(20), default="",
                                                nullable=True)
    dedupe_reason: Mapped[str] = mapped_column(String(400), default="",
                                                nullable=True)
    plan_item_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)

    @property
    def platforms(self) -> list:
        return list(self.platforms_json or [])

    @property
    def evidence(self) -> list:
        return list(self.evidence_json or [])

    @property
    def competition_evidence(self) -> dict:
        return dict(self.competition_json or {})

    @property
    def priority_inputs(self) -> dict:
        return dict(self.priority_inputs_json or {})


class ContentItem(Base, PKMixin, TimestampMixin):
    __tablename__ = "content_items"
    __table_args__ = (
        Index("ix_content_ws_status", "workspace_id", "status"),
        Index("ix_content_campaign", "campaign_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # --- content lineage (Work 01): every derived asset knows its ancestry ---
    parent_content_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    root_content_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # short|variant|localized|platform_cut|repurpose|translation|other
    derivation_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    lineage_version: Mapped[int] = mapped_column(Integer, default=1)
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
    __table_args__ = (
        # 0034's own composites: the reconciliation queries an operator
        # actually runs are per-workspace, not global.
        Index("ix_video_cost_outcome", "workspace_id", "cost_outcome"),
        Index("ix_video_submission_state", "workspace_id", "submission_state"),
    )

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

    # --- Work 15.5 §7: the paid-submission contract ---------------------
    # `status` above is the RENDER pipeline's concern and is deliberately NOT
    # widened. A paid submit that may have been billed is a different fact, and
    # folding it into `status` would force every reader of the render state to
    # handle a state they have no business interpreting.
    #
    # PREPARED | SUBMISSION_ATTEMPTED | REMOTE_ID_CONFIRMED | PROCESSING |
    # SUCCEEDED | FAILED | SUBMISSION_UNKNOWN
    #
    # SUBMISSION_UNKNOWN is the load-bearing one: the provider may have created
    # and billed the job and we cannot prove otherwise. It forbids automatic
    # resubmission and requires reconciliation or a human decision.
    # Work 16 §1: every ``server_default`` below is 0034's own literal. 0034
    # adds these columns to an EXISTING table with ``NOT NULL DEFAULT``, and
    # ``add_column_if_missing`` skips a column ``create_all`` already made --
    # so a ``create_all`` database had none of them, and a raw INSERT naming
    # only the pre-0034 columns failed 23502 there and succeeded on a
    # migrated one.
    submission_state: Mapped[str] = mapped_column(
        String(24), default="PREPARED", index=True,
        server_default=text("'PREPARED'"))
    #: the provider's durable id, for reconciling an ambiguous submission.
    provider_task_id: Mapped[str] = mapped_column(String(160), default="",
                                                server_default=text("''"))
    #: why the submission state is what it is, for audit.
    submission_detail: Mapped[str] = mapped_column(String(600), default="",
                                                 server_default=text("''"))
    #: sent upstream as the provider's idempotency key, so a lost response can
    #: be retried without buying a second job.
    idempotency_key: Mapped[str] = mapped_column(String(80), default="",
                                               server_default=text("''"))
    # --- Work 15.8 §6: the durable paid-submission record ----------------
    # `submission_state` above already IS the structural execution outcome (it
    # holds the canonical `SubmissionState` vocabulary and is indexed), so this
    # change adds NO second execution column -- two spellings of "may have been
    # billed" is how two dashboards end up disagreeing. What was missing is the
    # MONEY half and the correlation id:
    #
    # `cost_outcome` holds the canonical `CostOutcome` vocabulary
    # (NOT_APPLICABLE|ACTUAL|ESTIMATED|UNKNOWN_EXPOSURE) so an operator can
    # filter "renders whose cost is unknown" with a WHERE clause. Without it the
    # only place that fact existed was a reservation row id held in memory.
    cost_outcome: Mapped[str] = mapped_column(String(24), default="",
                                              index=True,
                                              server_default=text("''"))
    #: The canonical operation id of this attempt (PaidSubmission.submission_id).
    #: Nothing before this linked the Video row to the ledger row for the same
    #: submit, so a crash between the two made the pairing unrecoverable.
    submission_operation_id: Mapped[str] = mapped_column(String(64), default="",
                                                         index=True,
                                                         server_default=text("''"))
    #: When the request actually LEFT. `created_at` is when the row was made,
    #: which is not the same fact: a row created and never submitted must not
    #: look like an attempt that may have been billed.
    submission_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
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
    # Work 14: the four honest outcomes. `is_mock` is KEPT because existing
    # readers still use it, but it can no longer express a handoff, so the
    # mode is authoritative for anything user-visible. See
    # app/engine/distribution/modes.py.
    #
    # The default is UNAVAILABLE, NOT live: a row that did not declare a mode
    # has declared nothing, and defaulting to LIVE would let a mock (or a
    # half-written) row claim a live publication. The publish flow always sets
    # the mode explicitly; failing closed is the safe default.
    # Work 16 §1: ``nullable=True`` because 0031 declares it without
    # ``NOT NULL``. The server default is NOT copied across on purpose --
    # 0031's is ``'LIVE'`` and the paragraph above is the whole argument
    # against defaulting to LIVE. A non-ORM writer that omits this column
    # already gets ``'LIVE'`` on a migrated database; stamping that same
    # literal into ``create_all`` would spread it to fresh ones. Recorded
    # as a documented exception in test_work16_1_schema_parity.py.
    publication_mode: Mapped[str] = mapped_column(
        String(12), default="UNAVAILABLE", index=True, nullable=True)
    # Handoff detail: where the prepared media lives + how the user publishes.
    handoff_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    handoff_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    # Work 04 Lane A: lineage back to the platform variant + campaign.
    platform_variant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

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
    __table_args__ = (
        # 0032's own composite. ``plan_item_id`` is the planner's idempotency
        # key and is scoped per workspace, and this was on
        # ``Base.metadata`` for neither column -- a fresh database had the
        # single-column index only.
        Index("ix_schedule_plan_item", "workspace_id", "plan_item_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Work 15: the planner's idempotency key. campaign_id and content_item_id are
    # BOTH null for a plan item that has produced nothing yet, so keying on them
    # alone collapsed every such item on a platform onto one row -- and
    # re-planning one silently moved the other's time. NULL for every
    # pre-Work-15 entry, and ignored by the canonical Scheduler.
    plan_item_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(30))
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # PENDING is waiting for its run_at, DISPATCHING holds a short recovery
    # lease while the upload job is inserted, QUEUED means the durable upload
    # job exists, DONE means provider publication succeeded, and FAILED keeps
    # the entry visible for an operator retry.
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING"
    )  # PENDING|DISPATCHING|QUEUED|DONE|FAILED|CANCELLED
