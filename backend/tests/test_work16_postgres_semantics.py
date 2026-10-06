"""Work 16 §1: production PostgreSQL qualification -- the DATABASE's behaviour.

Why this file exists
--------------------
§1 is not "the migrations apply". That is already established: 37 migrations
apply clean on a real PostgreSQL 17, a replay applies 0, and the app boots.
Proving that only ever exercised ONE code path -- ``CREATE TABLE`` from the ORM
metadata, then migrations that mostly found their work already done.

Everything SQLite cannot falsify is still unproven:

* an **upgrade** from a deployment that is genuinely at an OLD schema, where the
  later migrations must actually DO something, with data in the tables;
* **indexes and unique constraints** as PostgreSQL enforces them, not as
  ``create_all`` hopes;
* **JSON/JSONB** portability, which is the one that bites: ``sqlalchemy.JSON``
  is TEXT on SQLite and native ``json`` on PostgreSQL, and the two emit
  *different operators* for the same ORM expression;
* **locking** (``FOR UPDATE SKIP LOCKED``, the workspace row lock) and
  **isolation**, which decide whether the reservation invariant holds;
* **rollback**, i.e. that an aborted transaction leaves nothing and the
  connection is still usable.

No mocks. Every test here is a claim about what the database does, and a mock
database does not lock, abort, or reject a duplicate.

Conventions borrowed from ``test_work16_distributed_jobs.py``: the DSN comes
from ``YMONEY_TEST_POSTGRES``, every test runs against a throwaway database
created with ``psycopg`` and dropped ``WITH (FORCE)``, and the whole module
skips -- declaratively, via ``pytestmark`` -- when no server answers.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import app.models  # noqa: F401 - registration side effect for Base.metadata

# ---------------------------------------------------------------------------
# Reachability probe. Declarative: the whole module is skipped when it fails,
# so the default SQLite suite never requires a database.
# ---------------------------------------------------------------------------

_DEFAULT_DSN = "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
PG_DSN = os.environ.get("YMONEY_TEST_POSTGRES", _DEFAULT_DSN).strip()


def _pg_reachable(dsn: str) -> bool:
    """Whether a PostgreSQL server answers on ``dsn``. Never raises.

    Set ``YMONEY_TEST_POSTGRES=""`` to skip the module without paying for a
    connection attempt.
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
    """The maintenance database, for CREATE/DROP DATABASE."""
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _engine_url(db_name: str) -> str:
    """SQLAlchemy needs the driver named: bare ``postgresql://`` means psycopg2,
    which is not installed here."""
    return _admin_url().rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://", 1) \
        .rstrip("/") + "/" + db_name


