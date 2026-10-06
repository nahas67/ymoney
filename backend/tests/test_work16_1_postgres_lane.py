"""Work 16 §9: ONE command, one real PostgreSQL, the whole §1 surface.

The command
-----------
::

    cd backend
    $env:YMONEY_TEST_POSTGRES="postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
    .\\.venv\\Scripts\\python -m pytest tests/test_work16_1_postgres_lane.py tests/test_work16_1_schema_parity.py -q

On Linux/macOS the two files also run as one command without the env var when
the server is on the default DSN, which is what
``tests/test_work16_postgres_semantics.py`` already assumes.

What it covers, and why each item is a ROW and not a STRING
-----------------------------------------------------------
Every assertion here reads a row back out of a real PostgreSQL 17. None of them
asserts that a compiled statement contains a substring, because a dialect
compiler will happily produce SQL that means something else on the server, and
on the two backends this repository supports it produces DIFFERENT SQL for the
same ORM expression. A test that reads rows is the only one that cannot be
fooled by that.

==========================  ====================================================
section                     what only a real server can falsify
==========================  ====================================================
migrations                  ``DROP``/``ALTER`` behaviour, transactional DDL, the
                            replay being a genuine no-op, 0038's up/down/up
schema parity               the ORM's declared schema vs a migrations-built one,
                            read out of ``information_schema``/``pg_indexes``
JSON queries                native ``json`` (not TEXT), ``->>``, parsed results,
                            22P02 on a malformed document, transaction revival
job claim                   ``FOR UPDATE SKIP LOCKED`` against a live row lock,
                            and the conditional-UPDATE ``rowcount`` as the answer
concurrent enqueue          two real connections racing on ``idempotency_key``:
                            exactly one row survives, 23505 on the loser
budget reservations         ``cost.reserve_spend`` under the workspace row lock:
                            a cap is never exceeded by concurrent spenders
reconciliation              the paid-submission ledger survives a round trip
                            through the server with its JSON and money intact
==========================  ====================================================

The services bind their session factory at IMPORT time (``from app.db import
SessionLocal``), so :func:`wired` points each one at this scratch database. That
is a test-local monkeypatch through pytest's own fixture, not a change to the
service: ``reserve_spend``, ``enqueue`` and ``claim_next`` are the SHIPPED
implementations running against a real server, which is the whole point.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import inspect, select, text

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
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


PG_UP = _pg_reachable(PG_DSN)

pytestmark = pytest.mark.skipif(
    not PG_UP, reason="no PostgreSQL reachable at YMONEY_TEST_POSTGRES")


def _admin_url() -> str:
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _engine_url(db_name: str) -> str:
    return (_admin_url().rsplit("/", 1)[0].replace("postgresql://",
                                                   "postgresql+psycopg://", 1)
            .rstrip("/") + "/" + db_name)


def _sid() -> str:
    import uuid

    return uuid.uuid4().hex


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch() -> Iterator:
    """Factory for throwaway databases, dropped ``WITH (FORCE)`` at teardown."""
    made: list[str] = []

    def make(label: str):
        import psycopg
        from sqlalchemy import create_engine

        name = f"w16_1_lane_{label}_{os.urandom(4).hex()}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return create_engine(_engine_url(name), pool_size=12, max_overflow=12)

    try:
        yield make
    finally:
        import psycopg

        for name in made:
            try:
                with psycopg.connect(_admin_url(), autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


@pytest.fixture(scope="module")
def pg(scratch) -> dict:
    """A fully migrated scratch database, built by the PRODUCTION boot path."""
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import applied_versions, run_migrations

    engine = scratch("main")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        first_pass = run_migrations(s)
        replay = run_migrations(s)
        versions = applied_versions(s)
    return {"engine": engine, "Session": factory, "first_pass": first_pass,
            "replay": replay, "versions": versions,
            "url": _engine_url(engine.url.database)}


@pytest.fixture
def wired(pg, monkeypatch):
    """Point the shipped services at THIS database, for the duration of a test.

    ``app.services.cost`` / ``jobs`` / ``job_leases`` each did
    ``from app.db import SessionLocal, session_scope`` at import time, so they
    hold the process-wide factory. Every reference is replaced here and restored
    by ``monkeypatch`` afterwards; nothing in ``app/`` is modified.
    """
    from contextlib import contextmanager

    from app.services import cost, job_leases, jobs

    factory = pg["Session"]

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

    monkeypatch.setattr(cost, "SessionLocal", factory)
    monkeypatch.setattr(cost, "session_scope", scoped)
    monkeypatch.setattr(jobs, "session_scope", scoped)
    monkeypatch.setattr(job_leases, "session_scope", scoped)
    return pg


@contextmanager
def _workspace(pg, label: str) -> Iterator[str]:
    """One committed ``Workspace`` row, cleaned up afterwards."""
    from app.models import Workspace

    with pg["Session"]() as s:
        ws = Workspace(name=label, slug=f"{label}-{os.urandom(4).hex()}",
                       niche="AI money")
        s.add(ws)
        s.commit()
        wid = str(ws.id)
    try:
        yield wid
    finally:
        with pg["Session"]() as s:
            s.execute(text("DELETE FROM workspaces WHERE id=:w"), {"w": wid})
            s.commit()


# ---------------------------------------------------------------------------
# 1. Migrations
# ---------------------------------------------------------------------------


def test_every_migration_applies_and_the_replay_applies_nothing(pg):
    """The corpus builds a real server schema, and building it twice is free."""
    assert pg["first_pass"], "no migration applied at all"
    assert pg["replay"] == [], (
        f"a second run applied {pg['replay']}; migrations are not idempotent")
    assert set(pg["first_pass"]) == set(pg["versions"]), (
        "a migration recorded itself without applying, or applied without "
        "recording")
    latest = max(pg["versions"])
    assert latest == max(pg["first_pass"]), (
        f"the newest applied migration is {latest}, not "
        f"{max(pg['first_pass'])}")


def test_the_migration_built_schema_has_the_objects_the_migrations_own(pg):
    """Tables and indexes that no ``create_all`` can be the source of."""
    insp = inspect(pg["engine"])
    tables = set(insp.get_table_names())
    for table in ("budget_rollup_limits", "gpu_devices", "gpu_reservations",
                  "storage_objects", "trend_signals", "editorial_plans",
                  "editorial_plan_items", "production_capacity"):
        assert table in tables, f"{table} is missing after the migration run"
    jobs_idx = {i["name"] for i in insp.get_indexes("jobs")}
    assert {"ix_jobs_lease_recovery", "ix_jobs_claimed_by"} <= jobs_idx


def test_0038_survives_an_up_down_up_cycle_on_this_server(pg):
    """The convergence migration is reversible HERE, not just in the parity file.

    ``downgrade`` drops indexes before it unsets a default, because SQLite
    refuses a ``DROP COLUMN`` an index still names and PostgreSQL silently keeps
    the dangling index. On this server the order is exercised for real.
    """
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import load_migrations

    module = dict(load_migrations())["0038_schema_parity"]
    engine = scratch_engine = pg["engine"]
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def names() -> set[str]:
        insp = inspect(engine)
        out: set[str] = set()
        for table in sorted({t for _n, t, _c in module._INDEXES}):
            out |= {str(i["name"]) for i in insp.get_indexes(table)}
        return {n for n in out if n in {name for name, _t, _c in module._INDEXES}}

    with factory() as s:
        module.upgrade(s)
        s.commit()
    up = names()
    assert len(up) == len(module._INDEXES)

    with factory() as s:
        module.downgrade(s)
        s.commit()
    assert names() == set(), "0038's downgrade left indexes behind"

    with factory() as s:
        module.upgrade(s)
        s.commit()
    assert names() == up, "0038 is not reversible"
    assert scratch_engine is engine


# ---------------------------------------------------------------------------
# 2. Schema parity (the comparator, run against this server)
# ---------------------------------------------------------------------------


def test_the_orm_and_the_migrations_describe_one_schema(pg):
    """The §1 comparison itself, on the database this lane just built.

    Delegates to the parity module's comparator so the lane and the dedicated
    parity file cannot drift apart: one implementation, two entry points. On a
    ``create_all`` + migrations database there should be NO differences at all --
    not even the documented ones, because 0038 and the ``server_default`` work
    closed every one of them -- so that is asserted too.
    """
    from app.db import Base
    from tests.test_work16_1_schema_parity import compare_table

    inspector = inspect(pg["engine"])
    live = set(inspector.get_table_names())
    metadata = Base.metadata
    absent = [name for name in sorted(metadata.tables) if name not in live]
    differences = []
    for name in sorted(metadata.tables):
        if name in live:
            differences += compare_table(name, metadata.tables[name], inspector)
    assert not absent, f"model tables absent from this database: {absent}"
    assert not differences, (
        f"{len(differences)} ORM/migration difference(s) on a database built by "
        f"the production boot path:\n  " + "\n  ".join(str(d) for d in differences))
    assert len(metadata.tables) >= 100, (
        f"only {len(metadata.tables)} tables compared; the comparison is thin")


# ---------------------------------------------------------------------------
# 3. JSON queries
# ---------------------------------------------------------------------------


def test_json_columns_are_native_json_and_parse_on_read(pg):
    """``json`` here, a TEXT column there -- the difference the ORM cannot hide."""
    with pg["Session"]() as s:
        rows = dict(s.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='jobs' AND column_name IN ('payload','result')")).all())
        assert rows == {"payload": "json", "result": "json"}, rows

        from app.models import Job

        payload = {"emoji": "\U0001F680", "arabic": "\u0645\u0631\u062d\u0628\u0627",
                   "n": 7, "f": 0.30000000000000004, "flag": True, "nothing": None,
                   "deep": {"list": [1, "two", {"three": 3}], "empty": {}}}
        job_id = _sid()
        s.add(Job(id=job_id, type="w16.1.json", status="QUEUED", payload=payload))
        s.commit()
        s.expire_all()

        # Through the ORM...
        assert s.get(Job, job_id).payload == payload
        # ...and through raw SQL, which PARSES on PostgreSQL instead of
        # returning a string. A row-level read is the only honest check: a
        # compiled-SQL assertion cannot tell these two apart.
        raw = s.execute(text("SELECT payload FROM jobs WHERE id=:i"),
                        {"i": job_id}).scalar()
        assert isinstance(raw, dict), (
            f"raw SQL returned {type(raw).__name__}, not a parsed document")
        assert raw["f"] == 0.30000000000000004 and raw["nothing"] is None

        # A JSON-path predicate through the ORM, executed by the server.
        hit = s.execute(select(Job.id).where(
            Job.payload["deep"]["list"][2]["three"].as_integer() == 3)).scalars().all()
        assert job_id in hit
        s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
        s.commit()


def test_a_malformed_json_document_is_refused_and_the_session_recovers(pg):
    """``22P02`` aborts the TRANSACTION on PostgreSQL; SQLite never does.

    So the second half -- the rollback that hands back a usable connection --
    is the half only a real server can prove.
    """
    from sqlalchemy.exc import DBAPIError

    from app.models import EventLog

    key = f"w16-1-badjson-{os.urandom(4).hex()}"
    eid = _sid()
    with pg["Session"]() as s:
        s.add(EventLog(id=eid, workspace_id=key, level="info", source="system",
                       kind="w16.1", message="ok", data_json={"a": 1}))
        s.commit()
        with pytest.raises(DBAPIError) as caught:
            s.execute(text("UPDATE events SET data_json = :v WHERE id=:i"),
                      {"v": "not json at all", "i": eid})
            s.commit()
        assert _sqlstate(caught.value) == "22P02", _sqlstate(caught.value)

        # The failed statement POISONED the transaction...
        with pytest.raises(DBAPIError) as aborted:
            s.execute(text("SELECT 1"))
        assert _sqlstate(aborted.value) == "25P02"
        # ...and the rollback is what makes the connection usable again.
        s.rollback()
        assert s.execute(text("SELECT 1")).scalar() == 1

        s.execute(text("DELETE FROM events WHERE workspace_id=:w"), {"w": key})
        s.commit()


# ---------------------------------------------------------------------------
# 4. Job claim
# ---------------------------------------------------------------------------


def _enqueue(wired, job_type: str, *, key: str | None = None,
             priority: int = 100) -> str | None:
    from app.services import jobs

    return jobs.enqueue(job_type, {"lane": "w16.1"}, idempotency_key=key,
                        priority=priority)


def test_a_job_is_claimed_exactly_once_and_its_lease_is_visible_in_the_row(wired):
    """``claim_next`` writes the evidence a dashboard reads: who, until when."""
    from app.models import Job
    from app.services import job_leases

    job_id = _enqueue(wired, "w16.1.render")
    assert job_id
    worker = f"w16-1-worker-{os.urandom(3).hex()}"

    claimed = job_leases.claim_next(worker)
    assert claimed is not None, "nothing was claimable"
    assert claimed.job_id == job_id

    with wired["Session"]() as s:
        row = s.get(Job, job_id)
        s.refresh(row)
        assert row.claimed_by == worker, (
            f"claimed_by is {row.claimed_by!r}, not the worker that won")
        assert row.lease_expires_at is not None
        assert row.claimed_at is not None
        assert job_leases.lease_is_valid(row) is True

    # A second claim must not steal a LIVE lease.
    assert job_leases.claim_next(f"w16-1-other-{os.urandom(3).hex()}") is None

    with wired["Session"]() as s:
        s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
        s.commit()


def test_concurrent_claimers_produce_one_winner_not_one_per_worker(wired):
    """Four real connections race for four queued jobs.

    Each claim is a single conditional UPDATE whose ``rowcount`` IS the answer,
    so the invariant is "every job is claimed exactly once" -- not "each worker
    got something". A per-worker assertion would pass even if the compare-and-set
    were broken and two workers shared a row.
    """
    from app.models import Job
    from app.services import job_leases

    workers = [f"w16-1-race-{i}" for i in range(4)]
    ids = [_enqueue(wired, f"w16.1.race.{i}") for i in range(4)]
    assert all(ids)

    barrier = threading.Barrier(len(workers))
    won: list[str] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def run(worker: str) -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(6):
                claimed = job_leases.claim_next(worker)
                if claimed is None:
                    return
                with lock:
                    won.append(claimed.job_id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"a claimer raised: {errors!r}"
    assert sorted(won) == sorted(ids), (
        f"claimed set {sorted(won)} != queued set {sorted(ids)}: a job was "
        f"claimed twice ({len(won)} claims for {len(ids)} jobs) or never")

    with wired["Session"]() as s:
        for job_id in ids:
            row = s.get(Job, job_id)
            s.refresh(row)
            assert row.claimed_by in workers, (
                f"{job_id} ended up owned by {row.claimed_by!r}")
        s.execute(text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": ids})
        s.commit()


# ---------------------------------------------------------------------------
# 5. Concurrent enqueue
# ---------------------------------------------------------------------------


def test_a_retried_enqueue_never_produces_two_rows_for_one_key(wired):
    """The SAME idempotency key from eight connections at once -> ONE job row.

    WHAT THIS PROVES, AND WHAT IT ALSO FOUND
    -----------------------------------------
    ``jobs.enqueue`` is read-then-write: ``SELECT ... WHERE idempotency_key = :k``
    and then ``INSERT``, with nothing between them. On SQLite the write lock
    serialises the two halves, so the loser reads "already there" and returns
    ``None``. On PostgreSQL two sessions interleave, both read "not there", both
    insert, and the second is refused by ``jobs_idempotency_key_key`` with 23505.

    So the invariant this lane pins is the one that actually holds and that the
    DATABASE is responsible for: **one key, one row, ever**. The UNIQUE index is
    what guarantees it; ``enqueue``'s SELECT is an optimisation, not the
    mechanism. A duplicate row cannot exist here, and if one did the count below
    would be 2.

    It also records, as a finding rather than an expectation, that ``enqueue``
    SURFACES the 23505 to the caller instead of returning ``None``. ``services/
    jobs.py`` is not this lane's to change, so the test asserts both branches are
    safe -- ``None`` or 23505, never two rows -- and the report names the
    behaviour. A caller that only tolerates ``None`` will see an exception under
    concurrency, and a caller that treats any exception as fatal will retry into
    the same race.
    """
    from sqlalchemy.exc import IntegrityError

    key = f"w16-1-idem-{_sid()}"
    results: list[str | None] = []
    violations: list[str] = []
    unexpected: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def run() -> None:
        try:
            barrier.wait(timeout=30)
            job_id = _enqueue(wired, "w16.1.idem", key=key)
            with lock:
                results.append(job_id)
        except IntegrityError as exc:
            with lock:
                violations.append(_sqlstate(exc) or "no-sqlstate")
        except Exception as exc:  # noqa: BLE001
            with lock:
                unexpected.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not unexpected, f"an enqueue raised something unexpected: {unexpected!r}"
    winners = [r for r in results if r]
    losers = [r for r in results if not r]
    assert len(winners) == 1, (
        f"{len(winners)} concurrent enqueues of one idempotency key returned an "
        f"id: {winners}")
    assert len(winners) + len(violations) + len(losers) == 8, (
        f"{len(winners)} winners + {len(losers)} None + "
        f"{len(violations)} refused != 8 attempts")
    assert all(state == "23505" for state in violations), (
        f"a concurrent enqueue was refused for a reason other than the "
        f"idempotency key: {violations}")

    with wired["Session"]() as s:
        count = s.scalar(text("SELECT count(*) FROM jobs WHERE idempotency_key=:k"),
                         {"k": key})
        assert count == 1, (
            f"{count} rows carry one idempotency key -- the UNIQUE index is the "
            f"only thing preventing duplicate work")
        s.execute(text("DELETE FROM jobs WHERE idempotency_key=:k"), {"k": key})
        s.commit()


def test_distinct_enqueue_keys_all_land(wired):
    """The control: dedupe must not eat unrelated work."""
    keys = [f"w16-1-distinct-{i}-{_sid()}" for i in range(6)]
    ids = [_enqueue(wired, "w16.1.distinct", key=k) for k in keys]
    assert all(ids), f"an unrelated enqueue was dropped: {ids}"
    assert len(set(ids)) == len(ids), "two distinct keys produced one row"
    with wired["Session"]() as s:
        assert s.scalar(text("SELECT count(*) FROM jobs WHERE id = ANY(:ids)"),
                        {"ids": ids}) == len(ids)
        s.execute(text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": ids})
        s.commit()


# ---------------------------------------------------------------------------
# 6. Budget reservations
# ---------------------------------------------------------------------------


def test_a_daily_cap_is_never_exceeded_by_concurrent_spenders(wired):
    """``reserve_spend`` under the workspace row lock, from real connections.

    The claim is arithmetic, not a log line: with a $10 daily cap and eight
    $4 reservations racing, the committed ledger total must be <= $10 and the
    number of accepted reservations must match it exactly. Anything that let two
    spenders read the same pre-lock total would breach the cap.
    """
    from app.services import cost

    with _workspace(wired, "W16-1 Budget") as wid:
        accepted: list[str] = []
        refused = 0
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def spend() -> None:
            nonlocal refused
            try:
                barrier.wait(timeout=30)
                reservation = cost.reserve_spend(wid, 4.0, category="llm",
                                                 provider="w16-1",
                                                 per_call_cap_usd=6.0,
                                                 daily_cap_usd=10.0)
                with lock:
                    accepted.append(reservation.entry_id)
            except cost.BudgetExceededError:
                with lock:
                    refused += 1

        threads = [threading.Thread(target=spend) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert len(accepted) + refused == 8, (
            f"{len(accepted)} accepted + {refused} refused != 8 attempts")
        assert len(accepted) >= 1, "every spender was refused; the cap is broken"
        assert refused >= 1, (
            "no spender was refused: 8 x $4 against a $10 cap cannot all pass, so "
            "the cap is not being enforced")

        with wired["Session"]() as s:
            total = s.scalar(text(
                "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
                "WHERE workspace_id=:w"), {"w": wid})
            assert float(total) <= 10.0 + 1e-9, (
                f"the ledger says ${float(total):.2f} against a $10.00 cap")
            rows = s.scalar(text(
                "SELECT count(*) FROM cost_entries WHERE workspace_id=:w"),
                {"w": wid})
            assert rows == len(accepted), (
                f"{rows} ledger rows for {len(accepted)} accepted reservations: "
                f"a refusal wrote a row")
            s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"),
                      {"w": wid})
            s.commit()


def test_a_refused_reservation_leaves_no_trace(wired):
    """Rollback, not a zero row.

    ``_reservation_session`` rolls back on refusal, so the next attempt must not
    see a spend that never happened. A ``0.00`` row would pass a "did it
    overspend" check while still corrupting the event count a rate limiter reads.
    """
    from app.services import cost

    with _workspace(wired, "W16-1 Refusal") as wid:
        with pytest.raises(cost.BudgetExceededError):
            cost.reserve_spend(wid, 50.0, category="llm", provider="w16-1",
                               daily_cap_usd=1.0)
        with wired["Session"]() as s:
            count = s.scalar(text(
                "SELECT count(*) FROM cost_entries WHERE workspace_id=:w"),
                {"w": wid})
            assert count == 0, (
                f"a refused reservation wrote {count} row(s); the rollback did "
                f"not happen")


# ---------------------------------------------------------------------------
# 7. Reconciliation persistence
# ---------------------------------------------------------------------------

_RECON = ("SUBMISSION_UNKNOWN", "UNKNOWN_EXPOSURE")


def test_the_paid_submission_ledger_round_trips_through_the_server(wired):
    """``videos`` + ``cost_entries`` together: the facts reconciliation needs.

    A submission that may already have been billed is only recoverable if the
    outcome, the provider's id, the correlation id and the money are all still
    there after a restart. So this writes them, drops every object, reads them
    back FROM THE SERVER with raw SQL, and checks the three that matter most:
    the JSON payload, the float money at the ledger's 6-decimal resolution, and
    the fact that the two tables agree on the operation id.
    """
    from app.models import ContentItem, CostEntry, Video, VideoVariant

    op_id = f"w16-1-op-{_sid()}"
    with _workspace(wired, "W16-1 Recon") as wid:
        video_id = _sid()
        item_id, variant_id = _sid(), _sid()
        with wired["Session"]() as s:
            # The full chain a Video can legally exist under: workspace ->
            # content item -> variant -> video. Each link is a real NOT NULL
            # foreign key, so a raw INSERT that skipped one would be rejected by
            # the server -- which is the point of writing this through the ORM.
            s.add(ContentItem(id=item_id, workspace_id=wid, topic="w16.1 recon",
                              status="IN_PRODUCTION"))
            s.add(VideoVariant(id=variant_id, content_item_id=item_id, label="v1"))
            s.add(Video(id=video_id, variant_id=variant_id, workspace_id=wid,
                        engine="mock", status="RENDERING",
                        submission_state=_RECON[0],
                        provider_task_id="remote-abc",
                        submission_detail="provider accepted, response lost",
                        cost_outcome=_RECON[1], submission_operation_id=op_id))
            s.add(CostEntry(id=_sid(), workspace_id=wid, category="video",
                            amount_usd=12345.678901, provider="w16-1",
                            is_estimate=True,
                            detail_json={"operation": "video_render_submit",
                                         "operation_id": op_id,
                                         "reservation": True}))
            s.commit()

        with wired["Session"]() as s:
            s.expunge_all()
            row = s.execute(text(
                "SELECT submission_state, cost_outcome, provider_task_id, "
                "submission_detail, submission_operation_id FROM videos "
                "WHERE id=:i"), {"i": video_id}).mappings().one()
            cost_row = s.execute(text(
                "SELECT amount_usd, is_estimate, detail_json FROM cost_entries "
                "WHERE workspace_id=:w"), {"w": wid}).mappings().one()

        assert row["submission_state"] == _RECON[0]
        assert row["cost_outcome"] == _RECON[1]
        assert row["provider_task_id"] == "remote-abc"
        assert row["submission_detail"] == "provider accepted, response lost"
        assert row["submission_operation_id"] == op_id
        assert cost_row["amount_usd"] == 12345.678901, (
            "money drifted through PostgreSQL's DOUBLE PRECISION at the "
            "ledger's 6-decimal resolution")
        assert cost_row["is_estimate"] is True
        assert isinstance(cost_row["detail_json"], dict)
        assert cost_row["detail_json"]["operation_id"] == op_id, (
            "the video row and the cost row no longer name the same operation, "
            "so the pair is unrecoverable")
        assert cost_row["detail_json"]["reservation"] is True

        # The reconciliation filter an operator runs, executed by the server.
        with wired["Session"]() as s:
            unknown = s.execute(select(Video.id).where(
                Video.workspace_id == wid,
                Video.cost_outcome == "UNKNOWN_EXPOSURE")).scalars().all()
            assert unknown == [video_id]
            s.execute(text("DELETE FROM videos WHERE id=:i"), {"i": video_id})
            s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"),
                      {"w": wid})
            s.commit()
