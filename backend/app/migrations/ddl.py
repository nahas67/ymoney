"""Backend-portable DDL helpers for migrations.

Why this exists
---------------
The migration corpus grew on SQLite, where a failed statement leaves the
transaction usable. It does NOT work that way on PostgreSQL: **any** failed
statement aborts the whole transaction, and every later command fails with
``25P02 current transaction is aborted, commands ignored until end of
transaction block``.

The original guard was::

    try:
        session.execute(text(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}"))
    except Exception:
        if "duplicate" not in str(exc).lower():
            raise

On SQLite that is harmless. On PostgreSQL the ``ALTER`` fails (the column
already exists, because ``create_all`` builds the *current* schema before
migrations run), the handler swallows it, and the transaction is left ABORTED.
The very next statement -- another ``ALTER``, a ``CREATE INDEX``, a
``CREATE TABLE`` -- then dies with 25P02 and the migration is permanently
stuck. The error that actually matters is never the one reported.

The fix is not a better ``except``; it is to **ask instead of guess**.
Reading the catalog cannot fail, so nothing is ever caught, so no transaction
is ever poisoned. The same code is correct on both backends.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _dialect(session: Session) -> str:
    bind = session.get_bind()
    return (bind.dialect.name if bind is not None else "")


def _inspector(session: Session):
    """Inspect the session's OWN connection, never the engine.

    ``session.get_bind()`` returns the **engine**, so ``inspect(engine)`` opens a
    *different* connection from the pool -- one that cannot see uncommitted DDL
    from the transaction that is creating the table right now. The failure is
    silent and severe: the ``CREATE TABLE`` succeeds, the very next catalog read
    says "table absent", every column and index is skipped with a warning, and
    the migration still records itself as applied against a table with no
    indexes at all.

    It hides on SQLite because the legacy ``isolation_level=""`` mode runs DDL
    in autocommit, and it hides whenever ``create_all`` already committed the
    table first. It bites on PostgreSQL, and only for tables a migration owns.

    ``session.connection()`` is the connection the transaction is actually
    running on, so it sees its own DDL on every backend.
    """

    return inspect(session.connection())


def table_exists(session: Session, table: str) -> bool:
    """Whether ``table`` is present. Reads the catalog; never raises."""
    return table in _inspector(session).get_table_names()


def columns_of(session: Session, table: str) -> set[str]:
    """Column names of ``table`` (empty set when the table is absent)."""
    insp = _inspector(session)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def has_column(session: Session, table: str, column: str) -> bool:
    """Whether ``table.column`` exists.

    ``case_insensitive`` matters because SQLite folds identifiers to lower
    case while PostgreSQL folds them to lower too but the model layer may have
    created a mixed-case column on some backend. Both spellings are compared
    so a migration behaves identically regardless of who created the column.
    """
    existing = columns_of(session, table)
    if not existing:
        return False
    return column in existing or column.lower() in {e.lower() for e in existing}


def index_exists(session: Session, table: str, index: str) -> bool:
    insp = _inspector(session)
    if table not in insp.get_table_names():
        return False
    return index in {i["name"] for i in insp.get_indexes(table)}


def constraint_exists(session: Session, table: str, constraint: str) -> bool:
    """Whether a UNIQUE / PRIMARY KEY constraint is present on ``table``."""
    insp = _inspector(session)
    if table not in insp.get_table_names():
        return False
    names = {u.get("name") for u in insp.get_unique_constraints(table)}
    names |= {pk.get("name") for pk in insp.get_pk_constraint(table) or []}
    names.discard(None)
    return constraint in names


def add_column_if_missing(session: Session, table: str, column: str, ddl: str,
                          *, when_absent_table: str = "skip") -> bool:
    """``ALTER TABLE ... ADD COLUMN`` exactly once, on any backend.

    ``ddl`` is the column definition *without* the column name, e.g.
    ``"VARCHAR(36)"`` or ``"JSON NOT NULL DEFAULT '{}'"``.

    Returns True when the column was added, False when it was already there.

    ``when_absent_table`` decides what to do if the table itself is missing:

    ``"skip"``
        Do nothing and log a warning. This is the safe default for a migration
        that runs after ``create_all``: on a fresh database the table is
        always present, and a missing table means the migration is being
        replayed against a partial schema where inventing a column would be
        worse than skipping it.
    ``"raise"``
        Fail loudly. Use when the table must exist.
    """
    if not table_exists(session, table):
        if when_absent_table == "raise":
            raise RuntimeError(
                f"migration expected table {table!r} to exist, but it does not")
        logger.warning("skipping column %s.%s: table absent", table, column)
        return False

    if has_column(session, table, column):
        return False

    session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
    logger.info("added column %s.%s", table, column)
    return True


def add_columns_if_missing(session: Session, table: str,
                           columns: list[tuple[str, str]],
                           *, when_absent_table: str = "skip") -> list[str]:
    """Batch form of :func:`add_column_if_missing`; returns columns added."""
    added: list[str] = []
    for column, ddl in columns:
        if add_column_if_missing(session, table, column, ddl,
                                 when_absent_table=when_absent_table):
            added.append(column)
    return added


def create_index_if_missing(session: Session, index: str, table: str,
                            columns: str, *, unique: bool = False) -> bool:
    """``CREATE INDEX IF NOT EXISTS`` with an explicit existence check.

    ``IF NOT EXISTS`` alone is not enough: it is a no-op on both backends but
    it also silently does nothing when the index exists with a DIFFERENT
    definition, which is exactly the case worth noticing. The check makes the
    skip observable in the log.
    """
    if index_exists(session, table, index):
        return False
    if not table_exists(session, table):
        logger.warning("skipping index %s: table %s absent", index, table)
        return False
    uniq = "UNIQUE " if unique else ""
    session.execute(
        text(f'CREATE {uniq}INDEX IF NOT EXISTS "{index}" '
             f'ON "{table}" ({columns})'))
    logger.info("created index %s on %s", index, table)
    return True


def table_sql_type_for_json() -> str:
    """The JSON column type for the current backend.

    SQLite has no JSON type; PostgreSQL does. Both accept a plain-text default
    (``'{}'``/``'[]'``) so the definitions can be shared -- this exists so a
    migration that must branch has one honest place to branch.
    """
    return "JSON"