@pytest.fixture(scope="module")
def scratch():
    """Factory for throwaway databases, dropped ``WITH (FORCE)`` at teardown."""
    made: list[str] = []

    def make(label: str):
        import psycopg
        from sqlalchemy import create_engine

        name = f"w16_sem_{label}_{os.urandom(4).hex()}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return create_engine(_engine_url(name), pool_size=10, max_overflow=10)

    try:
        yield make
    finally:
        import psycopg

        for name in made:
            with psycopg.connect(_admin_url(), autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _migrated(engine):
    """The schema the application actually runs against."""
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.migrations.runner import run_migrations

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        run_migrations(s, create_missing_tables=False)
    return factory


@pytest.fixture(scope="module")
def pg(scratch) -> dict:
    """A fully-migrated scratch database plus handles onto it."""
    engine = scratch("base")
    factory = _migrated(engine)
    return {"engine": engine, "Session": factory, "url": _engine_url(engine.url.database)}


def _sid() -> str:
    import uuid

    return uuid.uuid4().hex


def _workspace(session, label: str) -> str:
    """One committed ``Workspace`` row.

    Through the ORM on purpose: ``workspaces`` carries four NOT NULL columns
    with no server default, and every test that needs a tenant wants a row that
    a real deployment could actually have written -- not a raw INSERT that only
    works because it skipped half the table.
    """
    from app.models import Workspace

    ws = Workspace(name=label, slug=f"{label}-{os.urandom(4).hex()}", niche="AI money")
    session.add(ws)
    session.commit()
    return str(ws.id)


def create_sqlite(path):
    """A SQLite engine with the FULL ORM schema, for the portability compares."""
    from sqlalchemy import create_engine

    from app.db import Base

    engine = create_engine(f"sqlite:///{Path(path).as_posix()}")
    Base.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# 1. Server facts
# ---------------------------------------------------------------------------


def test_the_server_is_new_enough_to_have_skip_locked(pg):
    """``FOR UPDATE SKIP LOCKED`` landed in 9.5; the claim path needs it."""
    version = pg["engine"].dialect.server_version_info
    assert version >= (9, 5), (
        f"PostgreSQL {version} predates FOR UPDATE SKIP LOCKED (9.5)")


def test_the_app_runs_at_the_servers_default_isolation_level(pg):
    """The isolation level the reservation invariant actually depends on.

    Nothing in ``app/db.py`` configures one, so the engine takes whatever the
    server defaults to. That is a load-bearing fact, not an accident: the
    invariant proved in ``test_read_committed_...`` below only holds because
    each statement gets a fresh snapshot.
    """
    from sqlalchemy import text

    with pg["Session"]() as s:
        level = s.execute(text("SHOW transaction_isolation")).scalar()
    assert str(level).lower() == "read committed"

    db_py = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text(encoding="utf-8")
    assert "isolation_level" not in db_py, (
        "app/db.py now pins an isolation level; the level asserted above is "
        "no longer the one the application uses")


# ---------------------------------------------------------------------------
# 2. Upgrade from a representative PREVIOUS migration
# ---------------------------------------------------------------------------
#
# The drill, in order, and each step is asserted separately so a vacuous drill
# is impossible:
#
#   a. build the LATEST schema (create_all + every migration)
#   b. REWIND using each migration's OWN ``downgrade()`` back to 0034, and
#      delete those rows from ``schema_migrations``
#   c. assert the schema really IS as-of-0034 (lease columns gone, the 0036
#      and 0037 tables gone) -- this is the anti-vacuity guard
#   d. seed representative data and snapshot it FROM THE DATABASE
#   e. ``run_migrations(..., create_missing_tables=False)``: the remaining
#      migrations must actually run and complete the schema
#   f. assert every seeded row survived, byte for byte
#   g. assert the production boot path (``create_missing_tables=True``) is a
#      no-op afterwards
#
# 0034 is the chosen N because everything after it is load-bearing: 0035 adds
# four columns and two indexes to an existing table, 0036 adds three tables,
# 0037 adds a fourth table that is NOT on ``Base.metadata`` at all. All three
# have a ``downgrade()``, which is what makes (b) possible using shipped code.

_AS_OF = "0034_paid_execution_outcomes"
_REMAINDER = ("0035_job_leases", "0036_gpu_and_storage", "0037_budget_rollups",
              "0038_schema_parity")


def _seed_representative(session) -> dict:
    """Data that exercises every column kind the schema has.

    Written with RAW SQL, not the ORM, and deliberately so: this runs against
    the rewound as-of-0034 schema, where ``jobs`` does not yet have the four
    lease columns an ORM INSERT would emit. Naming the columns explicitly is
    also the honest way to seed "what a deployment at 0034 could have written".

    The snapshot is read back FROM THE DATABASE rather than from the Python
    objects, so a lossy migration (a truncation, a dropped column, a rewritten
    JSON document) cannot be hidden by re-hydrating the same values.
    """
    import json as _json

    from sqlalchemy import text

    from app.models import Workspace

    ws = Workspace(name="W16 Sem", slug=f"w16sem-{os.urandom(4).hex()}", niche="AI money")
    session.add(ws)
    session.commit()

    ids = {"job": _sid(), "cost": _sid(), "event": _sid(), "run": _sid(),
           "pattern": _sid()}
    payload = {
        "hook": "Stop scrolling \U0001F680",
        "arabic": "\u0645\u0631\u062d\u0628\u0627",
        "n": 7,
        "f": 0.30000000000000004,
        "flag": True,
        "nothing": None,
        "nested": {"list": [1, "two", {"three": 3}], "empty": {}},
    }
    # As-of-0034 `jobs`: no claimed_by / claimed_at / lease_expires_at /
    # heartbeat_at. Every NOT NULL column without a server default is named.
    session.execute(text(
        "INSERT INTO jobs (id, workspace_id, type, status, priority, payload, "
        "result, idempotency_key, cancel_requested, next_run_at, max_retries, "
        "retry_count, last_error, created_at, updated_at) VALUES "
        "(:i, :w, 'w16.sem.render', 'QUEUED', 7, CAST(:p AS JSON), '{}', :k, "
        "false, now(), 3, 0, '', now(), now())"),
        {"i": ids["job"], "w": ws.id, "p": _json.dumps(payload),
         "k": f"w16-sem-{_sid()}"})
    session.execute(text(
        "INSERT INTO events (id, workspace_id, level, source, kind, message, "
        "data_json, request_id, created_at, updated_at) VALUES "
        "(:i, :w, 'info', 'system', 'w16.sem', 'seeded', CAST(:d AS JSON), '', "
        "now(), now())"),
        {"i": ids["event"], "w": ws.id,
         "d": _json.dumps({"project_id": "p-1",
                           "target": {"type": "video", "id": "t-1"},
                           "tags": ["a", "b"]})})
    session.execute(text(
        "INSERT INTO cost_entries (id, workspace_id, category, amount_usd, "
        "provider, is_estimate, detail_json, created_at, updated_at) VALUES "
        "(:i, :w, 'video', 12345.678901, 'w16-engine', true, CAST(:d AS JSON), "
        "now(), now())"),
        {"i": ids["cost"], "w": ws.id,
         "d": _json.dumps({"reservation": True, "remote_id": "r-1"})})
    session.execute(text(
        "INSERT INTO agent_runs (id, workspace_id, agent_key, task_type, status, "
        "input_summary, output_summary, duration_ms, cost_usd, error, request_id, "
        "steps_json, created_at, updated_at) VALUES (:i, :w, 'w16.sem', 't', "
        "'COMPLETED', 'in', 'ok', 4321, 0.5, '', '', CAST(:s AS JSON), "
        "now(), now())"),
        {"i": ids["run"], "w": ws.id,
         "s": _json.dumps({"steps": [{"step": 1, "status": "ok"}]})})
    session.execute(text(
        "INSERT INTO learning_patterns (id, workspace_id, pattern_key, "
        "description, observed_improvement_pct, confidence, sample_size, "
        "evidence_json, active, created_at, updated_at) VALUES (:i, :w, 'k', '', "
        "12.5, 'high', 30, CAST(:e AS JSON), true, now(), now())"),
        {"i": ids["pattern"], "w": ws.id, "e": _json.dumps({"n": 30})})
    session.commit()

    snap = dict(session.execute(text(
        "SELECT id, type, status, priority, payload, result, idempotency_key, "
        "cancel_requested, workspace_id FROM jobs WHERE id=:j"),
        {"j": ids["job"]}).mappings().one())
    snap_cost = dict(session.execute(text(
        "SELECT id, category, amount_usd, provider, is_estimate, detail_json "
        "FROM cost_entries WHERE id=:c"), {"c": ids["cost"]}).mappings().one())
    snap_ev = dict(session.execute(text(
        "SELECT id, kind, message, data_json FROM events WHERE id=:e"),
        {"e": ids["event"]}).mappings().one())
    snap_run = dict(session.execute(text(
        "SELECT id, agent_key, duration_ms, cost_usd, steps_json "
        "FROM agent_runs WHERE id=:r"), {"r": ids["run"]}).mappings().one())
    snap_pat = dict(session.execute(text(
        "SELECT id, pattern_key, observed_improvement_pct, confidence, "
        "sample_size, evidence_json, active FROM learning_patterns WHERE id=:p"),
        {"p": ids["pattern"]}).mappings().one())
    return {
        "workspace_id": ws.id, "ids": ids,
        "job": snap, "cost": snap_cost, "event": snap_ev,
        "agent_run": snap_run, "pattern": snap_pat,
    }


@pytest.fixture(scope="module")
def upgrade_drill(scratch) -> dict:
    """Build as-of-0034 with data in it, then upgrade to latest."""
    from sqlalchemy import inspect, text

    from app.migrations.runner import applied_versions, load_migrations, run_migrations

    engine = scratch("upg")
    factory = _migrated(engine)

    # (b) rewind, using the shipped downgrade() of every migration past 0034.
    with factory() as s:
        mods = dict(load_migrations())
        for name in sorted(n for n in mods if n > _AS_OF):
            mods[name].downgrade(s)
            s.execute(text("DELETE FROM schema_migrations WHERE version=:v"), {"v": name})
            s.commit()
        before_applied = applied_versions(s)
        before_job_cols = {c["name"] for c in inspect(s.get_bind()).get_columns("jobs")}
        before_tables = set(inspect(s.get_bind()).get_table_names())

    # (d) seed, then snapshot from the database.
    with factory() as s:
        seeded = _seed_representative(s)

    # (e) upgrade, WITHOUT create_all: the migrations themselves must finish it.
    with factory() as s:
        applied_now = run_migrations(s, create_missing_tables=False)

    # (g) the production boot path, after the fact.
    with factory() as s:
        reapplied = run_migrations(s)

    return {
        "engine": engine, "Session": factory, "seeded": seeded,
        "before_applied": before_applied, "before_job_cols": before_job_cols,
        "before_tables": before_tables, "applied_now": applied_now,
        "reapplied": reapplied,
    }


def test_the_upgrade_drill_starts_from_a_genuinely_previous_schema(upgrade_drill):
    """Anti-vacuity guard. If this passes for the wrong reason, the next three
    tests prove nothing.

    ``downgrade()`` has to have actually removed the things 0035/0036/0037 add,
    and ``schema_migrations`` has to be back at 0034 -- otherwise the "upgrade"
    is a no-op replay and every preservation assertion below is theatre.
    """
    assert not ({"claimed_by", "claimed_at", "lease_expires_at", "heartbeat_at"}
                & upgrade_drill["before_job_cols"]), (
        "0035's downgrade did not remove the lease columns: the upgrade "
        "under test had nothing to add")
    assert "gpu_devices" not in upgrade_drill["before_tables"]
    assert "gpu_reservations" not in upgrade_drill["before_tables"]
    assert "storage_objects" not in upgrade_drill["before_tables"]
    assert "budget_rollup_limits" not in upgrade_drill["before_tables"]

    assert upgrade_drill["before_applied"]
    assert max(upgrade_drill["before_applied"]) == _AS_OF
    assert not (set(_REMAINDER) & upgrade_drill["before_applied"])


def test_the_upgrade_applies_exactly_the_remaining_migrations(upgrade_drill):
    from app.migrations.runner import applied_versions

    assert set(upgrade_drill["applied_now"]) == set(_REMAINDER), (
        f"expected only {_REMAINDER} to be pending, got {upgrade_drill['applied_now']}")
    with upgrade_drill["Session"]() as s:
        assert set(_REMAINDER) <= applied_versions(s)


def test_the_upgrade_completes_the_schema(upgrade_drill):
    """0035's columns and indexes, 0036's tables, 0037's migration-owned table."""
    from sqlalchemy import inspect

    engine = upgrade_drill["engine"]
    insp = inspect(engine)
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    assert {"claimed_by", "claimed_at", "lease_expires_at", "heartbeat_at"} <= job_cols

    job_idx = {i["name"] for i in insp.get_indexes("jobs")}
    assert {"ix_jobs_lease_recovery", "ix_jobs_claimed_by"} <= job_idx

    for table in ("gpu_devices", "gpu_reservations", "storage_objects",
                  "budget_rollup_limits"):
        assert table in insp.get_table_names(), f"{table} is still missing after the upgrade"

    rollup_cols = {c["name"] for c in insp.get_columns("budget_rollup_limits")}
    assert {"scope", "workspace_id", "daily_total_cap", "monthly_total_cap",
            "enabled", "meta_json"} <= rollup_cols
    rollup_idx = {i["name"]: i for i in insp.get_indexes("budget_rollup_limits")}
    assert rollup_idx["ix_budget_rollup_scope"]["unique"] is True


def test_the_production_boot_path_is_idempotent_after_the_upgrade(upgrade_drill):
    """``run_migrations()`` with its default ``create_missing_tables=True`` is
    what the app calls; after a real upgrade it must apply nothing."""
    assert upgrade_drill["reapplied"] == []


def test_the_upgrade_preserves_every_seeded_row(upgrade_drill):
    """The data-preservation assertion, stated per row and per column kind.

    Read back with raw SQL so the comparison is against what PostgreSQL stored,
    not against what the ORM would like to hand back. Covers: a JSON document
    with unicode / floats / booleans / null / nesting, a float money value at
    the ledger's 6-decimal resolution, a boolean, and a JSON list nested inside a
    JSON object.
    """
    from sqlalchemy import text

    seeded = upgrade_drill["seeded"]
    rows = {
        "job": "SELECT id, type, status, priority, payload, result, "
                "idempotency_key, cancel_requested, workspace_id, claimed_by, "
                "claimed_at, lease_expires_at, heartbeat_at FROM jobs WHERE id=:k",
        "cost": "SELECT id, category, amount_usd, provider, is_estimate, "
                 "detail_json FROM cost_entries WHERE id=:k",
        "event": "SELECT id, kind, message, data_json FROM events WHERE id=:k",
        "agent_run": "SELECT id, agent_key, duration_ms, cost_usd, steps_json "
                     "FROM agent_runs WHERE id=:k",
        "pattern": "SELECT id, pattern_key, observed_improvement_pct, confidence, "
                   "sample_size, evidence_json, active "
                   "FROM learning_patterns WHERE id=:k",
    }
    with upgrade_drill["Session"]() as s:
        found = {}
        for name, sql in rows.items():
            key = (seeded["job"] if name == "job" else
                   seeded["cost"] if name == "cost" else
                   seeded["event"] if name == "event" else
                   seeded["agent_run"] if name == "agent_run" else
                   seeded["pattern"])["id"]
            row = s.execute(text(sql), {"k": key}).mappings().one_or_none()
            found[name] = dict(row) if row is not None else None
        ws = s.execute(text("SELECT id, name, slug, niche FROM workspaces "
                            "WHERE id=:w"),
                       {"w": seeded["workspace_id"]}).mappings().one_or_none()

    for name, want in (("job", seeded["job"]), ("cost", seeded["cost"]),
                       ("event", seeded["event"]),
                       ("agent_run", seeded["agent_run"]),
                       ("pattern", seeded["pattern"])):
        assert found[name] is not None, (
            f"the upgrade LOST the {name} row {want['id']}")
        # Compare only the columns that existed BEFORE the upgrade; the four
        # lease columns 0035 added are asserted separately below.
        got = {k: v for k, v in found[name].items() if k in want}
        assert got == want, f"the upgrade changed the {name} row: {got} != {want}"

    assert ws is not None, "the upgrade LOST the workspace row"
    assert ws["id"] == seeded["workspace_id"]
    job, cost = found["job"], found["cost"]
    assert job["payload"]["arabic"] == "\u0645\u0631\u062d\u0628\u0627"
    assert job["payload"]["nothing"] is None
    assert job["payload"]["flag"] is True
    assert job["payload"]["f"] == 0.30000000000000004
    assert job["payload"]["nested"]["empty"] == {}
    assert job["payload"]["nested"]["list"][2]["three"] == 3
    assert cost["amount_usd"] == 12345.678901, "money drifted through the upgrade"
    assert cost["detail_json"] == {"reservation": True, "remote_id": "r-1"}
    assert found["event"]["data_json"]["tags"] == ["a", "b"]
    assert found["agent_run"]["steps_json"] == {"steps": [{"step": 1,
                                                             "status": "ok"}]}

    # 0035's four columns arrive with the values its docstring promises: a
    # pre-existing row gets '' (nobody holds it) and NULL (no renewable lease),
    # which job_leases reads as "recoverable", not "immortal".
    assert job["claimed_by"] == "", (
        "a row that predates 0035 must come out as unclaimed, not as an owner")
    assert job["claimed_at"] is None
    assert job["lease_expires_at"] is None
    assert job["heartbeat_at"] is None


def test_0036_creates_its_indexes_when_create_all_is_skipped(scratch):
    """A deployment whose tables genuinely do not exist yet.

    Not the app's boot path -- that runs ``create_all`` first and commits, so the
    catalog read sees the tables and the indexes are made. This is the path
    ``create_missing_tables=False`` takes, which the repo's own PostgreSQL race
    test uses.

    This was a live defect. ``ddl.py`` read the catalog through
    ``session.get_bind()``, which returns the ENGINE, so ``inspect(engine)``
    opened a *different* pooled connection that could not see the DDL this
    transaction had just issued. 0036 therefore created its three tables and
    then skipped all eight indexes with a warning, while still recording itself
    as applied. It hid on SQLite (DDL autocommits) and hid behind ``create_all``
    (which commits first), so only PostgreSQL plus this path exposed it.
    ``ddl._inspector`` now inspects ``session.connection()``.
    """
    from sqlalchemy import inspect, text

    from app.migrations.runner import run_migrations

    engine = scratch("noidx")
    factory = _migrated(engine)
    with factory() as s:
        for table in ("gpu_devices", "gpu_reservations", "storage_objects"):
            s.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        s.execute(text("DELETE FROM schema_migrations WHERE version='0036_gpu_and_storage'"))
        s.commit()
    with factory() as s:
        assert "0036_gpu_and_storage" in run_migrations(s, create_missing_tables=False)

    insp = inspect(engine)
    assert "gpu_devices" in insp.get_table_names()
    for table, index in (("gpu_devices", "ix_gpu_device_key"),
                         ("gpu_devices", "ix_gpu_device_enabled"),
                         ("gpu_reservations", "ix_gpu_res_device_state"),
                         ("gpu_reservations", "ix_gpu_res_lease"),
                         ("gpu_reservations", "ix_gpu_res_ws"),
                         ("storage_objects", "ix_storage_object_key"),
                         ("storage_objects", "ix_storage_object_state"),
                         ("storage_objects", "ix_storage_object_expiry")):
        names = {i["name"] for i in insp.get_indexes(table)}
        assert index in names, f"{index} was not created on {table}"


# ---------------------------------------------------------------------------
# 3. Indexes, constraints, unique constraints
# ---------------------------------------------------------------------------


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _unique_indexes(engine, table: str) -> set[str]:
    from sqlalchemy import inspect

    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    found = {i["name"] for i in insp.get_indexes(table) if i.get("unique")}
    for uc in insp.get_unique_constraints(table):
        found.add(uc["name"])
    return found


def test_the_uniqueness_the_queue_and_the_ledgers_rely_on_exists(pg):
    """One id per job, one idempotency key, one device, one object, one cap."""
    engine = pg["engine"]
    assert "jobs_idempotency_key_key" in _unique_indexes(engine, "jobs")
    assert "ix_gpu_device_key" in _unique_indexes(engine, "gpu_devices")
    assert "ix_storage_object_key" in _unique_indexes(engine, "storage_objects")
    assert "ix_budget_rollup_scope" in _unique_indexes(engine, "budget_rollup_limits")


def test_the_secondary_indexes_the_hot_paths_query_through_exist(pg):
    from sqlalchemy import inspect

    insp = inspect(pg["engine"])
    jobs = {i["name"] for i in insp.get_indexes("jobs")}
    assert {"ix_jobs_claim", "ix_jobs_lease_recovery", "ix_jobs_claimed_by",
            "ix_jobs_ws_type"} <= jobs
    assert {"ix_jobs_lease_recovery"} <= {i["name"] for i in insp.get_indexes("jobs")}
    costs = {i["name"] for i in insp.get_indexes("cost_entries")}
    assert "ix_cost_ws_day" in costs
    videos = {i["name"] for i in insp.get_indexes("videos")}
    assert {"ix_video_cost_outcome", "ix_video_submission_operation"} <= videos


def test_a_duplicate_primary_key_is_rejected_with_23505(pg):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.models import Workspace

    with pg["Session"]() as s:
        ws = Workspace(name="W16 PK", slug=f"w16pk-{os.urandom(4).hex()}", niche="x")
        s.add(ws)
        s.commit()
        wid = ws.id
        s.rollback()
    try:
        # A second row with the SAME primary key. Every column named, so the
        # refusal is unambiguously about the id and nothing else.
        with pytest.raises(IntegrityError) as caught, pg["Session"]() as s:
            s.execute(text(
                "INSERT INTO workspaces (id, name, slug, niche, brand_voice, "
                "language, timezone, currency, settings_json, created_at, "
                "updated_at) VALUES (:i, 'dupe', :g, 'x', '', 'en', 'UTC', "
                "'USD', '{}', now(), now())"),
                {"i": wid, "g": f"w16pk2-{os.urandom(4).hex()}"})
            s.commit()
        assert _sqlstate(caught.value) == "23505"
    finally:
        with pg["Session"]() as s:
            s.execute(text("DELETE FROM workspaces WHERE id=:i"), {"i": wid})
            s.commit()


def test_a_duplicate_idempotency_key_is_rejected(pg):
    """``jobs.idempotency_key`` is how a retried enqueue is deduplicated."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.models import Workspace

    key = f"w16-sem-idem-{_sid()}"
    insert = text(
        "INSERT INTO jobs (id, type, status, priority, payload, result, "
        "last_error, cancel_requested, claimed_by, idempotency_key, "
        "next_run_at, max_retries, retry_count, created_at, updated_at) "
        "VALUES (gen_random_uuid()::text, 'w16.sem', 'QUEUED', 100, "
        "'{}', '{}', '', false, '', :k, now(), 3, 0, now(), now())")
    with pg["Session"]() as s:
        ws = Workspace(name="W16 Idem", slug=f"w16idem-{os.urandom(4).hex()}", niche="x")
        s.add(ws)
        s.commit()
        wid = ws.id
        s.execute(insert, {"k": key})
        s.commit()
        try:
            with pytest.raises(IntegrityError) as caught:
                s.execute(insert, {"k": key})
                s.commit()
            assert _sqlstate(caught.value) == "23505"
        finally:
            # The refused statement left the transaction ABORTED on
            # PostgreSQL, so this rollback is not optional housekeeping: the
            # next command on this connection is dead until it happens.
            s.rollback()
            s.execute(text("DELETE FROM jobs WHERE idempotency_key=:k"), {"k": key})
            s.execute(text("DELETE FROM workspaces WHERE id=:i"), {"i": wid})
            s.commit()


def test_a_duplicate_gpu_device_key_is_rejected(pg):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    key = f"w16dev-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        s.execute(text(
            "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
            "name, backend, total_mb, reserved_mb, enabled, meta_json) "
            "VALUES (gen_random_uuid()::text, now(), now(), :k, 'd', 'cpu', "
            "1024, 0, true, '{}')"), {"k": key})
        s.commit()
        try:
            with pytest.raises(IntegrityError) as caught:
                s.execute(text(
                    "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
                    "name, backend, total_mb, reserved_mb, enabled, meta_json) "
                    "VALUES (gen_random_uuid()::text, now(), now(), :k, 'd2', 'cpu', "
                    "2048, 0, true, '{}')"), {"k": key})
                s.commit()
            assert _sqlstate(caught.value) == "23505"
        finally:
            s.rollback()
            s.execute(text("DELETE FROM gpu_devices WHERE device_key=:k"), {"k": key})
            s.commit()


def test_a_duplicate_storage_object_key_per_workspace_is_rejected_but_another_workspace_is_not(pg):
    """Idempotency is scoped to the tenant: the same key under a different
    workspace is a different object and must be insertable."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.models import Workspace

    okey = f"w16/obj-{os.urandom(4).hex()}.bin"
    ids: list[str] = []
    with pg["Session"]() as s:
        for i in (1, 2):
            ws = Workspace(name=f"W16 Obj {i}", slug=f"w16obj{i}-{os.urandom(4).hex()}",
                           niche="x")
            s.add(ws)
            s.flush()
            ids.append(ws.id)
        s.commit()

        def add(workspace: str, row_id: str) -> None:
            s.execute(text(
                "INSERT INTO storage_objects (id, created_at, updated_at, "
                "workspace_id, object_key, checksum, size_bytes, content_type, "
                "kind, state, backend, temp_path, ref_type, ref_id, meta_json) "
                "VALUES (:i, now(), now(), :w, :k, '', 0, 'application/octet-stream', "
                "'source', 'PENDING', 'local', '', '', '', '{}')"),
                {"i": row_id, "w": workspace, "k": okey})

        add(ids[0], _sid())
        s.commit()
        add(ids[1], _sid())          # a different tenant: allowed
        s.commit()
        try:
            with pytest.raises(IntegrityError) as caught:
                add(ids[0], _sid())   # same tenant, same key: refused
                s.commit()
            assert _sqlstate(caught.value) == "23505"
        finally:
            s.rollback()
            s.execute(text("DELETE FROM storage_objects WHERE object_key=:k"),
                      {"k": okey})
            s.execute(text("DELETE FROM workspaces WHERE id = ANY(:ids)"),
                      {"ids": ids})
            s.commit()


def test_a_duplicate_budget_rollup_scope_is_rejected(pg):
    """Two rows claiming different ceilings for one workspace would make the
    winner a race rather than a policy."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    wid = f"w16-roll-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        s.execute(text(
            "INSERT INTO budget_rollup_limits (id, created_at, updated_at, scope, "
            "workspace_id, daily_total_cap, monthly_total_cap, enabled, meta_json) "
            "VALUES (:i, now(), now(), 'workspace', :w, 5.0, NULL, true, '{}')"),
            {"i": _sid(), "w": wid})
        s.commit()
        try:
            with pytest.raises(IntegrityError) as caught:
                s.execute(text(
                    "INSERT INTO budget_rollup_limits (id, created_at, updated_at, "
                    "scope, workspace_id, daily_total_cap, monthly_total_cap, "
                    "enabled, meta_json) "
                    "VALUES (:i, now(), now(), 'workspace', :w, 99.0, NULL, true, '{}')"),
                    {"i": _sid(), "w": wid})
                s.commit()
            assert _sqlstate(caught.value) == "23505"
        finally:
            s.rollback()
            s.execute(text("DELETE FROM budget_rollup_limits WHERE workspace_id=:w"),
                      {"w": wid})
            s.commit()


def test_a_refused_duplicate_leaves_no_partial_row_and_the_session_still_works(pg):
    """A rejected unique violation must leave nothing behind AND hand back a
    usable transaction -- the second half is the one SQLite hides, because it
    never aborts the transaction."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    key = f"w16p{os.urandom(8).hex()}"   # gpu_devices.device_key is VARCHAR(40)
    with pg["Session"]() as s:
        s.execute(text(
            "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
            "name, backend, total_mb, reserved_mb, enabled, meta_json) "
            "VALUES (:i, now(), now(), :k, 'first', 'cpu', 1024, 0, true, '{}')"),
            {"i": _sid(), "k": key})
        s.commit()
        try:
            with pytest.raises(IntegrityError):
                # Two rows in ONE statement: the second violates the unique index.
                s.execute(text(
                    "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
                    "name, backend, total_mb, reserved_mb, enabled, meta_json) "
                    "SELECT gen_random_uuid()::text, now(), now(), device_key, name, "
                    "backend, total_mb, reserved_mb, enabled, meta_json "
                    "FROM gpu_devices WHERE device_key = :k"),
                    {"k": key})
                s.commit()
        except IntegrityError:
            pass
        s.rollback()

        count = s.scalar(text("SELECT count(*) FROM gpu_devices WHERE device_key=:k"),
                         {"k": key})
        assert count == 1, "the refused statement left a row behind"

        # And the very next statement on the SAME session works, which is only
        # true because the rollback undid the abort.
        assert s.execute(text("SELECT 1")).scalar() == 1
        s.execute(text("DELETE FROM gpu_devices WHERE device_key=:k"), {"k": key})
        s.commit()


# ---------------------------------------------------------------------------
# 4. Booleans and defaults
# ---------------------------------------------------------------------------


def test_orm_boolean_and_json_columns_are_real_postgres_types(pg):
    """``boolean``, not ``integer``; ``json``, not ``text``.

    The A-F5 canary in ``test_reconciliation_115.py`` proves no migration
    *writes* ``BOOLEAN DEFAULT 1``. This proves the resulting columns are
    actually typed ``boolean`` on a real server, which is the consequence that
    matters.
    """
    from sqlalchemy import text

    want = {
        ("jobs", "cancel_requested"): "boolean",
        ("cost_entries", "is_estimate"): "boolean",
        ("learning_patterns", "active"): "boolean",
        ("jobs", "payload"): "json",
        ("jobs", "result"): "json",
        ("cost_entries", "detail_json"): "json",
        ("events", "data_json"): "json",
    }
    with pg["Session"]() as s:
        rows = s.execute(text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ANY(:tables)"),
            {"tables": sorted({t for t, _c in want})}).all()
    got = {(r[0], r[1]): r[2] for r in rows}
    for key, expected in want.items():
        assert got.get(key) == expected, f"{key[0]}.{key[1]} is {got.get(key)!r}, not {expected!r}"


def test_the_orm_applies_its_python_defaults_on_insert(pg):
    """``default=dict`` / ``default=False`` are SQLAlchemy-side, so they hold
    however the row is created."""
    from app.models import CostEntry, Job, LearningPattern

    with pg["Session"]() as s:
        job = Job(id=_sid(), type="w16.sem", status="QUEUED")
        cost = CostEntry(workspace_id=f"w16-d-{os.urandom(4).hex()}", category="llm",
                         amount_usd=0.25)
        pat = LearningPattern(workspace_id=f"w16-d-{os.urandom(4).hex()}",
                              pattern_key="k")
        s.add_all([job, cost, pat])
        s.commit()
        assert job.cancel_requested is False
        assert job.payload == {}
        assert job.result == {}
        assert job.claimed_by == ""
        assert job.lease_expires_at is None
        assert job.priority == 100 and job.max_retries == 3 and job.retry_count == 0
        assert cost.is_estimate is False
        assert cost.detail_json == {}
        assert pat.active is True


def test_orm_columns_carry_their_declared_server_default_on_postgres(pg):
    """INVERTED by Work 16.1 §1 -- this test used to assert the defect.

    It previously read ``information_schema.columns`` and asserted
    ``defaults["claimed_by"] is None``, then proved a raw INSERT omitting the
    column fails with ``23502``. Its own docstring spelled out the operational
    consequence: "a migration backfill, a ``psql`` insert, a view or a CTE that
    omits ``claimed_by`` fails with 23502 rather than getting ``''``".

    The cause was that ``create_all`` builds columns from the ORM, where
    ``default=`` is a **Python callable** and emits no server default, while
    migration 0035 declared ``claimed_by VARCHAR(80) NOT NULL DEFAULT ''``.
    ``add_column_if_missing`` then saw the column and returned, so the migration
    never reconciled it. Two databases, two schemas, and which one you got
    depended on how it was created.

    Production authority is migrations -> schema. The ORM now declares
    ``server_default=`` so both paths agree, and migration 0038 sets the default
    on databases that already exist. The same raw INSERT below now succeeds.

    The mutation proof: remove ``claimed_by``'s server default and this fails
    with the INSERT's ``23502``.
    """
    from sqlalchemy import text

    with pg["Session"]() as s:
        defaults = dict(s.execute(text(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name='jobs' AND column_name IN "
            "('claimed_by','payload','result','cancel_requested','priority')")).all())
    assert defaults["claimed_by"] is not None, (
        "jobs.claimed_by has no server default again; a raw INSERT, view or "
        "CTE that omits it will fail with 23502 instead of getting ''")
    assert defaults["claimed_by"] in ("''::character varying", "''"), (
        f"unexpected default: {defaults['claimed_by']!r}")

    # The invariant is ORM == migration, not "every column has a default".
    # `payload`/`result`/`cancel_requested`/`priority` never had a declared
    # server default in any of the 37 migrations, so the correct converged state
    # is that they have none on both paths. Asserting a default here would
    # encode a schema change nobody asked for.
    for column in ("payload", "cancel_requested"):
        assert defaults[column] is None, (
            f"jobs.{column} gained a server default the migrations never "
            "declared; ORM and migration have diverged again")

    # The insert that previously raised 23502 must now succeed.
    with pg["Session"]() as s:
        job_id = _sid()
        s.execute(text(
            "INSERT INTO jobs (id, type, status, priority, payload, result, "
            "last_error, cancel_requested, next_run_at, max_retries, retry_count, "
            "created_at, updated_at) "
            "VALUES (:i,'w16.sem','QUEUED',100,'{}','{}','',false, now(),3,0,"
            " now(), now())"), {"i": job_id})
        s.commit()
        stored = s.execute(text(
            "SELECT claimed_by FROM jobs WHERE id=:i"), {"i": job_id}).scalar()
        assert stored == "", (
            f"claimed_by should have taken its declared default, got {stored!r}")
        s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
        s.commit()


def test_the_migration_owned_rollup_table_does_have_server_defaults(pg):
    """0037's table is not on ``Base.metadata``, so its DDL is the only thing
    that creates it -- and its ``TRUE`` / ``'{}'`` defaults must be real
    server-side values that a raw INSERT can lean on."""
    from sqlalchemy import text

    with pg["Session"]() as s:
        rows = dict(s.execute(text(
            "SELECT column_name, (data_type, column_default) "
            "FROM information_schema.columns WHERE table_name='budget_rollup_limits' "
            "AND column_name IN ('enabled','meta_json','scope','workspace_id')")).all())
    assert rows["enabled"] == ("boolean", "true")
    assert rows["meta_json"][0] == "json"
    assert rows["meta_json"][1] is not None and rows["meta_json"][1].endswith("::json")
    assert rows["scope"][1].endswith("workspace'::character varying")
    assert rows["workspace_id"][1].endswith("::character varying")

    wid = f"w16-def-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        s.execute(text(
            "INSERT INTO budget_rollup_limits (id, created_at, updated_at, "
            "scope, workspace_id) VALUES (:i, now(), now(), 'workspace', :w)"),
            {"i": _sid(), "w": wid})
        s.commit()
        row = s.execute(text(
            "SELECT enabled, meta_json, daily_total_cap, monthly_total_cap "
            "FROM budget_rollup_limits WHERE workspace_id=:w"), {"w": wid}).one()
        assert row[0] is True, "DEFAULT TRUE did not apply"
        assert row[1] == {}, "DEFAULT '{}' did not apply"
        assert row[2] is None, "an unset cap must stay NULL (NOT CONFIGURED)"
        assert row[3] is None
        s.execute(text("DELETE FROM budget_rollup_limits WHERE workspace_id=:w"),
                  {"w": wid})
        s.commit()


