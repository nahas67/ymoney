"""Cross-category budget ROLLUPS -- the ceilings a per-category cap cannot see.

Work 16 §11, closing the gap Work 15.9 named: *"there is no cross-category
rollup, so total daily spend across categories can exceed any single cap."*

Before this module every cap in YMONEY answered **"may this CATEGORY afford
this?"**. None answered **"may this WORKSPACE afford this?"**, and the two are
not the same question. A workspace with $5/day for ``llm`` and $5/day for
``tts`` has spent $10 against limits that each read $5. The category caps are
individually honest and collectively useless as a total, and nothing noticed
because no operator ever asks a per-category question when they ask about money.

So the hierarchy this module completes is::

    global/system  ->  workspace total  ->  category  ->  operation

and the rule that makes it a hierarchy rather than a pile of limits is that **a
reservation must satisfy every level that is configured**. This module does not
replace, weaken or reinterpret the Work 15.7 category / per-call caps --
:func:`app.services.cost.reserve_spend` still enforces those first, and this
check runs after them, in the SAME transaction, under the SAME workspace lock.

**It is opt-in, and "not configured" is a real value.** A cap column is NULL
when no ceiling was chosen. There is no default ceiling hiding in a column
default, no migration backfill, no sentinel number: a deployment that never
writes a row behaves exactly as it did before Work 16 §11 -- including the SQL it
runs, because the table-presence probe is cached per dialect after the first
answer. ``0.0`` is NOT the same as NULL: zero is a real ceiling and refuses
everything, so the two are never collapsed into each other.

**Atomicity is inherited from the lock, not re-implemented.**
:func:`assert_within_rollups` is a pure predicate over the CURRENT transaction:
it reads, it decides, it raises. It writes nothing and inserts no row, which is
what keeps "one reservation identity -> exactly one ledger row" true; the single
``CostEntry`` insert stays in :func:`app.services.cost.reserve_spend` where it
already was. Two callers racing the last dollar are already serialised by
``exclusive_workspace_lock``, and the loser re-reads these totals AFTER the
winner commits, so the rollup sees the winner's money. Reading the totals
outside that lock is the one way to break this, and this module deliberately
offers no API for it.

**Cross-workspace caps need their own lock, and the order is fixed.** A system
ceiling sums every row in ``cost_entries``, so two DIFFERENT workspaces can race
it and the workspace lock cannot help -- it excludes per workspace, not
globally. The system row is therefore locked with ``FOR UPDATE`` too, and the
acquisition order is always workspace-then-system. One order with no cycle
cannot deadlock; the reverse order could, and nothing in a test that only ever
used one workspace would ever show it.

**Money windows are named, not implied.** ``daily`` is the same rolling 24h
window :data:`app.services.cost.DAILY_WINDOW_HOURS` has always meant -- the
rollup deliberately reuses the existing window rather than inventing a second
answer to "since when". ``monthly`` is the **UTC calendar month to date**, not
"the last 30 days": a 30-day window drifts, covering two months when written on
the 31st, so a tenant's monthly ceiling would move without anybody editing it.

What this module deliberately does NOT do:

* it does not re-implement the daily cap, the per-call cap or the rate limit.
  Those live in ``services.cost``; two implementations of "is there room" is
  how a check and a gate drift apart, and the one that drifts is always the
  check;
* it does not police :func:`app.services.cost.settle_reservation`. A
  provider-reported actual above its estimate is money ALREADY SPENT, and
  refusing the write would erase a real charge from the books -- the exact
  failure ``book_unknown_exposure`` exists to prevent. Caps gate what may be
  STARTED;
* it does not touch Work 15.9's authority model. ``SYSTEM_OWNED`` still needs
  an explicitly configured system budget, ``OwnerlessSpendRefused`` is still
  raised before anything is sent, and a system-scope rollup here is a ceiling
  ON TOP of that, never a substitute for it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CostEntry
from app.models.base import utcnow
from app.services.cost import DAILY_WINDOW_HOURS, BudgetExceededError

#: The table migration 0037 owns. A constant, never anything an operator can
#: reach: it appears only in this module's own catalog probes and statements.
TABLE = "budget_rollup_limits"

#: The two altitudes. A ``workspace`` row carries a tenant id; the ``system``
#: row is the one with an empty ``workspace_id``.
SCOPE_WORKSPACE = "workspace"
SCOPE_SYSTEM = "system"

#: The system row's ``workspace_id``: empty, matching the column default. A
#: system-wide ceiling is not a tenant that happens to be named "".
SYSTEM_WORKSPACE_ID = ""

#: The named spend windows. A cap that does not say which window it bounds is a
#: cap nobody can reason about at 3am.
WINDOW_DAILY = "daily"
WINDOW_MONTHLY = "monthly"

#: One statement fetches BOTH levels. It is one round trip rather than two on
#: purpose: this runs INSIDE the reservation transaction while the workspace lock
#: is held, and on SQLite that lock is the whole database's write lock. Two
#: catalog-sized SELECTs per reservation measurably widened the window in which
#: sixteen racing writers queue behind each other -- enough to push a busy
#: timeout over on the existing 15.7 concurrency test, which is a regression in
#: somebody else's guarantee and therefore not an acceptable price for a
#: feature that is off by default.
_SELECT_BOTH = (
    f'SELECT id, scope, workspace_id, daily_total_cap, monthly_total_cap, '
    f'enabled, meta_json FROM "{TABLE}" '
    f"WHERE (scope = '{SCOPE_WORKSPACE}' AND workspace_id = :ws) "
    f"OR (scope = '{SCOPE_SYSTEM}' AND workspace_id = '{SYSTEM_WORKSPACE_ID}')"
)

#: Cheap existence probe, run OUTSIDE the reservation lock. One indexed point
#: lookup whose only answer is "could any ceiling possibly apply?" -- it exists
#: so the common case costs nothing INSIDE the lock, which is a hard requirement
#: rather than an optimisation. See :func:`rollup_required` for the measurement.
_SELECT_ANY = (
    f"SELECT 1 FROM {TABLE} WHERE scope = :scope AND workspace_id = :ws "
    f"UNION ALL SELECT 1 FROM {TABLE} WHERE scope = :scope2 "
    f"AND workspace_id = :ws2"
)

_SELECT_ONE = (
    f'SELECT id, scope, workspace_id, daily_total_cap, monthly_total_cap, '
    f'enabled, meta_json FROM "{TABLE}" '
    f"WHERE scope = :scope AND workspace_id = :ws"
)

#: Per-dialect cache of "has migration 0037 created the table on this engine?".
#: Normally ONE catalog read per process, because the alternative -- catching a
#: failed SELECT -- is not available: on PostgreSQL a failed statement aborts the
#: transaction and every later statement in the reservation dies with 25P02,
#: turning "this deployment has not migrated" into "spending is broken".
#: Reading the catalog cannot fail, so nothing is caught and no transaction is
#: ever poisoned. A downgrade invalidates this cache, which is acceptable in the
#: same way every other cached schema fact here is: a downgrade runs with the
#: application stopped.
_TABLE_PRESENT: dict[str, bool] = {}


class RollupRefusal(BudgetExceededError):
    """A cross-category ceiling said no.

    A :class:`~app.services.cost.BudgetExceededError` subclass for the same
    reason :class:`~app.services.paid_provider.OwnerlessSpendRefused` is one: this
    IS a money refusal, so every existing ``except BudgetExceededError`` handler
    already treats it correctly -- nothing was sent, nothing was billed -- while
    a caller that cares can catch this one and read WHICH ceiling said no.

    Carries the arithmetic rather than only the verdict, because "the workspace
    total is exhausted" and "this tenant hit its monthly ceiling at 09:14 on
    the 3rd" are different pages of the same incident.
    """

    def __init__(self, *, scope: str, window: str, workspace_id: str,
                 cap_usd: float, spent_usd: float, amount_usd: float,
                 category: str = "", provider: str = "") -> None:
        self.scope = str(scope)
        self.window = str(window)
        self.workspace_id = str(workspace_id or "")
        self.cap_usd = round(float(cap_usd), 6)
        self.spent_usd = round(float(spent_usd), 6)
        self.amount_usd = round(float(amount_usd), 6)
        self.category = str(category or "")
        self.provider = str(provider or "")
        where = ("YMONEY's total" if self.scope == SCOPE_SYSTEM
                 else f"workspace {self.workspace_id or '<empty>'}")
        super().__init__(
            f"{self.window} budget rollup exhausted for {where}: "
            f"${self.cap_usd:.2f} cap, ${self.spent_usd:.2f} already spent, "
            f"${self.cap_usd - self.spent_usd:.2f} left, this call needs "
            f"${self.amount_usd:.2f} "
            f"(category='{self.category or '-'}', provider='{self.provider or '-'}')"
        )

    def to_dict(self) -> dict:
        return {
            "reason": "budget_rollup_exceeded",
            "scope": self.scope,
            "window": self.window,
            "workspace_id": self.workspace_id,
            "cap_usd": self.cap_usd,
            "spent_usd": self.spent_usd,
            "amount_usd": self.amount_usd,
            "category": self.category,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class RollupLimit:
    """One configured ceiling, as read.

    ``daily_total_cap is None`` means the ceiling was never chosen. It stays
    ``None`` all the way through this module and is never collapsed to ``0.0``,
    because that collapse IS the bug: ``0.0`` refuses everything, and "not
    configured" must refuse nothing.
    """

    scope: str
    workspace_id: str = ""
    daily_total_cap: float | None = None
    monthly_total_cap: float | None = None
    enabled: bool = True
    meta: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """Whether this row enforces anything at all."""
        return bool(self.enabled
                    and (self.daily_total_cap is not None
                         or self.monthly_total_cap is not None))

    def cap_for(self, window: str) -> float | None:
        if window == WINDOW_DAILY:
            return self.daily_total_cap
        if window == WINDOW_MONTHLY:
            return self.monthly_total_cap
        raise ValueError(f"unknown rollup window: {window!r}")

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "workspace_id": self.workspace_id,
            "daily_total_cap": (None if self.daily_total_cap is None
                                else round(float(self.daily_total_cap), 6)),
            "monthly_total_cap": (None if self.monthly_total_cap is None
                                  else round(float(self.monthly_total_cap), 6)),
            "enabled": bool(self.enabled),
            "configured": self.configured,
        }


#: What "nothing is configured" looks like -- a real value, not ``None``, so a
#: caller cannot confuse it with a row that failed to load.
UNCONFIGURED = RollupLimit(scope=SCOPE_WORKSPACE, workspace_id="")


def month_start(now: datetime | None = None) -> datetime:
    """UTC calendar month to date. The start of the ``monthly`` window.

    A 30-day rolling window is NOT the same thing and would drift: written on
    the 31st it covers two months, and a tenant's "monthly" ceiling would move
    under them without anybody editing it.
    """
    moment = now or utcnow()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def window_start(window: str, now: datetime | None = None) -> datetime:
    """The inclusive lower bound of a named window. Caller holds the lock."""
    moment = now or utcnow()
    if window == WINDOW_DAILY:
        return moment - timedelta(hours=DAILY_WINDOW_HOURS)
    if window == WINDOW_MONTHLY:
        return month_start(moment)
    raise ValueError(f"unknown rollup window: {window!r}")


def _table_present(session: Session) -> bool:
    """Whether migration 0037 has created the table on this engine."""
    from app.migrations.ddl import table_exists

    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    cached = _TABLE_PRESENT.get(dialect)
    if cached is not None:
        return cached
    present = table_exists(session, TABLE)
    _TABLE_PRESENT[dialect] = present
    if not present:
        logger.warning(
            "budget rollup: {} is absent, so no cross-category ceiling is "
            "enforced. Run the migrations (0037_budget_rollups) to enable "
            "rollups; category and per-call caps are unaffected.", TABLE)
    return present


def _fetch(session: Session, scope: str, workspace_id: str):
    """One limit row as a mapping, or ``None``. No lock: see :func:`_fetch_both`.

    The upsert in :func:`set_limits` uses this. The reservation path does NOT --
    it reads both levels at once through :func:`_fetch_both`.
    """
    result = session.execute(text(_SELECT_ONE), {"scope": str(scope),
                                                "ws": str(workspace_id or "")})
    return result.mappings().first()


def _fetch_both(session: Session, workspace_id: str) -> dict[str, object]:
    """``{scope: row}`` for the workspace row and the system row, in one query.

    The hot path. See :data:`_SELECT_BOTH` for why it is one statement and not
    two: this executes while the workspace lock is held, and on SQLite that lock
    is global.
    """
    found: dict[str, object] = {}
    for row in session.execute(text(_SELECT_BOTH),
                               {"ws": str(workspace_id or "")}).mappings():
        found[str(row["scope"])] = row
    return found


def _as_mapping(value) -> dict:
    """Coerce a JSON column read through raw SQL into a dict.

    The column is declared ``JSON``, which PostgreSQL really has and SQLite only
    spells with TEXT affinity -- so the same query returns a dict on one backend
    and the string ``'{}'`` on the other. Both are honest readings of "empty
    object", and a policy table that crashes on the SQLite deployment is not a
    policy table.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except ValueError:
            logger.warning("budget rollup: unparseable meta_json %r", value[:120])
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _row_to_limit(scope: str, row) -> RollupLimit:
    data = dict(row or {})
    owner = str(data.get("workspace_id") or "")
    if scope == SCOPE_SYSTEM:
        owner = SYSTEM_WORKSPACE_ID
    return RollupLimit(
        scope=str(scope),
        workspace_id=owner,
        daily_total_cap=(None if data.get("daily_total_cap") is None
                         else float(data["daily_total_cap"])),
        monthly_total_cap=(None if data.get("monthly_total_cap") is None
                           else float(data["monthly_total_cap"])),
        enabled=bool(data.get("enabled", 1)),
        meta=_as_mapping(data.get("meta_json")),
    )


