"""Cost accounting with budget enforcement.

Work 15.7 §11 -- the limit is now DATABASE-ATOMIC.

Before this file, "may this workspace spend?" was answered by a read followed,
later and possibly much later, by a write:

    assert_can_spend()  ->  SELECT sum(amount_usd)   (no lock)
    ... the provider is called ...
    track_cost()        ->  INSERT

Two things are wrong with that shape, and both are money:

1. **It is not atomic.** N workers each read the same total, each conclude
   there is room, and each spends. The cap holds in exactly one process and is
   meaningless behind two. A limit nobody can rely on is a comment, not a limit.
2. **There is no reservation.** A read that is not tied to a write can always be
   overtaken, so "we checked before we spent" is unverifiable after the fact.

So the authoritative gate is :func:`reserve_spend`, which does the check and the
write in ONE transaction while holding a database lock that actually excludes
other writers:

* **PostgreSQL / MySQL** -- ``SELECT id FROM workspaces WHERE id = :ws FOR UPDATE``
  takes a row lock on the workspace. Two transactions for one workspace queue up
  behind each other, and the loser re-reads the total *after* the winner commits.
* **SQLite** -- there is no ``FOR UPDATE``, so the write lock is taken at the
  start of the transaction with ``BEGIN IMMEDIATE``. Without it a reader holds a
  stale snapshot and two writers both insert: measured on this repository's
  SQLite configuration, 16 concurrent reservations against a cap of 5 wrote
  ``total=16.0``. With it, the same 16 write ``total=5.0``. See
  ``tests/test_work15_7_budget_preview.py::test_concurrent_reservations_never_exceed_the_cap``.

A process-local counter may exist in front of this -- as a *cache* of the DB
answer, to save a query -- but never as the limit. That is exactly the shape
Work 15.6 shipped for the voice preview (a dict in the module), and it is what
this revision removes.

**Unknown exposure is a value, not a zero.** :func:`track_cost` drops any amount
``<= 0`` early, which is correct for "this call was free" and catastrophic for
"this call may already have been billed and we lost the response". Booking the
lost response as ``$0`` deletes the charge from the books. So
:func:`book_unknown_exposure` writes a real row with an explicit
``cost_outcome="UNKNOWN_EXPOSURE"`` marker and no amount, which keeps the event
visible to a rate limit and to an operator without inventing a number.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import SessionLocal, session_scope
from app.models import CostEntry
from app.models.base import utcnow

# Rough public price table (USD per 1M tokens). Configurable via data file.
_PRICES_PATH = Path(__file__).resolve().parent.parent / "data" / "model_prices.json"

#: The default spend window. 24h is what ``spent_since`` has always meant by
#: "daily", so it is named rather than hidden behind a number.
DAILY_WINDOW_HOURS = 24.0

#: ``detail_json`` marker on a cost row that records an UNKNOWN exposure rather
#: than an amount. Read by the operator incidents endpoint; never inferred.
UNKNOWN_EXPOSURE_MARKER = "UNKNOWN_EXPOSURE"
#: ``detail_json`` marker on a row that is a spend RESERVATION.
RESERVATION_MARKER = "reservation"


# ---------------------------------------------------------------------------
# Metric provenance (Work 16.5.7 §8)
#
# The vocabulary and the arithmetic that must not lie. Every number an API shows
# belongs to exactly one class:
#
#   MEASURED      a real observation, including a real measured 0
#   DERIVED       computed from measured values by the stated formula
#   UNAVAILABLE   cannot be known; serialised as null, rendered UNAVAILABLE,
#                 NEVER rendered as 0
#
# WHY THESE LIVE HERE AND NOT IN A DISPLAY MODULE
# -------------------------------------------------
# Because they are the BILLING module's own vocabulary: ``UNKNOWN_EXPOSURE_MARKER``
# is already declared above, and every helper below is about not turning an
# unpriceable charge into a number. A display-only helper would have to import
# this module (pulling in a session factory) just to read one string, or
# re-declare the marker and drift from it. Declared once, used by every
# serializer, and pinned to the billing constant by
# ``tests/test_analytics_honesty.py``.
#
# The three rules made impossible by the helpers below:
#   1. missing != 0      -- an absent measurement is None
#   2. unsupported != 0  -- a metric nobody can report is None
#   3. unknown != 0      -- an unpriceable exposure makes a MONEY total None
# ---------------------------------------------------------------------------

MEASURED = "MEASURED"
DERIVED = "DERIVED"
UNAVAILABLE = "UNAVAILABLE"

PROVENANCE_CLASSES = (MEASURED, DERIVED, UNAVAILABLE)


def is_unknown_exposure(row: object) -> bool:
    """Whether one ledger row records an UNKNOWN exposure rather than an amount.

    Two independent signals, because two write paths exist:
    :func:`book_unknown_exposure` sets ``exposure_unknown``, while
    ``reserve_spend(unknown_exposure=True)`` sets only ``cost_outcome``.
    Either is enough to make the row unpriceable.
    """
    detail = getattr(row, "detail_json", None) or {}
    if not isinstance(detail, dict):
        return False
    return bool(detail.get("exposure_unknown")) or (
        detail.get("cost_outcome") == UNKNOWN_EXPOSURE_MARKER
    )


def measured_sum(values) -> int | float | None:
    """Sum observations, or ``None`` when there are none.

    ``sum([0, 0])`` and ``sum([])`` are both ``0`` in Python. For a metric the
    difference is the whole point: two posts that both genuinely recorded zero is
    ``0``; no post reported at all is ``None``. This is the only place that
    distinction is made.
    """
    items = list(values)
    if not items:
        return None
    return sum(items)


def derived_mean(pairs) -> float | None:
    """Weighted mean over ``(value, weight)`` pairs; ``None`` with no weight.

    Pairs with a zero weight are dropped rather than counted as a zero
    observation -- an unweighted ``0`` for something nobody measured is exactly
    the fabrication these helpers exist to stop.
    """
    weighted = [(v, w) for v, w in pairs if w]
    if not weighted:
        return None
    total_weight = sum(w for _, w in weighted)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in weighted) / total_weight


def derived_ratio(numerator: float, denominator: float) -> float | None:
    """A rate. ``None`` when the denominator is zero -- not ``0``.

    ``0/n`` is a real measurement (nothing happened out of many). ``n/0`` is
    arithmetic that does not exist, and reporting it as ``0`` is what made a
    never-run agent look like a 0%-failure one.
    """
    if not denominator:
        return None
    return numerator / denominator


def money_total(rows, *, amount_attr: str = "amount_usd") -> tuple[float | None, int]:
    """Total a set of ledger rows honestly. Returns ``(total, unknown_rows)``.

    ``total`` is:

    * ``None`` when no row exists -- an empty ledger is UNKNOWN-but-zero-
      observed, not proof that nothing was ever spent;
    * ``None`` when ANY row is an UNKNOWN exposure -- summing the priced rows and
      calling it the total would under-report real money as a smaller confident
      number, which is worse than reporting nothing;
    * otherwise the arithmetic sum, which may legitimately be ``0.0`` when rows
      exist and each really cost nothing.

    The count is returned rather than discarded so a caller can say WHY the total
    is unknown instead of silently rendering a dash.
    """
    priced: list[float] = []
    unknown = 0
    for row in rows:
        if is_unknown_exposure(row):
            unknown += 1
            continue
        amount = getattr(row, amount_attr, None)
        if amount is None:
            unknown += 1
            continue
        priced.append(float(amount))
    if unknown or not priced:
        return None, unknown
    return round(sum(priced), 4), 0


def load_prices() -> dict:
    try:
        return json.loads(_PRICES_PATH.read_text())
    except Exception:
        return {
            "default": {"input": 0.15, "output": 0.60},
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "gpt-4o": {"input": 2.50, "output": 10.00},
        }


def estimate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = load_prices()
    p = prices.get(model, prices["default"])
    return (prompt_tokens / 1e6) * p["input"] + (completion_tokens / 1e6) * p["output"]


def track_cost(
    workspace_id: str,
    category: str,
    amount_usd: float,
    provider: str = "",
    cycle_id: str | None = None,
    detail: dict | None = None,
    is_estimate: bool = False,
) -> float:
    if amount_usd <= 0:
        return 0.0
    with session_scope() as s:
        s.add(
            CostEntry(
                workspace_id=workspace_id,
                category=category,
                amount_usd=round(amount_usd, 6),
                provider=provider,
                cycle_id=cycle_id,
                detail_json=detail or {},
                is_estimate=is_estimate,
            )
        )
    logger.debug(f"cost[{category}] ${amount_usd:.4f} ws={workspace_id}")
    return amount_usd


def spent_since(workspace_id: str, hours: float = 24.0) -> float:
    """Priced spend in the window, as a BUDGET-GATE input -- NOT a display metric.

    HONESTY (Work 16.5.7 §11) -- DELIBERATELY UNCHANGED, and the reason matters:

    ``coalesce(sum(amount_usd), 0.0)`` here is not a fabricated display zero.
    This function answers "may we spend?", and a cap that resolved UNKNOWN to
    "spent nothing" would fail OPEN -- every unknown exposure would buy free
    budget. The honest direction for a gate is the conservative one, so an
    unpriceable row is refused room rather than granted it.

    Two things make the gate safe without making it lie:

    * a reservation written with ``unknown_exposure=True`` carries its ESTIMATE
      in ``amount_usd``, so the unknown money is counted against the cap;
    * an ``UNKNOWN_EXPOSURE`` row is still a ROW, so it counts against the rate
      limit (:func:`_window_totals` ``events``), which is what stops a retry
      loop from spinning on an unpriceable call.

    The DISPLAY surface is the opposite and lives in
    ``app.services.cost.money_total``: there, an unpriceable row makes the total
    ``None``. Recorded in docs/ANALYTICS_HONESTY_AUDIT.json as
    ``GATE_INPUT`` / deliberately excluded from the display contract.
    """
    since = utcnow() - timedelta(hours=hours)
    with session_scope() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id,
                CostEntry.created_at >= since,
            )
        )
    return float(total or 0.0)


@dataclass(frozen=True)
class BudgetReservation:
    """A spend permission that has been WRITTEN DOWN before anything was sent.

    ``entry_id`` is the row that holds the permission. It is not a receipt: the
    same row is updated in place by :func:`settle_reservation` when the real
    amount arrives, so a reservation plus its settlement is ONE ledger row, not
    two. A reservation plus a separate ``track_cost`` would double-count the
    estimate against the actual, which is the accounting bug the single-row
    design exists to make impossible.
    """

    workspace_id: str
    category: str
    provider: str
    amount_usd: float
    entry_id: str
    window_hours: float
    window_count: int
    spent_in_window: float
    unknown_exposure: bool = False

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "category": self.category,
            "provider": self.provider,
            "amount_usd": round(float(self.amount_usd), 6),
            "entry_id": self.entry_id,
            "window_hours": self.window_hours,
            "window_count": self.window_count,
            "spent_in_window": round(float(self.spent_in_window), 6),
            "unknown_exposure": self.unknown_exposure,
        }


@contextmanager
def exclusive_workspace_lock(session: Session, workspace_id: str) -> Iterator[None]:
    """Hold the database lock that serialises spend for one workspace.

    The lock must be taken BEFORE the totals are read, or the read is a snapshot
    of a moment nobody is holding:

    * SQLite has no ``FOR UPDATE``, so the write lock is taken up front with
      ``BEGIN IMMEDIATE``. Deferred transactions read a snapshot first and lose
      the write race in exactly the way that breaches a cap.
    * Everything else takes a row lock on the workspace row. Every workspace
      already has one (it is the tenant root), so this needs no new table.

    ``FOR UPDATE`` is emitted only when the dialect is not SQLite; SQLite
    rejects the syntax outright rather than ignoring it, which would be a silent
    loss of the guarantee on the one database most deployments actually run.
    """
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.execute(
            text("SELECT id FROM workspaces WHERE id = :ws FOR UPDATE"),
            {"ws": str(workspace_id or "")},
        )
    yield


@dataclass(frozen=True)
class BudgetHeadroom:
    """What is left, read under the workspace lock. No write, no reservation.

    Returned by :func:`budget_headroom` so a caller that only wants to ASK gets
    the same arithmetic :func:`reserve_spend` uses. Two implementations of "is
    there room" is how a check and a gate drift apart, and the one that drifts is
    always the check.
    """

    spent_usd: float
    events: int
    daily_cap_usd: float
    per_call_cap_usd: float

    @property
    def remaining_usd(self) -> float:
        return self.daily_cap_usd - self.spent_usd

    def to_dict(self) -> dict:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "events": self.events,
            "daily_cap_usd": round(self.daily_cap_usd, 6),
            "per_call_cap_usd": round(self.per_call_cap_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
        }


def _window_totals(session: Session, workspace_id: str, *, hours: float,
                   category: str, window_seconds: float) -> tuple[float, int]:
    """``(spent_usd, events)`` in the window. Caller must hold the lock.

    ``hours`` bounds the MONEY window (the daily cap); ``window_seconds`` bounds
    the EVENT window (the rate cap). They are separate because they are
    separate policies: $5/day and 20 previews/minute are not the same question,
    and conflating them is how a rate limit ends up enforcing a dollar budget
    with the wrong units.
    """
    if window_seconds:
        since = utcnow() - timedelta(seconds=float(window_seconds))
    else:
        since = utcnow() - timedelta(hours=float(hours))
    # HONESTY: unchanged on purpose, same reasoning as ``spent_since`` above.
    # This is the arithmetic a BUDGET CAP refuses against, and an unresolved
    # unknown exposure must never read as "no money spent" here. It is
    # conservative by design; the honest DISPLAY total is
    # ``app.services.cost.money_total``.
    spent = float(session.scalar(
        select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
            CostEntry.workspace_id == workspace_id,
            CostEntry.created_at >= since,
        )
    ) or 0.0)
    events = int(session.scalar(
        select(func.count(CostEntry.id)).where(
            CostEntry.workspace_id == workspace_id,
            CostEntry.category == category,
            CostEntry.created_at >= since,
        )
    ) or 0)
    return spent, events


def budget_headroom(
    workspace_id: str,
    *,
    category: str,
    window_hours: float = DAILY_WINDOW_HOURS,
    window_seconds: float = 0.0,
    daily_cap_usd: float | None = None,
    per_call_cap_usd: float | None = None,
) -> BudgetHeadroom:
    """Read what is left, holding the lock so the answer cannot be stale.

    A NON-RESERVING read. It is honest about what it is: it cannot stop a
    concurrent spender, so a caller that must not overspend uses
    :func:`reserve_spend`. This exists for "may I even show this button?" and
    for the advisory legacy gate.
    """
    limits_daily, limits_per_call = _workspace_budget_limits(workspace_id)
    daily_cap = float(limits_daily if daily_cap_usd is None else daily_cap_usd)
    per_call_cap = float(limits_per_call if per_call_cap_usd is None else per_call_cap_usd)
    with _reservation_session() as session, \
            exclusive_workspace_lock(session, workspace_id):
        spent, events = _window_totals(
            session, workspace_id, hours=float(window_hours or DAILY_WINDOW_HOURS),
            category=category, window_seconds=float(window_seconds or 0.0))
    return BudgetHeadroom(
        spent_usd=spent, events=events,
        daily_cap_usd=daily_cap, per_call_cap_usd=per_call_cap)


def reserve_spend(
    workspace_id: str,
    estimated_usd: float = 0.0,
    *,
    category: str,
    provider: str = "",
    detail: dict | None = None,
    window_hours: float = DAILY_WINDOW_HOURS,
    max_events: int = 0,
    window_seconds: float = 0.0,
    unknown_exposure: bool = False,
    daily_cap_usd: float | None = None,
    per_call_cap_usd: float | None = None,
) -> BudgetReservation:
    """Atomically decide "may this spend happen?" and record the answer.

    This is the authoritative gate. Everything it enforces is decided while the
    workspace lock is held and is committed together with the row that proves
    the permission was taken, so two concurrent callers can never both be told
    yes for the same last unit of budget.

    Limits enforced, all inside the one transaction:

    * ``per_call_cap_usd`` / the workspace's ``per_video_budget_usd`` -- this one
      call may not exceed the per-call ceiling;
    * ``daily_cap_usd`` / the workspace's ``daily_budget_usd`` -- the window total
      including THIS reservation may not exceed the daily cap;
    * ``max_events`` -- at most N reservations of ``category`` inside
      ``window_seconds``. This is the rate limit, and it is counted from the
      database rather than from a dict, because a dict is per-process and a rate
      limit that is per-process is per-replica.
    * the OPTIONAL rollup ceilings -- ``services.budget_rollup`` (Work 16 §11).
      Cross-category: a workspace total and a deployment total, over ALL
      categories rather than one. Unset means unset: no ceiling, no behaviour
      change, no extra write.

    ``unknown_exposure=True`` records "money may have been spent and we do not
    know the amount". The row exists, the event is counted against
    ``max_events``, and the amount is NOT invented. This is the case
    :func:`track_cost` would have deleted.

    Raises :class:`BudgetExceededError` -- and writes nothing -- on refusal.
    """
    amount = float(estimated_usd or 0.0)
    if amount < 0:
        raise ValueError(f"a reservation cannot be negative: {amount}")
    hours = float(window_hours or DAILY_WINDOW_HOURS)
    limits_daily, limits_per_call = _workspace_budget_limits(workspace_id)
    daily_cap = float(limits_daily if daily_cap_usd is None else daily_cap_usd)
    per_call_cap = float(limits_per_call if per_call_cap_usd is None else per_call_cap_usd)

    if amount > per_call_cap:
        raise BudgetExceededError(
            f"estimated cost ${amount:.2f} exceeds per-video budget ${per_call_cap:.2f}"
        )

    # Work 16 §11: could ANY cross-category ceiling apply to this workspace?
    #
    # Asked HERE, on the reservation's own session and BEFORE the lock is taken
    # -- never inside it. That placement is a correctness requirement, not an
    # optimisation: on SQLite ``exclusive_workspace_lock`` holds
    # ``BEGIN IMMEDIATE``, which is the whole DATABASE's write lock, so one extra
    # statement inside it is paid for by every other writer. Measured on this
    # configuration (16 threads x 12 rounds): 192/192 reservations in 2.3s
    # without the rollup, versus 183 with 9 ``database is locked`` failures in
    # 8.0s with a single trivial statement added inside the lock. Shipping the
    # check unconditionally would degrade Work 15.7's own guarantee, so an
    # unconfigured deployment pays nothing in the critical section and the
    # locked transaction stays byte-for-byte what 15.7 shipped. See
    # ``services.budget_rollup.rollup_required``.
    from app.services import budget_rollup as _rollup

    with _reservation_session() as session:
        rollup_needed = _rollup.rollup_required(session, workspace_id)

        with exclusive_workspace_lock(session, workspace_id):
            # Re-read the totals INSIDE the lock. A read taken before the lock
            # belongs to a snapshot the lock does not cover.
            spent, count = _window_totals(
                session, workspace_id, hours=hours, category=category,
                window_seconds=float(window_seconds or 0.0))

            if max_events and count >= int(max_events):
                raise RateLimitExceeded(
                    f"at most {int(max_events)} '{category}' operations per "
                    f"{float(window_seconds):.0f}s per workspace "
                    f"({count} already recorded)"
                )
            if spent + amount > daily_cap:
                raise BudgetExceededError(
                    f"daily budget exhausted (${daily_cap - spent:.2f} left, "
                    f"this call needs ${amount:.2f})"
                )

            # Work 16 §11: the OPTIONAL cross-category levels, in the same
            # transaction and under the same lock, so
            #     global/system -> workspace total -> category -> operation
            # is a hierarchy every level of which must agree. Nothing above is
            # weakened: the per-call cap, the daily cap and the rate limit were
            # already decided, and this only ever refuses MORE. The check is a
            # pure predicate -- it writes no row -- so the reservation identity
            # still produces exactly one ledger row.
            if rollup_needed:
                _rollup.assert_within_rollups(
                    session, workspace_id=workspace_id, amount_usd=amount,
                    category=category, provider=provider)

            payload = dict(detail or {})
            payload[RESERVATION_MARKER] = True
            payload["window_hours"] = hours
            if window_seconds:
                payload["window_seconds"] = float(window_seconds)
            if unknown_exposure:
                payload["cost_outcome"] = UNKNOWN_EXPOSURE_MARKER
                payload["exposure_unknown"] = True
            entry = CostEntry(
                workspace_id=workspace_id,
                category=category,
                amount_usd=round(amount, 6),
                provider=provider,
                detail_json=payload,
                is_estimate=True,
            )
            session.add(entry)
            session.flush()
            entry_id = entry.id
            new_count = count + 1
            new_spent = spent + amount
        # Commit outside the lock context but inside the session scope: the
        # COMMIT is what publishes the reservation, and it is the only thing
        # another transaction waits for.
    return BudgetReservation(
        workspace_id=workspace_id,
        category=category,
        provider=provider,
        amount_usd=amount,
        entry_id=entry_id,
        window_hours=hours,
        window_count=new_count,
        spent_in_window=new_spent,
        unknown_exposure=unknown_exposure,
    )


@contextmanager
def _reservation_session() -> Iterator[Session]:
    """A session that COMMITS a reservation and ROLLS BACK a refusal.

    ``session_scope`` already does this, but it is imported for the read paths
    too and this name says what the transaction is for. Nothing is written on
    refusal: a rejected reservation must leave no trace, or the next attempt
    sees a spend that never happened.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def settle_reservation(entry_id: str, actual_usd: float) -> None:
    """Replace a reservation's ESTIMATE with the real amount, on the same row.

    Updating rather than inserting is the point: an estimate already counted
    against the daily cap, so a second row would bill the same operation twice.
    An actual below the estimate also RELEASES the difference back to the cap,
    which is what makes an over-estimate self-correcting instead of sticky.
    """
    amount = float(actual_usd or 0.0)
    if amount < 0:
        raise ValueError(f"a settled cost cannot be negative: {amount}")
    with session_scope() as s:
        entry = s.get(CostEntry, entry_id)
        if entry is None:
            # A reservation that no longer exists cannot be settled. That is a
            # bookkeeping loss, not a second charge: say so instead of
            # inserting a replacement row that would double-count.
            logger.warning(
                "cost: reservation %s vanished before settlement; "
                "no row written to avoid double counting", entry_id)
            return
        detail = dict(entry.detail_json or {})
        detail.pop("exposure_unknown", None)
        detail.pop("cost_outcome", None)
        detail["cost_outcome"] = "ACTUAL"
        detail["estimated_usd"] = round(float(entry.amount_usd or 0.0), 6)
        entry.amount_usd = round(amount, 6)
        entry.is_estimate = False
        entry.detail_json = detail