def test_an_integer_default_on_a_boolean_column_is_refused_by_this_server(pg):
    """The live form of the A-F5 regex canary.

    ``test_reconciliation_115.py:55`` proves the *sources* no longer say
    ``BOOLEAN DEFAULT 0``. This proves why that matters, on this server: an
    integer default on a boolean is refused outright, and -- because the
    statement failed inside a transaction -- every later command on that
    transaction dies with 25P02 until it is rolled back. That is precisely the
    trap ``app/migrations/ddl.py`` was written to avoid.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with pg["Session"]() as s:
        with pytest.raises(DBAPIError) as caught:
            s.execute(text("ALTER TABLE budget_rollup_limits "
                           "ALTER COLUMN enabled SET DEFAULT 1"))
            s.commit()
        state = _sqlstate(caught.value)
        assert state == "42804", f"expected 42804 datatype_mismatch, got {state}"

        # The failed statement poisoned the transaction...
        with pytest.raises(DBAPIError) as aborted:
            s.execute(text("SELECT 1"))
        assert _sqlstate(aborted.value) == "25P02"

        # ...and the rollback is what makes the session usable again.
        s.rollback()
        assert s.execute(text("SELECT 1")).scalar() == 1

        # The correct spelling is accepted, and is recorded as a real boolean.
        s.execute(text("ALTER TABLE budget_rollup_limits "
                       "ALTER COLUMN enabled SET DEFAULT TRUE"))
        s.commit()
        recorded = s.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='budget_rollup_limits' "
            "AND column_name='enabled'")).scalar()
        assert recorded == "true"


def test_money_survives_postgres_double_precision_at_the_ledgers_resolution(pg):
    """``CostEntry.amount_usd`` is ``FLOAT``, so the ledger's money is a double.
    At ``round(x, 6)`` -- the resolution ``reserve_spend`` writes -- every
    representative value must come back exactly."""
    from sqlalchemy import select, text

    from app.models import CostEntry

    values = [0.1, 0.2, 1 / 3, 12345.678901, 0.000001, 99.999999]
    ws = f"w16-money-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        ids = []
        for _i, v in enumerate(values):
            row = CostEntry(workspace_id=ws, category="llm", amount_usd=round(v, 6),
                            provider="p")
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
        read_back = [s.get(CostEntry, i).amount_usd for i in ids]
        assert read_back == [round(v, 6) for v in values], (
            f"float money drifted: {[round(v, 6) for v in values]} -> {read_back}")
        total = s.scalar(select(__import__("sqlalchemy").func.sum(CostEntry.amount_usd))
                         .where(CostEntry.workspace_id == ws))
        assert abs(float(total) - sum(round(v, 6) for v in values)) < 1e-6
        s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"), {"w": ws})
        s.commit()


# ---------------------------------------------------------------------------
# 5. Transactions and rollback
# ---------------------------------------------------------------------------


def test_commit_publishes_and_rollback_discards(pg):
    from sqlalchemy import text

    key = f"w16-tx-{_sid()}"
    with pg["Session"]() as s:
        s.execute(text(
            "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
            "name, backend, total_mb, reserved_mb, enabled, meta_json) "
            "VALUES (:i, now(), now(), :k, 'a', 'cpu', 1, 0, true, '{}')"),
            {"i": _sid(), "k": key})
        s.rollback()
        assert s.scalar(text("SELECT count(*) FROM gpu_devices WHERE device_key=:k"),
                        {"k": key}) == 0

        s.execute(text(
            "INSERT INTO gpu_devices (id, created_at, updated_at, device_key, "
            "name, backend, total_mb, reserved_mb, enabled, meta_json) "
            "VALUES (:i, now(), now(), :k, 'b', 'cpu', 1, 0, true, '{}')"),
            {"i": _sid(), "k": key})
        s.commit()
    with pg["Session"]() as s:
        assert s.scalar(text("SELECT count(*) FROM gpu_devices WHERE device_key=:k"),
                        {"k": key}) == 1
        s.execute(text("DELETE FROM gpu_devices WHERE device_key=:k"), {"k": key})
        s.commit()


def test_rollback_inside_a_real_transaction_leaves_nothing_behind(pg):
    """The ledger invariant: a refused reservation writes no row.

    ``_reservation_session`` in ``services/cost.py`` rolls back on refusal, so
    the next attempt must not see a spend that never happened. Modelled with the
    same shape: several writes inside one transaction, then a failure.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    ws = f"w16-rb-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        for _i in range(3):
            s.execute(text(
                "INSERT INTO cost_entries (id, created_at, updated_at, workspace_id, "
                "category, amount_usd, provider, detail_json, is_estimate) "
                "VALUES (:i, now(), now(), :w, 'llm', 1.0, 'p', :d, true)"),
                {"i": _sid(), "w": ws, "d": '{"reservation": true}'})
        try:
            # A PRIMARY KEY violation on the LAST statement of the transaction.
            s.execute(text(
                "INSERT INTO cost_entries (id, created_at, updated_at, workspace_id, "
                "category, amount_usd, provider, detail_json, is_estimate) "
                "VALUES (:i, now(), now(), :w, 'llm', 1.0, 'p', '{}', true)"),
                {"i": "w16-rb-dup", "w": ws})
            with pytest.raises(DBAPIError):
                s.execute(text(
                    "INSERT INTO cost_entries (id, created_at, updated_at, "
                    "workspace_id, category, amount_usd, provider, detail_json, "
                    "is_estimate) VALUES (:i, now(), now(), :w, 'llm', 1.0, 'p', "
                    "'{}', true)"), {"i": "w16-rb-dup", "w": ws})
                s.commit()
        finally:
            s.rollback()

        # Nothing from the aborted transaction survived -- not even the first
        # three rows, which had succeeded on their own.
        assert s.scalar(text("SELECT count(*) FROM cost_entries WHERE workspace_id=:w"),
                        {"w": ws}) == 0
        # The connection is reusable, which is the half SQLite never gives you.
        assert s.execute(text("SELECT 1")).scalar() == 1
        s.execute(text("INSERT INTO cost_entries (id, created_at, updated_at, "
                       "workspace_id, category, amount_usd, provider, detail_json, "
                       "is_estimate) VALUES (:i, now(), now(), :w, 'llm', 2.0, 'p', "
                       "'{}', true)"), {"i": _sid(), "w": ws})
        s.commit()
        assert s.scalar(text("SELECT count(*) FROM cost_entries WHERE workspace_id=:w"),
                        {"w": ws}) == 1
        s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"), {"w": ws})
        s.commit()