def system_limit_from_settings() -> RollupLimit:
    """The deployment-wide ceiling, from settings.

    Uses the same ``0.0 means unconfigured`` convention as
    :func:`app.services.paid_provider.system_budget_usd`, because it is the same
    operator-facing idea: an unset ceiling must read as absent, never as a
    generous default nobody chose.

    A rollup ceiling is an ADDITIONAL limit on top of Work 15.9's per-operation
    system budget. It never replaces it, and configuring one does not make
    ``SYSTEM_OWNED`` work spendable.
    """
    daily = float(getattr(settings, "budget_rollup_system_daily_total_cap_usd",
                          0.0) or 0.0)
    monthly = float(getattr(settings, "budget_rollup_system_monthly_total_cap_usd",
                            0.0) or 0.0)
    return RollupLimit(
        scope=SCOPE_SYSTEM, workspace_id=SYSTEM_WORKSPACE_ID,
        daily_total_cap=(daily if daily > 0 else None),
        monthly_total_cap=(monthly if monthly > 0 else None),
        enabled=True, meta={"source": "settings"})


def _resolve(session: Session, workspace_id: str) -> tuple[RollupLimit, RollupLimit]:
    """``(workspace_limit, system_limit)`` with the precedence rules applied.

    An explicit system ROW always beats the settings default -- including a row
    whose caps are both NULL, which is how an operator turns the
    deployment-wide ceiling OFF while keeping the row and its history. Only the
    complete absence of a row falls through to settings. The reverse rule (row
    present but empty -> settings default) would make "explicitly disabled" and
    "never configured" the same value, and a switch that cannot be turned off is
    not a switch.
    """
    rows = _fetch_both(session, workspace_id)
    ws_row = rows.get(SCOPE_WORKSPACE)
    workspace_limit = _row_to_limit(SCOPE_WORKSPACE, ws_row) if ws_row else \
        RollupLimit(scope=SCOPE_WORKSPACE, workspace_id=str(workspace_id or ""))
    system_row = rows.get(SCOPE_SYSTEM)
    system_limit = (_row_to_limit(SCOPE_SYSTEM, system_row) if system_row
                    else system_limit_from_settings())
    return workspace_limit, system_limit


