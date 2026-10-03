"""Work 15 planning ORM models (signals, editorial plans, capacity).

``Opportunity`` is EXTENDED, never replaced: Work 05/09 cycle scoring already
owns ``topic``/``score``/``lifecycle``/``skipped_reason`` and the planner reads
them rather than forking them.

The one field worth calling out is ``Opportunity.basis``:

    ``OBSERVED``    the topic was seen in real evidence (a community request,
                    a measured performance shift, an ingested source)
    ``INFERRED``    derived from evidence by scoring or clustering
    ``RECOMMENDED`` an AI suggestion with no measurement behind it

A plan may not schedule a ``RECOMMENDED`` item as though it were demand. That
distinction is the point of the column, and it is enforced in
``engine.planning.engine`` rather than trusted to the UI.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

#: A signal that has been verified against at least one evidence record.
SIGNAL_ACTIVE = "ACTIVE"
#: Superseded by a newer observation of the same topic.
SIGNAL_SUPERSEDED = "SUPERSEDED"
#: The underlying evidence aged out; needs revalidation before planning on it.
SIGNAL_STALE = "STALE"
SIGNAL_STATUSES = (SIGNAL_ACTIVE, SIGNAL_SUPERSEDED, SIGNAL_STALE)

#: Freshness bands, reusing the Work 10 knowledge thresholds.
FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"

#: How an opportunity was derived. See the module docstring.
OBSERVED = "OBSERVED"
INFERRED = "INFERRED"
RECOMMENDED = "RECOMMENDED"
BASES = (OBSERVED, INFERRED, RECOMMENDED)

#: Editorial plan item lifecycle. Deliberately NOT a copy of Campaign.status or
#: ScheduleEntry.status: those own execution, this owns intent. The item
#: REFERENCES them (``campaign_id`` / ``schedule_entry_id``).
IDEA = "IDEA"
PLANNED = "PLANNED"
RESEARCHING = "RESEARCHING"
READY = "READY"
IN_PRODUCTION = "IN_PRODUCTION"
SCHEDULED = "SCHEDULED"
PUBLISHED = "PUBLISHED"
BLOCKED = "BLOCKED"
CANCELLED = "CANCELLED"
PLAN_ITEM_STATUSES = (IDEA, PLANNED, RESEARCHING, READY, IN_PRODUCTION,
                     SCHEDULED, PUBLISHED, BLOCKED, CANCELLED)

#: Autonomy modes. Planning autonomy NEVER implies publishing autonomy.
AUTONOMY_DISABLED = "DISABLED"
AUTONOMY_RECOMMEND = "RECOMMEND"
AUTONOMY_APPROVAL = "APPROVAL"
AUTONOMY_AUTONOMOUS = "AUTONOMOUS"
AUTONOMY_MODES = (AUTONOMY_DISABLED, AUTONOMY_RECOMMEND, AUTONOMY_APPROVAL,
                  AUTONOMY_AUTONOMOUS)

#: Dedupe verdicts from the fatigue model.
DEDUPE_NEW = "NEW"
DEDUPE_RELATED = "RELATED"
DEDUPE_DUPLICATE = "DUPLICATE"
DEDUPE_SATURATED = "SATURATED"
DEDUPE_VERDICTS = (DEDUPE_NEW, DEDUPE_RELATED, DEDUPE_DUPLICATE,
                   DEDUPE_SATURATED)


class TrendSignal(Base, PKMixin, TimestampMixin):
    """One observation, with the evidence that supports it.

    A signal NEVER carries a "trend score". It carries what was seen, when,
    how fresh it is, and which evidence records back it. Velocity is a
    measurement of *change between two observed signals of the same topic* --
    when there is only one observation, ``velocity_json`` is ``None`` and the
    consumer must say "unknown", not invent a rate.
    """

    __tablename__ = "trend_signals"
    __table_args__ = (
        # One row per (workspace, source, topic, external ref): re-ingesting the
        # same source item updates the observation instead of duplicating it.
        Index("uq_trend_signal_dedupe", "workspace_id", "source", "topic_key",
              "external_ref", unique=True),
        Index("ix_trend_signal_ws_topic", "workspace_id", "topic_key"),
        Index("ix_trend_signal_ws_observed", "workspace_id", "observed_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    #: research | source | community | request | performance | platform | operator
    source: Mapped[str] = mapped_column(String(60))
    topic: Mapped[str] = mapped_column(String(400))
    #: normalized topic, used for dedupe/clustering
    topic_key: Mapped[str] = mapped_column(String(400))
    #: the source's own id (a post id, a URL, a request id). Empty for
    #: operator/derived signals.
    external_ref: Mapped[str] = mapped_column(String(500), default="")
    #: evidence record ids (Work 05 EvidenceRecord / Work 10 memory evidence)
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    observed_at: Mapped[datetime] = mapped_column(index=True)
    freshness: Mapped[str] = mapped_column(String(12), default=FRESH)
    #: workspace | brand | platform
    scope: Mapped[str] = mapped_column(String(40), default="workspace")
    #: None when there is a single observation and no rate is measurable
    velocity_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 0..1 confidence in the OBSERVATION. Not a demand score: how sure are we
    #: the thing was actually seen. An unverified signal cannot exceed 0.5.
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default=SIGNAL_ACTIVE,
                                        index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def evidence_ids(self) -> list:
        return list(self.evidence_ids_json or [])

    @property
    def velocity(self) -> dict | None:
        return self.velocity_json


class EditorialPlan(Base, PKMixin, TimestampMixin):
    """A planning horizon for one workspace."""

    __tablename__ = "editorial_plans"
    __table_args__ = (
        Index("ix_editorial_plan_ws", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, default=30)
    goals_json: Mapped[list] = mapped_column(JSON, default=list)
    platforms_json: Mapped[list] = mapped_column(JSON, default=list)
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    autonomy: Mapped[str] = mapped_column(String(20),
                                          default=AUTONOMY_RECOMMEND)
    constraints_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")

    @property
    def goals(self) -> list:
        return list(self.goals_json or [])

    @property
    def platforms(self) -> list:
        return list(self.platforms_json or [])

    @property
    def constraints(self) -> dict:
        return dict(self.constraints_json or {})

    @property
    def budget_remaining(self) -> float:
        """Budget left after everything already committed against this plan.

        ``spent_usd`` is the COMMITTED estimate (plus anything the caller
        declared as already spent), not a settled invoice -- the renderer is
        what turns an estimate into an actual, and it writes its own ledger.
        """
        return max(0.0, float(self.budget_usd) - float(self.spent_usd))


class EditorialPlanItem(Base, PKMixin, TimestampMixin):
    """One planned piece of work.

    ``campaign_id`` and ``schedule_entry_id`` are REFERENCES. The item's
    ``status`` is the planner's own intent state; the campaign and the
    schedule entry keep owning execution. Nothing is duplicated.
    """

    __tablename__ = "editorial_plan_items"
    __table_args__ = (
        Index("ix_plan_item_plan", "plan_id"),
        Index("ix_plan_item_ws_status", "workspace_id", "status"),
        Index("ix_plan_item_target", "target_date"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("editorial_plans.id", ondelete="CASCADE"))
    opportunity_id: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    schedule_entry_id: Mapped[str | None] = mapped_column(String(36),
                                                           nullable=True)
    content_format: Mapped[str] = mapped_column(String(60), default="SHORT")
    angle: Mapped[str] = mapped_column(String(400), default="")
    platforms_json: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    target_date: Mapped[datetime | None] = mapped_column(nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    dependencies_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default=IDEA, index=True)
    blocked_reason: Mapped[str] = mapped_column(String(400), default="")
    #: the WHY: why it was created, selected, scheduled, or rejected, which
    #: memories and lessons informed it, and which constraints changed it.
    why_json: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def platforms(self) -> list:
        return list(self.platforms_json or [])

    @property
    def dependencies(self) -> list:
        return list(self.dependencies_json or [])

    @property
    def why(self) -> dict:
        return dict(self.why_json or {})


class ProductionCapacity(Base, PKMixin, TimestampMixin):
    """What the workspace can actually produce.

    The planner refuses to schedule beyond this rather than promising a
    workload nobody can make. All rates are per the units in the column names;
    ``locale`` scopes a row so one team can have different capacity per market.
    """

    __tablename__ = "production_capacity"
    __table_args__ = (
        Index("ux_capacity_ws_locale", "workspace_id", "locale", unique=True),
        Index("ix_capacity_ws", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    longform_per_week: Mapped[float] = mapped_column(Float, default=0.0)
    shorts_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    ugc_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    localization_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    #: render/GPU hours available per day
    render_hours_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    #: human review slots per day -- the real bottleneck in most teams
    review_slots_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    locale: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(String(400), default="")

    #: pool -> (column, unit) where unit is the divisor that turns the column
    #: into a per-day figure. ``None`` would be a per-item cap, but every pool
    #: here is a rate, so all are time-based.
    RATE_UNITS: dict[str, tuple[str, float]] = {
        "shorts": ("shorts_per_day", 1.0),
        "ugc": ("ugc_per_day", 1.0),
        "localization": ("localization_per_day", 1.0),
        "render": ("render_hours_per_day", 1.0),
        "review": ("review_slots_per_day", 1.0),
        "longform": ("longform_per_week", 7.0),
    }

    def remaining(self, committed: dict | None = None, *,
                  horizon_days: int = 30) -> dict:
        """Room left in each pool over a HORIZON, after ``committed``.

        Two things this gets right that a naive ``rate - committed`` does not:

        **Units.** The columns are RATES (``shorts_per_day``,
        ``longform_per_week``) but ``committed`` is a COUNT of items across a
        whole planning horizon. Comparing them directly treats a 2/day rate as
        a 2-per-month allowance, so the first plan exhausts the workspace for
        30 days. Rates are therefore scaled to the horizon first.

        **Unset is not zero.** A pool the operator never declared (0.0) is
        UNBOUNDED, not empty. Reporting it as 0.0 would refuse every plan --
        a workspace that declares only render hours could never plan a short --
        and would invent a limit the operator never set. Unbounded pools return
        ``None``, and the caller skips them.
        """
        used = committed or {}
        days = max(1.0, float(horizon_days))
        out: dict[str, float | None] = {}
        for pool, (column, divisor) in self.RATE_UNITS.items():
            rate = float(getattr(self, column) or 0.0)
            if rate <= 0.0:
                out[pool] = None       # unbounded: no limit was declared
                continue
            per_day = rate / divisor
            out[pool] = max(0.0, per_day * days - float(used.get(pool, 0.0)))
        return out

    @property
    def is_unbounded(self) -> bool:
        """True when the operator has declared no limit at all."""
        return not any(float(getattr(self, column) or 0.0)
                       for column, _divisor in self.RATE_UNITS.values())


__all__ = [
    "AGING",
    "AUTONOMY_APPROVAL",
    "AUTONOMY_AUTONOMOUS",
    "AUTONOMY_DISABLED",
    "AUTONOMY_MODES",
    "AUTONOMY_RECOMMEND",
    "BASES",
    "BLOCKED",
    "CANCELLED",
    "DEDUPE_DUPLICATE",
    "DEDUPE_NEW",
    "DEDUPE_RELATED",
    "DEDUPE_SATURATED",
    "DEDUPE_VERDICTS",
    "FRESH",
    "EditorialPlan",
    "EditorialPlanItem",
    "IDEA",
    "IN_PRODUCTION",
    "INFERRED",
    "OBSERVED",
    "PLANNED",
    "PLAN_ITEM_STATUSES",
    "ProductionCapacity",
    "PUBLISHED",
    "READY",
    "RECOMMENDED",
    "RESEARCHING",
    "SCHEDULED",
    "SIGNAL_ACTIVE",
    "SIGNAL_STALE",
    "SIGNAL_STATUSES",
    "SIGNAL_SUPERSEDED",
    "STALE",
    "TrendSignal",
    "TREND_SIGNAL_NOTES",
]

TREND_SIGNAL_NOTES: tuple[str, ...] = (
    "A signal is an observation, never a demand estimate. Nothing here "
    "computes a probability of success.",
    "velocity_json is None until the SAME topic is observed twice; a single "
    "observation has no measurable rate and the consumer must say unknown.",
    "confidence measures how sure we are the thing was SEEN, not how much "
    "demand it represents.",
)