def test_ddl_is_transactional_on_postgres(pg):
    """``CREATE TABLE`` inside a transaction that rolls back leaves no table.

    This is what makes the runner's per-migration transaction meaningful: a
    failed migration must not leave half its tables behind.
    """
    from sqlalchemy import inspect, text

    name = "w16_sem_ddl_probe"
    with pg["Session"]() as s:
        s.execute(text(f'CREATE TABLE "{name}" (id VARCHAR(36) PRIMARY KEY)'))
        # NOTE the inspector is bound to the SESSION's connection, not the
        # engine: `inspect(engine)` opens a different pooled connection that
        # cannot see this transaction's uncommitted DDL. That is the same trap
        # migrations 0035/0036/0037 each had to work around.
        assert name in inspect(s.connection()).get_table_names()
        s.rollback()
    assert name not in inspect(pg["engine"]).get_table_names()
    with pg["Session"]() as s:
        assert s.execute(text("SELECT 1")).scalar() == 1


# ---------------------------------------------------------------------------
# 6. Locking
# ---------------------------------------------------------------------------


def _seed_lock_rows(pg, count: int = 3) -> list[str]:
    """Three claimable rows, returned in the ORDER BY id order B will use."""
    from sqlalchemy import text

    ids = sorted(_sid() for _ in range(count))
    with pg["Session"]() as s:
        for job_id in ids:
            s.execute(text(
                "INSERT INTO jobs (id, type, status, priority, payload, result, "
                "last_error, cancel_requested, claimed_by, next_run_at, max_retries, "
                "retry_count, created_at, updated_at) "
                "VALUES (:i,'w16.sem.lock','QUEUED',100,'{}','{}','',false,'',"
                " now(),3,0, now(), now())"), {"i": job_id})
        s.commit()
    return ids