def get_limits(session: Session, workspace_id: str) -> tuple[RollupLimit, RollupLimit]:
    """``(workspace_limit, system_limit)`` for one tenant.

    An absent workspace row reads as :data:`UNCONFIGURED` in shape -- all caps
    ``None`` -- which is exactly what makes the feature opt-in.
    """
    if not _table_present(session):
        return UNCONFIGURED, UNCONFIGURED
    return _resolve(session, workspace_id)


def set_limits(*, scope: str = SCOPE_WORKSPACE, workspace_id: str = "",
               daily_total_cap: float | None = None,
               monthly_total_cap: float | None = None, enabled: bool = True,
               meta: dict | None = None, session: Session | None = None) -> RollupLimit:
    """Write (or update) one ceiling. The operator-facing side of the feature.

    An UPSERT keyed on ``(scope, workspace_id)`` -- the same key the unique index
    enforces -- because a tenant with two rows holding different ceilings makes
    "which one applies" a race rather than a policy.

    ``None`` CLEARS that ceiling (the column goes back to NULL) and ``0.0`` sets
    a ceiling of zero, which refuses everything. Passing ``session=`` joins the
    caller's transaction so configuration and the first reservation can be one
    atomic unit; with no session this opens, commits and closes its own.
    """
    kind = str(scope or SCOPE_WORKSPACE).strip()
    if kind not in (SCOPE_WORKSPACE, SCOPE_SYSTEM):
        raise ValueError(f"unknown rollup scope: {scope!r}")
    owner = SYSTEM_WORKSPACE_ID if kind == SCOPE_SYSTEM else str(workspace_id or "")
    for name, value in (("daily_total_cap", daily_total_cap),
                        ("monthly_total_cap", monthly_total_cap)):
        if value is not None and float(value) < 0:
            raise ValueError(f"{name} cannot be negative: {value}")

    payload = dict(meta or {})
    encoded = json.dumps(payload)

    def _write(session: Session) -> RollupLimit:
        if session.get_bind().dialect.name != "sqlite":
            session.execute(text(
                f'INSERT INTO "{TABLE}" '
                "(id, created_at, updated_at, scope, workspace_id, "
                "daily_total_cap, monthly_total_cap, enabled, meta_json) "
                "VALUES (:id, :now, :now, :scope, :ws, :daily, :monthly, "
                ":enabled, :meta) "
                "ON CONFLICT (scope, workspace_id) DO UPDATE SET "
                "updated_at = EXCLUDED.updated_at, "
                "daily_total_cap = EXCLUDED.daily_total_cap, "
                "monthly_total_cap = EXCLUDED.monthly_total_cap, "
                "enabled = EXCLUDED.enabled, meta_json = EXCLUDED.meta_json"), {
                    "id": str(uuid.uuid4()), "now": utcnow(), "scope": kind,
                    "ws": owner, "daily": daily_total_cap,
                    "monthly": monthly_total_cap,
                    "enabled": bool(enabled), "meta": encoded})
        else:
            # SQLite has no ON CONFLICT ... DO UPDATE in every build this project
            # supports, so the upsert is spelled as DELETE + INSERT against the
            # same unique key. Still one statement pair and still atomic inside
            # the caller's transaction.
            session.execute(text(
                f'DELETE FROM "{TABLE}" WHERE scope = :scope AND workspace_id = :ws'),
                {"scope": kind, "ws": owner})
            session.execute(text(
                f'INSERT INTO "{TABLE}" '
                "(id, created_at, updated_at, scope, workspace_id, "
                "daily_total_cap, monthly_total_cap, enabled, meta_json) "
                "VALUES (:id, :now, :now, :scope, :ws, :daily, :monthly, "
                ":enabled, :meta)"), {
                    "id": str(uuid.uuid4()), "now": utcnow(), "scope": kind,
                    "ws": owner, "daily": daily_total_cap,
                    "monthly": monthly_total_cap,
                    "enabled": bool(enabled), "meta": encoded})
        return RollupLimit(scope=kind, workspace_id=owner,
                           daily_total_cap=daily_total_cap,
                           monthly_total_cap=monthly_total_cap,
                           enabled=bool(enabled), meta=payload)

    if session is not None:
        return _write(session)
    from app.db import session_scope

    with session_scope() as own:
        return _write(own)