def book_unknown_exposure(
    workspace_id: str,
    *,
    category: str,
    provider: str = "",
    detail: dict | None = None,
) -> str:
    """Record that money MAY have been spent and the amount is unknown.

    :func:`track_cost` returns early on ``amount_usd <= 0``, which is right for
    a genuinely free call and wrong here: a lost response is not a $0 charge, it
    is a charge nobody can price yet. Booking ``0.0`` through ``track_cost``
    would make it invisible in the ledger, in the daily total and in the
    operator's incident list. So this writes the row directly, marks it
    ``UNKNOWN_EXPOSURE``, and invents no number.

    Returns the new row's id.
    """
    payload = dict(detail or {})
    payload["cost_outcome"] = UNKNOWN_EXPOSURE_MARKER
    payload["exposure_unknown"] = True
    with session_scope() as s:
        entry = CostEntry(
            workspace_id=workspace_id,
            category=category,
            amount_usd=0.0,
            provider=provider,
            detail_json=payload,
            is_estimate=True,
        )
        s.add(entry)
        s.flush()
        return str(entry.id)


def void_reservation(entry_id: str) -> bool:
    """Delete a reservation. ONLY for a submit that provably never happened.

    A cancelled-before-submit call billed nothing, so keeping its row would
    understate the remaining budget for no reason. A call that may have reached
    the provider must NOT be voided -- that is the case
    :func:`book_unknown_exposure` exists for. Returns whether a row was removed.
    """
    with session_scope() as s:
        entry = s.get(CostEntry, entry_id)
        if entry is None:
            return False
        s.delete(entry)
        return True