def _drop_lock_rows(pg, ids: list[str]) -> None:
    from sqlalchemy import text

    with pg["Session"]() as s:
        for job_id in ids:
            s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
        s.commit()


def test_skip_locked_exists_on_this_server_and_really_skips(pg):
    """What ``job_leases._for_update`` asks for, proved against a live lock.

    Session A holds ``FOR UPDATE`` on the FIRST row and does NOT commit.
    Session B asks for the same set with ``SKIP LOCKED``: it must return the
    other rows immediately, and must not return A's.
    """
    from sqlalchemy import text

    ids = _seed_lock_rows(pg)
    try:
        with pg["Session"]() as a:
            a.execute(text("SET LOCAL statement_timeout = '10s'"))
            # Exactly one row, so B has something to skip TO.
            a.execute(text("SELECT id FROM jobs WHERE id = :i FOR UPDATE"),
                      {"i": ids[0]}).scalars().all()

            started = time.monotonic()
            with pg["Session"]() as b:
                b.execute(text("SET LOCAL statement_timeout = '5s'"))
                got = b.execute(text(
                    "SELECT id FROM jobs WHERE id = ANY(:ids) ORDER BY id "
                    "FOR UPDATE SKIP LOCKED"), {"ids": ids}).scalars().all()
                elapsed = time.monotonic() - started
                b.rollback()

            assert ids[0] not in got, "SKIP LOCKED returned the locked row"
            assert got == ids[1:], f"expected the other rows, got {got}"
            assert elapsed < 4.0, (
                f"SKIP LOCKED waited {elapsed:.1f}s; it should skip, not queue")
            a.rollback()
    finally:
        _drop_lock_rows(pg, ids)


