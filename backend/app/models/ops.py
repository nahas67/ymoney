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

    **Work 16 §2: a claimed job carries a lease.** ``status='RUNNING'`` on its
    own says only that *some* worker took the job; ``claimed_by`` names which,
    ``lease_expires_at`` says until when that ownership is good, and
    ``heartbeat_at`` records the last proof of life. Only an EXPIRED lease may
    be reclaimed, which is what separates "this worker died holding it" from
    "this worker is rendering it right now and you must not touch it".

    The lease window splits the two timestamps rather than replacing either:
    ``claimed_at`` is when the lease was taken, ``started_at`` is when the
    handler was entered, so ``CLAIMED`` (lease held, handler not yet entered)
    is an observable state rather than a gap in the data.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "next_run_at", "priority"),
        Index("ix_jobs_ws_type", "workspace_id", "type"),
        # The recovery sweep's only query: RUNNING rows whose lease has lapsed.
        Index("ix_jobs_lease_recovery", "status", "lease_expires_at"),
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
    # -- Work 16 §2: the lease. ------------------------------------------
    # `claimed_by=''` means "no worker holds this", which is why it is a
    # non-null string rather than NULL: every read is then a plain equality.
    claimed_by: Mapped[str] = mapped_column(String(80), default="", index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # NULL means "nobody holds a renewable lease on this row". For a row that
    # predates 0035 that is the honest reading, and it is what makes such a row
    # recoverable rather than immortal.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    request_id: Mapped[str] = mapped_column(String(32), default="", index=True)


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
    request_id: Mapped[str] = mapped_column(String(32), default="")


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


class GpuDevice(Base, PKMixin, TimestampMixin):
    """One physical GPU and the VRAM it owns (Work 16 §4).

    ``reserved_mb`` is a DENORMALISED COUNTER, not a derived value, and that is
    the entire point. Deriving it with ``SELECT SUM(vram_mb) ... WHERE status
    IN (...)`` and then comparing to ``total_mb`` is a check-then-act: two
    processes both read "3 GB free" and both admit a 4 GB job, and the result is
    an out-of-memory kill that no query afterwards can explain. Keeping the
    balance in the same row as the capacity makes admission a single
    conditional UPDATE whose ``rowcount`` is the answer -- the same
    compare-and-set that ``job_leases`` uses for the claim.

    ``device_key`` (``"cuda:0"``, ``"cpu"``) is the stable identity across
    processes; ``id`` is a surrogate that may differ between environments.
    """

    __tablename__ = "gpu_devices"
    __table_args__ = (
        Index("ix_gpu_device_key", "device_key", unique=True),
        Index("ix_gpu_device_enabled", "enabled"),
    )

    device_key: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(80), default="")
    #: ``cuda`` | ``rocm`` | ``cpu`` | ``simulated``. ``cpu`` is a REAL device
    #: with REAL (finite) capacity, not "unlimited" -- a CPU lane that admits
    #: everything is the oversubscription bug wearing a different hat.
    backend: Mapped[str] = mapped_column(String(20), default="cpu")
    total_mb: Mapped[int] = mapped_column(Integer, default=0)
    reserved_mb: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def free_mb(self) -> int:
        return max(0, int(self.total_mb or 0) - int(self.reserved_mb or 0))


class GpuReservation(Base, PKMixin, TimestampMixin):
    """One job's claim on a device's VRAM (Work 16 §4).

    Carries a LEASE for the same reason ``jobs`` does: a worker that dies
    without releasing must not hold VRAM forever, and a worker that is alive
    must not have its slot stolen. ``lease_expires_at`` is renewed by the
    holder; only an expired lease may be reclaimed. ``job_id`` links the slot to
    the queue row so the recovery sweep can consult ``job_leases`` for the
    authoritative answer about liveness rather than guessing from a timestamp.
    """

    __tablename__ = "gpu_reservations"
    __table_args__ = (
        Index("ix_gpu_res_device_state", "device_id", "status"),
        Index("ix_gpu_res_lease", "status", "lease_expires_at"),
        Index("ix_gpu_res_job", "job_id"),
        Index("ix_gpu_res_ws", "workspace_id", "status"),
    )

    #: A reservation holding VRAM. Anything else is history.
    HELD: tuple[str, ...] = ("RESERVED", "RUNNING")
    RESERVED = "RESERVED"
    RUNNING = "RUNNING"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    device_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    #: ``jobs.id`` when this slot backs a queue job. NULL for a direct call.
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: what the slot is for: ``musetalk`` | ``segmentation`` | ``avatar`` ...
    kind: Mapped[str] = mapped_column(String(40), default="")
    job_type: Mapped[str] = mapped_column(String(60), default="")
    vram_mb: Mapped[int] = mapped_column(Integer, default=0)
    #: lower = sooner, matching ``jobs.priority``.
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(15), default=RESERVED, index=True)
    #: True only when the CALLER declared the work CPU-capable AND the operator
    #: enabled fallback. Never inferred.
    cpu_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_reason: Mapped[str] = mapped_column(String(40), default="")


class StorageObject(Base, PKMixin, TimestampMixin):
    """The canonical record of one stored object (Work 16 §5).

    ``state`` is the invariant that matters: a ``PENDING`` row is a *promise*
    (an upload is in flight somewhere) and can never be read as content. Only
    ``FINALIZED`` rows name bytes that exist, and the row is written in the
    SAME transaction that renames the ``.part`` file into place -- so there is
    no window in which a partially written file is addressable.

    ``temp_path`` exists precisely so a temp file can be tracked WITHOUT being
    canonical. It is never used to serve bytes, and
    ``services.storage_objects`` refuses to finalize from inside the staging
    root.
    """

    __tablename__ = "storage_objects"
    __table_args__ = (
        Index("ix_storage_object_key", "workspace_id", "object_key", unique=True),
        Index("ix_storage_object_state", "state", "workspace_id"),
        Index("ix_storage_object_expiry", "state", "expires_at"),
    )

    PENDING = "PENDING"
    FINALIZED = "FINALIZED"
    DELETED = "DELETED"

    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    #: stable, content-independent identity: same logical name -> same id, so a
    #: retried upload is idempotent instead of littering duplicates.
    object_key: Mapped[str] = mapped_column(String(300), default="")
    #: ``sha256`` of the bytes. Empty while PENDING (there are no bytes yet).
    checksum: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(120), default="")
    #: source | proxy | render | thumbnail | subtitle | generated | export | archive
    kind: Mapped[str] = mapped_column(String(20), default="source", index=True)
    state: Mapped[str] = mapped_column(String(15), default=PENDING, index=True)
    backend: Mapped[str] = mapped_column(String(20), default="local")
    #: where the bytes are being written BEFORE finalization. Never servable.
    temp_path: Mapped[str] = mapped_column(String(400), default="")
    #: the asset/video row this object backs, when there is one.
    ref_type: Mapped[str] = mapped_column(String(30), default="")
    ref_id: Mapped[str] = mapped_column(String(36), default="")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemLog(Base, PKMixin, TimestampMixin):
    """Searchable system/production/publishing/security log store."""

    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_syslog_cat_time", "category", "created_at"),)

    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="system")
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
