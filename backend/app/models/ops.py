"""Operational ORM models: jobs, agents, autopilot, events, costs, learning."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import PKMixin, TimestampMixin, utcnow


class Job(Base, PKMixin, TimestampMixin):
    """Durable job record backing the internal queue.

    The queue is database-backed so state survives restarts. A Redis-backed
    implementation can be added later without changing the API.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "next_run_at", "priority"),
        Index("ix_jobs_ws_type", "workspace_id", "type"),
    )

    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(15), default="QUEUED", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = sooner
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentConfig(Base, PKMixin, TimestampMixin):
    """Per-workspace agent configuration (enable/disable, model, prompts...)."""

    __tablename__ = "agent_configs"
    __table_args__ = (Index("ix_agent_cfg", "workspace_id", "agent_key"),)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    agent_key: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_override: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentRun(Base, PKMixin, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_agent", "workspace_id", "agent_key"),)

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_key: Mapped[str] = mapped_column(String(40), index=True)
    task_type: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(15), default="RUNNING")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    # ordered step trace: [{step, detail, started_ms, duration_ms, status}]
    steps_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AutopilotRun(Base, PKMixin, TimestampMixin):
    """Autopilot control state per workspace. One active run at a time."""

    __tablename__ = "autopilot_runs"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(20), default="CONTINUOUS")  # CONTINUOUS|SINGLE_CYCLE
    state: Mapped[str] = mapped_column(String(15), default="IDLE", index=True)
    cycles_completed: Mapped[int] = mapped_column(Integer, default=0)
    cycles_target: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_stop_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)

    cycles: Mapped[list[Cycle]] = relationship(back_populates="autopilot_run")


class Cycle(Base, PKMixin, TimestampMixin):
    """One FIND→…→LEARN execution of the autonomous loop."""

    __tablename__ = "cycles"

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    autopilot_run_id: Mapped[str] = mapped_column(
        ForeignKey("autopilot_runs.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(20), default="FIND", index=True)
    status: Mapped[str] = mapped_column(String(15), default="RUNNING")  # RUNNING|COMPLETED|FAILED|CANCELLED
    selected_opportunity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)

    autopilot_run: Mapped[AutopilotRun] = relationship(back_populates="cycles")


class EventLog(Base, PKMixin, TimestampMixin):
    """Append-only activity feed powering live UI + notifications."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_ws_time", "workspace_id", "created_at"),)

    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info|success|warning|error
    source: Mapped[str] = mapped_column(String(40), default="system")
    kind: Mapped[str] = mapped_column(String(60), default="event", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CostEntry(Base, PKMixin, TimestampMixin):
    __tablename__ = "cost_entries"
    __table_args__ = (Index("ix_cost_ws_day", "workspace_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(30))  # llm|tts|image|video|search|publishing|storage
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(40), default="")
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)


class LearningPattern(Base, PKMixin, TimestampMixin):
    __tablename__ = "learning_patterns"

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    pattern_key: Mapped[str] = mapped_column(String(120))  # e.g. hook_style_question
    description: Mapped[str] = mapped_column(String(400), default="")
    observed_improvement_pct: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[str] = mapped_column(String(10), default="low")  # low|medium|high
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MemoryRecord(Base, PKMixin, TimestampMixin):
    """Persistent memory (spec #25): typed records with scoped, targeted
    retrieval — never a dump-all store."""

    __tablename__ = "memory_records"
    __table_args__ = (
        Index("ix_memory_ws_type", "workspace_id", "type"),
        Index("ix_memory_ws_scope", "workspace_id", "scope"),
    )

    TYPE_SHORT_TERM = "short_term"
    TYPE_EPISODIC = "episodic"
    TYPE_SEMANTIC = "semantic"
    TYPE_STRATEGIC = "strategic"
    TYPE_PREFERENCE = "preference"
    VALID_TYPES = (TYPE_SHORT_TERM, TYPE_EPISODIC, TYPE_SEMANTIC, TYPE_STRATEGIC, TYPE_PREFERENCE)

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(20), default=TYPE_SEMANTIC)  # short_term|episodic|semantic|strategic|preference
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="")  # agent key or "user"
    confidence: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1
    importance: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1
    scope: Mapped[str] = mapped_column(String(120), default="")  # topic/brand/platform tag for targeted retrieval
    related_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {content_id, opportunity_id, pattern_key, ...}
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SystemLog(Base, PKMixin, TimestampMixin):
    """Searchable system/production/publishing/security log store."""

    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_syslog_cat_time", "category", "created_at"),)

    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="system")
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