def test_the_same_select_without_skip_locked_really_blocks(pg):
    """The control. Without ``SKIP LOCKED`` the identical statement must WAIT,
    which is what proves the previous test skipped rather than simply not
    locking anything. ``lock_timeout`` converts the wait into a refusal rather
    than a hung test."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    ids = _seed_lock_rows(pg)
    try:
        with pg["Session"]() as a:
            a.execute(text("SELECT id FROM jobs WHERE id = ANY(:ids) ORDER BY id "
                           "FOR UPDATE"), {"ids": ids}).scalars().all()
            with pg["Session"]() as b:
                b.execute(text("SET LOCAL lock_timeout = '750ms'"))
                started = time.monotonic()
                try:
                    with pytest.raises(DBAPIError) as caught:
                        b.execute(text("SELECT id FROM jobs WHERE id = ANY(:ids) "
                                       "ORDER BY id FOR UPDATE"), {"ids": ids})
                    assert _sqlstate(caught.value) == "55P03", (
                        f"expected a lock timeout, got {_sqlstate(caught.value)}")
                    assert time.monotonic() - started >= 0.5
                finally:
                    b.rollback()
            a.rollback()
    finally:
        _drop_lock_rows(pg, ids)


def test_the_claim_path_asks_for_skip_locked_on_postgres(pg):
    """The app's own claim query, inspected rather than reimplemented."""
    from sqlalchemy import select

    from app.models import Job
    from app.services import job_leases

    with pg["Session"]() as s:
        stmt = job_leases._for_update(select(Job).order_by(Job.priority), s)
        rendered = str(stmt.compile(dialect=pg["engine"].dialect))
    assert "SKIP LOCKED" in rendered, (
        "job_leases no longer requests SKIP LOCKED on PostgreSQL; the claim "
        f"path will serialise on one row's lock. Compiled: {rendered}")


def test_exclusive_workspace_lock_serialises_two_real_sessions(pg):
    """``services/cost.exclusive_workspace_lock`` on a real PostgreSQL session.

    Two independent connections, each its own transaction. A takes the lock and
    holds it; B must not get in until A's transaction ends. That is the whole
    basis of ``reserve_spend``, so it is measured rather than assumed.
    """
    from app.services import cost

    ws = f"w16-lock-{os.urandom(4).hex()}"
    inside = threading.Event()
    timings: dict[str, float] = {}
    hold_seconds = 1.5

    with pg["Session"]() as setup:
        ws = _workspace(setup, "W16 Lock")

    def holder() -> None:
        from sqlalchemy import text

        with pg["Session"]() as s:
            with cost.exclusive_workspace_lock(s, ws):
                inside.set()
                # A TIMER, not a signal: the holder must end on its own so the
                # test cannot deadlock waiting for a waiter that is blocked on it.
                time.sleep(hold_seconds)
                s.execute(text(
                    "INSERT INTO cost_entries (id, created_at, updated_at, "
                    "workspace_id, category, amount_usd, provider, detail_json, "
                    "is_estimate) VALUES (:i, now(), now(), :w, 'llm', 7.0, 'p', "
                    "'{}', true)"), {"i": _sid(), "w": ws})
            s.commit()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert inside.wait(timeout=20), "the holder never entered the lock"
    try:
        from sqlalchemy import text

        with pg["Session"]() as b:
            b.execute(text("SET LOCAL lock_timeout = '30s'"))
            pre = b.scalar(text("SELECT coalesce(sum(amount_usd),0) "
                                "FROM cost_entries WHERE workspace_id=:w"), {"w": ws})
            assert float(pre) == 0.0, "the holder's row is not committed yet"
            started = time.monotonic()
            with cost.exclusive_workspace_lock(b, ws):
                elapsed = time.monotonic() - started
                post = b.scalar(text("SELECT coalesce(sum(amount_usd),0) "
                                     "FROM cost_entries WHERE workspace_id=:w"),
                                {"w": ws})
            timings["wait"] = elapsed
            timings["post"] = float(post)
            b.rollback()
    finally:
        thread.join(timeout=30)

    assert timings["wait"] >= hold_seconds * 0.5, (
        f"B entered the workspace lock after only {timings['wait']:.3f}s: the "
        "lock did not serialise the two sessions")
    assert timings["post"] == 7.0, (
        "B re-read the total INSIDE the lock and did not see the winner's "
        "committed row -- the reservation invariant would be broken")


def test_read_committed_gives_a_fresh_snapshot_inside_the_lock(pg):
    """Why READ COMMITTED is the level the reservation invariant needs.

    A session reads the total, then a competing transaction commits a spend,
    then the session takes the workspace lock and reads again. Under READ
    COMMITTED the second read sees the winner -- because each statement takes a
    new snapshot. That is exactly the order ``reserve_spend`` performs.
    """
    from sqlalchemy import text

    from app.services import cost

    with pg["Session"]() as setup:
        ws = _workspace(setup, "W16 Iso")

    try:
        with pg["Session"]() as b:
            pre = float(b.scalar(text(
                "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
                "WHERE workspace_id=:w"), {"w": ws}))
            assert pre == 0.0

            # The competitor commits while B's transaction is already open.
            with pg["Session"]() as a:
                a.execute(text(
                    "INSERT INTO cost_entries (id, created_at, updated_at, "
                    "workspace_id, category, amount_usd, provider, detail_json, "
                    "is_estimate) VALUES (:i, now(), now(), :w, 'llm', 3.5, 'p', "
                    "'{}', true)"), {"i": _sid(), "w": ws})
                a.commit()

            with cost.exclusive_workspace_lock(b, ws):
                post = float(b.scalar(text(
                    "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
                    "WHERE workspace_id=:w"), {"w": ws}))
            b.rollback()
        assert post == 3.5, (
            f"the read inside the lock still saw the pre-lock snapshot ({post}); "
            "READ COMMITTED is not in force and the invariant is unsafe")
    finally:
        with pg["Session"]() as s:
            s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"), {"w": ws})
            s.execute(text("DELETE FROM workspaces WHERE id=:i"), {"i": ws})
            s.commit()