def _workspace_budget_limits(workspace_id: str) -> tuple[float, float]:
    """Workspace safety budgets, falling back to global settings.

    Safety Center values live in ``Workspace.settings_json["safety"]``; the
    decision engine already honors them, so budget enforcement must read the
    same source or a workspace's configured caps are silently ignored here.
    A lookup failure falls back to global defaults rather than blocking spend.
    """
    from app.models import Workspace

    daily = float(settings.daily_budget_usd)
    per_video = float(settings.per_video_budget_usd)
    try:
        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if ws and ws.settings_json:
                safety = ws.settings_json.get("safety") or {}
                daily = float(safety.get("daily_budget_usd", daily))
                per_video = float(safety.get("per_video_budget_usd", per_video))
    except (TypeError, ValueError):
        logger.warning(f"invalid workspace budget config for {workspace_id}; using defaults")
    return daily, per_video


def budget_available(workspace_id: str) -> tuple[bool, float]:
    """Returns (within_budget, remaining_daily_budget)."""
    spent = spent_since(workspace_id, hours=24.0)
    daily, _ = _workspace_budget_limits(workspace_id)
    remaining = daily - spent
    return remaining > 0, remaining


def assert_can_spend(
    workspace_id: str,
    estimated_usd: float,
    *,
    category: str = "",
    provider: str = "",
    reserve: bool = False,
    max_events: int = 0,
    window_seconds: float = 0.0,
    unknown_exposure: bool = False,
) -> BudgetReservation | None:
    """Gate a spend. Returns the reservation when it reserved, else ``None``.

    Two modes, and the difference is the whole point of Work 15.7 §11:

    * ``reserve=True`` (with a ``category``) delegates to :func:`reserve_spend`
      and is **authoritative**: the answer and the proof are one committed
      transaction, so N concurrent callers cannot all be told yes.
    * the default is the legacy **advisory** read that every provider's
      pre-spend gate has always used. It cannot be authoritative -- a read with
      no write cannot exclude a concurrent spender -- and it is documented as
      such rather than quietly relied upon. Callers in code this change does not
      own still arrive here; every path this change DOES own passes a category
      so it gets the authoritative branch.
    """
    if reserve:
        if not category:
            raise ValueError(
                "reserve=True needs a category: the reservation is the ledger "
                "row, and a row without a category cannot be counted")
        return reserve_spend(
            workspace_id, estimated_usd, category=category, provider=provider,
            max_events=max_events, window_seconds=window_seconds,
            unknown_exposure=unknown_exposure)

    ok, remaining = budget_available(workspace_id)
    if not ok:
        raise BudgetExceededError(f"daily budget exhausted (${remaining:.2f} left)")
    _, per_video = _workspace_budget_limits(workspace_id)
    if estimated_usd > per_video:
        raise BudgetExceededError(
            f"estimated video cost ${estimated_usd:.2f} exceeds per-video budget "
            f"${per_video:.2f}"
        )
    return None


class BudgetExceededError(Exception):
    pass


class RateLimitExceeded(BudgetExceededError):
    """The refusal was a RATE limit, not a money limit.

    A subclass, so existing ``except BudgetExceededError`` handlers keep
    working, and so a caller can tell "this workspace is out of money" (402,
    do not retry today) from "this workspace is clicking too fast" (429, retry
    in a minute) instead of pattern-matching an error message.
    """


__all__ = [
    "BudgetExceededError",
    "BudgetHeadroom",
    "BudgetReservation",
    "DAILY_WINDOW_HOURS",
    "DERIVED",
    "MEASURED",
    "PROVENANCE_CLASSES",
    "RESERVATION_MARKER",
    "RateLimitExceeded",
    "UNAVAILABLE",
    "UNKNOWN_EXPOSURE_MARKER",
    "assert_can_spend",
    "book_unknown_exposure",
    "budget_available",
    "budget_headroom",
    "derived_mean",
    "derived_ratio",
    "estimate_llm_cost",
    "exclusive_workspace_lock",
    "is_unknown_exposure",
    "measured_sum",
    "money_total",
    "reserve_spend",
    "settle_reservation",
    "spent_since",
    "track_cost",
    "void_reservation",
]