def _spent_in_window(session: Session, since: datetime, *,
                     workspace_id: str | None = None) -> float:
    """``SUM(amount_usd)`` in the window. Caller holds the lock.

    Deliberately has NO category filter: that is the entire point. The category
    caps answer per-category questions and this answers the cross-category one,
    and reusing the same aggregate with a filter removed is what makes the
    rollup the SAME arithmetic rather than a second opinion about money.

    ``workspace_id=None`` is the system scope and sums EVERY row, including the
    ``__system__`` ledger owner Work 15.9 writes for operator spend: YMONEY's
    own maintenance calls are money this deployment spent.
    """
    query = select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
        CostEntry.created_at >= since)
    if workspace_id is not None:
        query = query.where(CostEntry.workspace_id == str(workspace_id))
    return float(session.scalar(query) or 0.0)


def rollup_required(session: Session, workspace_id: str) -> bool:
    """Could any ceiling apply to this tenant? Read BEFORE the reservation lock.

    **This is why the feature is free when it is off.** Measured on this
    repository's SQLite configuration, 16 threads x 12 rounds against
    ``reserve_spend``: adding even ONE trivial statement (``SELECT 1 WHERE 0``)
    inside ``exclusive_workspace_lock`` turns 192/192 successful reservations
    into 183 with 9 ``database is locked`` failures and takes the run from 2.3s
    to 8.0s. On SQLite that context manager's ``BEGIN IMMEDIATE`` is the whole
    DATABASE's write lock, so every statement inside it is paid for by every
    other writer and SQLite's busy handler turns the extra microseconds into tens
    of milliseconds of sleep. The same probe on a SECOND connection cost 3.4s
    and bought nothing over the original 2.3s, because the connection checkout
    itself queues behind the writers.

    So the probe runs on the reservation's OWN session, before the lock: no new
    connection, and a reader that holds nothing. The locked transaction then
    stays byte-for-byte what Work 15.7 shipped, and a deployment that configures
    a ceiling opts into the extra statements -- a price it chose.

    **Call it BEFORE ``exclusive_workspace_lock``**, on the session the
    reservation will use. Read it as a question about the caller's transaction
    rather than as a function of this module's state: the cap VALUES are re-read
    inside the lock by :func:`assert_within_rollups`, so what this answers is
    only "is a full check worth doing", and the gap between the two reads is
    microseconds on the same connection. Spend totals -- the thing that decides
    whether two callers both fit inside a cap -- are always read under the lock.

    Fails CLOSED. If the probe raises, the answer is ``True``: a false positive
    costs one extra query inside the lock, a false negative costs money spent
    past a ceiling nobody can then explain.
    """
    if not _table_present(session):
        return False
    try:
        if _system_hint_configured():
            # A deployment-wide ceiling exists in settings, so a check is needed
            # whatever any tenant row says -- and the answer is already known
            # without a query.
            return True
        found = session.execute(text(_SELECT_ANY), {
            "scope": SCOPE_WORKSPACE, "ws": str(workspace_id or ""),
            "scope2": SCOPE_SYSTEM, "ws2": SYSTEM_WORKSPACE_ID}).first()
        return found is not None
    except Exception as exc:  # noqa: BLE001 - a probe must not block a reservation
        logger.warning("budget rollup: configuration probe failed (%s: %s); "
                       "assuming a ceiling applies", type(exc).__name__, exc)
        return True


