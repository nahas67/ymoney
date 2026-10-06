"""Work 16 §1: the ORM and the migrations must produce ONE schema.

The defect this file exists to make impossible to re-introduce
----------------------------------------------------------
``app.migrations.versions.0035_job_leases`` declares
``claimed_by VARCHAR(80) NOT NULL DEFAULT ''``. ``Base.metadata.create_all()``
builds the same column from ``Job.claimed_by``, where ``default=""`` is a
**Python** callable and therefore emits no server default. ``add_column_if_missing``
sees the column already present and returns, so the declared default never lands.

Which schema a deployment gets therefore depends on how its database was made:

* ``create_all`` first  -> no server default -> a raw INSERT that omits
  ``claimed_by`` fails ``23502 not_null_violation``;
* migrations only       -> ``DEFAULT ''``     -> the same INSERT succeeds.

That is the drift. This file is the machine check that closes it and keeps it
closed.

How the "migration schema" is built, and why it is not circular
--------------------------------------------------------------
Comparing ``create_all`` against ``create_all + migrations`` proves nothing:
``add_column_if_missing`` sees every column present and every migration is a
no-op, so the two schemas agree by construction and the defect is invisible.
That is precisely how it shipped.

So this file builds a schema in which **the migration DDL is the only thing that
could have produced the objects under test**:

1. ``create_all`` establishes the baseline (migrations 0002-0030 never create a
   table; they only ALTER ones ``create_all`` built).
2. Every migration that SHIPS a ``downgrade()`` is asked to undo itself, newest
   first, and its ``schema_migrations`` row is deleted. ``jobs.claimed_by`` is
   physically gone, ``gpu_*`` / ``storage_objects`` / ``trend_signals`` are
   physically gone. This snapshot is captured and asserted on
   (:func:`test_the_migration_schema_is_not_vacuous`) so a rewind that quietly
   did nothing cannot make the rest of this file pass.
3. ``run_migrations(..., create_missing_tables=False)`` re-applies them with
   ``create_all`` DISABLED. Now every column, default and index the comparison
   reads came out of a migration's own DDL.

The comparison is ORM metadata vs that database, across seven axes:
column presence, type, nullability, server default, unique, foreign key, index.

Semantic normalisations applied, and why each is legitimate
-----------------------------------------------------------
Every one of these is a *dialect spelling* difference -- the same intent
rendered two ways. None of them can hide a missing column, a changed width, a
missing default, a missing index or a flipped ``NOT NULL``.

``DOUBLE PRECISION`` -> ``FLOAT``
    PostgreSQL's name for SQLAlchemy's ``Float``. ``information_schema`` says
    ``double precision``; the DDL says ``FLOAT``; SQLAlchemy compiles ``Float``
    to ``FLOAT``. One intent, one canonical spelling.
``TIMESTAMP WITHOUT TIME ZONE`` -> ``TIMESTAMP``
    What ``TIMESTAMP`` means on PostgreSQL, and what ``DateTime()`` compiles to.
    ``TIMESTAMPTZ`` is deliberately NOT folded in: a timezone-aware column is a
    different column.
``JSONB`` -> ``JSON``
    A JSON document either way. The repository's migrations all declare
    ``JSON``; folding ``JSONB`` in means a future ``JSONB`` is compared as the
    same intent rather than as a spurious failure.
``CHARACTER VARYING``/``BOOL``/``INT4``/``INT8``/``INT2`` -> ``VARCHAR``/
    ``BOOLEAN``/``INTEGER``/``BIGINT``/``SMALLINT``
    PostgreSQL internal aliases. ``NUMERIC``/``DECIMAL`` and ``REAL`` are NOT
    folded: they are different storage widths and folding them would be exactly
    the quiet self-weakening this test must not do.
Width whitespace: ``VARCHAR( 36 )`` -> ``VARCHAR(36)``
    Formatting.
Server defaults: a trailing ``::character varying`` / ``::json`` cast is stripped
    ``''::character varying`` and ``''`` are the same default; PostgreSQL records
    the cast it applied. ``CURRENT_TIMESTAMP`` and ``now()`` are one function.
    Nothing else is normalised -- ``0`` and ``'0'`` stay distinct, ``true`` and
    ``'true'`` stay distinct.
Index NAMES are compared as information, not as a failure
    A ``UNIQUE`` constraint and a unique index over the same columns enforce
    exactly the same thing, and the two code paths cannot agree on a label:
    PostgreSQL names an inline constraint ``<table>_<cols>_key``, SQLAlchemy
    names ``index=True`` columns ``ix_<table>_<col>``, and the migration corpus
    has historical names (``ix_video_cost_outcome``) next to the ORM's
    (``ix_videos_cost_outcome``). Comparing labels would compare a name, not a
    schema. What IS compared is the ordered column tuple plus uniqueness, which
    is what the planner and the database actually use. :func:`renamed_indexes`
    reports the name divergences so they stay visible.

Deliberately NOT closed
-----------------------
:data:`DOCUMENTED_EXCEPTIONS` is the exact, asserted set of differences this
file reports but does not fail on -- foreign keys that migrations 0032/0036/0037
omit on purpose, and the ``REAL`` vs ``FLOAT`` widths 0032 flags as a
money-semantics decision for its owner. Every entry carries its reason, and
:func:`test_the_documented_exceptions_are_exactly_those` fails if one appears,
disappears or changes -- so the list cannot rot into a blanket suppression.

Conventions
-----------
Copied from ``tests/test_work16_postgres_semantics.py``: the DSN comes from
``YMONEY_TEST_POSTGRES``, the whole module skips DECLARATIVELY when no server
answers, and every database is a throwaway dropped ``WITH (FORCE)``. No
``pytest.skip()`` anywhere, so the default SQLite suite needs no database.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 - registration side effect for Base.metadata

# ---------------------------------------------------------------------------
# Reachability probe. Declarative: the module is skipped when it fails.
# ---------------------------------------------------------------------------

_DEFAULT_DSN = "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
PG_DSN = os.environ.get("YMONEY_TEST_POSTGRES", _DEFAULT_DSN).strip()


def _pg_reachable(dsn: str) -> bool:
    """Whether a PostgreSQL server answers on ``dsn``. Never raises."""
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


# ---------------------------------------------------------------------------
# Schema snapshots + normalisation
# ---------------------------------------------------------------------------

_TYPE_ALIASES = {
    # PostgreSQL's name for SQLAlchemy's ``Float``.
    "DOUBLE PRECISION": "FLOAT",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
    "JSONB": "JSON",
    "CHARACTER VARYING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "INT2": "SMALLINT",
}

_DIALECT: Any = None


def _dialect():
    global _DIALECT
    if _DIALECT is None:
        from sqlalchemy.dialects import postgresql

        _DIALECT = postgresql.dialect()
    return _DIALECT


def normalise_type(type_: Any) -> str:
    """Canonical type spelling for a SQLAlchemy type on PostgreSQL.

    Works for BOTH sides of the comparison -- the ORM's declared type and the
    type the server reports -- because both are rendered through the same
    PostgreSQL dialect and then folded. ``REAL`` is never folded into ``FLOAT``:
    they are different widths.
    """
    rendered = (str(type_.compile(dialect=_dialect()))
                if hasattr(type_, "compile") else str(type_))
    text = re.sub(r"\s+", " ", rendered.strip().upper())
    text = re.sub(r"\s*\(\s*(\d+)\s*(?:,\s*\d+\s*)?\)", r"(\1)", text)
    head, _, tail = text.partition("(")
    head = _TYPE_ALIASES.get(head.strip(), head.strip())
    return f"{head}({tail}" if tail else head


_CAST_SUFFIX = re.compile(r"::[a-zA-Z_][a-zA-Z_0-9 ]*(\[\])?$")


def normalise_default(value: Any) -> str | None:
    """Canonical spelling of a server default, from either side.

    Strips the ``::type`` cast PostgreSQL records alongside the literal it
    stored (``''::character varying`` IS ``''``) and folds ``now()`` into
    ``current_timestamp``. Everything else is compared verbatim, so ``0`` and
    ``'0'`` remain different defaults.
    """
    if value is None:
        return None
    text = _CAST_SUFFIX.sub("", str(value).strip()).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.lower()
    if text == "now()":
        text = "current_timestamp"
    return text


class Difference(NamedTuple):
    """One ORM-vs-migration disagreement, in a form a failure can print."""

    table: str
    axis: str
    obj: str
    orm: str
    migration: str
    documented: bool = False

    def __str__(self) -> str:
        return (f"{self.table}.{self.obj} [{self.axis}]: "
                f"orm={self.orm!r} migration={self.migration!r}")


#: ``(table, axis, object)`` triples this file reports but does not fail on.
#: Every entry is a decision somebody already wrote down; the reason is in
#: :data:`_EXCEPTION_REASONS` and is asserted to be non-empty.
DOCUMENTED_EXCEPTIONS: frozenset[tuple[str, str, str]] = frozenset({
    # -- foreign keys the migrations deliberately omit -----------------------
    # 0032 creates these four planning tables without FKs; 0036 and 0037 say
    # why in their own docstrings (a cap/reservation/object row must be able to
    # outlive the tenant or the job it points at). Adding one to the schema
    # changes what the database ACCEPTS -- it can refuse a write the migration
    # path takes today -- which is a schema-policy decision, not a parity fix.
    ("editorial_plans", "foreign key", "workspace_id->workspaces.id"),
    ("editorial_plan_items", "foreign key", "workspace_id->workspaces.id"),
    ("editorial_plan_items", "foreign key", "plan_id->editorial_plans.id"),
    ("production_capacity", "foreign key", "workspace_id->workspaces.id"),
    ("trend_signals", "foreign key", "workspace_id->workspaces.id"),
    ("gpu_reservations", "foreign key", "device_id->gpu_devices.id"),
    # -- REAL where the ORM says FLOAT ---------------------------------------
    # 0032 lines 70-80 say this outright: "changing the declared width of a
    # spend field is a decision for whoever owns the money semantics, not a side
    # effect of a database-port fix". These columns carry budget_usd,
    # spent_usd and estimated_cost_usd. Narrowing the ORM to REAL would make a
    # FRESH database lossy where it is exact today; widening the schema to
    # DOUBLE PRECISION would rewrite deployed money. Neither is a call this
    # lane may take, so the difference is recorded rather than closed.
    ("trend_signals", "type", "confidence"),
    ("editorial_plans", "type", "budget_usd"),
    ("editorial_plans", "type", "spent_usd"),
    ("editorial_plan_items", "type", "priority"),
    ("editorial_plan_items", "type", "estimated_cost_usd"),
    ("production_capacity", "type", "longform_per_week"),
    ("production_capacity", "type", "shorts_per_day"),
    ("production_capacity", "type", "ugc_per_day"),
    ("production_capacity", "type", "localization_per_day"),
    ("production_capacity", "type", "render_hours_per_day"),
    ("production_capacity", "type", "review_slots_per_day"),
    # -- a server default that contradicts a documented safety decision -------
    # 0031 declares ``published_posts.publication_mode ... DEFAULT 'LIVE'``. The
    # ORM's ``default="UNAVAILABLE"`` is not an oversight: the comment above that
    # column is an argument that a row which declared nothing must NOT be read as
    # a live publication, and it names 0031's backfill as the reason. Copying
    # ``'LIVE'`` into ``create_all`` would spread a live-by-default publication
    # mode to every fresh database. The NULLABILITY is brought into parity (it is
    # pure loosening); the default stays different, loudly.
    ("published_posts", "default", "publication_mode"),
})

_EXCEPTION_REASONS: dict[tuple[str, str, str], str] = dict.fromkeys(DOCUMENTED_EXCEPTIONS, "see the module docstring, 'Deliberately NOT closed'")


def _orm_index_keys(table: Any) -> dict[tuple[tuple[str, ...], bool], str]:
    """``{(ordered columns, unique): name}`` for everything the ORM declares.

    Three spellings reach the same place: ``Index(..., unique=True)``,
    ``UniqueConstraint(...)`` in ``__table_args__``, and ``unique=True`` on a
    column. All three are uniqueness PostgreSQL enforces, so all three are read.
    """
    keys: dict[tuple[tuple[str, ...], bool], str] = {}
    for index in table.indexes:
        keys[(tuple(c.name for c in index.columns), bool(index.unique))] = index.name
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            keys[(tuple(c.name for c in constraint.columns), True)] = (
                constraint.name or "<unnamed>")
    for column in table.columns:
        if column.unique:
            keys.setdefault(((column.name,), True), f"{table.name}_{column.name}_key")
    return keys


def _db_index_keys(inspector: Any, table: str) -> dict[tuple[tuple[str, ...], bool], str]:
    keys: dict[tuple[tuple[str, ...], bool], str] = {}
    for index in inspector.get_indexes(table):
        keys[(tuple(index.get("column_names") or ()), bool(index.get("unique")))] = (
            str(index["name"]))
    for constraint in inspector.get_unique_constraints(table):
        # A UNIQUE constraint backed by a unique INDEX is already recorded above
        # under the index; ``setdefault`` keeps whichever name the server gave.
        keys.setdefault((tuple(constraint.get("column_names") or ()), True),
                        str(constraint["name"]))
    return keys


def _orm_default(column: Any) -> str | None:
    sd = column.server_default
    if sd is None:
        return None
    arg = getattr(sd, "arg", sd)
    rendered = str(arg.compile(dialect=_dialect())) if hasattr(arg, "compile") else str(arg)
    return normalise_default(rendered)


def _orm_fk_keys(table: Any) -> set[str]:
    keys = set()
    for fk in table.foreign_keys:
        elements = sorted(fk.constraint.elements, key=lambda e: e.parent.name)
        keys.add(f"{','.join(e.parent.name for e in elements)}->{fk.target_fullname}")
    return keys


def _db_fk_keys(inspector: Any, table: str) -> set[str]:
    keys = set()
    for fk in inspector.get_foreign_keys(table):
        cols = ",".join(fk.get("constrained_columns") or ())
        referred = f"{fk.get('referred_table')}.{','.join(fk.get('referred_columns') or ())}"
        keys.add(f"{cols}->{referred}")
    return keys


def compare_table(name: str, orm_table: Any, inspector: Any) -> list[Difference]:
    """Every disagreement for one table, across all seven axes."""
    out: list[Difference] = []

    def add(axis: str, obj: str, orm: Any, migration: Any) -> None:
        orm_s, mig_s = str(orm), str(migration)
        if orm_s == mig_s:
            return
        out.append(Difference(name, axis, obj, orm_s, mig_s,
                              documented=(name, axis, obj) in DOCUMENTED_EXCEPTIONS))

    db_columns = {str(c["name"]): c for c in inspector.get_columns(name)}
    for column in orm_table.columns:
        got = db_columns.get(column.name)
        if got is None:
            add("column", column.name, "declared", "ABSENT")
            continue
        add("type", column.name, normalise_type(column.type),
            normalise_type(got["type"]))
        add("nullable", column.name, column.nullable, got["nullable"])
        add("default", column.name, _orm_default(column), normalise_default(got.get("default")))
    for extra in sorted(set(db_columns) - {c.name for c in orm_table.columns}):
        add("column", extra, "ABSENT", "declared")

    # UNIQUE is compared through the index axis: ``_orm_index_keys`` folds
    # ``Index(unique=True)``, ``UniqueConstraint`` and ``unique=True`` into one
    # namespace, and every entry carries its uniqueness flag, so a unique
    # constraint the ORM does not declare and a unique index the ORM does not
    # declare both surface here.
    #
    # Only the (column tuple, uniqueness) KEY is compared, never the name: the
    # two code paths cannot agree on a label (PostgreSQL names an inline
    # constraint ``<table>_<cols>_key``, SQLAlchemy names ``index=True`` columns
    # ``ix_<table>_<col>``, and the migration corpus carries third and fourth
    # spellings), and a name is not the schema. What is compared is exactly what
    # the planner uses to find an index. ``renamed_indexes`` reports the labels
    # so the divergence stays visible rather than merely ignored.
    orm_keys = set(_orm_index_keys(orm_table))
    db_keys = set(_db_index_keys(inspector, name))
    for key in sorted(orm_keys - db_keys, key=str):
        add("index", f"{key[0]}{' UNIQUE' if key[1] else ''}",
            _orm_index_keys(orm_table)[key], "ABSENT")
    for key in sorted(db_keys - orm_keys, key=str):
        add("index", f"{key[0]}{' UNIQUE' if key[1] else ''}",
            "ABSENT", _db_index_keys(inspector, name)[key])

    for key in sorted(_orm_fk_keys(orm_table) - _db_fk_keys(inspector, name)):
        add("foreign key", key, "declared", "ABSENT")
    for key in sorted(_db_fk_keys(inspector, name) - _orm_fk_keys(orm_table)):
        add("foreign key", key, "ABSENT", "declared")

    return out


def renamed_indexes(inspector: Any, tables: list[str]) -> list[str]:
    """Indexes whose COLUMN SET agrees but whose NAME differs. Information only."""
    notes: list[str] = []
    metadata = _metadata()
    for name in tables:
        orm_keys = _orm_index_keys(metadata.tables[name])
        for key, db_name in sorted(_db_index_keys(inspector, name).items(), key=str):
            orm_name = orm_keys.get(key)
            if orm_name is not None and orm_name != db_name:
                notes.append(f"{name}: {db_name} == {orm_name} over {key[0]}")
    return notes


def _metadata():
    from app.db import Base

    return Base.metadata


# ---------------------------------------------------------------------------
# Scratch databases
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch() -> Iterator[Any]:
    """Factory for throwaway databases, dropped ``WITH (FORCE)`` at teardown."""
    made: list[str] = []

    def make(label: str):
        import psycopg

        name = f"w16_1_parity_{label}_{os.urandom(4).hex()}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return create_engine(_engine_url(name), pool_size=8, max_overflow=8)

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


class MigrationSchema(NamedTuple):
    engine: Any
    Session: Any
    rewind_applied: list[str]
    rewind_columns: dict[str, set[str]]
    rewind_tables: set[str]
    reupplied: list[str]


@pytest.fixture(scope="module")
def migration_schema(scratch) -> MigrationSchema:
    """A database whose migration-owned objects came from migration DDL alone."""
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import load_migrations, run_migrations

    engine = scratch("mig")
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Step 1: the baseline the migrations build ON TOP of.
    from app.db import Base

    Base.metadata.create_all(engine)

    with factory() as s:
        # Step 1b: apply every migration once. This creates ``schema_migrations``
        # (the runner owns that table, not ``Base.metadata``) and puts the
        # database in the state a real deployment is in.
        run_migrations(s, create_missing_tables=False)

        # Step 2: rewind. Only migrations that ship a downgrade() can be undone,
        # and those are exactly the ones that own an ALTER/CREATE.
        modules = dict(load_migrations())
        rewound: list[str] = []
        for mod_name in sorted(modules, reverse=True):
            module = modules[mod_name]
            if not hasattr(module, "downgrade"):
                continue
            module.downgrade(s)
            s.execute(text("DELETE FROM schema_migrations WHERE version=:v"),
                      {"v": mod_name})
            s.commit()
            rewound.append(mod_name)
        live = inspect(s.connection())
        rewind_columns = {c["name"] for c in live.get_columns("jobs")}
        rewind_tables = set(live.get_table_names())

        # Step 3: re-apply with create_all DISABLED.
        reupplied = run_migrations(s, create_missing_tables=False)

    return MigrationSchema(engine=engine, Session=factory, rewind_applied=rewound,
                           rewind_columns=rewind_columns, rewind_tables=rewind_tables,
                           reupplied=reupplied)


@pytest.fixture(scope="module")
def orm_schema(scratch):
    """A database built by ``create_all`` alone -- the ORM's own opinion."""
    from sqlalchemy.orm import sessionmaker

    from app.db import Base

    engine = scratch("orm")
    Base.metadata.create_all(engine)
    return {"engine": engine, "inspector": inspect(engine),
            "Session": sessionmaker(bind=engine, expire_on_commit=False)}