def test_repeatable_read_would_freeze_the_pre_lock_read(pg):
    """The flip side, recorded so nobody raises the isolation level casually.

    Same script under REPEATABLE READ: the second read inside the lock still
    returns the OLD total. The reservation would then be authorised against a
    stale number, and two workspaces' caps could both be spent. This is why
    ``app/db.py`` pins nothing and the server default is load-bearing.
    """
    from sqlalchemy import text

    from app.services import cost

    with pg["Session"]() as setup:
        ws = _workspace(setup, "W16 RR")
    try:
        with pg["Session"]() as b:
            b.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            pre = float(b.scalar(text(
                "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
                "WHERE workspace_id=:w"), {"w": ws}))
            with pg["Session"]() as a:
                a.execute(text(
                    "INSERT INTO cost_entries (id, created_at, updated_at, "
                    "workspace_id, category, amount_usd, provider, detail_json, "
                    "is_estimate) VALUES (:i, now(), now(), :w, 'llm', 3.5, 'p', "
                    "'{}', true)"), {"i": _sid(), "w": ws})
                a.commit()
            with cost.exclusive_workspace_lock(b, ws):
                post = float(b.scalar(text(
                    "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
                    "WHERE workspace_id=:w"), {"w": ws}))
            b.rollback()
        assert post == pre == 0.0, (
            f"REPEATABLE READ did not freeze the read ({pre} -> {post}); this "
            "test only means something while the snapshot really is stale")
    finally:
        with pg["Session"]() as s:
            s.execute(text("DELETE FROM cost_entries WHERE workspace_id=:w"), {"w": ws})
            s.execute(text("DELETE FROM workspaces WHERE id=:i"), {"i": ws})
            s.commit()


# ---------------------------------------------------------------------------
# 7. JSON / JSONB portability
# ---------------------------------------------------------------------------