def _system_hint_configured() -> bool:
    """Is a SETTINGS-derived deployment ceiling configured?

    Two ``getattr``s and two comparisons, no database. It short-circuits the
    probe query for a deployment that has deliberately turned a system ceiling
    on -- the answer is then known without asking the database, because the
    settings ceiling applies to every tenant regardless of what rows exist.

    It is NOT a short-circuit for "nothing is configured": a tenant row in the
    database is just as much a ceiling as an environment variable, and a probe
    that skipped the table would have missed every per-workspace cap.
    """
    return (float(getattr(settings, "budget_rollup_system_daily_total_cap_usd",
                          0.0) or 0.0) > 0
            or float(getattr(settings, "budget_rollup_system_monthly_total_cap_usd",
                             0.0) or 0.0) > 0)


def assert_within_rollups(
    session: Session,
    *,
    workspace_id: str,
    amount_usd: float,
    category: str = "",
    provider: str = "",
) -> list[RollupLimit]:
    """Refuse ``amount_usd`` if ANY configured ceiling would be broken.

    Called from inside :func:`app.services.cost.reserve_spend`'s transaction,
    after the category / per-call / rate checks and before the ledger row is
    inserted. Raises :class:`RollupRefusal` (a ``BudgetExceededError``) on the
    first ceiling that would break, and writes NOTHING -- the enclosing session
    rolls back, so a refused reservation leaves no trace.

    **The caller must already hold the workspace lock.** That is what makes two
    concurrent callers unable to both pass a ceiling only one of them fits
    inside: the loser re-reads these totals after the winner has committed. The
    caller should gate the call on :func:`rollup_required` so that a deployment
    with no ceiling pays no statements inside the lock; the checks here are
    unconditional anyway, so calling this directly is always SAFE -- it just
    costs the queries.

    Returns the limits it enforced, so a caller that wants to record "this
    reservation was judged against these ceilings" has them without re-reading.
    """
    amount = float(amount_usd or 0.0)
    if not _table_present(session):
        return []

    workspace_limit, system_limit = _resolve(session, workspace_id)
    configured = [limit for limit in (workspace_limit, system_limit)
                  if limit.configured]
    if not configured:
        return []  # nothing configured: byte-for-byte the pre-0037 path

    # The workspace row lock is already held by the caller. The system row is
    # not, and two different workspaces can race a system total -- so take it
    # now, always AFTER the workspace lock and never before. One order, no cycle.
    if system_limit.configured and session.get_bind().dialect.name != "sqlite":
        session.execute(text(
            f'SELECT id FROM "{TABLE}" WHERE scope = :scope '
            "AND workspace_id = :ws FOR UPDATE"), {
                "scope": SCOPE_SYSTEM, "ws": SYSTEM_WORKSPACE_ID})

    now = utcnow()
    for limit in configured:
        scoped_to = (limit.workspace_id
                     if limit.scope == SCOPE_WORKSPACE else None)
        for window in (WINDOW_DAILY, WINDOW_MONTHLY):
            cap = limit.cap_for(window)
            if cap is None:
                continue
            spent = _spent_in_window(session, window_start(window, now),
                                     workspace_id=scoped_to)
            if spent + amount > float(cap):
                raise RollupRefusal(
                    scope=limit.scope, window=window,
                    workspace_id=(limit.workspace_id
                                  if limit.scope == SCOPE_WORKSPACE
                                  else str(workspace_id or "")),
                    cap_usd=float(cap), spent_usd=spent, amount_usd=amount,
                    category=category, provider=provider)
    return configured