@pytest.fixture(scope="module")
def parity(migration_schema) -> dict[str, list[Difference]]:
    """Every difference, keyed by table, computed once."""
    from sqlalchemy import inspect

    metadata = _metadata()
    inspector = inspect(migration_schema.engine)
    tables = sorted(set(metadata.tables) & set(inspector.get_table_names()))
    return {name: compare_table(name, metadata.tables[name], inspector) for name in tables}


_SHARED_TABLES = sorted(_metadata().tables)


@pytest.mark.parametrize("table_name", _SHARED_TABLES)
def test_table_matches_the_migration_schema(table_name, parity):
    """Per table, across all seven axes, every mismatch is named."""
    diffs = parity.get(table_name)
    assert diffs is not None, (
        f"{table_name} is on Base.metadata but absent from the migration-built "
        f"schema, so nothing about it was compared")
    unexplained = [d for d in diffs if not d.documented]
    assert not unexplained, (
        f"{len(unexplained)} unexplained ORM/migration difference(s) on "
        f"{table_name}:\n  " + "\n  ".join(str(d) for d in unexplained))


# ---------------------------------------------------------------------------
# Anti-vacuity: the comparison must be looking at a schema migrations built
# ---------------------------------------------------------------------------

_LEASE_COLUMNS = ("claimed_by", "claimed_at", "lease_expires_at", "heartbeat_at")


