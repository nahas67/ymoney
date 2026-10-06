"""Upgrade 0038: make the migration-built schema and the ORM-built schema ONE.

Why this exists
---------------
``app.db.Base.metadata.create_all()`` and the migration corpus are two code paths
that both try to describe the same database, and until this migration they
described two DIFFERENT databases. Which one a deployment got depended on how
its database was created, and two facts demonstrate the split:

``jobs.claimed_by`` had no server default on a ``create_all`` database
    0035 declares ``claimed_by VARCHAR(80) NOT NULL DEFAULT ''``. The ORM says
    ``default=""``, which is a PYTHON callable and emits no DDL default, and
    ``add_column_if_missing`` sees the column and returns -- so the declared
    default never landed. A raw INSERT, a view or a CTE that omits ``claimed_by``
    therefore failed ``23502 not_null_violation`` on one schema and succeeded on
    the other. ``claimed_by = ''`` is the reading every reader depends on ("no
    worker holds this"), so a NULL there is not an alternative spelling, it is a
    different fact.

A migration-built database was missing a batch of indexes the ORM declares
    0032, 0036 and 0037 create four and three and one tables outright. Every
    ``index=True`` / ``__table_args__`` entry on those tables was therefore
    dropped and never recreated, because the migration that owned the table never
    mentions it: ``ix_storage_objects_state``, ``ix_gpu_res_job``,
    ``ix_trend_signals_status``, ``ix_editorial_plans_created_at`` and twenty more
    simply do not exist on a database that reached its schema through the
    migrations. Those are the indexes the recovery sweep, the expiry sweeper and
    the operator's "what is unknown exposure" filter all run through.

What this migration does, and nothing else
------------------------------------------
Two additive, reversible things:

1. ``ALTER TABLE ... ALTER COLUMN ... SET DEFAULT`` for the nine columns whose
   ``NOT NULL DEFAULT`` the declaring migration states and the ORM used to omit
   (``jobs.claimed_by`` plus 0034's eight paid-submission columns). No value is
   rewritten, no type is narrowed, no constraint is added or removed -- a default
   only ever applies to rows that arrive WITHOUT the column, which is precisely
   the case that used to fail ``23502``.
2. ``CREATE INDEX IF NOT EXISTS`` for the indexes above. On a deployment
   ``create_all`` built, every one of them already exists and this is a no-op; on
   the migration path it is the missing half.

It deliberately does NOT touch: column types (0032's ``REAL`` vs the ORM's
``FLOAT`` is a spend-field width decision its own author deferred -- see 0032
lines 70-80), foreign keys (0032/0036/0037 omit them on purpose, so their tables
can outlive the tenant they point at), or any column's meaning.

Reversible
----------
``downgrade`` drops the indexes FIRST, in reverse creation order, then unsets the
defaults. Index-before-object ordering is the only one that works on both
backends: SQLite refuses ``DROP COLUMN`` while an index still names it (0033 and
0034 found that out) and PostgreSQL happily leaves a dangling index behind. The
defaults are unset rather than the columns altered, because ``ALTER COLUMN ...
DROP DEFAULT`` is exactly the inverse of what ``upgrade`` did and touches nothing
else -- no column is dropped, so there is no ``DROP COLUMN`` here at all.

One backend limit, stated rather than hidden: SQLite has no
``ALTER TABLE ... ALTER COLUMN`` form for defaults at all, so the default half of
this migration is skipped there (see :func:`_can_alter_column_default`). The
index half runs on both backends.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: (index name, table, ordered columns) for every index the ORM declares on a
#: table the MIGRATIONS own, and that the migration path therefore drops and
#: never recreates. Discovered empirically by
#: ``tests/test_work16_1_schema_parity.py``, not guessed: every name here is the
#: ORM's own ``index=True`` / ``__table_args__`` name, so a ``create_all``
#: database and a migrated database end up with the SAME index names, not merely
#: equivalent ones.
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # -- 0032's tables -------------------------------------------------------
    ("ix_editorial_plan_items_created_at", "editorial_plan_items", ("created_at",)),
    ("ix_editorial_plan_items_workspace_id", "editorial_plan_items", ("workspace_id",)),
    ("ix_editorial_plan_items_status", "editorial_plan_items", ("status",)),
    ("ix_editorial_plan_items_opportunity_id", "editorial_plan_items",
     ("opportunity_id",)),
    ("ix_editorial_plans_created_at", "editorial_plans", ("created_at",)),
    ("ix_trend_signals_created_at", "trend_signals", ("created_at",)),
    ("ix_trend_signals_workspace_id", "trend_signals", ("workspace_id",)),
    ("ix_trend_signals_status", "trend_signals", ("status",)),
    ("ix_trend_signals_observed_at", "trend_signals", ("observed_at",)),
    ("ix_production_capacity_created_at", "production_capacity", ("created_at",)),
    # -- tables the migrations ALTER, whose single-column indexes a later
    # -- migration's downgrade DISCOVERS and drops without recreating. 0032
    # -- discovers indexes naming ``schedule_entries.plan_item_id``; 0034 does the
    # -- same for every column it adds. On a re-upgrade those names come back as
    # -- the migration's COMPOSITE only, so the single-column index an operator's
    # -- "everything with unknown exposure" query uses simply vanished.
    ("ix_schedule_entries_plan_item_id", "schedule_entries", ("plan_item_id",)),
    ("ix_opportunities_plan_item_id", "opportunities", ("plan_item_id",)),
    ("ix_published_posts_publication_mode", "published_posts", ("publication_mode",)),
    ("ix_videos_cost_outcome", "videos", ("cost_outcome",)),
    ("ix_videos_submission_state", "videos", ("submission_state",)),
    ("ix_lipsync_jobs_cost_outcome", "lipsync_jobs", ("cost_outcome",)),
    ("ix_lipsync_jobs_execution_outcome", "lipsync_jobs", ("execution_outcome",)),
    # -- 0036's tables -------------------------------------------------------
    ("ix_gpu_devices_created_at", "gpu_devices", ("created_at",)),
    ("ix_gpu_reservations_created_at", "gpu_reservations", ("created_at",)),
    ("ix_gpu_reservations_workspace_id", "gpu_reservations", ("workspace_id",)),
    ("ix_gpu_reservations_device_id", "gpu_reservations", ("device_id",)),
    ("ix_gpu_reservations_status", "gpu_reservations", ("status",)),
    # ``job_id`` is a bare String with no ``index=True``; this is the ORM's own
    # ``__table_args__`` name and it is the lookup the recovery sweep does when a
    # job dies and its VRAM has to be found.
    ("ix_gpu_res_job", "gpu_reservations", ("job_id",)),
    ("ix_storage_objects_created_at", "storage_objects", ("created_at",)),
    ("ix_storage_objects_workspace_id", "storage_objects", ("workspace_id",)),
    ("ix_storage_objects_state", "storage_objects", ("state",)),
    ("ix_storage_objects_kind", "storage_objects", ("kind",)),
)

#: (table, column, default literal) for every column whose ``DEFAULT`` the ORM
#: used to omit, so an EXISTING database has none of them. ``ALTER COLUMN ... SET
#: DEFAULT`` is catalogue-only -- no row is read, locked or rewritten, because a
#: default only ever applies to an INSERT that omits the column and every
#: existing row already has a value there.
#:
#: All nine are ``NOT NULL DEFAULT ...`` in the migration that declared them
#: (0035 for ``jobs.claimed_by``, 0034 for the paid-submission columns), and the
#: ORM now declares the same literal -- so this is the other half of that fix:
#: the model change converges every FRESH database, this converges every
#: EXISTING one. Without it the split would simply move rather than close.
_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "claimed_by", "''"),
    ("videos", "submission_state", "'PREPARED'"),
    ("videos", "provider_task_id", "''"),
    ("videos", "submission_detail", "''"),
    ("videos", "idempotency_key", "''"),
    ("videos", "cost_outcome", "''"),
    ("videos", "submission_operation_id", "''"),
    ("lipsync_jobs", "execution_outcome", "''"),
    ("lipsync_jobs", "cost_outcome", "''"),
)


def _existing(session, table: str) -> bool:
    from app.migrations.ddl import table_exists

    return table_exists(session, table)


def _can_alter_column_default(session) -> bool:
    """Whether this backend can ``ALTER COLUMN ... SET/DROP DEFAULT`` at all.

    SQLite cannot. Its grammar has exactly one ``ALTER TABLE`` form that touches
    a column (``ADD COLUMN``); ``ALTER COLUMN`` is not a keyword it recognises,
    so the statement dies with ``sqlite3.OperationalError: near "DEFAULT":
    syntax error``. Emulating it would mean rebuilding the table -- copying every
    row, every index and every trigger -- which is not a thing a migration should
    do to a table full of job-ownership records.

    Skipping is also the RIGHT answer rather than a cop-out: a SQLite database
    can only ever have been created by ``create_all`` (the migration corpus
    cannot build one -- ``run_migrations(create_missing_tables=False)`` on an
    empty SQLite file dies on the first table that does not exist yet), and
    ``create_all`` now emits ``DEFAULT ''`` for ``jobs.claimed_by`` because the
    ORM declares it. So the only SQLite databases that lack the default are ones
    created before this migration, and those are local development copies.

    The indexes below are portable and are created on BOTH backends; only the
    ``ALTER COLUMN`` half is skipped here.
    """
    dialect = session.get_bind()
    name = dialect.dialect.name if dialect is not None else ""
    return name != "sqlite"


def _current_default(session, table: str, column: str) -> str | None:
    """The column's current ``column_default``, read on THIS connection.

    ``ddl._inspector`` documents why: ``session.get_bind()`` returns the engine,
    and ``inspect(engine)`` opens a different connection that cannot see this
    transaction's own DDL. On PostgreSQL the two then deadlock against the
    ``ACCESS EXCLUSIVE`` lock the ALTER just took.
    """
    from sqlalchemy import inspect

    if not _existing(session, table):
        return None
    for col in inspect(session.connection()).get_columns(table):
        if str(col["name"]) == column:
            return col.get("default")
    return None


def upgrade(session) -> None:
    from app.migrations.ddl import create_index_if_missing

    alterable = _can_alter_column_default(session)
    for table, column, literal in _DEFAULTS:
        if not _existing(session, table):
            continue
        if _current_default(session, table, column) is not None:
            continue   # already correct; nothing to write
        if not alterable:
            logger.warning(
                "skipping DEFAULT %s on %s.%s: this backend cannot ALTER COLUMN",
                literal, table, column)
            continue
        # ``ALTER COLUMN ... SET DEFAULT`` touches the catalogue only. No row is
        # read, locked or rewritten: a default applies to INSERTs that omit the
        # column, and every existing row already HAS a value there.
        session.execute(
            text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                 f'SET DEFAULT {literal}'))

    for name, table, columns in _INDEXES:
        if not _existing(session, table):
            continue   # a trimmed deployment has nothing to index
        create_index_if_missing(session, name, table, ", ".join(columns))


def downgrade(session) -> None:
    from app.migrations.ddl import index_exists

    # Indexes FIRST. SQLite raises "error in index ... after drop column" if a
    # column goes while an index still names it, and PostgreSQL keeps the dangling
    # index silently; index-then-object is the only order that is clean on both.
    for name, table, _columns in reversed(_INDEXES):
        if index_exists(session, table, name):
            session.execute(text(f'DROP INDEX IF EXISTS "{name}"'))

    if not _can_alter_column_default(session):
        return

    for table, column, _literal in reversed(_DEFAULTS):
        if not _existing(session, table):
            continue
        if _current_default(session, table, column) is None:
            continue
        session.execute(
            text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT'))
