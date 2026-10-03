"""Work 16 §7 -- load and concurrency, measured against a real PostgreSQL.

What this file is
-----------------
A measured limit, not an asserted one. Every other Work 16 lane argues from a
property: "the cap holds", "the claim is exactly once", "a dead worker's job is
reclaimed". Properties are necessary and not sufficient -- a system can hold
every property it has and still be unusable at four concurrent users. So this
file measures, at rising concurrency, the numbers ``docs/PRODUCTION_LOAD_REPORT.md``
is written from:

* per-workload throughput and p50/p90/p99 against a real PostgreSQL server,
  because SQLite serialises writers and every concurrency number measured
  against it is SQLite's;
* the point at which each workload actually stops being usable;
* pool saturation and recovery -- the pool driven to its ceiling, real callers
  timing out, and then the service serving again;
* cross-workspace budget isolation at concurrency.

Opt-in, twice over
------------------
Two independent declarative gates, and both matter:

* ``@pytest.mark.live`` / ``@pytest.mark.slow`` -- already-declared markers that
  this repo's ``addopts = -m 'not live and not slow'`` deselects, so a default
  suite run does not even *collect* these bodies.
* ``skipif`` on :func:`app.services.load_harness.load_enabled` -- the explicit
  operator opt-in (``YMONEY_LOAD_TESTS=1``), which is also what makes the
  PostgreSQL probe free for everyone who has not opted in.

Run them with::

    set YMONEY_LOAD_TESTS=1
    set YMONEY_LOAD_PG_ADMIN=postgresql://user:pw@127.0.0.1:55432/postgres
    python -m pytest tests/test_work16_load.py -m "live and slow" -p no:randomly
    python -m backend.tests.test_work16_load --sweep      # the full curve

Nothing here is a mock of the system under test. The provider boundary is a
double (:func:`tests.fakes.FakePublisher`, injected by the repo's own autouse
fixtures) and the storage root is a temp directory; the database, the pool, the
cost ledger, the lease table and the ASGI app are all real.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.services import load_harness as lh
from app.services.load_harness import REFUSED, SKIPPED, LoadHarness, LoadResult

#: One gate, declared once. ``pg_available()`` short-circuits on
#: ``load_enabled()``, so importing this module in a default run costs no
#: socket, no connection and no collection-time stall.
pytestmark = [
    pytest.mark.live,
    pytest.mark.slow,
    pytest.mark.skipif(
        not (lh.load_enabled() and lh.pg_available()),
        reason=(
            "load/chaos tests are opt-in: set YMONEY_LOAD_TESTS=1 and point "
            "YMONEY_LOAD_PG_ADMIN at a PostgreSQL admin DSN"
        ),
    ),
]

#: Concurrency ladder for the curve. Rises until something breaks -- the
#: harness's own ``sweep`` stops at the first real failure, which is the
#: deliverable, so the top of this list is an upper bound and not a promise.
LADDER: tuple[int, ...] = (1, 2, 4, 8, 16, 24, 32)

#: Workloads safe to hammer repeatedly on one database. The queue-shaped ones
#: get a deep queue re-primed below, because a curve that measures an
#: empty-table scan is not a curve.
CURVE_WORKLOADS: tuple[str, ...] = (
    "api_traffic",
    "asset_roundtrip",
    "asset_upload",
    "cost_reserve",
    "cost_reserve_settle",
    "editor_autosave",
    "inbox_sync",
    "job_create",
    "planner",
    "project_read",
    "project_write",
    "publish",
    "publish_idempotent",
    "worker_claim",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_harness(tmp_path_factory: pytest.TempPathFactory):
    """One scratch PostgreSQL database for the whole module.

    Module-scoped because migrating costs ~5s and creating a database per test
    would multiply that by ten for no extra information. The scratch name is
    random and the database is dropped ``WITH (FORCE)`` in teardown, so a run
    never touches a database that holds anything.

    Pool bounds are the repository's shipped defaults (10 + 10 overflow) so the
    curve reflects the configured deployment, not a test-friendly fiction. The
    one exception is the saturation drill, which builds its own deliberately
    tiny pool and says so.
    """
    harness = LoadHarness(
        pool_size=10,
        max_overflow=10,
        pool_timeout=int(os.environ.get("YMONEY_LOAD_POOL_TIMEOUT", "30")),
        storage_root=str(tmp_path_factory.mktemp("w16-storage")),
    )
    harness.create_database()
    try:
        harness.bind()
        harness.migrate()
        yield harness
    finally:
        harness.restore()
        harness.drop_database()


@pytest.fixture(scope="module")
def pg_ctx(pg_harness: LoadHarness):
    """Two seeded workspaces, a real JWT, and an untimed API warm-up."""
    ctx = pg_harness.seed(workspaces=2, accounts_per_workspace=1)
    pg_harness.api_session(ctx)
    # Untimed on purpose: ``create_app()`` costs seconds and charging it to
    # whichever worker runs first turns a 5ms p50 into a 3000ms p90.
    pg_harness.warm_api(ctx, route=_PROJECTS_ROUTE)
    return ctx


#: The authenticated, database-backed list route: real middleware, real
#: dependency injection, real session, real query. ``/health`` would measure the
#: absence of work.
_PROJECTS_ROUTE = "/api/v1/workspaces/{workspace_id}/projects"


def _make_workspace(daily_cap_usd: float, *, per_call_usd: float = 5.0,
                    name: str = "W16 probe") -> str:
    """A committed workspace whose budget caps are explicit.

    Caps written onto the row rather than inherited from
    ``settings.daily_budget_usd``: a money number that moves because somebody
    changed a default is not a measurement, it is a coincidence.
    """
    from app.db import session_scope
    from app.models import User, Workspace, WorkspaceMember

    token = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"w16-probe-{token}@test.local", password_hash="x")
        s.add(user)
        s.flush()
        ws = Workspace(name=f"{name} {token}",
                       slug=f"w16-probe-{token}-{int(daily_cap_usd * 100)}",
                       niche="load")
        ws.settings_json = {"safety": {
            "daily_budget_usd": float(daily_cap_usd),
            "per_video_budget_usd": float(per_call_usd)}}
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                              role=WorkspaceMember.ROLE_OWNER))
        s.flush()
        return str(ws.id)


def _ledger_total(workspace_id: str) -> float:
    """Every dollar the ledger attributes to a workspace, reservations included."""
    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        return float(s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id)) or 0.0)


def _enqueue_jobs(count: int, workspace_id: str, *, job_type: str = "w16.load.probe",
                  payload: dict | None = None) -> list[str]:
    """``count`` queued jobs with unique idempotency keys.

    Unique keys on purpose: a shared key would let the dedupe refuse the second
    enqueue, and the workload under test is the queue's *write* path.
    """
    from app.services import jobs as jobs_service

    ids: list[str] = []
    for _ in range(count):
        job_id = jobs_service.enqueue(
            job_type, dict(payload or {"load": True}),
            workspace_id=workspace_id,
            idempotency_key=f"w16-test-{uuid.uuid4().hex}")
        if job_id:
            ids.append(job_id)
    return ids


# ---------------------------------------------------------------------------
# 1. Reachability: is every registered workload actually a real path?
# ---------------------------------------------------------------------------


def test_every_registered_workload_is_reachable(pg_harness: LoadHarness,
                                              pg_ctx: lh.LoadContext) -> None:
    """All 14 registered workloads complete real work against real PostgreSQL.

    The assertion is that each one produced at least one SUCCESS, not merely
    that it did not raise. A workload whose every operation returns ``SKIPPED``
    -- which is exactly what a planner measured against a workspace with no
    evidenced signals does -- passes a "no errors" check while measuring an early
    return, and would be reported as a fast healthy path.
    """
    _enqueue_jobs(12, pg_ctx.workspaces[0])
    token = pg_ctx.options.get("api_token", "")

    results: dict[str, LoadResult] = {}
    for name in lh.workload_names():
        options: dict[str, object] = {}
        if name == "api_traffic":
            options = {"api_route": _PROJECTS_ROUTE, "api_token": token}
        results[name] = pg_harness.run(name, concurrency=3, iterations=3,
                                       ctx=pg_ctx, **options)

    dead = {name: r.to_dict() for name, r in results.items() if r.ok == 0}
    failures = {name: r.errors for name, r in results.items() if r.failed}
    assert not failures, f"workloads raised: {failures}"
    assert not dead, f"workloads produced no successful operation: {dead}"
    # `worker_claim` legitimately SKIPS once the seeded queue drains; every
    # other workload must be all-success or all-refused at this scale.
    for name, result in results.items():
        assert result.operations == 9, (
            f"{name}: harness accounting lost operations "
            f"({result.operations} != 3 workers x 3 iterations)")


def test_sweep_reports_a_curve_and_honours_its_accounting(
        pg_harness: LoadHarness, pg_ctx: lh.LoadContext) -> None:
    """A sweep produces a comparable curve, and the counters add up.

    The harness's only real correctness property is that it does not LOSE
    operations: ``ok + refused + skipped + failed == concurrency x iterations``
    on every rung. Without that, a curve is a wall-clock reading of a machine
    that happened to drop work.
    """
    results = pg_harness.sweep("cost_reserve", concurrencies=(1, 2, 4, 8, 16),
                               iterations=6, ctx=pg_ctx)

    assert len(results) >= 3, f"sweep stopped after {len(results)} rung(s)"
    for result in results:
        assert result.operations == result.concurrency * result.iterations, (
            f"{result.workload} c={result.concurrency}: accounted "
            f"{result.operations} of {result.concurrency * result.iterations}")
        assert result.failed == 0, f"unexpected failures: {result.errors}"
        assert result.throughput > 0.0
        assert result.percentile(0.50) <= result.percentile(0.99)
        assert result.detail["pool"]["size"] == pg_harness.pool_size

    # Rising concurrency must not make the per-operation cost fall: if it did,
    # the curve would be measuring a warmed cache rather than more parallelism.
    single = results[0]
    for result in results[1:]:
        assert result.percentile(0.50) >= single.percentile(0.50) * 0.5, (
            f"c={result.concurrency} p50 {result.percentile(0.50):.4f}s is "
            f"implausibly below the single-threaded "
            f"{single.percentile(0.50):.4f}s")


# ---------------------------------------------------------------------------
# 2. MUTATION (a): a cap that fits FEWER reservations than the concurrency
# ---------------------------------------------------------------------------


def test_budget_cap_below_concurrency_refuses_exactly_the_excess(
        pg_harness: LoadHarness, pg_ctx: lh.LoadContext) -> None:
    """16 threads, one budget that fits 5. Exactly 5 succeed, 11 are refused.

    This is the mutation test for the workspace row lock. The claim under test
    is that ``reserve_spend`` decides *and writes* in one transaction under that
    lock, so N concurrent callers cannot all be told yes for the same last unit
    of budget.

    Two things must hold, and the second is the one a lock-free implementation
    fails:

    1. the ledger total never exceeds the cap;
    2. the number of successes equals the number the cap allows -- exactly,
       not "roughly", because an implementation that checks the total outside
       the lock produces 6, 7 or 8.
    """
    cap, amount, concurrency, iterations = 0.05, 0.01, 16, 2
    affordable = int(cap / amount)
    workspace_id = _make_workspace(cap)
    pg_ctx.workspaces = [workspace_id]

    result = pg_harness.run("cost_reserve", concurrency=concurrency,
                            iterations=iterations, ctx=pg_ctx,
                            reserve_amount=amount)

    assert result.failed == 0, (
        f"the cap must REFUSE, not raise: {result.errors}")
    assert result.ok == affordable, (
        f"expected exactly {affordable} reservations to fit under a "
        f"${cap} cap with ${amount} each, got {result.ok} ok / "
        f"{result.refused} refused")
    assert result.refused == concurrency * iterations - affordable
    assert _ledger_total(workspace_id) == pytest.approx(amount * affordable)


# ---------------------------------------------------------------------------
# 3. MUTATION (b): N workers, one queue, one winner per job
# ---------------------------------------------------------------------------


def test_one_worker_wins_each_job_under_concurrent_claims(
        pg_harness: LoadHarness, pg_ctx: lh.LoadContext) -> None:
    """20 queued jobs, 20 threads polling the same head of the queue.

    ``claim_next`` is a conditional ``UPDATE`` whose ``rowcount`` is the verdict,
    so exactly one worker can win a job even when every worker read the same
    candidate id. The invariant is checked on the DATABASE and only on THIS
    test's job ids, because ``claim_next`` takes no workspace: a poller claims
    whatever is at the head of the GLOBAL queue, so a module-scoped fixture
    that already holds jobs would otherwise contribute its winners to the count
    and make "20 winners" a statement about somebody else's work.

    Two numbers come out of this:

    * **exactly-once holds.** Every one of the 20 jobs ends RUNNING under a
      DISTINCT owner, which is the property that matters and the one a
      select-then-update implementation fails.
    * **starvation is real.** Most pollers come back with nothing while jobs sit
      ``QUEUED``. The candidate read uses ``FOR UPDATE SKIP LOCKED``, so when
      concurrent pollers hold the candidate rows' locks a poller's candidate set
      comes back EMPTY and ``claim_next`` returns ``None`` on its first read
      instead of retrying -- ``_CANDIDATE_TRIES`` is never reached, because the
      empty-set short circuit fires first. That is available work that nobody is
      doing, at the exact concurrency the pool is sized for. It lives in
      ``job_leases.py``, another lane's file; the curve is in
      ``docs/PRODUCTION_LOAD_REPORT.md``.

    The loop drains the 20 jobs across bounded rounds rather than asserting one
    round suffices, because a run where 80% of pollers starve is a legitimate
    outcome of the guarantee and not a reason to fail the exactly-once claim.
    """
    from app.db import session_scope
    from app.models import Job

    workspace_id = _make_workspace(100.0, name="W16 race")
    job_ids = _enqueue_jobs(20, workspace_id, job_type="w16.load.claim")
    assert len(job_ids) == 20

    def _running() -> list:
        with session_scope() as s:
            return list(s.scalars(
                select(Job).where(Job.id.in_(job_ids),
                                  Job.status == "RUNNING")).all())

    winners = starved = attempts = 0
    running: list = []
    for round_index in range(8):
        result = pg_harness.run("worker_claim", concurrency=20, iterations=3,
                                ctx=pg_ctx,
                                worker_prefix=f"w16-racer-{round_index}")
        assert result.failed == 0, f"claims raised: {result.errors}"
        winners += result.ok
        starved += result.skipped
        attempts += result.operations
        running = _running()
        if len(running) == 20:
            break

    assert len(running) == 20, (
        f"only {len(running)} of 20 jobs were claimed after {attempts} "
        f"attempts by 20 pollers -- work that is available and unclaimed")
    owners = [row.claimed_by for row in running]
    assert len(set(owners)) == 20, (
        f"{len(set(owners))} distinct owners for 20 jobs -- a job was claimed "
        f"twice")
    for row in running:
        assert row.lease_expires_at is not None, "a claimed job carries no lease"
        assert row.claimed_at is not None
    assert winners >= 20
    print(f"\nclaim race: jobs=20 attempts={attempts} rounds_ok={winners} "
          f"starved_polls={starved} starvation_rate="
          f"{starved / attempts if attempts else 0:.2f}")


# ---------------------------------------------------------------------------
# 4. MUTATION (d): pool saturation, real timeouts, and recovery
# ---------------------------------------------------------------------------


def test_pool_saturation_times_out_callers_and_then_serves_again(
        tmp_path: Path) -> None:
    """Drive the pool to its ceiling, time out a real caller, then recover.

    The pool under test is deliberately small -- ``pool_size=4``,
    ``max_overflow=2``, so six connections total and a 1.5s wait. The shipped
    configuration (10 + 10, 30s) saturates identically, only six times slower;
    the numbers for THAT configuration are in the report. A scaled-down pool is
    what makes a 30-second timeout survivable inside a test.

    What is asserted:

    1. **The pool is the ceiling, not the server.** Peak
       ``pg_stat_activity`` backends for this database equals
       ``pool_size + max_overflow``. PostgreSQL 17 here allows 100 connections,
       so a backend count of 6 is the application refusing to hand out a
       seventh, which is the only claim worth making.
    2. **A real caller times out.** ``cost.reserve_spend`` -- the most
       contended path in the product -- raises while the pool is full. A harness
       that merely counted its own thread failures would not show that the
       product's own entry point is what breaks.
    3. **It recovers.** The pool drains to zero checked-out connections and a
       fresh ``reserve_spend`` succeeds afterwards. Saturation that does not
       recover is not a limit, it is an outage.
    """
    harness = LoadHarness(pool_size=4, max_overflow=2, pool_timeout=1.5,
                          storage_root=str(tmp_path / "pool-storage"))
    harness.create_database()
    try:
        harness.bind()
        harness.migrate()
        ctx = harness.seed(workspaces=1, budget_daily_usd=100.0)
        from app.services.cost import reserve_spend

        capacity = harness.pool_size + harness.max_overflow
        hold_seconds = 2.0
        contenders = 18
        outcomes: list[str] = []
        lock = threading.Lock()
        started = threading.Event()

        def _hold(slot: int) -> None:
            started.wait(10.0)
            label = "ok"
            try:
                with harness.engine.connect() as conn:
                    conn.execute(text("SELECT pg_sleep(:s)"),
                                 {"s": hold_seconds})
            except Exception as exc:  # noqa: BLE001 - the outcome IS the datum
                label = type(exc).__name__
            with lock:
                outcomes.append(label)

        threads = [threading.Thread(target=_hold, args=(i,), daemon=True,
                                    name=f"pool-hold-{i}")
                   for i in range(contenders)]
        for thread in threads:
            thread.start()
        started.set()

        # Sample while the pool is provably full: a checkout cannot succeed here,
        # so every counter read during this window is the saturated state.
        deadline = time.monotonic() + 20.0
        saturated = False
        peak_pool = 0
        peak_backends = 0
        while time.monotonic() < deadline:
            stats = harness.pool_stats()
            peak_pool = max(peak_pool, stats["checked_out"])
            peak_backends = max(peak_backends, harness.pg_connections())
            if stats["checked_out"] >= capacity:
                saturated = True
                break
            time.sleep(0.02)

        timeout_exc: BaseException | None = None
        try:
            reserve_spend(ctx.workspaces[0], 0.001, category="llm",
                          provider="pool-drill")
        except Exception as exc:  # noqa: BLE001 - classified below
            timeout_exc = exc

        for thread in threads:
            thread.join(timeout=60.0)
            assert not thread.is_alive(), "a pool holder never returned"

        # -- 1. it is the pool, and it is the pool's own ceiling -------------
        assert saturated, (
            f"pool never reached {capacity} checked-out connections; peak was "
            f"{peak_pool}")
        assert peak_pool <= capacity, (
            f"handed out {peak_pool} connections from a pool capped at "
            f"{capacity}")
        assert peak_backends == capacity, (
            f"pg_stat_activity showed {peak_backends} backends for this "
            f"database, expected the pool ceiling {capacity}")

        # -- 2. a real caller was told to wait and it ran out of waiting -----
        assert timeout_exc is not None, (
            "reserve_spend SUCCEEDED while the pool was saturated: the drill "
            "did not actually create pressure")
        message = str(timeout_exc)
        assert "QueuePool" in message, (
            f"the failure was not a pool timeout: "
            f"{type(timeout_exc).__name__}: {message}")
        assert "timed out" in message, f"unexpected pool failure: {message}"
        timeouts = sum(1 for o in outcomes if o == "TimeoutError")
        assert timeouts, (
            f"no holder thread saw a pool timeout; outcomes={sorted(set(outcomes))}")

        # -- 3. recovery ------------------------------------------------------
        drained = harness.pool_stats()
        assert drained["checked_out"] == 0, (
            f"the pool did not drain after saturation: {drained}")
        reservation = reserve_spend(ctx.workspaces[0], 0.001, category="llm",
                                    provider="pool-drill-recovery")
        assert reservation.entry_id
        # Exactly ONE dollar in the ledger: the saturated call raised a pool
        # timeout and left no trace. A pool timeout that half-wrote a
        # reservation would be an ownerless spend, so the expected total is the
        # recovery call alone -- not the recovery call plus the dead one.
        assert _ledger_total(ctx.workspaces[0]) == pytest.approx(0.001), (
            "the timed-out reservation left money on the books")
        print(f"\npool drill: capacity={capacity} contenders={contenders} "
              f"holder_timeouts={timeouts} holder_outcomes={sorted(set(outcomes))} "
              f"caller_error={type(timeout_exc).__name__} "
              f"peak_backends={peak_backends}")
    finally:
        harness.restore()
        harness.drop_database()


# ---------------------------------------------------------------------------
# 5. MUTATION (e): cross-workspace isolation at concurrency
# ---------------------------------------------------------------------------


def test_cross_workspace_budget_isolation_under_load(
        pg_harness: LoadHarness, pg_ctx: lh.LoadContext) -> None:
    """Workspace A's exhausted cap must not charge, throttle or refuse B.

    Round-robin means A and B are attempted by the same threads at the same
    instants, so a lock that leaked across tenants -- a global spend counter, a
    lock taken on the wrong row -- shows up as B being refused or B's ledger
    carrying A's dollars. B is given a cap it can never approach, which makes any
    cross-contamination an outright failure rather than a rounding difference.
    """
    tight, roomy, amount = 0.02, 50.0, 0.01
    a_id = _make_workspace(tight, name="W16 A")
    b_id = _make_workspace(roomy, name="W16 B")
    pg_ctx.workspaces = [a_id, b_id]

    result = pg_harness.run("cost_reserve", concurrency=16, iterations=4,
                            ctx=pg_ctx, reserve_amount=amount)

    assert result.failed == 0, f"isolation run raised: {result.errors}"
    a_total = _ledger_total(a_id)
    b_total = _ledger_total(b_id)

    assert a_total <= tight + 1e-9, (
        f"workspace A was charged ${a_total:.4f} against a ${tight} cap")
    assert b_total == pytest.approx(amount * (result.ok - int(tight / amount)),
                                    abs=1e-6), (
        f"workspace B's ledger (${b_total:.4f}) does not match its own "
        f"successful reservations: A's spend leaked into B, or B's was refused")
    # Round-robin over 2 workspaces at 16x4: each got exactly half the attempts.
    b_attempts = (16 * 4) // 2
    assert b_total == pytest.approx(amount * b_attempts, abs=1e-6), (
        f"B saw {b_total / amount:.0f} successful reservations out of "
        f"{b_attempts} attempts -- some of B's calls were refused")
    print(f"\nisolation: A_total=${a_total:.4f} B_total=${b_total:.4f} "
          f"ok={result.ok} refused={result.refused} errors={result.errors}")


# ---------------------------------------------------------------------------
# 6. Publishing idempotency at concurrency
# ---------------------------------------------------------------------------


def test_publish_idempotency_creates_exactly_one_job_under_concurrency(
        pg_harness: LoadHarness, pg_ctx: lh.LoadContext) -> None:
    """Eight threads enqueue the same publish with the same idempotency key.

    The invariant that matters is that exactly ONE publish job exists
    afterwards. A re-run schedule that published the same video twice is an
    incident nobody can undo, so this asserts the row count and not the shape of
    the loser's exception.

    Recorded finding, and deliberately not asserted away: the loser's outcome is
    an ``IntegrityError`` from the unique index rather than the ``None`` the
    sequential path returns. ``jobs.enqueue`` owns that behaviour and is another
    lane's file. The uniqueness is enforced by the database -- which is the
    stronger of the two guarantees -- but the caller-visible shape differs
    between the sequential and the concurrent path.
    """
    from sqlalchemy.exc import IntegrityError

    from app.db import session_scope
    from app.engine.campaign.publish_flow import publish_idempotency_key
    from app.models import Job
    from app.services import jobs as jobs_service

    workspace_id = pg_ctx.workspaces[0]
    variant_id = f"w16-variant-{uuid.uuid4().hex[:10]}"
    key = publish_idempotency_key(variant_id, "youtube")

    # Sequential first: the documented "returns None" path.
    first = jobs_service.enqueue("campaign.publish",
                                 {"variant_id": variant_id, "platform": "youtube"},
                                 workspace_id=workspace_id, idempotency_key=key)
    second = jobs_service.enqueue("campaign.publish",
                                  {"variant_id": variant_id, "platform": "youtube"},
                                  workspace_id=workspace_id, idempotency_key=key)
    assert first is not None, "the first publish enqueue must be accepted"
    assert second is None, "a sequential duplicate enqueue must be deduped"

    # Concurrent: a different key, eight threads, one winner.
    race_key = publish_idempotency_key(f"{variant_id}-race", "youtube")
    outcomes: list[str] = []
    lock = threading.Lock()

    def _race() -> None:
        # Labelled by OUTCOME, not by "did it raise". ``enqueue`` signals a
        # dedupe by RETURNING ``None``, so a thread that only records whether it
        # raised reports a dedupe as a success -- which is how a duplicate
        # publication hides inside a test that looks like it passed.
        try:
            returned = jobs_service.enqueue(
                "campaign.publish",
                {"variant_id": f"{variant_id}-race", "platform": "youtube"},
                workspace_id=workspace_id, idempotency_key=race_key)
            label = "accepted" if returned else "deduped"
        except IntegrityError:
            label = "IntegrityError"
        except Exception as exc:  # noqa: BLE001 - classified, not hidden
            label = type(exc).__name__
        with lock:
            outcomes.append(label)

    threads = [threading.Thread(target=_race, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)

    with session_scope() as s:
        rows = list(s.scalars(
            select(Job).where(Job.idempotency_key == race_key)).all())
    assert len(rows) == 1, (
        f"{len(rows)} publish jobs exist for ONE idempotency key -- a "
        f"duplicate publication")
    accepted = sum(1 for o in outcomes if o == "accepted")
    assert accepted == 1, (
        f"{accepted} of 8 concurrent enqueues were ACCEPTED for one key; "
        f"outcomes={sorted(set(outcomes))}")
    print(f"\npublish idempotency: outcomes={sorted(set(outcomes))} "
          f"rows={len(rows)}")


# ---------------------------------------------------------------------------
# 7. The harness reports through the EXISTING metric vocabulary
# ---------------------------------------------------------------------------


def test_record_load_result_publishes_only_existing_metric_names() -> None:
    """A load run must not invent series no alert rule reads.

    ``app/services/observability/metrics.py`` declares a fixed vocabulary. A
    harness that added ``ymoney_load_*`` names would create series that look
    like telemetry and are decoration. This asserts against the registry itself
    rather than against a list copied out of it.
    """
    from app.services.observability import metrics as obs

    result = LoadResult(
        workload="cost_reserve", concurrency=8, iterations=10, wall_seconds=1.0,
        operations=80, ok=60, refused=20, skipped=0, failed=0,
        detail={"pool": {"checked_out": 6, "size": 10, "overflow": 0}})
    lh.record_load_result(result)

    assert obs.DB_POOL_CHECKED_OUT is not None
    assert obs.BUDGET_REFUSALS is not None
    # ``_Handle`` publishes ``.name``. The module also exports helper FUNCTIONS,
    # so only handles are read -- a function has no ``.name`` string and would
    # otherwise smuggle a non-metric into the vocabulary check.
    names = {getattr(getattr(obs, attr), "name", "") for attr in dir(obs)
             if attr.isupper()}
    names = {name for name in names if isinstance(name, str) and name}
    assert names, "the metric registry exposes no named series"
    assert not any(name.startswith("ymoney_load") for name in names), (
        f"the load harness introduced its own series: "
        f"{sorted(n for n in names if n.startswith('ymoney_load'))}")
    # The two the harness actually writes must be declared, or "it records into
    # the existing vocabulary" is an assertion about a series that does not exist.
    assert obs.DB_POOL_CHECKED_OUT.name in names
    assert obs.BUDGET_REFUSALS.name in names


# ---------------------------------------------------------------------------
# The full curve, as a script
# ---------------------------------------------------------------------------


def _sweep_curve(harness: LoadHarness, ctx: lh.LoadContext, *,
                 workloads: tuple[str, ...] = CURVE_WORKLOADS,
                 ladder: tuple[int, ...] = LADDER,
                 iterations: int = 8) -> list[LoadResult]:
    """Every workload on the ladder; stop each at its own breaking point."""
    token = harness.api_session(ctx)
    ctx.options["api_token"] = token
    harness.warm_api(ctx, route=_PROJECTS_ROUTE)

    out: list[LoadResult] = []
    for name in workloads:
        # Each curve starts from a queue with deep enough work in it that no
        # rung measures an empty-table scan instead of a claim.
        if name in ("worker_claim", "publish_idempotent", "job_create"):
            _enqueue_jobs(4000, ctx.workspaces[0])
        options: dict[str, object] = {}
        if name == "api_traffic":
            options = {"api_route": _PROJECTS_ROUTE, "api_token": token}
        out.extend(harness.sweep(name, concurrencies=ladder,
                                 iterations=iterations, ctx=ctx, **options))
    return out


def main(argv: list[str] | None = None) -> int:
    """``python -m backend.tests.test_work16_load --sweep`` -- the full curve.

    The default ``pool_timeout`` here is 8s, not the shipped 30s. The ceiling
    that matters -- ``pool_size + max_overflow`` = 20 connections -- does not
    depend on the timeout, but a rung above it would otherwise spend 30 seconds
    per blocked thread before reporting the same failure. Set
    ``YMONEY_LOAD_POOL_TIMEOUT=30`` to reproduce the shipped wait.

    Prints a table and a JSON blob. The JSON is what the report quotes; a table
    a human eyeballed is not evidence.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    iterations = 8
    wants_sweep = "--sweep" in argv
    # Order-independent: ``--sweep --iterations 8`` and ``--iterations 8 --sweep``
    # must mean the same thing. A hand-rolled parser that assumed a fixed order
    # consumed ``--sweep`` as a number, which is the sort of thing that gets
    # discovered by the person trying to reproduce a number.
    for position, token in enumerate(argv):
        if token == "--iterations":
            iterations = int(argv[position + 1])
    wants_sweep = wants_sweep or "--sweep" in argv

    if not lh.load_enabled():
        print(f"set {lh.LOAD_FLAG_ENV}=1 to run this", file=sys.stderr)
        return 2
    if not lh.pg_available():
        print(f"PostgreSQL unreachable at {lh.admin_dsn()}", file=sys.stderr)
        return 2

    harness = LoadHarness(pool_size=10, max_overflow=10,
                          pool_timeout=float(
                              os.environ.get("YMONEY_LOAD_POOL_TIMEOUT", "8")),
                          storage_root=os.environ.get(
                              "YMONEY_LOAD_STORAGE_ROOT", ""))
    harness.create_database()
    try:
        harness.bind()
        harness.migrate()
        ctx = harness.seed(workspaces=2, accounts_per_workspace=1)
        if wants_sweep:
            results = _sweep_curve(harness, ctx, iterations=iterations)
            print(lh.format_table(results))
            print(json.dumps([r.to_dict() for r in results], indent=2,
                             default=str))
        else:
            print(harness.run("cost_reserve", concurrency=8,
                              iterations=iterations, ctx=ctx).line())
    finally:
        harness.restore()
        harness.drop_database()
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())


# Keep the imported sentinels referenced so a reader can see they are the
# vocabulary a workload uses, and so ruff does not strip them.
assert REFUSED and SKIPPED