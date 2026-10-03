"""Upgrade 0035: job leases — who owns a running job, and until when (Work 16 §2).

The queue had no owner. ``jobs.status = 'RUNNING'`` said only *that* a worker
had taken a job, never *which* worker or *for how long*, so a second process
could not tell two situations apart::

    worker A is rendering this 30-minute job right now
    worker A died holding this job and nobody will ever finish it

Both look identical in the table, and ``services.jobs.recover_orphans``
resolved the ambiguity the worst way available: on startup it flipped **every**
``RUNNING`` row to ``RETRYING``. One worker restarting for an unrelated deploy
was enough to put another worker's live, possibly-already-billed render back on
the queue. Nothing in the schema made the distinction recoverable.

Three columns make it recoverable, and they are all evidence rather than
optimistic bookkeeping:

``claimed_by``
    The worker identity holding the lease. An operator can ask "what is worker
    ``w-3`` doing" and get an answer from the queue itself.

``lease_expires_at``
    The only thing that may authorise a steal. A live worker renews it; a dead
    one cannot, so expiry is proof of absence rather than a guess.

``heartbeat_at``
    When the holder last proved it was alive. Distinct from ``claimed_at``
    because "how long have you had it" and "are you still there" are different
    questions, and collapsing them loses the second one.

``claimed_at``
    When the lease was taken. Paired with the pre-existing ``started_at`` this
    is what makes ``QUEUED -> CLAIMED -> RUNNING`` two real, separately
    observable states instead of one blur: a lease held with ``started_at``
    still NULL means the worker has the job but the handler has not entered yet.

Nothing is invented here. A ``RUNNING`` row that predates this migration has
``lease_expires_at IS NULL``, and that NULL is read as "nobody can renew this,
so it is recoverable" — the same treatment it got before, and no worse. It is
NOT backfilled with a far-future timestamp: that would mark every pre-existing
in-flight job as permanently un-reclaimable and leak it.

Reversible: the downgrade discovers and drops every index referencing each new
column BEFORE the column, because SQLite refuses ``DROP COLUMN`` while an index
still names it (versions/0033 found this the hard way).
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, table_exists

_TABLE = "jobs"

#: (column, DDL type) for every column this migration adds. ``claimed_by``
#: defaults to the empty string rather than NULL for the same reason the rest of
#: the schema does: ``''`` reads as "no worker holds this" in a WHERE clause and
#: in an operator's dashboard, whereas NULL has to be COALESCEd every time.
_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("claimed_by", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("claimed_at", "TIMESTAMP NULL"),
    ("lease_expires_at", "TIMESTAMP NULL"),
    ("heartbeat_at", "TIMESTAMP NULL"),
)

#: Explicit composite indexes for the two questions the recovery path asks on
#: every sweep. ``ix_jobs_lease_recovery`` serves the sweep itself
#: (``status='RUNNING' AND lease_expires_at <= now``); ``ix_jobs_claimed_by``
#: serves "what is this worker holding", which is an operational question a
#: lease-expiry index cannot answer.
_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ix_jobs_lease_recovery", ("status", "lease_expires_at")),
    ("ix_jobs_claimed_by", ("claimed_by",)),
)


def _columns(session, table: str = _TABLE) -> set[str]:
    """Column names of ``table``, read on the SESSION'S OWN connection.

    ``session.get_bind()`` hands back the Engine, and ``inspect(engine)`` opens
    a *second* connection. That is fatal mid-migration on PostgreSQL: the
    session's transaction holds the ACCESS EXCLUSIVE lock taken by the ALTER
    just executed, the new connection's catalog read blocks behind it, and the
    two wait for each other until the lock timeout. Binding the inspector to
    ``session.connection()`` keeps every read in the same transaction as the
    DDL, which is the only order that terminates.
    """
    from sqlalchemy import inspect

    if table not in inspect(session.connection()).get_table_names():
        return set()
    return {c["name"] for c in inspect(session.connection()).get_columns(table)}


def _indexes_on(session, columns: tuple[str, ...]) -> list[str]:
    """Every index on ``jobs`` that names one of ``columns``.

    Discovered, not hardcoded: the ORM's ``index=True`` on ``claimed_by``
    creates ``ix_jobs_claimed_by`` while the composite in ``__table_args__``
    creates ``ix_jobs_lease_recovery``, and dropping only the known ones makes
    the downgrade fail outright on SQLite. Read ALL of them up front, before
    any DDL, because every catalog read after the first DROP would otherwise be
    another round trip inside a transaction holding locks.
    """
    from sqlalchemy import inspect

    if _TABLE not in inspect(session.connection()).get_table_names():
        return []
    found: list[str] = []
    for index in inspect(session.connection()).get_indexes(_TABLE):
        if any(col in (index.get("column_names") or ()) for col in columns):
            found.append(str(index["name"]))
    return found


def upgrade(session) -> None:
    from sqlalchemy import text

    # ``create_all`` builds the CURRENT schema before migrations run, so on a
    # fresh install these columns already exist. That is a no-op, not an error;
    # the catalog read is what makes it one, and it is correct on both backends
    # (see app/migrations/ddl.py for why catching the duplicate-column error was
    # a trap on PostgreSQL: the swallowed failure aborts the transaction and
    # every later statement dies with 25P02).
    if table_exists(session, _TABLE):
        add_columns_if_missing(session, _TABLE, list(_NEW_COLUMNS))
    for name, cols in _INDEXES:
        if not table_exists(session, _TABLE):
            continue  # a trimmed deployment has no jobs table to extend
        session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{name}" '
            f'ON "{_TABLE}" ({", ".join(cols)})'))


def downgrade(session) -> None:
    from sqlalchemy import text

    # Read the catalog ONCE, before touching anything: which columns exist, and
    # which indexes reference them.
    existing = _columns(session)
    names = tuple(name for name, _ddl in _NEW_COLUMNS if name in existing)
    index_names = _indexes_on(session, names)

    # Indexes FIRST, then columns. Never the other way round: SQLite raises
    # ``error in index ... after drop column`` and PostgreSQL drops the index
    # silently, leaving either a broken downgrade or a leaked index.
    for name in sorted(set(index_names)):
        session.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    for name in names:
        session.execute(text(f'ALTER TABLE "{_TABLE}" DROP COLUMN "{name}"'))