def rollup_headroom(session: Session, workspace_id: str) -> dict:
    """What is left at EVERY level. A read: no reservation, no lock, no row.

    Advisory by construction and documented as such -- it cannot stop a
    concurrent spender, so a caller that must not overspend uses
    :func:`app.services.cost.reserve_spend`. It exists so the API can SHOW the
    hierarchy rather than only enforce it.
    """
    workspace_limit, system_limit = get_limits(session, workspace_id)
    now = utcnow()
    out: dict = {"workspace_id": str(workspace_id or ""), "levels": []}
    for limit in (workspace_limit, system_limit):
        if not limit.configured:
            continue
        scoped_to = (limit.workspace_id
                     if limit.scope == SCOPE_WORKSPACE else None)
        for window in (WINDOW_DAILY, WINDOW_MONTHLY):
            cap = limit.cap_for(window)
            if cap is None:
                continue
            start = window_start(window, now)
            spent = _spent_in_window(session, start, workspace_id=scoped_to)
            out["levels"].append({
                "scope": limit.scope,
                "window": window,
                "window_start": start.isoformat(),
                "cap_usd": round(float(cap), 6),
                "spent_usd": round(spent, 6),
                "remaining_usd": round(float(cap) - spent, 6),
            })
    out["enforced"] = bool(out["levels"])
    return out


__all__ = [
    "SCOPE_SYSTEM",
    "SCOPE_WORKSPACE",
    "SYSTEM_WORKSPACE_ID",
    "TABLE",
    "UNCONFIGURED",
    "WINDOW_DAILY",
    "WINDOW_MONTHLY",
    "RollupLimit",
    "RollupRefusal",
    "assert_within_rollups",
    "get_limits",
    "month_start",
    "rollup_headroom",
    "set_limits",
    "system_limit_from_settings",
    "window_start",
]
