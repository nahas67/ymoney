"""Work 16 §3 (queue claim starvation) and §4 (concurrent enqueue idempotency).

The command
-----------
::

    cd backend
    $env:YMONEY_TEST_POSTGRES="postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
    .\\.venv\\Scripts\\python -m pytest tests/test_work16_1_queue.py -q

Every test here is parameterised over BOTH backends -- the suite's SQLite and a
real PostgreSQL 17 scratch database -- because the defect this lane fixes is
*only* visible on a server-grade engine. SQLite serialises writers, so a losing
poller's conditional UPDATE is merely refused; PostgreSQL lets it *block* on the
winner's row lock for the length of the winner's transaction, which is what turns
a fairness problem into a wall-clock one. A test that only ran on SQLite would
have passed while the production deployment starved.

Declarative skipping only
-------------------------
The PostgreSQL tests carry ``pytest.mark.skipif``, never ``pytest.skip()``, and
the whole file runs unchanged with no server on the machine. ``YMONEY_TEST_POSTGRES=""``
forces the skip without paying for a connect probe.

Anti-vacuity
------------
A starvation test that asserts "zero duplicate claims" while nothing was claimed
proves nothing about starvation. Every concurrency assertion in this file is
paired with an assertion that work actually happened: a non-zero claim count, a
non-zero contention count, or an explicit statement of how many claims the
defective shape would have produced.

===========================  ==================================================
property                    the test that would fail if it broke
===========================  ==================================================
no job starves              ``test_no_queued_job_starves_at_any_depth``
at most one active owner    ``test_every_job_has_exactly_one_owner``
the claim is atomic         ``test_only_one_of_n_racing_pollers_wins_a_row``
a lost take is not a win    ``test_a_take_that_lost_is_not_handed_back_as_a_win``
leases                      ``test_a_live_lease_cannot_be_stolen``
heartbeat                   ``test_only_the_owner_can_renew_a_lease``
crash recovery              ``test_recovery_waits_for_the_lease_to_expire``
distinguishable outcomes    ``test_idleness_and_losing_are_different_answers``
not a busy loop             ``test_an_idle_worker_polls_once_and_stops``
SKIP LOCKED has teeth       ``test_a_peers_row_lock_does_not_block_a_poll``
concurrent enqueue          ``test_concurrent_duplicates_answer_like_sequential``
integrity errors survive    ``test_a_real_integrity_error_is_not_swallowed``
===========================  ==================================================
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import text

import app.models  # noqa: F401 - registration side effect for Base.metadata

# ---------------------------------------------------------------------------
# Reachability probe. Declarative, so the default SQLite suite needs no server.
# ---------------------------------------------------------------------------

_DEFAULT_DSN = "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
PG_DSN = os.environ.get("YMONEY_TEST_POSTGRES", _DEFAULT_DSN).strip()


def _pg_reachable(dsn: str) -> bool:
    """Whether a PostgreSQL server answers on ``dsn``. Never raises.

    Set ``YMONEY_TEST_POSTGRES=""`` to skip without paying for a connect.
    """
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            return bool(conn.execute("SELECT 1").fetchone()[0] == 1)
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


PG_UP = _pg_reachable(PG_DSN)
SKIP_NO_PG = pytest.mark.skipif(
    not PG_UP, reason="no PostgreSQL reachable at YMONEY_TEST_POSTGRES")

#: Pollers per run. Eight is the default pool's SMALL slot count times a typical
#: single-process deployment, and the interesting depths are the ones where the
#: queue is about the same size as the pool.
POLLERS = 8

#: The depths the brief names: one job, one fewer slot than jobs, exactly as
#: many jobs as slots, one more, and an order of magnitude more.
DEPTHS: tuple[int, ...] = (1, POLLERS - 1, POLLERS, POLLERS + 1, 10 * POLLERS)


def _sid() -> str:
    return uuid.uuid4().hex


#: Job ids are ``VARCHAR(36)``, which PostgreSQL enforces and SQLite does not --
#: so a 44-character id passes on one backend and dies on the other. Everything
#: here stays under it.
_JOB_PREFIX = "w16q"


def _job_id(tag: str = "") -> str:
    body = f"{_JOB_PREFIX}-{_sid()[:10]}"
    return f"{body}-{tag}" if tag else body


# ---------------------------------------------------------------------------
# The PostgreSQL scratch database
# ---------------------------------------------------------------------------


def _admin_url() -> str:
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _scratch_url(name: str) -> str:
    """``postgresql+psycopg://user:pw@host:port/<name>`` for a scratch database.

    The base is split off the database name ONCE, before the scheme is
    rewritten -- splitting after would cut inside ``+psycopg://`` and hand
    SQLAlchemy a hostname of ``postgresql+psycopg:``.
    """
    base = PG_DSN.rsplit("/", 1)[0]
    return base.replace("postgresql://", "postgresql+psycopg://", 1) + "/" + name


@pytest.fixture(scope="module")
def pg_scratch() -> Iterator:
    """Throwaway databases, dropped ``WITH (FORCE)`` at teardown."""
    import psycopg
    from sqlalchemy import create_engine

    made: list[str] = []

    def make(label: str):
        name = f"w16_1_queue_{label}_{os.urandom(4).hex()}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        # A pool of real connections: "concurrent" here means concurrent
        # *sessions*, not concurrent users of one connection.
        return create_engine(_scratch_url(name), pool_size=24, max_overflow=24), \
            _scratch_url(name)

    try:
        yield make
    finally:
        for name in made:
            try:
                with psycopg.connect(_admin_url(), autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


@pytest.fixture(scope="module")
def pg_migrated(pg_scratch) -> dict:
    """A fully migrated scratch database, built by the PRODUCTION boot path."""
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import applied_versions, run_migrations

    engine, url = pg_scratch("main")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        run_migrations(s)
        versions = applied_versions(s)
    assert versions, "the scratch database got no migrations"
    return {"engine": engine, "Session": factory, "url": url}


# ---------------------------------------------------------------------------
# A "lane": where a test's pollers read and write
# ---------------------------------------------------------------------------


class _Lane:
    """One backend, behind the shipped services, for one test."""

    def __init__(self, scope, label: str, engine=None, url: str = "") -> None:
        self._scope = scope
        self.label = label
        self.engine = engine
        #: The scratch database's own DSN, for a raw driver connection that
        #: must NOT go through the service's session factory (the row-lock
        #: holder has to be an independent session by construction).
        self.url = url

    @contextmanager
    def scope(self):
        with self._scope() as s:
            yield s


@pytest.fixture
def sqlite_lane() -> _Lane:
    """The suite's own database, reached through ``app.db.session_scope``.

    Nothing is monkeypatched: the services already bind the process-wide factory
    at import time, so this lane runs the SHIPPED wiring.
    """
    from app.db import session_scope

    return _Lane(session_scope, "sqlite")


@pytest.fixture
def pg_lane(pg_migrated, monkeypatch) -> _Lane:
    """Point the shipped services at the scratch database for this test.

    ``jobs`` and ``job_leases`` each did ``from app.db import session_scope`` at
    import time, so every reference is replaced here and restored by
    ``monkeypatch``. Nothing in ``app/`` is modified, and the code under test is
    still the production implementation.
    """
    from app.services import job_leases, jobs

    factory = pg_migrated["Session"]

    @contextmanager
    def scoped():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(jobs, "session_scope", scoped)
    monkeypatch.setattr(job_leases, "session_scope", scoped)
    return _Lane(scoped, "postgres", engine=pg_migrated["engine"],
                 url=pg_migrated["url"])


#: One entry per backend. The PostgreSQL case carries the declarative skip, so
#: the file still runs with no server.
LANES = [
    pytest.param("sqlite_lane", id="sqlite"),
    pytest.param("pg_lane", id="postgres", marks=SKIP_NO_PG),
]


@pytest.fixture
def lane(request) -> _Lane:
    """Resolve the backend named by the ``lanes`` parameterisation."""
    return request.getfixturevalue(request.param)


def _lanes(*names: str) -> list:
    """A parameterisation list honouring the per-backend skip marks."""
    out = []
    for entry in LANES:
        name = entry.values[0]
        if name in names:
            out.append(entry)
    return out


@pytest.fixture
def lanes(request) -> _Lane:
    return request.getfixturevalue(request.param)


_ALL_LANES = _lanes("sqlite_lane", "pg_lane")
_SQLITE_ONLY = _lanes("sqlite_lane")
_PG_ONLY = _lanes("pg_lane")


# ---------------------------------------------------------------------------
# Queue plumbing
# ---------------------------------------------------------------------------


def _drain(lane: _Lane, prefix: str = "") -> None:
    """Empty the queue, so a poll cannot claim another test's leftovers.

    ``conftest`` hands the whole session one SQLite file, and several suites
    enqueue background work into it. A starvation assertion that silently
    included somebody else's job would be worthless, so the queue starts empty
    and ends empty.
    """
    from sqlalchemy import select

    from app.models import Job

    with lane.scope() as s:
        query = select(Job).where(Job.status.in_(
            ("QUEUED", "RETRYING", "RUNNING", "CLAIMED")))
        if prefix:
            query = query.where(Job.id.like(f"{prefix}%"))
        for row in s.scalars(query).all():
            s.delete(row)


def _seed(lane: _Lane, depth: int, *, prefix: str = _JOB_PREFIX) -> list[str]:
    """``depth`` due, claimable, SMALL-class jobs. Committed, so pollers see them."""
    from app.models import Job
    from app.models.base import utcnow

    ids: list[str] = []
    with lane.scope() as s:
        for i in range(depth):
            job_id = f"{prefix}-{_sid()[:10]}-{i}"
            # No hint token in the type, so ``classify_workload`` returns SMALL
            # and an undifferentiated slot can run it.
            s.add(Job(id=job_id, type=f"w16.1.q{i}", status="QUEUED",
                      payload={"i": i}, priority=100, next_run_at=utcnow()))
            ids.append(job_id)
    return ids


def _due_count(lane: _Lane, workload: str = "") -> int:
    """Due CLAIMABLE jobs, optionally restricted to one workload class.

    Class-aware on purpose: a RENDER slot finding only SMALL work is idle by
    design, and counting that as starvation would make the number meaningless.
    """
    from sqlalchemy import select

    from app.models import Job
    from app.models.base import utcnow
    from app.services.worker_pool import classify_workload

    with lane.scope() as s:
        rows = s.execute(
            select(Job.type, Job.payload).where(
                Job.status.in_(("QUEUED", "RETRYING")),
                Job.next_run_at <= utcnow())).all()
    return sum(1 for t, p in rows
               if not workload or str(classify_workload(t, p or {})) == workload)


class _Run:
    """The result of one concurrent poll race."""

    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.outcomes: Counter[str] = Counter()
        self.polls_per_worker: Counter[str] = Counter()
        self.latencies: list[float] = []
        self.errors: list[BaseException] = []
        #: (worker, detail) pairs that lied: "IDLE" while work was still due.
        self.false_idle: list[str] = []


def _race(lane: _Lane, pollers: int, *, pins: dict[str, str] | None = None,
          max_polls: int = 400) -> _Run:
    """``pollers`` threads polling like pool slots until the queue is empty.

    Exactly the shape :meth:`app.services.worker_pool.WorkerPool._slot` uses:
    claim, and on a non-claim look again only while there is genuinely work to be
    had. ``max_polls`` is a spin detector -- a caller that cannot conclude
    "nothing to do" hits it, and the assertion downstream says so.
    """
    from app.services import job_leases

    run = _Run()
    lock = threading.Lock()
    barrier = threading.Barrier(pollers)
    workers = [f"w16-1-w{i}" for i in range(pollers)]

    def poll(worker: str) -> None:
        pinned = (pins or {}).get(worker, "")
        try:
            barrier.wait(timeout=60)
            while True:
                with lock:
                    run.polls_per_worker[worker] += 1
                    spun = run.polls_per_worker[worker] >= max_polls
                if spun:
                    return
                started = time.monotonic()
                poll_result = job_leases.claim_next_poll(worker, workload=pinned)
                elapsed = time.monotonic() - started
                outcome = str(poll_result.outcome)
                with lock:
                    run.outcomes[outcome] += 1
                    if poll_result.job is not None:
                        run.claimed.append(poll_result.job.job_id)
                        run.latencies.append(elapsed)
                        continue
                    # An IDLE that lands on a non-empty queue is the defect.
                    # Sampled with the class filter so a pinned slot that
                    # legitimately has no work of its class is not counted.
                    #
                    # Only IDLE is sampled this way, and the asymmetry is the
                    # point: IDLE is the terminal answer (the worker sleeps and
                    # gives up), so anything still due afterwards was genuinely
                    # abandoned by that worker. A CONTENTED poll is a transient
                    # retry by construction, and by the time a post-hoc query
                    # runs, the peer that made it contended has usually already
                    # claimed the rows -- so it cannot be sampled after the
                    # fact. Its anti-spin half is asserted deterministically
                    # instead, by test_an_empty_queue_answers_idle_never_contended.
                    if outcome == "IDLE" and _due_count(lane, pinned):
                        run.false_idle.append(
                            f"{worker} reported IDLE with due work remaining "
                            f"(considered={poll_result.considered}, "
                            f"lost={len(poll_result.lost)})")
                if outcome == "IDLE":
                    return
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted on
            with lock:
                run.errors.append(exc)

    threads = [threading.Thread(target=poll, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
    return run


def _assert_drained(lane: _Lane, ids: list[str], run: _Run) -> None:
    """Every seeded job claimed exactly once, by somebody, with no leftovers."""
    from app.models import Job

    assert not run.errors, f"a poller raised: {run.errors[:3]!r}"
    claimed = Counter(run.claimed)
    assert len(claimed) == len(ids), (
        f"{len(claimed)} of {len(ids)} queued jobs were claimed; "
        f"missing={sorted(set(ids) - set(claimed))[:4]}")
    dupes = {j: n for j, n in claimed.items() if n > 1}
    assert not dupes, f"these jobs were claimed more than once: {dupes}"
    assert _due_count(lane) == 0, (
        f"{_due_count(lane)} job(s) still claimable after every poller stopped")
    with lane.scope() as s:
        for job_id in ids:
            row = s.get(Job, job_id)
            assert row is not None
            assert row.claimed_by, f"{job_id} was returned by a claimer but names no owner"
            assert row.lease_expires_at is not None, (
                f"{job_id} was claimed without a lease")
            assert row.status == "RUNNING", (
                f"{job_id} is {row.status!r} after being claimed")


# ===========================================================================
# 1. Starvation: no queued job starves because of poller topology
# ===========================================================================


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
@pytest.mark.parametrize("depth", DEPTHS)
def test_no_queued_job_starves_at_any_depth(lane, depth):
    """Depths 1, P-1, P, P+1 and 10P with P pollers: every job is claimed once.

    The defect this replaces, measured on PostgreSQL 17 with these same eight
    pollers: at depth 8, 20 of 36 polls returned ``None`` while all 8 jobs were
    still ``QUEUED`` -- each loser had read the identical head-of-queue batch,
    lost it row by row, re-read the same batch four times, and reported the
    queue as empty. A poller that says "empty" when the queue is full is not
    slow, it is blind, and the pool then sleeps it for a full poll interval while
    the one slot that keeps winning drains everything underneath it.

    Anti-vacuous: ``len(claimed) == depth`` and ``depth >= 1``, so an
    implementation that claims nothing fails here rather than passing an
    "all zero duplicates" assertion.
    """
    _drain(lane)
    ids = _seed(lane, depth)
    assert len(ids) == depth

    run = _race(lane, POLLERS)

    assert run.outcomes["CLAIMED"] >= depth, (
        f"only {run.outcomes['CLAIMED']} claims for {depth} queued jobs")
    _assert_drained(lane, ids, run)


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
@pytest.mark.parametrize("depth", (POLLERS, 10 * POLLERS))
def test_a_poller_never_reports_idleness_while_work_is_due(lane, depth):
    """The queue is not empty, so no poll may say ``IDLE``. ``CONTENDED`` is fine.

    This is the assertion the old return type made impossible to write: with
    ``ClaimedJob | None`` there was no way to tell "the queue is empty" from "a
    peer won every row I could see", so a test could only count ``None``s and
    could not say which of the two a given ``None`` was.
    """
    _drain(lane)
    ids = _seed(lane, depth)

    run = _race(lane, POLLERS)

    assert not run.errors, f"a poller raised: {run.errors[:3]!r}"
    assert run.outcomes["CLAIMED"] >= depth
    assert not run.false_idle, (
        f"{len(run.false_idle)} poll(s) reported idleness with work due: "
        f"{run.false_idle[:3]}")
    _assert_drained(lane, ids, run)


@pytest.mark.parametrize("lane", _PG_ONLY, indirect=True)
def test_losing_a_race_is_different_from_finding_an_empty_queue(lane):
    """``CONTENDED`` is reachable, means something, and is not ``IDLE``.

    Constructed rather than raced, because the real thing is too fast to
    provoke on demand: with one row and eight pollers the winner's transaction
    usually commits before the losers even read, so they correctly report an
    empty queue and the contended branch is never entered. So this holds the
    row behind a real PostgreSQL ``FOR UPDATE`` from a separate connection --
    the job is ``QUEUED`` and due, and provably invisible to a poller that skips
    locked rows -- and asserts the poller says "CONTENDED" rather than
    "IDLE". Releasing the lock then has to produce a claim, which proves the
    answer was a statement about contention and not a permanent block.

    Both directions are asserted, because a one-sided distinction is not a
    distinction: ``test_an_empty_queue_answers_idle_never_contended`` covers
    the other half, and a slot that got it wrong either way would spin or sleep
    through real work.

    The old API made this test unwritable: with ``ClaimedJob | None`` a caller
    could count ``None``s but could not say which of the two a given ``None``
    was, which is precisely the information the pool needs.
    """
    import psycopg

    from app.services.job_leases import PollOutcome

    _drain(lane)
    ids = _seed(lane, 1)
    assert len(ids) == 1
    job_id = ids[0]

    holder = psycopg.connect(lane.url.replace("+psycopg", ""), connect_timeout=10)
    try:
        holder.execute("BEGIN")
        locked = holder.execute(
            "SELECT id FROM jobs WHERE status='QUEUED' FOR UPDATE").fetchall()
        assert len(locked) == 1, (
            f"the lock holder locked {len(locked)} of 1 due rows")

        blocked = job_leases_module().claim_next_poll("w16-1-blocked")
        assert blocked.job is None, (
            f"a poll claimed {blocked.job.job_id} from a row another session "
            f"holds FOR UPDATE")
        assert blocked.outcome is PollOutcome.CONTENDED, (
            f"a row was due, another session held it, and the poll answered "
            f"{blocked.outcome!r} (considered={blocked.considered}); "
            f"CONTENDED is the answer that tells the slot to retry rather than "
            f"sleep through the work")
        assert blocked.contended is True
        assert blocked.claimed is False
    finally:
        holder.rollback()
        holder.close()

    # Released: the same poller can now take it, so CONTENDED meant "ask again",
    # not "this row is unreachable".
    released = job_leases_module().claim_next_poll("w16-1-blocked")
    assert released.job is not None and released.job.job_id == job_id, (
        f"after the lock was released the row was not claimable: "
        f"{released.job}")
    assert released.outcome is PollOutcome.CLAIMED

    # And then the queue really is empty.
    empty = job_leases_module().claim_next_poll("w16-1-post")
    assert empty.outcome is PollOutcome.IDLE
    assert empty.job is None
    assert empty.contended is False


def job_leases_module():
    """Late import so the module-level fixtures stay import-order clean."""
    from app.services import job_leases

    return job_leases


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_an_empty_queue_answers_idle_never_contended(lane):
    """The counter-test for the distinction, on both backends.

    A distinguishable outcome is only safe if the *idle* answer stays cheap and
    honest. If a slot could not tell "empty" from "lost", the fix for starvation
    would be a spin: retry forever on a queue that will never have work. So the
    empty case is asserted to be ``IDLE`` -- which is what the pool slot turns
    into a sleep -- and repeated to show the answer is stable rather than
    decaying into contention.
    """
    from app.services.job_leases import PollOutcome

    _drain(lane)
    for _ in range(4):
        result = job_leases_module().claim_next_poll("w16-1-void")
        assert result.outcome is PollOutcome.IDLE, (
            f"an empty queue answered {result.outcome!r}; a slot would retry it "
            f"instead of sleeping, which is the hot loop the distinction is "
            f"supposed to make impossible")
        assert result.job is None
        assert result.claimed is False
        assert result.considered == 0
        assert result.lost == ()


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_an_idle_worker_polls_once_and_stops(lane):
    """Not a busy loop: a claimed queue still yields to ``IDLE`` in one poll.

    The fix adds a distinguishable outcome so a slot can retry a lost race
    instead of sleeping. That is only safe if the *idle* answer stays cheap: a
    poller that cannot tell the two apart would either sleep through real work
    (the old bug) or spin forever on an empty queue (a worse one). So the
    counter-test is here, and it is bounded by statement count, not by luck.
    """
    _drain(lane)
    ids = _seed(lane, 2)
    assert len(ids) == 2

    # Claim the two jobs, so what remains claimable is nothing. A worker must
    # treat that as idleness, not as contention.
    for worker in ("w16-1-drain-a", "w16-1-drain-b"):
        claimed = job_leases_module().claim_next_poll(worker)
        assert claimed.job is not None, "setup could not claim the seeded jobs"

    polls = 0
    for _ in range(5):
        result = job_leases_module().claim_next_poll("w16-1-idle")
        polls += 1
        assert result.job is None
        assert not result.contended, (
            "a queue with nothing due was reported as CONTENTED, so a slot "
            "would retry it instead of sleeping")
        assert result.outcome == "IDLE"
    assert polls == 5, "this test does not poll five times"


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_the_pool_gives_every_slot_work_at_the_depth_of_the_pool(lane):
    """Anti-vacuity for the sleep decision: N slots, N jobs, every slot busy.

    A real :class:`~app.services.worker_pool.WorkerPool` with ``POLLERS``
    undifferentiated slots, ``POLLERS`` jobs, and a poll interval of 15s against
    a 12s deadline -- so a slot that SLEEPS on a lost race cannot get another job
    before the test gives up, and only a slot that retries one can. The interval
    is the whole test: at the default 1s the pre-fix shape still spreads eight
    jobs across eight slots by accident, because the losers eventually wake up.

    Before the fix the winners loop straight back round and the losers sleep, so
    exactly one slot executed everything.
    """
    from app.core.config import settings
    from app.services.worker_pool import PoolPlan, WorkerPool

    _drain(lane)
    ids = _seed(lane, POLLERS)

    executed: list[str] = []
    lock = threading.Lock()

    async def execute(claimed) -> None:
        with lock:
            executed.append(claimed.job_id)

    old_interval = settings.job_poll_interval_seconds
    settings.job_poll_interval_seconds = 15.0
    pool = WorkerPool(worker="w16-1-pool",
                      plan=PoolPlan(slots={None: POLLERS}),
                      lease_ttl=60.0)
    try:
        import asyncio

        async def run() -> None:
            # Started and drained on ONE loop: the pool's tasks and its
            # draining Event belong to whichever loop created them.
            await pool.start(execute)
            try:
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline:
                    with lock:
                        if len(executed) >= len(ids):
                            break
                    await asyncio.sleep(0.02)
            finally:
                await pool.drain(5.0)

        asyncio.run(run())
    finally:
        settings.job_poll_interval_seconds = old_interval

    assert len(executed) == len(ids), (
        f"{len(executed)} of {len(ids)} jobs executed; the queue did not drain")
    claims = Counter(executed)
    assert not {j: n for j, n in claims.items() if n > 1}, (
        f"a job ran twice: {[j for j, n in claims.items() if n > 1]}")

    # The real anti-vacuity check: the work was spread over the pool, not
    # monopolised by one slot. Every job carries the slot identity that claimed
    # it, so this reads the owners straight off the rows.
    with lane.scope() as s:
        from app.models import Job

        owners = Counter()
        for job_id in ids:
            row = s.get(Job, job_id)
            s.refresh(row)
            assert row.claimed_by.startswith("w16-1-pool#"), row.claimed_by
            owners[row.claimed_by] += 1
    assert len(owners) >= 2, (
        f"only {len(owners)} slot(s) ever claimed work ({dict(owners)}): the "
        f"losers slept through a non-empty queue, which is the defect")


# ===========================================================================
# 2. The guarantees that must NOT regress
# ===========================================================================


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_the_claim_is_atomic_across_sessions(lane):
    """One row, ``POLLERS`` pollers: exactly one winner.

    The claim is one conditional UPDATE whose ``rowcount`` is the answer, so
    there is no read-then-write window for two pollers to interleave in. This is
    the test that caught the earlier ``_Take`` truthiness bug -- ``_Take`` is a
    ``StrEnum``, so ``if not _take(...)`` is False for ``LOST`` too and a lost
    race silently becomes a second owner.
    """
    _drain(lane)
    ids = _seed(lane, 1)
    assert len(ids) == 1

    barrier = threading.Barrier(POLLERS)
    winners: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def poll(worker: str) -> None:
        try:
            barrier.wait(timeout=60)
            for _ in range(3):
                got = job_leases_module().claim_next_poll(worker)
                if got.job is not None:
                    with lock:
                        winners.append(got.job.job_id)
                    return
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=poll, args=(f"w16-1-atomic-{i}",))
               for i in range(POLLERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert not errors, f"a poller raised: {errors[:3]!r}"
    assert winners == ids, (
        f"{len(winners)} poller(s) claimed one job: {winners}; exactly one "
        f"owner is the whole point of the conditional UPDATE")


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_every_job_has_exactly_one_owner(lane):
    """At most one active owner, read back off the rows after a full drain.

    "One owner per job", not "one job per owner": a slot that claims three jobs
    in a row is working correctly. What must never happen is one job with two
    owners, or one job handed out twice -- and both are checked against the rows
    here and against the claim set in ``_assert_drained``.
    """
    _drain(lane)
    ids = _seed(lane, 4 * POLLERS)

    run = _race(lane, POLLERS)
    assert not run.errors, f"a poller raised: {run.errors[:3]!r}"

    _assert_drained(lane, ids, run)

    with lane.scope() as s:
        from app.models import Job

        rows = []
        for job_id in ids:
            row = s.get(Job, job_id)
            s.refresh(row)
            rows.append(row)

    for row in rows:
        assert row.claimed_by and row.lease_expires_at and row.claimed_at, (
            f"{row.id} is missing its ownership evidence: "
            f"by={row.claimed_by!r} lease={row.lease_expires_at!r}")
        assert isinstance(row.claimed_by, str)
    owners = Counter(r.claimed_by for r in rows)
    assert sum(owners.values()) == len(ids)
    assert len(owners) >= 2, (
        f"a single worker claimed all {len(ids)} jobs ({dict(owners)}); the "
        f"losers never got a turn, which is the starvation this lane removes")


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_a_take_that_lost_is_not_handed_back_as_a_win(lane):
    """``LOST`` means somebody else owns it. It must never become a claim.

    The take is forced to report ``LOST`` for the first attempt while the row
    stays perfectly claimable, so a guard that treats "not REFUSED" as "WON"
    hands back a job this worker does not own -- and a second worker can then
    claim the same row for real. The control half of this test claims the same
    job with an unpatched take, so a passing run proves the patch is what caused
    the non-claim and not that the row was unclaimable anyway.
    """
    _drain(lane)
    ids = _seed(lane, 1)
    job_id = ids[0]
    leases = job_leases_module()

    real_take_in = leases._take_in
    real_take = leases._take
    state = {"calls": 0}

    def losing_take_in(*args, **kwargs):
        state["calls"] += 1
        return leases._Take.LOST

    def losing_take(*args, **kwargs):
        state["calls"] += 1
        return leases._Take.LOST

    leases._take_in = losing_take_in
    leases._take = losing_take
    try:
        result = leases.claim_next_poll("w16-1-fake-loser")
    finally:
        leases._take_in = real_take_in
        leases._take = real_take

    assert state["calls"] >= 1, (
        "the take was never called, so this test did not exercise the guard")
    assert result.job is None, (
        f"a LOST take was handed back as a claim: {result.job.job_id}; that is "
        f"a job this worker does not own")
    assert not result.claimed

    # Control: unpatched, the same row is claimable, so the assertion above is
    # about the guard and not about the fixture.
    control = leases.claim_next_poll("w16-1-control")
    assert control.job is not None and control.job.job_id == job_id, (
        "the control could not claim the row, so the negative assertion above "
        "was vacuous")


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_a_live_lease_cannot_be_stolen(lane):
    """``claim_by_id`` and ``claim_next`` both refuse a job with a live lease."""
    from app.models.base import utcnow

    _drain(lane)
    ids = _seed(lane, 2)
    leases = job_leases_module()

    first = leases.claim_next_poll("w16-1-owner")
    assert first.job is not None
    job_id = first.job.job_id
    remaining = [j for j in ids if j != job_id]

    assert leases.claim_by_id("w16-1-thief", job_id) is None, (
        "a job with a live lease was claimed by a second worker")
    assert all(leases.claim_next_poll(f"w16-1-thief-{i}").job.job_id in remaining
               for i in range(len(remaining))), (
        "claim_next returned a job whose lease is held")
    with lane.scope() as s:
        from app.models import Job

        row = s.get(Job, job_id)
        s.refresh(row)
        assert row.claimed_by == first.job.claimed_by
        assert leases.lease_is_valid(row, now=utcnow()) is True


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_only_the_owner_can_renew_a_lease(lane):
    """Heartbeat is conditional on ``claimed_by``, so a thief cannot renew."""
    _drain(lane)
    _seed(lane, 1)
    leases = job_leases_module()

    claimed = leases.claim_next_poll("w16-1-beat")
    assert claimed.job is not None
    job_id = claimed.job.job_id

    assert leases.renew_lease(job_id, "w16-1-impostor") is False, (
        "a non-owner renewed a lease; the heartbeat is not proof of ownership")
    assert leases.renew_lease(job_id, "w16-1-beat", ttl=600.0) is True
    with lane.scope() as s:
        from app.models import Job

        row = s.get(Job, job_id)
        s.refresh(row)
        assert row.heartbeat_at is not None
        assert (row.lease_expires_at - row.claimed_at).total_seconds() > 400.0, (
            "the renewal did not push the deadline out")

    assert leases.begin_run(job_id, "w16-1-impostor") is False, (
        "a non-owner entered the handler")
    assert leases.begin_run(job_id, "w16-1-beat") is True


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_recovery_waits_for_the_lease_to_expire(lane):
    """A live worker's job is never reclaimed; a dead worker's job is.

    The distinction is the whole of Work 16 §2. ``reclaim_expired`` requeued every
    ``RUNNING`` row on every worker boot, so with two workers a deploy could hand
    a live, possibly-billed render to a second process and run it twice.
    """
    from datetime import timedelta

    from app.models.base import utcnow

    _drain(lane)
    _seed(lane, 2)
    leases = job_leases_module()

    live = leases.claim_next_poll("w16-1-live")
    dead = leases.claim_next_poll("w16-1-dead")
    assert live.job is not None and dead.job is not None

    # The crashed worker's lease has lapsed; the live one has not.
    with lane.scope() as s:
        from app.models import Job

        s.execute(
            text("UPDATE jobs SET lease_expires_at=:t WHERE id=:i"),
            {"t": utcnow() - timedelta(seconds=1), "i": dead.job.job_id})

    reclaimed = leases.reclaim_expired(now=utcnow())
    ids = [r.job_id for r in reclaimed]
    assert dead.job.job_id in ids, (
        f"an expired lease was not reclaimed: {ids}")
    assert live.job.job_id not in ids, (
        "recovery reclaimed a job whose worker is still heartbeating; a live "
        "30-minute render would be handed to a second worker and paid for twice")

    with lane.scope() as s:
        from app.models import Job

        assert s.get(Job, dead.job.job_id).status == "RETRYING"
        assert s.get(Job, live.job.job_id).status == "RUNNING"

    # And the reclaimed job is immediately claimable again, by somebody else.
    retaken = leases.claim_next_poll("w16-1-reclaimer")
    assert retaken.job is not None and retaken.job.job_id == dead.job.job_id, (
        f"the reclaimed job was not claimable again: {retaken.job}")


@pytest.mark.parametrize("lane", _PG_ONLY, indirect=True)
def test_a_peers_row_lock_does_not_block_a_poll(lane, monkeypatch):
    """``SKIP LOCKED`` has teeth: a locked row is skipped, not waited on.

    A separate connection holds ``FOR UPDATE`` (no ``SKIP LOCKED``) on every due
    row. A poller that asks for ``SKIP LOCKED`` must come back immediately with
    nothing it can take; one that asks for a plain ``FOR UPDATE`` blocks behind
    the holder on its first write, which is the difference between a pool that
    scales and a pool that serialises.

    The poll runs under a 2-second PostgreSQL ``statement_timeout`` so that
    removing ``SKIP LOCKED`` fails this test in two seconds with a legible
    message instead of hanging until the suite timeout. Without the clause the
    failure is a genuine block, not a slow assertion: the poller's conditional
    UPDATE waits on the holder's row lock and never returns.
    """
    import psycopg

    from app.services import job_leases

    _drain(lane)
    ids = _seed(lane, 4)
    assert len(ids) == 4

    # A dedicated driver connection to THIS scratch database. It must not be a
    # pooled service session: the point is that the lock is held by a session
    # the poll cannot see or join.
    holder = psycopg.connect(lane.url.replace("+psycopg", ""),
                             connect_timeout=10)
    done = threading.Event()
    box: dict[str, object] = {}

    # Bound every statement the poller runs, so a blocking read fails in two
    # seconds instead of hanging the suite. Transaction-local, so the pooled
    # connection does not keep the setting after this test.
    inner = lane._scope  # noqa: SLF001 - the lane's own factory, deliberately

    @contextmanager
    def bounded():
        with inner() as s:
            s.execute(text("SET LOCAL statement_timeout = '2s'"))
            yield s

    monkeypatch.setattr(job_leases, "session_scope", bounded)

    try:
        holder.execute("BEGIN")
        locked = holder.execute(
            "SELECT id FROM jobs WHERE status='QUEUED' FOR UPDATE").fetchall()
        assert len(locked) == len(ids), (
            f"the lock holder only locked {len(locked)} of {len(ids)} due rows, "
            f"so this test would pass without proving anything")

        def poll() -> None:
            try:
                box["poll"] = job_leases.claim_next_poll("w16-1-blocked")
            except BaseException as exc:  # noqa: BLE001 - recorded below
                box["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        # Generous: a healthy skip comes back in single-digit milliseconds, and
        # anything under this bound means it did not queue behind the lock.
        finished_while_locked = done.wait(timeout=10.0)
    finally:
        holder.rollback()
        holder.close()
        monkeypatch.undo()

    thread.join(timeout=60)
    assert finished_while_locked, (
        f"a poll blocked behind a peer's row lock instead of skipping it "
        f"({box.get('error')!r}); the candidate SELECT is not using SKIP "
        f"LOCKED, so every worker in the pool serialises on whichever row a "
        f"peer happens to be claiming")
    assert "error" not in box, (
        f"the poll raised while a row was locked: {box.get('error')!r} -- a "
        f"poller that waits on a peer's row lock instead of skipping it is a "
        f"pool that serialises on whichever row a peer happens to be claiming")
    result = box["poll"]
    assert result.job is None, (
        f"the poll claimed {result.job.job_id} from a row another session "
        f"holds FOR UPDATE")
    assert result.considered == 0, (
        f"the poll read {result.considered} candidate(s) it should have skipped")


# ===========================================================================
# 3. Concurrent enqueue idempotency
# ===========================================================================


def _enqueue(lane: _Lane, key: str, *, job_type: str = "w16.1.idem") -> str | None:
    from app.services import jobs

    return jobs.enqueue(job_type, {"lane": lane.label}, idempotency_key=key)


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_a_sequential_duplicate_returns_none(lane):
    """The shape the concurrent path has to match."""
    _drain(lane)
    key = f"w16-1-seq-{_sid()}"

    first = _enqueue(lane, key)
    second = _enqueue(lane, key)
    assert first, "the first enqueue created nothing"
    assert second is None, (
        f"a sequential duplicate returned {second!r}; the documented answer for "
        f"a deduped enqueue is None")

    with lane.scope() as s:
        count = s.scalar(text("SELECT count(*) FROM jobs WHERE idempotency_key=:k"),
                         {"k": key})
    assert count == 1, f"{count} rows carry one idempotency key"


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_concurrent_duplicates_answer_like_sequential(lane):
    """Eight real sessions, one key: one winner, seven ``None``, no exceptions.

    This is the §4 defect. ``enqueue`` is read-then-write, and on an engine that
    does not serialise writers two sessions both read "not there" and both
    insert; the UNIQUE index refuses the second with 23505, which ``enqueue``
    passed straight to the caller. So the same logical event -- "this key was
    already taken" -- had two different caller-visible shapes, and the
    exception-shaped one only ever appeared under concurrency. Measured before
    the fix on PostgreSQL 17 with these eight sessions: three raised 23505.

    Integrity is asserted too, but it is not the point: the index held before the
    fix as well. The point is that ``None`` is now the only answer.

    Each thread runs the shipped ``jobs.enqueue`` through the lane's session
    factory, and the factory hands out a different pooled connection to each, so
    these are concurrent *sessions*, not concurrent users of one connection.
    """
    _drain(lane)
    key = f"w16-1-conc-{_sid()}"
    attempts = 8

    barrier = threading.Barrier(attempts)
    ids: list[str | None] = []
    raised: list[BaseException] = []
    lock = threading.Lock()

    def enqueue_once() -> None:
        try:
            barrier.wait(timeout=60)
            job_id = _enqueue(lane, key)
            with lock:
                ids.append(job_id)
        except BaseException as exc:  # noqa: BLE001 - this is the assertion
            with lock:
                raised.append(exc)

    threads = [threading.Thread(target=enqueue_once) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert not raised, (
        f"{len(raised)} concurrent enqueue(s) raised instead of returning the "
        f"documented dedupe answer; a caller that only tolerates None breaks "
        f"here, and a caller that treats any exception as fatal retries into "
        f"the same race: {[repr(e) for e in raised[:3]]}")
    assert len(ids) == attempts, f"{len(ids)} of {attempts} enqueues returned"
    winners = [i for i in ids if i]
    losers = [i for i in ids if not i]
    assert len(winners) == 1, (
        f"{len(winners)} concurrent enqueues of one key returned an id: "
        f"{winners}; the same key must resolve to one canonical job")
    assert len(losers) == attempts - 1

    with lane.scope() as s:
        rows = s.execute(text(
            "SELECT id FROM jobs WHERE idempotency_key=:k"), {"k": key}).all()
    assert len(rows) == 1, (
        f"{len(rows)} rows carry one idempotency key; the UNIQUE index is the "
        f"only thing preventing duplicate work")
    assert rows[0][0] == winners[0], (
        "the surviving row is not the one the winner was told about")


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_distinct_keys_all_land(lane):
    """Control: dedupe must not eat unrelated work."""
    _drain(lane)
    keys = [f"w16-1-distinct-{i}-{_sid()}" for i in range(6)]
    ids = [_enqueue(lane, k) for k in keys]
    assert all(ids), f"an unrelated enqueue was dropped: {ids}"
    assert len(set(ids)) == len(ids), "two distinct keys produced one row"
    with lane.scope() as s:
        from sqlalchemy import select

        from app.models import Job

        found = s.scalars(select(Job.id).where(Job.id.in_(ids))).all()
    assert len(found) == len(ids), f"{len(found)} of {len(ids)} rows landed"


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_a_real_integrity_error_is_not_swallowed(lane, monkeypatch):
    """The 23505 guard is narrow: a uniqueness failure on ANOTHER column raises.

    This is the sharpest version of the test, because the failure it provokes
    *is* a 23505 -- the same SQLSTATE the idempotency race produces. The only
    thing separating "this writer lost the key race, the winner's row is
    canonical" from "the primary key collided because the producer generated a
    bad id" is the confirmation re-read, so this drives a genuine duplicate-key
    insert and asserts it propagates instead of becoming a cheerful ``None``.
    A producer bug turned into a silently dropped job is the failure mode a
    blanket ``except IntegrityError`` would introduce.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import Job
    from app.models.base import utcnow

    _drain(lane)
    anchor = _job_id("anchor")
    with lane.scope() as s:
        s.add(Job(id=anchor, type="w16.1.anchor", status="QUEUED",
                  payload={}, next_run_at=utcnow()))

    from app.services import jobs

    real_job = jobs.Job

    class _Clashing:
        """Proxies ``Job``, but every row it builds collides on the primary key.

        A plain proxy rather than a declarative subclass: subclassing registers
        a second mapped class under the same name and makes SQLAlchemy warn
        about replacing the entry in its string-lookup table every run. The
        column attributes are re-exposed because ``enqueue`` builds its own
        ``select(Job.idempotency_key)`` against whatever ``Job`` names.
        """

        def __new__(cls, **kwargs):
            kwargs["id"] = anchor
            return real_job(**kwargs)

    for _attr in real_job.__mapper__.column_attrs:
        setattr(_Clashing, _attr.key, getattr(real_job, _attr.key))
    monkeypatch.setattr(jobs, "Job", _Clashing)
    key = f"w16-1-clash-{_sid()}"
    with pytest.raises(IntegrityError):
        jobs.enqueue("w16.1.clash", {}, idempotency_key=key)

    # Nothing was recorded for that key: the failure was not converted into a
    # no-op, so the caller still knows its write did not happen.
    with lane.scope() as s:
        assert s.scalar(text(
            "SELECT count(*) FROM jobs WHERE idempotency_key=:k"),
            {"k": key}) == 0, (
            "a failed enqueue left a row behind; the rollback did not happen")


@pytest.mark.parametrize("lane", _ALL_LANES, indirect=True)
def test_an_enqueue_without_a_key_never_dedupes(lane):
    """``idempotency_key=None`` means "no dedupe", and the guard must not fire."""
    _drain(lane)
    ids = [_enqueue(lane, "") for _ in range(3)]
    ids = [i for i in ids if i]
    # An empty string is falsy, so it takes the "no key" branch; three distinct
    # rows must exist, not one.
    assert len(set(ids)) == 3, f"unkeyed enqueues collided: {ids}"


# ---------------------------------------------------------------------------
# Teardown: leave the shared SQLite file as we found it.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _leave_no_jobs_behind():
    yield
    try:
        from app.db import session_scope

        with session_scope() as s:
            for row in s.execute(text(
                    "SELECT id FROM jobs WHERE id LIKE :p OR id LIKE :q"),
                    {"p": f"{_JOB_PREFIX}%", "q": "w16-1-%"}).all():
                s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": row[0]})
    except Exception:  # noqa: BLE001 - teardown must not mask a real failure
        pass
