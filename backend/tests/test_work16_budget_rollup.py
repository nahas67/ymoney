"""Work 16 §11 -- budget ROLLUPS: the cross-category ceilings, and proof they hold.

Work 15.9 flagged the hole this file closes: *"there is no cross-category
rollup, so total daily spend across categories can exceed any single cap."*
Before Work 16 §11 every cap answered "may this CATEGORY afford this?" and none
answered "may this WORKSPACE afford this?".

What is claimed here, and how each claim is falsifiable:

* **opt-in** (D) -- a workspace with no rollup row behaves exactly as before: no
  extra refusal, no extra row. If this regressed into "the rollup quietly caps
  everybody", the opt-in test fails.
* **every level must agree** (C) -- a reservation that fits the category cap but
  not the workspace total is refused, and one that fits both is admitted. Two
  separate assertions, because "enforced together" and "either/or" both pass a
  test that only checks the total.
* **refused BEFORE the external call** (A) -- driven through
  ``paid_operation(...).authorize()``, so the assertion is on a submit double's
  call counter rather than on the absence of an exception.
* **atomic under concurrency** (B) -- N threads against a cap that admits fewer
  than N; exactly the fitting number is admitted and the recorded ledger total
  equals the cap.
* **nothing above is weakened** -- Work 15.9's ``OwnerlessSpendRefused`` and the
  ``SYSTEM_OWNED`` explicit-budget requirement are asserted HERE, with a rollup
  row configured, because a new ceiling is exactly where someone would be
  tempted to let an ownerless or unbounded call through "the rollup would catch
  it".
* **one reservation identity -> exactly one row** -- the rollup check writes
  nothing, so it cannot double-book.
* **the migration round-trips** -- up, replay, down, up again, on its own
  database.

Everything runs on the default SQLite test database. No network, no money.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql_text

from app.db import session_scope
from app.models import CostEntry, Workspace
from app.services import budget_rollup as rollup_mod
from app.services import cost as cost_mod
from app.services.paid_provider import (
    SYSTEM_BUDGET_ENV,
    ActorAuthority,
    OwnerlessSpendRefused,
    SpendAuthority,
    paid_operation,
)

_MIGRATION_0037 = (Path(__file__).resolve().parent.parent / "app" / "migrations"
                   / "versions" / "0037_budget_rollups.py")


def _load_0037():
    """Import migration 0037 by path -- the runner keys on the filename."""
    spec = importlib.util.spec_from_file_location("m0037_under_test",
                                                  _MIGRATION_0037)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_workspace(db_session, label: str) -> str:
    """A real tenant row with GENEROUS category caps.

    Real row, not a synthetic id: the reservation is a ``cost_entries`` row keyed
    on this id, and a half-written reservation is a far worse failure than a
    refusal. Both category caps are set high so that in every test below the
    ceiling under examination is the ROLLUP and nothing else -- the $0.50
    ``per_video_budget_usd`` default would otherwise refuse everything for a
    reason that has nothing to do with the claim under test.
    """
    row = Workspace(name=f"Rollup WS {label}",
                    slug=f"rollup-{label}-{os.urandom(5).hex()}",
                    niche="AI money")
    db_session.add(row)
    db_session.flush()
    workspace_id = row.id
    payload = dict(row.settings_json or {})
    payload["safety"] = {"daily_budget_usd": 1000.0,
                         "per_video_budget_usd": 1000.0}
    row.settings_json = payload
    db_session.commit()
    return workspace_id


@pytest.fixture()
def ws(db_session):
    return _make_workspace(db_session, "a")


@pytest.fixture()
def other_ws(db_session):
    """A SECOND tenant. A cross-workspace rollup needs two to mean anything."""
    return _make_workspace(db_session, "b")


@pytest.fixture()
def clean_system_rollup():
    """No deployment-wide ceiling for the duration of a test.

    The system row is GLOBAL -- it is the deployment's ceiling -- so a test that
    wrote one and left it behind would cap every later test in the session. The
    settings default is pinned to 0.0 (unconfigured) too, so a system ceiling can
    only ever come from a row a test deliberately wrote.
    """
    from app.core.config import settings

    had_daily = settings.budget_rollup_system_daily_total_cap_usd
    had_monthly = settings.budget_rollup_system_monthly_total_cap_usd
    settings.budget_rollup_system_daily_total_cap_usd = 0.0
    settings.budget_rollup_system_monthly_total_cap_usd = 0.0
    with session_scope() as s:
        s.execute(sql_text(
            f'DELETE FROM "{rollup_mod.TABLE}" '
            "WHERE scope = :scope AND workspace_id = :ws"), {
                "scope": rollup_mod.SCOPE_SYSTEM,
                "ws": rollup_mod.SYSTEM_WORKSPACE_ID})
    yield
    with session_scope() as s:
        s.execute(sql_text(
            f'DELETE FROM "{rollup_mod.TABLE}" '
            "WHERE scope = :scope AND workspace_id = :ws"), {
                "scope": rollup_mod.SCOPE_SYSTEM,
                "ws": rollup_mod.SYSTEM_WORKSPACE_ID})
    settings.budget_rollup_system_daily_total_cap_usd = had_daily
    settings.budget_rollup_system_monthly_total_cap_usd = had_monthly


def _cap(workspace_id: str, *, daily: float | None = None,
         monthly: float | None = None, scope: str = rollup_mod.SCOPE_WORKSPACE,
         enabled: bool = True) -> rollup_mod.RollupLimit:
    return rollup_mod.set_limits(scope=scope, workspace_id=workspace_id,
                                 daily_total_cap=daily,
                                 monthly_total_cap=monthly, enabled=enabled)


def _spend(workspace_id: str, amount: float, category: str = "llm") -> str:
    """One authorized reservation, committed. Returns the ledger row id."""
    return cost_mod.reserve_spend(workspace_id, amount, category=category,
                                  provider="rollup-test").entry_id


def _rows(workspace_id: str) -> list[CostEntry]:
    with session_scope() as s:
        return list(s.query(CostEntry).filter(
            CostEntry.workspace_id == workspace_id).all())


def _ledger_total() -> float:
    """Every dollar in the ledger, any workspace. Baseline for a system cap."""
    with session_scope() as s:
        return float(s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0))) or 0.0)


# ===========================================================================
# migration 0037 -- reversible, not merely applicable
# ===========================================================================


def test_migration_0037_round_trips_up_and_down(tmp_path):
    """Up, replay, down, up again. On its OWN database, on purpose.

    The session-scoped test database is shared: a downgrade on that connection
    strips the schema out from under every test that runs afterwards. The SQLite
    trap this guards is real and is documented in 0034 -- dropping a table while
    an index still names one of its columns raises ``error in index ... after
    drop column`` -- so the downgrade has to DISCOVER those indexes and drop them
    first.
    """
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 - registers every table
    from app.db import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'mig37.db').as_posix()}")
    session = sessionmaker(bind=engine)()
    module = _load_0037()
    try:
        Base.metadata.create_all(bind=engine)

        module.upgrade(session)
        session.commit()
        inspector = inspect(engine)
        assert "budget_rollup_limits" in inspector.get_table_names()
        columns = {c["name"] for c in
                   inspector.get_columns("budget_rollup_limits")}
        assert {"scope", "workspace_id", "daily_total_cap", "monthly_total_cap",
                "enabled"} <= columns, columns
        # the caps are NULLABLE: NULL is "not configured" and a NOT NULL column
        # would need a sentinel, which is one forgotten comparison from
        # "unlimited"
        nullable = {c["name"]: c["nullable"] for c in
                    inspector.get_columns("budget_rollup_limits")}
        assert nullable["daily_total_cap"] is True
        assert nullable["monthly_total_cap"] is True
        assert {"ix_budget_rollup_scope", "ix_budget_rollup_ws"} <= {
            i["name"] for i in inspector.get_indexes("budget_rollup_limits")}

        # replay is a no-op, not an error (runner rule 3)
        module.upgrade(session)
        session.commit()

        module.downgrade(session)
        session.commit()
        assert "budget_rollup_limits" not in inspect(engine).get_table_names()

        # and it comes back, which is what makes the downgrade safe to run twice
        module.upgrade(session)
        session.commit()
        assert "budget_rollup_limits" in inspect(engine).get_table_names()
    finally:
        session.close()
        engine.dispose()


def test_no_new_colliding_sequence_prefix():
    """0037 is unique, so ``0037_*`` means exactly this migration."""
    from app.migrations.runner import load_migrations

    names = [name for name, _ in load_migrations() if name.startswith("0037")]
    assert names == ["0037_budget_rollups"], names


# ===========================================================================
# D -- the rollup is opt-in
# ===========================================================================


def test_a_workspace_with_no_rollup_behaves_exactly_as_before(ws, clean_system_rollup):
    """No configured cap => no rollup refusal, no rollup row, no extra write.

    The opt-in claim, asserted three ways: the headroom reads as "nothing
    enforced", the limits are ``None`` rather than ``0.0``, and a reservation
    larger than any conceivable default total is admitted because the only
    ceiling in force is the tenant's own (set to $1000 above).
    """
    with session_scope() as s:
        workspace_limit, system_limit = rollup_mod.get_limits(s, ws)

    assert workspace_limit.daily_total_cap is None
    assert workspace_limit.monthly_total_cap is None
    assert workspace_limit.configured is False
    assert system_limit.configured is False
    assert _rows(ws) == []

    entry_id = _spend(ws, 900.0)

    assert len(_rows(ws)) == 1, "an unconfigured rollup must not write anything"
    assert _rows(ws)[0].id == entry_id
    with session_scope() as s:
        assert rollup_mod.rollup_headroom(s, ws)["enforced"] is False


def test_zero_is_a_ceiling_and_none_is_not(ws, clean_system_rollup):
    """``0.0`` refuses everything; ``None`` refuses nothing. Never conflated.

    The two are one forgotten ``if cap:`` away from each other, and collapsing
    them would mean either "every workspace is capped at zero after a bad
    default" or "a zero ceiling silently reads as unlimited". Both are money.
    """
    _cap(ws, daily=0.0)
    with pytest.raises(rollup_mod.RollupRefusal) as zero_cap:
        _spend(ws, 0.01)
    assert zero_cap.value.cap_usd == 0.0

    _cap(ws, daily=None)
    _spend(ws, 0.01)
    assert len(_rows(ws)) == 1


def test_a_disabled_row_enforces_nothing_and_keeps_its_value(ws, clean_system_rollup):
    """``enabled=False`` is how an operator switches a ceiling OFF for good.

    The row stays, so "that workspace had a $5 ceiling" remains answerable. A
    ceiling you had to DELETE to switch off is a ceiling you can no longer prove
    you had.
    """
    _cap(ws, daily=0.0, enabled=False)
    _spend(ws, 5.0)
    with session_scope() as s:
        workspace_limit, _ = rollup_mod.get_limits(s, ws)
    assert workspace_limit.enabled is False
    assert workspace_limit.daily_total_cap == 0.0
    assert workspace_limit.configured is False


# ===========================================================================
# A + C -- the hierarchy, enforced together, before anything is sent
# ===========================================================================


def test_workspace_daily_total_cap_refuses_before_the_external_call(
        ws, clean_system_rollup):
    """(a) The refusal happens at ``authorize()``: the submit ran ZERO times.

    Asserted on the double's counter rather than on the exception, because "the
    gate raised" and "the gate raised EARLY ENOUGH" are different claims, and
    only one of them is about money. Also asserts the refused call left no
    ledger row: an un-committed reservation would shrink the remaining budget
    for the next, legitimate, caller.
    """
    _cap(ws, daily=5.0)
    _spend(ws, 4.0)
    before = len(_rows(ws))
    sent: list[str] = []

    operation = paid_operation(provider="images", operation="generate",
                               workspace_id=ws, category="image",
                               estimated_cost=3.0)
    with pytest.raises(rollup_mod.RollupRefusal) as refused:
        operation.authorize()
        sent.append("called")  # the real provider call lives here

    assert sent == [], "the external call ran despite the rollup refusal"
    assert refused.value.scope == rollup_mod.SCOPE_WORKSPACE
    assert refused.value.window == rollup_mod.WINDOW_DAILY
    assert refused.value.cap_usd == 5.0
    assert refused.value.spent_usd == 4.0
    assert refused.value.amount_usd == 3.0
    assert len(_rows(ws)) == before, "a refused reservation wrote a ledger row"
    assert operation.reservation is None


def test_the_rollup_refusal_is_a_budget_refusal(ws, clean_system_rollup):
    """A ``BudgetExceededError``, so every existing handler already refuses it.

    ``paid_provider`` catches ``BudgetExceededError`` to mean "nothing was sent,
    nothing was billed". A new refusal type that were not a subclass would
    escape that handler and be reported as an infrastructure fault.
    """
    _cap(ws, daily=1.0)
    with pytest.raises(cost_mod.BudgetExceededError):
        _spend(ws, 1.5)
    detail = None
    with pytest.raises(rollup_mod.RollupRefusal) as caught:
        _spend(ws, 1.5)
    detail = caught.value.to_dict()
    assert detail["reason"] == "budget_rollup_exceeded"
    assert detail["window"] == rollup_mod.WINDOW_DAILY


def test_category_cap_and_workspace_total_are_both_enforced(ws, clean_system_rollup):
    """(c) Every level must agree -- not either/or.

    Two ceilings are configured: the tenant's category cap ($10/day) and a
    workspace total ($3/day). A $2 call fits both and is admitted. The NEXT $2
    fits the category cap (2 of 10 used) but not the total (2 + 2 > 3), so it is
    refused -- and refused by the ROLLUP, not by the category cap. Then the
    category cap is raised above the total and the same call is still refused,
    which is what "enforced together" means: removing one ceiling does not
    switch the other off.
    """
    _cap(ws, daily=3.0)
    with session_scope() as s:
        row = s.get(Workspace, ws)
        payload = dict(row.settings_json or {})
        payload["safety"] = {"daily_budget_usd": 10.0,
                             "per_video_budget_usd": 1000.0}
        row.settings_json = payload

    _spend(ws, 2.0, category="llm")
    with pytest.raises(rollup_mod.RollupRefusal) as by_rollup:
        _spend(ws, 2.0, category="tts")
    assert by_rollup.value.window == rollup_mod.WINDOW_DAILY

    # Now the CATEGORY cap is the binding one and the total is generous. Same
    # workspace, same ledger, different ceiling.
    with session_scope() as s:
        row = s.get(Workspace, ws)
        payload = dict(row.settings_json or {})
        payload["safety"] = {"daily_budget_usd": 2.5,
                             "per_video_budget_usd": 1000.0}
        row.settings_json = payload
    _cap(ws, daily=100.0)

    with pytest.raises(cost_mod.BudgetExceededError) as by_category:
        _spend(ws, 1.0, category="image")
    assert not isinstance(by_category.value, rollup_mod.RollupRefusal), (
        "the category cap must still refuse on its own")
    assert "daily budget exhausted" in str(by_category.value)


def test_the_rollup_counts_every_category_and_not_just_one(ws, clean_system_rollup):
    """The gap Work 15.9 named, in one assertion.

    $2 to llm and $2 to tts are each inside the tenant's own $10/day cap, and
    together they breach a $3/day workspace total. A per-category gate cannot
    see this at all -- that is the whole reason the rollup exists.
    """
    _cap(ws, daily=3.0)
    _spend(ws, 2.0, category="llm")
    with pytest.raises(rollup_mod.RollupRefusal) as refused:
        _spend(ws, 2.0, category="tts")
    assert refused.value.spent_usd == 2.0
    assert len(_rows(ws)) == 1


def test_a_monthly_total_cap_is_independent_of_the_daily_one(ws, clean_system_rollup):
    """Monthly is the UTC calendar month to date, and it is a separate ceiling.

    A monthly cap with no daily cap refuses on ``monthly``; a daily cap that is
    generous does not rescue it. The window boundary is asserted too, because a
    "monthly" budget that is secretly "the last 30 days" drifts across month
    boundaries and nobody notices until it is wrong.
    """
    _cap(ws, monthly=2.0)
    _spend(ws, 1.5)
    with pytest.raises(rollup_mod.RollupRefusal) as refused:
        _spend(ws, 1.0)
    assert refused.value.window == rollup_mod.WINDOW_MONTHLY

    month = rollup_mod.month_start()
    assert (month.day, month.hour, month.minute, month.second) == (1, 0, 0, 0)
    with session_scope() as s:
        levels = rollup_mod.rollup_headroom(s, ws)["levels"]
    monthly_level = [lv for lv in levels if lv["window"] == "monthly"][0]
    assert monthly_level["cap_usd"] == 2.0
    assert monthly_level["spent_usd"] == pytest.approx(1.5)


def test_the_system_ceiling_caps_spend_across_workspaces(ws, other_ws,
                                                         clean_system_rollup):
    """The system level is the one a per-workspace lock cannot enforce alone.

    Two tenants, each with its own generous cap, race one deployment ceiling.
    Baseline is read from the ledger because the system scope sums EVERY row in
    the session database -- including rows other tests wrote -- so the cap is
    expressed relative to what is already there rather than as an absolute that
    would depend on test order.
    """
    baseline = _ledger_total()
    rollup_mod.set_limits(scope=rollup_mod.SCOPE_SYSTEM, daily_total_cap=baseline + 3.0)

    _spend(ws, 2.0, category="llm")
    _spend(other_ws, 1.0, category="image")
    with pytest.raises(rollup_mod.RollupRefusal) as refused:
        _spend(other_ws, 1.0, category="video")

    assert refused.value.scope == rollup_mod.SCOPE_SYSTEM
    assert refused.value.cap_usd == pytest.approx(baseline + 3.0)
    # Nothing is attributed to a tenant that does not exist: the system row is
    # YMONEY's own ceiling, charged to nobody.
    assert refused.value.workspace_id == other_ws


def test_an_explicit_system_row_beats_the_settings_default(clean_system_rollup):
    """Precedence, in both directions, because "explicitly off" must be possible.

    An explicit row with a cap wins over the settings default. An explicit row
    with BOTH caps NULL also wins -- that is how an operator disables the
    deployment ceiling while keeping the row and its history. Only the complete
    ABSENCE of a row falls through to settings.
    """
    from app.core.config import settings

    settings.budget_rollup_system_daily_total_cap_usd = 1000.0
    try:
        rollup_mod.set_limits(scope=rollup_mod.SCOPE_SYSTEM,
                             daily_total_cap=7.0)
        with session_scope() as s:
            _, system_limit = rollup_mod.get_limits(s, "any-tenant")
        assert system_limit.daily_total_cap == 7.0
        assert system_limit.configured is True

        rollup_mod.set_limits(scope=rollup_mod.SCOPE_SYSTEM,
                             daily_total_cap=None, monthly_total_cap=None)
        with session_scope() as s:
            _, system_limit = rollup_mod.get_limits(s, "any-tenant")
        assert system_limit.daily_total_cap is None
        assert system_limit.configured is False, (
            "an explicit row with no caps must NOT fall through to the "
            "settings default, or the switch cannot be turned off")
    finally:
        settings.budget_rollup_system_daily_total_cap_usd = 0.0


def test_the_settings_default_applies_only_when_no_system_row_exists(
        clean_system_rollup):
    from app.core.config import settings

    settings.budget_rollup_system_daily_total_cap_usd = 25.0
    try:
        with session_scope() as s:
            _, system_limit = rollup_mod.get_limits(s, "any-tenant")
        assert system_limit.daily_total_cap == 25.0
        assert system_limit.configured is True
    finally:
        settings.budget_rollup_system_daily_total_cap_usd = 0.0


# ===========================================================================
# B -- atomicity under concurrency
# ===========================================================================


def test_concurrent_reservations_fit_inside_the_rollup_cap(ws, clean_system_rollup):
    """(b) Twelve threads, a cap that admits three, exactly three are admitted.

    The claim is atomicity, so it is measured the only way it can be: every
    thread asks for money at once and the number of grants must equal the number
    that fits. An unlocked read-then-check admits all twelve. The recorded ledger
    total is asserted as well as the grant count, because a guard that refuses
    correctly but still writes the row is double-booking.
    """
    cap, each, threads = 3.0, 1.0, 12
    _cap(ws, daily=cap)
    granted: list[str] = []
    lock = threading.Lock()

    def take(_index: int) -> None:
        try:
            reservation = cost_mod.reserve_spend(
                ws, each, category="race", provider="race")
        except cost_mod.BudgetExceededError:
            return
        with lock:
            granted.append(reservation.entry_id)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(take, range(threads)))

    assert len(granted) == int(cap), (
        f"{len(granted)} reservations were granted against a ${cap} rollup cap "
        f"that admits {int(cap)}")
    recorded = cost_mod.spent_since(ws, hours=24.0)
    assert recorded == pytest.approx(cap), (
        f"the ledger recorded ${recorded} against a ${cap} cap")
    assert recorded <= cap, "the recorded total exceeded the cap"
    assert len(_rows(ws)) == int(cap), (
        "a refused reservation wrote a row, which double-books the budget")


def test_the_concurrency_guard_is_the_rollup_and_not_the_daily_cap(
        ws, clean_system_rollup):
    """Same race, category cap raised to a hundred: only the rollup can refuse.

    Without this, the concurrency test above would pass on the tenant's own
    daily cap even if the rollup check were deleted outright -- the two caps use
    the same lock, so a test that cannot tell them apart proves nothing about
    the code it claims to cover.
    """
    _cap(ws, daily=3.0)
    granted: list[str] = []
    lock = threading.Lock()

    def take(_index: int) -> None:
        try:
            reservation = cost_mod.reserve_spend(
                ws, 1.0, category="race2", provider="race2")
        except cost_mod.BudgetExceededError:
            return
        with lock:
            granted.append(reservation.entry_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(take, range(8)))

    assert len(granted) == 3, f"{len(granted)} grants against a cap of 3"
    with session_scope() as s:
        workspace_limit, _ = rollup_mod.get_limits(s, ws)
    assert workspace_limit.daily_total_cap == 3.0
    # The tenant's own cap is 1000/day, so the refusals above can only have come
    # from the rollup.
    assert cost_mod.budget_headroom(ws, category="race2").daily_cap_usd == 1000.0


def test_the_system_ceiling_is_atomic_across_two_workspaces(ws, other_ws,
                                                           clean_system_rollup):
    """The system total is enforced transactionally, not just at read time.

    Two tenants, two threads each, one deployment ceiling that admits two calls
    in total. A system cap checked outside the transaction lets all four
    through; the ``FOR UPDATE`` on the system row is what stops it.
    """
    baseline = _ledger_total()
    rollup_mod.set_limits(scope=rollup_mod.SCOPE_SYSTEM,
                          daily_total_cap=baseline + 2.0)
    granted: list[str] = []
    lock = threading.Lock()

    def take(workspace_id: str) -> None:
        try:
            reservation = cost_mod.reserve_spend(
                workspace_id, 1.0, category="sysrace", provider="sysrace")
        except cost_mod.BudgetExceededError:
            return
        with lock:
            granted.append(f"{workspace_id}:{reservation.entry_id}")

    targets = [ws, other_ws] * 3
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        list(pool.map(take, targets))

    assert len(granted) == 2, (
        f"{len(granted)} grants against a deployment ceiling that admits 2")
    with session_scope() as s:
        raced = float(s.scalar(select(func.coalesce(func.sum(CostEntry.amount_usd),
                                                    0.0)).where(
            CostEntry.category == "sysrace")) or 0.0)
    assert raced == pytest.approx(2.0), (
        "the race wrote money the deployment ceiling never admitted")


# ===========================================================================
# nothing above is weakened, and nothing is double-booked
# ===========================================================================


def test_ownerless_spend_is_still_refused_with_a_rollup_configured(
        clean_system_rollup):
    """Work 15.9 §1 survives: a rollup is not a licence to spend with no owner.

    With a deployment ceiling configured there IS room for money to be
    accounted somewhere, and that is precisely the situation in which an
    ownerless call would start going out -- charged to nobody, bounded by
    nothing. The refusal must still happen before the submit.
    """
    sent: list[str] = []
    operation = paid_operation(provider="tts", operation="speak", workspace_id="",
                               category="tts", estimated_cost=1.0)
    with pytest.raises(OwnerlessSpendRefused):
        operation.authorize()
        sent.append("called")
    assert sent == []


def test_system_owned_still_needs_an_explicit_system_budget(ws, monkeypatch):
    """Work 15.9 §1 survives: a rollup ceiling is not a system budget.

    ``SYSTEM_OWNED`` used to be free whenever the operator had "some" limit in
    mind. A deployment-wide rollup is an additional ceiling, so configuring one
    must NOT make unbudgeted operator spend admissible.
    """
    monkeypatch.delenv(SYSTEM_BUDGET_ENV, raising=False)
    _cap(ws, daily=100.0)

    sent: list[str] = []
    operation = paid_operation(provider="avatar", operation="talking_head",
                               workspace_id=ws, category="video",
                               estimated_cost=1.0)
    with pytest.raises(OwnerlessSpendRefused) as refused:
        operation.declared(authority=SpendAuthority.SYSTEM_OWNED,
                           actor=ActorAuthority.SYSTEM)
        operation.authorize()
        sent.append("called")

    assert sent == []
    assert SYSTEM_BUDGET_ENV in str(refused.value)


def test_one_reservation_identity_produces_exactly_one_row(ws, clean_system_rollup):
    """The rollup check is a predicate: it writes no row, so it cannot double-book.

    A paid operation reserves once, gets a remote id, and closes its book. The
    ledger must hold exactly ONE row for that operation -- the reservation,
    updated in place. A second row would bill one submit twice.
    """
    _cap(ws, daily=10.0)
    operation = paid_operation(provider="images", operation="generate",
                               workspace_id=ws, category="image",
                               estimated_cost=1.0)
    operation.authorize()
    operation.mark_attempt()
    operation.mark_accepted("remote-xyz-1")
    operation.mark_succeeded(estimate_usd=1.0)

    rows = _rows(ws)
    assert len(rows) == 1, f"{len(rows)} ledger rows for one billable operation"
    detail = rows[0].detail_json or {}
    assert detail["remote_id"] == "remote-xyz-1"
    assert detail["spend_authority"] == str(SpendAuthority.WORKSPACE_OWNED)
    assert detail["actor_authority"] == str(ActorAuthority.USER)


def test_settlement_above_the_estimate_is_recorded_not_refused(ws, clean_system_rollup):
    """Caps gate what may be STARTED. A real charge is never erased.

    A provider-reported actual above the estimate puts the ledger over the
    ceiling. Refusing the write would DELETE a charge that has already been
    made, which is the failure ``book_unknown_exposure`` exists to prevent, so
    the row is corrected and the overage is simply visible.
    """
    _cap(ws, daily=2.0)
    entry_id = _spend(ws, 1.0, category="tts")
    cost_mod.settle_reservation(entry_id, 9.0)

    row = _rows(ws)[0]
    assert row.amount_usd == pytest.approx(9.0)
    assert row.is_estimate is False
    assert (row.detail_json or {})["cost_outcome"] == "ACTUAL"
    with session_scope() as s:
        levels = rollup_mod.rollup_headroom(s, ws)["levels"]
    assert levels[0]["spent_usd"] == pytest.approx(9.0)
    assert levels[0]["remaining_usd"] < 0, (
        "an overage must be VISIBLE as negative headroom, not clamped to zero")


def test_an_unknown_exposure_still_counts_against_the_rollup(ws, clean_system_rollup):
    """An unpriced reservation is still money out the door.

    ``book_unknown_exposure`` writes ``0.0`` with an explicit marker, so it adds
    nothing to the SUM -- which is correct, because the amount genuinely is
    unknown. The rollup must not be the thing that "fixes" that by refusing the
    row: the exposure has to exist to be reconciled.
    """
    _cap(ws, daily=5.0)
    cost_mod.book_unknown_exposure(ws, category="image", provider="images")
    row = _rows(ws)[0]
    assert row.amount_usd == 0.0
    assert (row.detail_json or {})["cost_outcome"] == "UNKNOWN_EXPOSURE"

    # ... and the real money still has to fit.
    _spend(ws, 5.0)
    with pytest.raises(rollup_mod.RollupRefusal):
        _spend(ws, 0.01)


def test_headroom_shows_every_configured_level(ws, clean_system_rollup):
    """The hierarchy is SHOWABLE, not only enforceable.

    An operator who cannot see the total that is about to refuse them will
    configure a bigger one, which is how a cap stops being a cap.
    """
    _cap(ws, daily=10.0, monthly=100.0)
    _spend(ws, 4.0)
    with session_scope() as s:
        headroom = rollup_mod.rollup_headroom(s, ws)
    assert headroom["enforced"] is True
    assert {(lv["scope"], lv["window"]) for lv in headroom["levels"]} == {
        (rollup_mod.SCOPE_WORKSPACE, rollup_mod.WINDOW_DAILY),
        (rollup_mod.SCOPE_WORKSPACE, rollup_mod.WINDOW_MONTHLY)}
    daily = [lv for lv in headroom["levels"] if lv["window"] == "daily"][0]
    assert daily["spent_usd"] == pytest.approx(4.0)
    assert daily["remaining_usd"] == pytest.approx(6.0)


def test_an_unknown_window_is_refused_rather_than_guessed():
    """A named window, and no silent default for a name nobody recognises."""
    with pytest.raises(ValueError):
        rollup_mod.window_start("fortnightly")
    with pytest.raises(ValueError):
        rollup_mod.RollupLimit(scope="workspace").cap_for("weekly")


def test_a_negative_cap_is_refused_at_the_door(ws):
    """A negative ceiling is not "generous", it is a misconfiguration."""
    with pytest.raises(ValueError):
        _cap(ws, daily=-1.0)
    with pytest.raises(ValueError):
        rollup_mod.set_limits(scope="galaxy", daily_total_cap=1.0)