def test_json_columns_are_native_json_on_postgres_not_text(pg):
    """The mapping the whole JSON section rests on: ``json`` here, TEXT there."""
    from sqlalchemy import text

    with pg["Session"]() as s:
        types = dict(s.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='jobs' AND column_name IN ('payload','result')")).all())
    assert types == {"payload": "json", "result": "json"}


def test_a_json_payload_round_trips_unchanged_through_native_json(pg):
    """Unicode, float precision, booleans, nulls, nesting, empty containers."""
    from app.models import Job

    payload = {
        "emoji": "\U0001F680",
        "arabic": "\u0645\u0631\u062d\u0628\u0627",
        "n": 7,
        "f": 0.30000000000000004,
        "flag": True,
        "nothing": None,
        "deep": {"list": [1, "two", {"three": 3}], "empty": {}},
    }
    job_id = _sid()
    with pg["Session"]() as s:
        s.add(Job(id=job_id, type="w16.sem", status="QUEUED", payload=payload))
        s.commit()
        s.expire_all()
        assert s.get(Job, job_id).payload == payload
        # And through raw SQL, which parses rather than returning a string.
        from sqlalchemy import text

        raw = s.execute(text("SELECT payload FROM jobs WHERE id=:i"),
                        {"i": job_id}).scalar()
        assert raw == payload
        assert isinstance(raw, dict)
        assert raw["f"] == 0.30000000000000004
        assert raw["nothing"] is None
        assert raw["deep"]["empty"] == {}
        s.delete(s.get(Job, job_id))
        s.commit()


def test_a_raw_sql_read_of_a_json_column_returns_an_object_on_postgres_but_text_on_sqlite(
        tmp_path, pg):
    """The portability trap in one line.

    A ``json`` column read with ``text()`` is PARSED on PostgreSQL and returned
    as a raw string on SQLite, so ``row["detail_json"]["k"]`` works on one
    backend and raises ``TypeError`` on the other. Any raw-SQL report, admin
    script or migration that reaches for a JSON column has to know this.
    """
    import json as _json

    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from app.models import EventLog

    job_id = _sid()
    with pg["Session"]() as s:
        s.add(EventLog(id=job_id, workspace_id="w16.sem", level="info",
                       source="system", kind="w16.sem", message="m",
                       data_json={"k": "v"}))
        s.commit()
        pg_value = s.execute(text("SELECT data_json FROM events WHERE id=:i"),
                             {"i": job_id}).scalar()
        s.execute(text("DELETE FROM events WHERE id=:i"), {"i": job_id})
        s.commit()
    assert isinstance(pg_value, dict)

    lite = create_sqlite(tmp_path / "json.db")
    lite_session = sessionmaker(bind=lite)()
    try:
        lite_session.add(EventLog(id=job_id, workspace_id="w16.sem", level="info",
                                  source="system", kind="w16.sem", message="m",
                                  data_json={"k": "v"}))
        lite_session.commit()
        lite_value = lite_session.execute(
            text("SELECT data_json FROM events WHERE id=:i"), {"i": job_id}).scalar()
    finally:
        lite_session.close()
        lite.dispose()
    assert isinstance(lite_value, str), (
        "SQLite returned a parsed object; the two backends no longer differ")
    assert _json.loads(lite_value) == pg_value


def test_the_json_path_index_compiles_to_two_different_operators(pg):
    """One ORM expression, two SQL texts. This is the portability finding.

    ``.as_string()`` CASTs to VARCHAR on PostgreSQL (``data_json ->> 'k'``) but
    NOT on SQLite (``JSON_EXTRACT(data_json, '$.k')``, which keeps JSON's own
    type). Both are asserted so a SQLAlchemy upgrade that changes either is a
    visible test failure rather than a silent behaviour change.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models import EventLog

    flat = EventLog.data_json["project_id"].as_string() == "x"
    nested = EventLog.data_json["target"]["id"].as_string() == "y"

    pg_flat = str(flat.compile(dialect=postgresql.dialect()))
    lite_flat = str(flat.compile(dialect=sqlite.dialect()))
    pg_nested = str(nested.compile(dialect=postgresql.dialect()))
    lite_nested = str(nested.compile(dialect=sqlite.dialect()))

    assert "->>" in pg_flat and "CAST" in pg_flat
    assert "JSON_EXTRACT" in lite_flat and "CAST" not in lite_flat
    assert "->" in pg_nested
    assert "JSON_EXTRACT(JSON_QUOTE" in lite_nested
    assert pg_flat != lite_flat


def _event_payloads() -> list[tuple[str, dict]]:
    """Four ledgers rows, two of which hold the SAME id under different JSON
    types. The numeric/string pair is the whole portability question."""
    return [
        ("numeric", {"project_id": 42, "target": {"type": "video", "id": 42}}),
        ("string", {"project_id": "42", "target": {"type": "video", "id": "42"}}),
        ("other", {"project_id": "7", "target": {"type": "image", "id": "7"}}),
        ("empty", {}),
    ]


def _seed_events(session, workspace_id: str) -> None:
    from app.models import EventLog

    for name, blob in _event_payloads():
        session.add(EventLog(id=_sid(), workspace_id=workspace_id, level="info",
                             source="system", kind="w16.sem", message=name,
                             data_json=blob))
    session.commit()


def _event_names(rows: list[dict]) -> list[str]:
    return sorted(r["message"] for r in rows)


def test_activity_query_json_path_filters_agree_on_both_backends_for_string_ids(
        tmp_path, pg):
    """``services/activity.query`` claims its structured filters are portable
    on SQLite + PostgreSQL. For the string ids it actually stores, they are."""
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from app.services import activity

    with pg["Session"]() as s:
        seeded = {"workspace_id": _workspace(s, "W16 Act")}
        _seed_events(s, seeded["workspace_id"])

    def ask(session, **kwargs) -> list[str]:
        return _event_names(activity.query(session, seeded["workspace_id"], **kwargs))

    try:
        with pg["Session"]() as s:
            pg_project = ask(s, project_id="7")
            pg_target = ask(s, target_type="image", target_id="7")
            pg_kind = ask(s, kind="w16.sem")
        assert pg_project == ["other"]
        assert pg_target == ["other"]
        assert pg_kind == sorted(n for n, _b in _event_payloads())

        lite = create_sqlite(tmp_path / "act.db")
        lite_session = sessionmaker(bind=lite)()
        try:
            _seed_events(lite_session, seeded["workspace_id"])
            assert ask(lite_session, project_id="7") == pg_project
            assert ask(lite_session, target_type="image", target_id="7") == pg_target
            assert ask(lite_session, kind="w16.sem") == pg_kind
        finally:
            lite_session.close()
            lite.dispose()
    finally:
        with pg["Session"]() as s:
            s.execute(text("DELETE FROM events WHERE workspace_id=:w"),
                      {"w": seeded["workspace_id"]})
            s.commit()


def test_activity_query_json_path_filters_PARITY_when_the_stored_id_is_numeric(pg):
    """The portability finding, now CLOSED.

    This test used to be named ``..._DIVERGE_...`` and asserted the opposite of
    what it asserts now, because the divergence was real:

    ``services/activity.py`` filtered with
    ``EventLog.data_json["project_id"].as_string() == str(project_id)``. On
    PostgreSQL the CAST stringifies a JSON number, so a numeric id matched. On
    SQLite ``JSON_EXTRACT`` keeps JSON's own type, making the comparison
    ``42 = '42'``, which SQLite answers False -- the row was invisible. Same
    call, same data, same argument: one backend found the row, the other did
    not.

    It was latent only because every current writer stores these as strings, so
    nothing looked broken. That is the worst kind of defect: it is invisible
    until a writer changes, and then it is a support ticket about missing data.

    Work 16.1 §2 replaced the three ``as_string()`` filters with
    ``json_value_equals`` (see ``services/json_portability.py``), which casts
    explicitly on both backends. The finding is now asserted as **parity**, and
    the anti-vacuity guard below means it can never pass by both sides returning
    nothing.
    """
    from sqlalchemy import text

    from app.services import activity

    with pg["Session"]() as s:
        seeded = {"workspace_id": _workspace(s, "W16 Div")}
        _seed_events(s, seeded["workspace_id"])
        s.expire_all()
        pg_hits = _event_names(activity.query(s, seeded["workspace_id"],
                                              project_id="42"))
    assert pg_hits == ["numeric", "string"], (
        "PostgreSQL should match both the numeric and the string payload")

    with tempfile_sqlite() as (lite, lite_session):
        _seed_events(lite_session, seeded["workspace_id"])
        lite_hits = _event_names(activity.query(lite_session,
                                                 seeded["workspace_id"],
                                                 project_id="42"))
    assert lite_hits == ["numeric", "string"], (
        f"SQLite returned {lite_hits}; it must match PostgreSQL exactly. "
        "The numeric JSON payload is invisible again.")
    assert set(lite_hits) == set(pg_hits) and lite_hits, (
        "the two backends must agree, and the comparison must not be vacuous "
        "-- both sides returning nothing would satisfy a naive equality check")

    with pg["Session"]() as s:
        s.execute(text("DELETE FROM events WHERE workspace_id=:w"),
                  {"w": seeded["workspace_id"]})
        s.commit()


def test_paid_provider_json_filter_is_identical_on_both_backends(tmp_path, pg):
    """The one place the codebase uses ``as_string()`` inside a recovery path,
    and the shape most likely to break: ``(... == :op).is_(True)``.

    ``services/paid_provider.py:676-677``. The ``IS TRUE`` wrapper is written
    for SQLite's 0/1 integers; on PostgreSQL the comparison is already a real
    boolean. Both must return the same row.
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import CostEntry

    ws = f"w16-paid-{os.urandom(4).hex()}"
    detail = {"operation": "video_render_submit", "remote_id": "r-1"}
    ids = []
    with pg["Session"]() as s:
        for i in range(3):
            row = CostEntry(workspace_id=ws, category="video", amount_usd=0.2,
                            provider="w16-engine",
                            detail_json=detail if i == 0 else {"note": "x"})
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
        hit = _reattach_candidates(s)
        miss = _reattach_candidates(s, operation="no_such_operation")
    assert hit == [ids[0]]
    assert miss == []

    lite = create_sqlite(tmp_path / "paid.db")
    lite_session = sessionmaker(bind=lite)()
    try:
        lite_session.add(CostEntry(id=ids[0], workspace_id=ws, category="video",
                                   amount_usd=0.2, provider="w16-engine",
                                   detail_json=detail))
        lite_session.add(CostEntry(id=ids[1], workspace_id=ws, category="video",
                                   amount_usd=0.2, provider="w16-engine",
                                   detail_json={"note": "x"}))
        lite_session.commit()
        assert _reattach_candidates(lite_session) == [ids[0]]
        assert _reattach_candidates(lite_session, operation="no_such_operation") == []
    finally:
        lite_session.close()
        lite.dispose()


def _reattach_candidates(session, operation: str = "video_render_submit") -> list[str]:
    """``paid_provider``'s exact predicate, extracted so both backends run it."""
    from sqlalchemy import select

    from app.models import CostEntry

    return list(session.scalars(
        select(CostEntry.id).where(
            (CostEntry.detail_json["operation"].as_string() == operation).is_(True))
    ).all())


def test_contains_on_a_json_column_is_a_hard_error_on_postgres(tmp_path, pg):
    """Latent landmine: ``JSON.contains()`` compiles to a LIKE.

    On SQLite the column is TEXT, so ``LIKE '%' || :p || '%'`` works (and is a
    substring match on the serialised document, which is wrong for containment
    but at least runs). On PostgreSQL ``json`` has no ``~~`` operator, so the
    identical expression raises 42883 and aborts the transaction. Nothing in
    the repository calls it today -- this is pinned so the first person who
    does finds it here.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError

    from app.models import EventLog

    with pg["Session"]() as s:
        stmt = select(EventLog.id).where(EventLog.data_json.contains({"a": 1}))
        rendered = str(stmt.compile(dialect=pg["engine"].dialect))
        assert "LIKE" in rendered, rendered
        with pytest.raises(DBAPIError) as caught:
            s.execute(stmt).all()
        assert _sqlstate(caught.value) == "42883", (
            f"expected 'operator does not exist: json ~~ text', got "
            f"{_sqlstate(caught.value)}: {rendered}")
        s.rollback()


def test_comparing_a_json_column_to_a_text_literal_also_fails_on_postgres(pg):
    """The other landmine: ``Job.payload == '{"a": 1}'``.

    SQLAlchemy only casts a ``dict``/``list`` to JSON for ``==``; a ``str``
    goes out as a bare literal. PostgreSQL has no ``json = unknown`` operator
    (42883); SQLite compares TEXT and quietly matches nothing. So a filter that
    is merely useless on SQLite raises -- and poisons the transaction -- on the
    production database. Nothing in the repo does this today.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError

    from app.models import EventLog

    with pg["Session"]() as s:
        stmt = select(EventLog.id).where(EventLog.data_json == '{"project_id": "x"}')
        with pytest.raises(DBAPIError) as caught:
            s.execute(stmt).all()
        assert _sqlstate(caught.value) == "42883", _sqlstate(caught.value)
        s.rollback()


def test_writing_a_non_json_document_into_a_json_column_is_refused_on_postgres(pg):
    """The write-side twin. On SQLite any TEXT is storable; on PostgreSQL a
    malformed document is 22P02 and the transaction is dead until rollback."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    key = f"w16-badjson-{os.urandom(4).hex()}"
    with pg["Session"]() as s:
        from app.models import EventLog

        s.add(EventLog(id=_sid(), workspace_id=key, level="info", source="system",
                       kind="w16.sem", message="ok", data_json={"a": 1}))
        s.commit()
        eid = s.execute(text("SELECT id FROM events WHERE workspace_id=:w "
                             "ORDER BY created_at LIMIT 1"), {"w": key}).scalar()
        with pytest.raises(DBAPIError) as caught:
            s.execute(text("UPDATE events SET data_json = :v WHERE id=:i"),
                      {"v": "not json at all", "i": eid})
            s.commit()
        assert _sqlstate(caught.value) == "22P02", _sqlstate(caught.value)
        s.rollback()
        s.execute(text("DELETE FROM events WHERE workspace_id=:w"), {"w": key})
        s.commit()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@contextmanager
def tempfile_sqlite() -> Iterator[tuple]:
    """A throwaway SQLite file with the full schema, for portability compares."""
    import tempfile

    from sqlalchemy.orm import sessionmaker

    directory = Path(tempfile.mkdtemp(prefix="w16-sem-lite-"))
    engine = create_sqlite(directory / "probe.db")
    session = sessionmaker(bind=engine)()
    try:
        yield engine, session
    finally:
        session.close()
        engine.dispose()


def test_unicode_survives_the_json_column_on_postgres(pg):
    """Kept separate from the round-trip test so a failure names the cause."""
    from app.models import EventLog

    with pg["Session"]() as s:
        eid = _sid()
        s.add(EventLog(id=eid, workspace_id="w16.sem", level="info", source="system",
                       kind="w16.sem", message="\U0001F680 \u0645\u0631\u062d\u0628\u0627",
                       data_json={"emoji": "\U0001F680",
                                  "arabic": "\u0645\u0631\u062d\u0628\u0627",
                                  "cjk": "\u4f60\u597d"}))
        s.commit()
        s.expire_all()
        row = s.get(EventLog, eid)
        assert row.message == "\U0001F680 \u0645\u0631\u062d\u0628\u0627"
        assert row.data_json == {"emoji": "\U0001F680",
                                 "arabic": "\u0645\u0631\u062d\u0628\u0627",
                                 "cjk": "\u4f60\u597d"}
        s.delete(row)
        s.commit()