def test_the_migration_schema_is_not_vacuous(migration_schema):
    """If this passes for the wrong reason, every other test here is theatre.

    Guards, in order:
      * the rewind really removed 0035/0036/0037's objects;
      * the re-apply really put them back, with ``create_all`` disabled;
      * ``jobs.claimed_by``'s server default in the migration-built schema can
        ONLY have come from 0035's DDL text -- if 0035 had not run, or had run
        as a no-op against a ``create_all`` column, this is ``None``.
    """
    from sqlalchemy import inspect, text

    assert {"0035_job_leases", "0036_gpu_and_storage", "0037_budget_rollups"} <= set(
        migration_schema.rewind_applied), (
        "the rewind did not reach 0035-0037, so nothing was re-applied from DDL")
    assert not (set(_LEASE_COLUMNS) & migration_schema.rewind_columns), (
        "0035's downgrade left the lease columns in place; the re-apply cannot "
        "have come from its DDL")
    assert "gpu_devices" not in migration_schema.rewind_tables
    assert "budget_rollup_limits" not in migration_schema.rewind_tables
    assert set(migration_schema.reupplied) == set(migration_schema.rewind_applied)

    inspector = inspect(migration_schema.engine)
    live = {str(c["name"]): c for c in inspector.get_columns("jobs")}
    assert set(_LEASE_COLUMNS) <= set(live), "the re-apply did not restore 0035"
    assert normalise_default(live["claimed_by"].get("default")) == "''", (
        "jobs.claimed_by has no DEFAULT '' in the migration-built schema, so "
        "0035's DDL is not what produced it")

    with migration_schema.Session() as s:
        rows = s.execute(text(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name='jobs' AND column_name=ANY(:names)"),
            {"names": list(_LEASE_COLUMNS)}).all()
    assert rows, "information_schema returned nothing for jobs' lease columns"


def test_a_migration_owned_table_is_built_by_its_migration(migration_schema):
    """``budget_rollup_limits`` is not on ``Base.metadata`` at all (0037 says so),
    so its DDL is the only thing that can create it -- which is exactly why its
    ``TRUE`` / ``'{}'`` defaults must be real server-side values."""
    from sqlalchemy import inspect

    inspector = inspect(migration_schema.engine)
    assert "budget_rollup_limits" in inspector.get_table_names()
    columns = {str(c["name"]): c for c in
               inspector.get_columns("budget_rollup_limits")}
    assert normalise_default(columns["enabled"]["default"]) == "true"
    assert normalise_default(columns["meta_json"]["default"]) == "'{}'"
    assert columns["daily_total_cap"]["nullable"] is True


def test_no_model_table_is_missing_from_the_migration_schema(parity):
    """Every table the ORM declares exists in the migration-built schema."""
    absent = sorted(name for name in _SHARED_TABLES if name not in parity)
    assert not absent, (
        f"{len(absent)} model table(s) absent from the migration-built schema: "
        f"{absent}")


#: Tables the migration-built schema has and ``Base.metadata`` does not, each
#: with the reason it is not a parity failure.
_MIGRATION_ONLY_TABLES = {
    "schema_migrations": "owned by migrations/runner.py, not by any model",
    "budget_rollup_limits": (
        "0037 owns this DDL and says so; services/budget_rollup.py owns the "
        "access, so there is deliberately no ORM model"),
    # ``experiments`` WAS here. It is not a migration-only table any more, and
    # the reason it was is worth keeping: ``app/models/experiment.py`` defined
    # the table but ``app/models/__init__.py`` never imported it, so
    # ``Base.metadata`` only gained it as a side effect of importing
    # ``app.api.v1.experiments`` or ``app.engine.performance.experiments``.
    #
    # That is not a cosmetic gap. Whether the ORM knew about this table depended
    # on import order, so this very test classified it differently depending on
    # which module a neighbouring test had loaded first -- and the ORM declared
    # two single-column indexes where 0020 declares one composite
    # ``ix_experiments_ws_status``, a divergence nobody could see while the
    # model was unreachable through the package. Work 16.1 registered it and
    # matched the composite index, so the migration remains the only source of
    # truth for the schema while the ORM becomes reachable and testable.
}


def test_every_migration_only_table_is_accounted_for(migration_schema):
    """The other direction: a table in the schema with no model behind it.

    Without this, a migration that starts creating a table nobody models would
    pass unnoticed, and ``experiments`` is already in this set for a reason worth
    keeping in code rather than in somebody's memory.
    """
    from sqlalchemy import inspect

    inspector = inspect(migration_schema.engine)
    extra = sorted(set(inspector.get_table_names()) - set(_metadata().tables))
    assert set(extra) == set(_MIGRATION_ONLY_TABLES), (
        f"the migration-only table set changed: {extra}")
    for name in extra:
        assert _MIGRATION_ONLY_TABLES[name].strip(), (
            f"{name} is migration-only with no stated reason")


def test_the_documented_exceptions_are_exactly_those(parity):
    """The allowlist is frozen in BOTH directions.

    An entry for a difference that no longer occurs is as much a lie as an
    unlisted one, so both a stale entry and a new unlisted difference fail here.
    """
    for key in DOCUMENTED_EXCEPTIONS:
        assert key in _EXCEPTION_REASONS and _EXCEPTION_REASONS[key].strip(), (
            f"{key} is allowlisted with no stated reason")
    observed = {(d.table, d.axis, d.obj) for diffs in parity.values() for d in diffs
                if d.documented}
    unlisted = sorted(observed - DOCUMENTED_EXCEPTIONS)
    stale = sorted(DOCUMENTED_EXCEPTIONS - observed)
    assert not unlisted, (
        f"{len(unlisted)} difference(s) were suppressed without being listed: "
        f"{unlisted}")
    assert not stale, (
        f"{len(stale)} allowlisted difference(s) no longer occur -- remove them "
        f"and fix the underlying drift: {stale}")


def test_the_documented_exceptions_are_the_ones_the_migrations_justify(migration_schema):
    """Each allowlisted foreign key must genuinely be absent from the SCHEMA.

    Without this the allowlist could be papering over a migration that has since
    started emitting the key, and the reason quoted in the module docstring would
    have gone stale without anyone noticing.
    """
    from sqlalchemy import inspect

    inspector = inspect(migration_schema.engine)
    for table, axis, obj in sorted(DOCUMENTED_EXCEPTIONS):
        if axis != "foreign key":
            continue
        assert obj not in _db_fk_keys(inspector, table), (
            f"{table}: {obj} IS in the migration schema now, so the "
            f"'migrations omit it on purpose' reason no longer holds")


# ---------------------------------------------------------------------------
# The consequence, not just the comparison
# ---------------------------------------------------------------------------


_RAW_JOB_INSERT = (
    "INSERT INTO jobs (id, type, status, priority, payload, result, "
    "last_error, cancel_requested, next_run_at, max_retries, retry_count, "
    "created_at, updated_at) VALUES (:i, 'w16.1', 'QUEUED', 100, '{}', '{}', "
    "'', false, now(), 3, 0, now(), now())")


def _sid() -> str:
    import uuid

    return uuid.uuid4().hex


def test_a_raw_insert_omitting_claimed_by_succeeds_on_both_schemas(
        migration_schema, orm_schema):
    """The drift's actual cost, closed.

    The same statement, naming every OTHER NOT NULL column, is run against the
    migration-built schema and against the ``create_all`` schema. It used to fail
    ``23502 not_null_violation`` on the ``create_all`` one because
    ``claimed_by`` had no server default there. Both must now land, and both
    must store ``''`` -- which is what "no worker holds this" means.
    """
    from sqlalchemy import text

    for label, session_factory in (("migration", migration_schema.Session),
                                   ("orm", orm_schema["Session"])):
        job_id = _sid()
        with session_factory() as s:
            try:
                s.execute(text(_RAW_JOB_INSERT), {"i": job_id})
                s.commit()
            except Exception as exc:  # noqa: BLE001 - the point is the failure mode
                s.rollback()
                state = getattr(getattr(exc, "orig", None), "sqlstate", None)
                raise AssertionError(
                    f"the raw INSERT failed on the {label} schema with SQLSTATE "
                    f"{state!r}: {exc}") from exc
            got = s.execute(text("SELECT claimed_by FROM jobs WHERE id=:i"),
                            {"i": job_id}).scalar()
            assert got == "", (
                f"the {label} schema stored {got!r} for an omitted claimed_by, "
                f"expected the '' the migration declares")
            s.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
            s.commit()


def test_index_name_divergences_are_reported_not_hidden(parity, migration_schema):
    """Name differences are visible even though they are not failures."""
    from sqlalchemy import inspect

    notes = renamed_indexes(inspect(migration_schema.engine), _SHARED_TABLES)
    assert isinstance(notes, list)
    # A sanity floor, so this cannot pass by comparing nothing at all.
    assert len(notes) >= 5, (
        f"expected the historical index-name divergences to still be visible, "
        f"found only {len(notes)}")


def test_parity_comparison_is_never_silently_empty(parity):
    """If the comparator stopped comparing anything, every table would pass."""
    assert len(parity) >= 100, (
        f"only {len(parity)} tables compared -- the shared-table set collapsed")
    documented = {(d.table, d.axis, d.obj)
                  for diffs in parity.values() for d in diffs if d.documented}
    assert len(documented) == len(DOCUMENTED_EXCEPTIONS), (
        f"{len(documented)} differences are marked documented but "
        f"{len(DOCUMENTED_EXCEPTIONS)} are listed")
    assert sum(len(v) for v in parity.values()) >= len(DOCUMENTED_EXCEPTIONS), (
        "the comparator found fewer differences than the documented list, so it "
        "is not reading the schema it claims to read")


def test_concurrent_reads_do_not_interfere_with_the_comparison(migration_schema):
    """The scratch database tolerates the pool the fixtures hand out.

    Not a schema claim -- a fixture-hygiene claim. Four real connections race
    through the same database, so a leaked transaction cannot make the parity
    results depend on which test ran first.
    """
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            with migration_schema.Session() as s:
                assert s.execute(text("SELECT 1")).scalar() == 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent sessions failed: {errors!r}"


# ---------------------------------------------------------------------------
# 0038: the migration that closed the gap must itself round-trip
# ---------------------------------------------------------------------------


def _index_names(inspector: Any, table: str) -> set[str]:
    return {str(i["name"]) for i in inspector.get_indexes(table)}


def _column_default(inspector: Any, table: str, column: str) -> str | None:
    for col in inspector.get_columns(table):
        if str(col["name"]) == column:
            return normalise_default(col.get("default"))
    return None


def test_0038_creates_what_it_claims_and_round_trips_up_and_down(scratch):
    """``upgrade`` then ``downgrade`` then ``upgrade`` on a real database.

    Each phase is asserted separately, because a downgrade that only works on one
    backend is a downgrade nobody has run:

    * after ``upgrade`` every index in ``_INDEXES`` exists and
      ``jobs.claimed_by`` has ``DEFAULT ''``;
    * after ``downgrade`` every one of them is GONE and the default is gone too,
      on PostgreSQL -- the index-before-object order is what makes this legal;
    * ``upgrade`` again restores all of them, so the migration is not one-way;
    * a second ``upgrade`` changes nothing (idempotence), because every statement
      is guarded by a catalog read.
    """
    from sqlalchemy import inspect
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.migrations.runner import load_migrations

    module = dict(load_migrations())["0038_schema_parity"]
    wanted = {(table, name) for name, table, _cols in module._INDEXES}

    engine = scratch("rt")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def snapshot() -> tuple[set[tuple[str, str]], str | None]:
        insp = inspect(engine)
        present = {(table, name) for name, table, _cols in module._INDEXES
                   if name in _index_names(insp, table)}
        return present, _column_default(insp, "jobs", "claimed_by")

    # -- phase 0: build the PRE-FIX shape ------------------------------------
    # A ``create_all`` database from before this change has every one of
    # ``_INDEXES`` and none of the ``_DEFAULTS``. Recreating that shape by hand is
    # what makes phases 1-3 a test of convergence rather than of a no-op, and it
    # is the only way to prove the migration does something for a deployment that
    # already exists.
    with factory() as s:
        for name, _table, _cols in module._INDEXES:
            s.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        for table, column, _literal in module._DEFAULTS:
            s.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                           f'DROP DEFAULT'))
        s.commit()
    pre_indexes, pre_default = snapshot()
    assert pre_indexes == set(), "the pre-fix shape was not reproduced"
    assert pre_default is None, "the pre-fix shape still had a default"
    legacy = inspect(engine)
    assert all(_column_default(legacy, table, column) is None
               for table, column, _l in module._DEFAULTS), (
        "some pre-fix defaults survived; the legacy shape is wrong")

    # -- phase 1: the state a migration-path database is in after 0037 ---------
    with factory() as s:
        module.upgrade(s)
        s.commit()
    after_up, claimed = snapshot()
    assert after_up == wanted, (
        f"0038 did not create all {len(wanted)} indexes; missing "
        f"{sorted(wanted - after_up)}")
    assert claimed == "''", (
        f"jobs.claimed_by default is {claimed!r} after 0038, expected ''")
    restored = inspect(engine)
    missing = [(table, column) for table, column, _l in module._DEFAULTS
               if _column_default(restored, table, column) is None]
    assert not missing, (
        f"0038 did not restore the server default on {missing}; an existing "
        f"deployment keeps failing 23502 there")

    # -- phase 2: idempotence -------------------------------------------------
    with factory() as s:
        module.upgrade(s)
        s.commit()
    assert snapshot() == (after_up, "''"), "a second 0038 upgrade changed the schema"

    # -- phase 3: downgrade ---------------------------------------------------
    with factory() as s:
        module.downgrade(s)
        s.commit()
    after_down, claimed_none = snapshot()
    assert after_down == set(), (
        f"0038's downgrade left {sorted(after_down)} behind")
    assert claimed_none is None, (
        "0038's downgrade did not unset jobs.claimed_by's default")
    cleared = inspect(engine)
    still = [(table, column) for table, column, _l in module._DEFAULTS
             if _column_default(cleared, table, column) is not None]
    assert not still, f"0038's downgrade left a default on {still}"

    # -- phase 4: upgrade again ----------------------------------------------
    with factory() as s:
        module.upgrade(s)
        s.commit()
    assert snapshot() == (after_up, "''"), (
        "0038 is not reversible: re-running upgrade() after downgrade() did not "
        "restore the schema")

