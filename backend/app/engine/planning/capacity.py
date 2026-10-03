"""Work 15 §8 — production capacity, and refusing impossible workloads.

The planner must not schedule work nobody can make. ``ProductionCapacity`` is
per-workspace and per-locale; this module turns it into a *commitment ledger* for
a planning horizon and answers one question:

    can this many items of this format, at this cost, fit — and if not, what
    exactly is short?

An over-capacity plan is ``BLOCKED`` with the specific exhausted resource named
(shorts/day, review slots, render hours, budget). "Blocked" without a reason is
useless to an operator, so every refusal carries the resource and the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.planning import (
    EditorialPlan,
    EditorialPlanItem,
    ProductionCapacity,
)

__all__ = [
    "CapacityDecision",
    "CapacityLedger",
    "FORMAT_COST_DEFAULTS",
    "format_commitment",
    "get_capacity",
    "load_ledger",
    "upset_capacity",
]

#: Default estimated cost per format, used when a plan item has no estimate.
#: These are YMONEY's planning defaults, NOT a vendor price list; an item's own
#: ``estimated_cost_usd`` always wins.
FORMAT_COST_DEFAULTS: dict[str, float] = {
    "SHORT": 1.20,
    "LONGFORM": 6.50,
    "UGC": 0.90,
    "LOCALIZATION": 0.40,
    "STATIC": 0.30,
}

#: Which capacity pool each format draws on.
FORMAT_POOL = {
    "SHORT": "shorts",
    "LONGFORM": "longform",
    "UGC": "ugc",
    "LOCALIZATION": "localization",
    "STATIC": "review",
}

#: Every item needs a human review slot; this is the real bottleneck in most
#: teams, so it is checked even when the format looks cheap.
REVIEW_POOL = "review"


@dataclass
class CapacityDecision:
    """Whether a batch fits, and precisely what is short."""

    fits: bool
    reasons: list[str] = field(default_factory=list)
    #: resource -> (available, requested)
    shortfalls: dict[str, tuple[float, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"fits": self.fits, "reasons": list(self.reasons),
                "shortfalls": {k: {"available": v[0], "requested": v[1]}
                               for k, v in self.shortfalls.items()}}


@dataclass
class CapacityLedger:
    """Committed capacity for a horizon, derived from live plan items."""

    locale: str = ""
    #: pool -> committed units
    committed: dict[str, float] = field(default_factory=dict)
    #: cost committed by plan items
    committed_cost: float = 0.0
    #: items counted, for the audit trail
    item_ids: list[str] = field(default_factory=list)

    def add(self, pool: str, units: float) -> None:
        self.committed[pool] = self.committed.get(pool, 0.0) + float(units)

    def to_dict(self) -> dict:
        return {"locale": self.locale, "committed": dict(self.committed),
                "committed_cost": round(self.committed_cost, 4),
                "item_count": len(self.item_ids)}


def format_commitment(content_format: str) -> tuple[str, float]:
    """(capacity pool, units) for one item of this format."""
    pool = FORMAT_POOL.get(str(content_format).upper())
    if pool is None:
        # An unknown format still consumes review capacity: a human must look
        # at it before it ships.
        pool = REVIEW_POOL
    return pool, 1.0


def get_capacity(db, workspace_id: str, *, locale: str = "") -> ProductionCapacity | None:
    """The capacity row for this workspace/locale, or ``None`` when unset.

    ``None`` means *unbounded*: the operator has declared no limit, so the
    planner must not invent one and claim the workload is infeasible.
    """
    return db.scalar(select(ProductionCapacity).where(
        ProductionCapacity.workspace_id == workspace_id,
        ProductionCapacity.locale == (locale or "")))


def upset_capacity(db, workspace_id: str, *, locale: str = "",
                   longform_per_week: float = 0.0, shorts_per_day: float = 0.0,
                   ugc_per_day: float = 0.0,
                   localization_per_day: float = 0.0,
                   render_hours_per_day: float = 0.0,
                   review_slots_per_day: float = 0.0,
                   notes: str = "") -> ProductionCapacity:
    """Create or update the capacity row (idempotent per workspace+locale)."""
    row = get_capacity(db, workspace_id, locale=locale)
    if row is None:
        row = ProductionCapacity(workspace_id=workspace_id, locale=locale or "")
        db.add(row)
    row.longform_per_week = float(longform_per_week)
    row.shorts_per_day = float(shorts_per_day)
    row.ugc_per_day = float(ugc_per_day)
    row.localization_per_day = float(localization_per_day)
    row.render_hours_per_day = float(render_hours_per_day)
    row.review_slots_per_day = float(review_slots_per_day)
    row.notes = notes[:400]
    db.flush()
    return row


#: Item states that no longer consume forward capacity.
_DONE_STATUSES = ("PUBLISHED", "CANCELLED")


def load_ledger(db, workspace_id: str, *, plan_id: str = "",
                horizon_days: int = 30, locale: str = "",
                exclude_item_ids: set[str] | None = None) -> CapacityLedger:
    """Sum committed capacity for a horizon from live plan items.

    Only items inside the horizon and not finished count, so a plan does not
    over-reserve against work that already shipped or was cancelled.

    ``locale`` scopes which items are counted. An item's locale is read from
    its plan's ``constraints["locale"]``; the single-market case (every plan
    stored with the default empty locale) keeps working because the default
    plan is itself stored with the empty locale.
    """
    ledger = CapacityLedger(locale=locale)
    now = datetime.now(UTC)
    horizon_end = now + timedelta(days=max(1, int(horizon_days)))
    query = select(EditorialPlanItem).where(
        EditorialPlanItem.workspace_id == workspace_id)
    if plan_id:
        query = query.where(EditorialPlanItem.plan_id == plan_id)
    rows = db.scalars(query).all()
    # plan_id -> locale, so a per-market ledger only counts its own market
    plan_locale: dict[str, str] = {}
    for row in rows:
        if row.plan_id not in plan_locale:
            parent = db.get(EditorialPlan, row.plan_id)
            plan_locale[row.plan_id] = str(
                (parent.constraints or {}).get("locale", "") if parent else "")
    for row in rows:
        if exclude_item_ids and row.id in exclude_item_ids:
            continue
        if row.status in _DONE_STATUSES:
            continue
        # a market's capacity is not shared with another market's
        if plan_locale.get(row.plan_id, "") != (locale or ""):
            continue
        if row.target_date is not None:
            target = row.target_date
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            if target > horizon_end:
                continue
        pool, units = format_commitment(row.content_format)
        ledger.add(pool, units)
        # every item needs review, whatever its format
        ledger.add(REVIEW_POOL, 1.0)
        ledger.committed_cost += float(row.estimated_cost_usd or 0.0)
        ledger.item_ids.append(row.id)
    return ledger


def check_capacity(capacity: ProductionCapacity | None, ledger: CapacityLedger,
                   *, items: list[dict], budget_remaining: float | None = None,
                   horizon_days: int = 30) -> CapacityDecision:
    """Can ``items`` fit alongside what is already committed?

    ``items`` is a list of ``{"content_format", "estimated_cost_usd"}``. A
    ``None`` capacity means unbounded and is not checked. ``budget_remaining``
    of ``None`` means the budget is not enforced at this layer.

    A pool the operator did not declare is UNBOUNDED and skipped; only a
    declared pool can produce a shortfall.
    """
    decision = CapacityDecision(fits=True)
    requested: dict[str, float] = {}
    cost = 0.0
    for item in items:
        content_format = str(item.get("content_format", ""))
        pool, units = format_commitment(content_format)
        requested[pool] = requested.get(pool, 0.0) + units
        requested[REVIEW_POOL] = requested.get(REVIEW_POOL, 0.0) + 1.0
        # `is None`, not `or`: a $0.00 estimate is a real estimate, and
        # treating 0.0 as falsy would silently charge the format default to an
        # item the operator priced at nothing.
        raw_cost = item.get("estimated_cost_usd")
        if raw_cost is None:
            raw_cost = FORMAT_COST_DEFAULTS.get(content_format.upper(), 1.0)
        cost += float(raw_cost)

    if capacity is not None and not capacity.is_unbounded:
        available = capacity.remaining(ledger.committed,
                                       horizon_days=horizon_days)
        for pool, want in requested.items():
            room = available.get(pool)
            if room is None:
                continue        # unbounded pool: no limit was declared
            if want > room:
                decision.fits = False
                decision.shortfalls[pool] = (round(room, 3), round(want, 3))
                decision.reasons.append(
                    f"{pool}: {room:g} available over {horizon_days}d, "
                    f"{want:g} requested")
    else:
        decision.reasons.append(
            "capacity unbounded: this workspace declares no limits, so only "
            "the budget constrains this batch")

    if budget_remaining is not None:
        committed = ledger.committed_cost + cost
        remaining = float(budget_remaining) - committed
        if remaining < 0:
            decision.fits = False
            decision.shortfalls["budget"] = (round(float(budget_remaining), 4),
                                            round(committed, 4))
            decision.reasons.append(
                f"budget: ${float(budget_remaining):.2f} approved, "
                f"${committed:.2f} would be committed")
    return decision
