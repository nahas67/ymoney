"""Upgrade 0037: budget ROLLUP limits -- the cross-category ceilings.

Work 16 §11, and the gap Work 15.9 named: **"there is no cross-category
rollup, so total daily spend across categories can exceed any single cap."**
Every cap in the schema before this migration answers "may this CATEGORY
afford this?" and none of them answers "may this WORKSPACE afford this?".
Those are different questions. A workspace with ``daily_budget_usd=5`` for
llm and five more for tts has spent ten dollars against a limit that reads
five, and nothing anywhere can see that.

``budget_rollup_limits``
    One row per scope. ``scope='workspace'`` with a ``workspace_id`` is that
    tenant's cross-category ceiling; ``scope='system'`` with an EMPTY
    ``workspace_id`` is YMONEY's own ceiling over every row in
    ``cost_entries``. One table rather than two because the two are the same
    question asked at a different altitude, and a second table would invite a
    second implementation of the arithmetic.

    Both caps are NULLABLE, and NULL is the load-bearing value: **NULL means
    NOT CONFIGURED**, which is what makes the feature opt-in. A deployment that
    never writes a row keeps the pre-0037 behaviour exactly -- there is no
    default ceiling hiding in a column default, no migration backfill inventing
    a number no operator chose, and no silent tightening of a live budget. A
    cap of ``0.0`` means something different and is honoured as such: zero is a
    real ceiling, so it refuses everything.

    ``enabled=False`` is a third spelling of the same idea for an operator who
    wants to switch a ceiling off WITHOUT deleting the row -- the value stays on
    disk for the audit, the ceiling stops being enforced. It is a column rather
    than a delete because a budget you silently deleted is a budget you can no
    longer prove you had.

The hierarchy this table completes is

    global/system -> workspace total -> category -> operation

and every level is enforced by :func:`app.services.cost.reserve_spend` in ONE
transaction, under the workspace lock. No level replaces another: the category
caps and the per-call cap that Work 15.7 shipped are untouched, and a
reservation must satisfy every level that happens to be configured.

No foreign key on ``workspace_id``, for the same reason ``storage_objects`` and
``gpu_reservations`` do not declare one: a cap row is policy and audit history,
and it must be able to outlive a tenant deletion so "that workspace had a $50
ceiling on the day it was removed" stays answerable. The ORM would declare the
FK for new databases; this DDL does not, and the mismatch is deliberate.

Reversible: the downgrade DISCOVERS every index naming a dropped column and
drops them before the table, which is the only order that works on both SQLite
and PostgreSQL (see 0034, whose downgrade used to fail on SQLite with
``error in index ... after drop column``).
"""

from __future__ import annotations

# NOTE: this migration deliberately does NOT use ``app.migrations.ddl``'s
# helpers. They probe the catalog through ``session.get_bind()`` -- the ENGINE,
# hence a different connection -- which cannot see the DDL this very transaction
# has not committed. For a table ``create_all`` already created that is
# invisible; for a table this migration owns it means every column and index is
# skipped and the migration records itself as applied against a bare table.
# ``_own_connection`` below is the whole fix.

#: The table this migration owns. Named once so upgrade and downgrade cannot
#: disagree about it.
_TABLE = "budget_rollup_limits"

#: (table, column, DDL type) for every column, so a deployment whose table
#: predates a later revision of this migration still converges. ``FLOAT`` and
#: ``NULL`` are used deliberately: a cap column that could not be NULL would
#: need a sentinel, and every sentinel is one forgotten comparison away from
#: reading as "unlimited".
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "VARCHAR(36) NOT NULL PRIMARY KEY"),
    ("created_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("scope", "VARCHAR(20) NOT NULL DEFAULT 'workspace'"),
    ("workspace_id", "VARCHAR(36) NOT NULL DEFAULT ''"),
    ("daily_total_cap", "FLOAT NULL"),
    ("monthly_total_cap", "FLOAT NULL"),
    # ``TRUE``, not ``1``: PostgreSQL refuses an integer default expression for a
    # boolean column (42P08 DatatypeMismatch) and this table is NOT on
    # ``Base.metadata``, so this DDL is the only thing that creates it and the
    # error cannot be hidden behind a ``create_all``.
    ("enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("meta_json", "JSON NOT NULL DEFAULT '{}'"),
)

#: ``(scope, workspace_id)`` is unique so a tenant cannot end up with two rows
#: claiming different ceilings, where which one wins would be a race rather than
#: a policy. The system row is just the ``scope='system', workspace_id=''`` case
#: of the same key -- no second table, no second code path.
_INDEXES: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("ix_budget_rollup_scope", _TABLE, ("scope", "workspace_id"), True),
    ("ix_budget_rollup_ws", _TABLE, ("workspace_id", "scope"), False),
)


def _own_connection(session):
    """The session's OWN connection, for catalog reads.

    **This is not the same as ``session.get_bind()``, and the difference is the
    whole bug this helper exists to avoid.** ``get_bind()`` returns the ENGINE,
    so ``inspect(engine)`` opens a *different* connection from the pool -- one
    that cannot see uncommitted DDL from the transaction that is creating the
    table. That is invisible for a table ``create_all`` already committed (0036
    gets away with it) and fatal for a table this migration owns: the CREATE
    TABLE succeeds, the very next catalog read says "table absent", every column
    and index is skipped with a warning, and the migration records itself as
    applied against a table with no indexes at all. On SQLite the same mistake
    happens to work, because the legacy ``isolation_level=""`` mode runs DDL in
    autocommit -- so the bug is one that only shows on PostgreSQL and only for
    migration-owned tables.

    ``session.connection()`` returns the connection the transaction is running
    on, so it sees its own DDL on every backend.
    """
    return session.connection()


def _table_present(session) -> bool:
    from sqlalchemy import inspect

    return _TABLE in inspect(_own_connection(session)).get_table_names()


def _columns_present(session) -> set[str]:
    from sqlalchemy import inspect

    if not _table_present(session):
        return set()
    return {str(c["name"]) for c in
            inspect(_own_connection(session)).get_columns(_TABLE)}


def _index_names(session) -> set[str]:
    from sqlalchemy import inspect

    if not _table_present(session):
        return set()
    return {str(i["name"]) for i in
            inspect(_own_connection(session)).get_indexes(_TABLE)}


def _add_column_if_missing(session, column: str, ddl_type: str) -> bool:
    """``ALTER TABLE ... ADD COLUMN`` exactly once, on any backend.

    A local helper rather than ``ddl.add_column_if_missing`` for the reason
    :func:`_own_connection` documents: the shared one probes through the engine,
    which cannot see this transaction's own table. It also cannot be extended
    with a connection parameter here without editing shared infrastructure this
    change does not own.
    """
    from sqlalchemy import text

    if column in _columns_present(session):
        return False
    session.execute(text(f'ALTER TABLE "{_TABLE}" ADD COLUMN "{column}" {ddl_type}'))
    return True


def _create_index_if_missing(session, index: str, columns: str, *,
                             unique: bool = False) -> bool:
    from sqlalchemy import text

    if index in _index_names(session):
        return False
    uniq = "UNIQUE " if unique else ""
    session.execute(text(f'CREATE {uniq}INDEX IF NOT EXISTS "{index}" '
                         f'ON "{_TABLE}" ({columns})'))
    return True


def _create_table(session) -> None:
    """``CREATE TABLE IF NOT EXISTS budget_rollup_limits``.

    ``create_all`` runs before migrations, but this table is NOT on
    ``Base.metadata`` (see the module docstring: the migration owns the DDL and
    ``services.budget_rollup`` owns the access, so no ORM model and no
    ``models/ops.py`` edit is required). The catalog check is therefore the only
    thing that makes this idempotent, and reading the catalog cannot fail -- on
    PostgreSQL a caught duplicate-``CREATE`` poisons the transaction and every
    later statement dies with 25P02.
    """
    from sqlalchemy import text

    if _table_present(session):
        return
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS budget_rollup_limits ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY,"
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "scope VARCHAR(20) NOT NULL DEFAULT 'workspace',"
        "workspace_id VARCHAR(36) NOT NULL DEFAULT '',"
        "daily_total_cap FLOAT NULL,"
        "monthly_total_cap FLOAT NULL,"
        "enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        "meta_json JSON NOT NULL DEFAULT '{}')"))


def upgrade(session) -> None:
    _create_table(session)
    for column, ddl_type in _COLUMNS:
        if column == "id":
            continue  # part of the PRIMARY KEY; never bolted on
        _add_column_if_missing(session, column, ddl_type)
    for name, _table, cols, unique in _INDEXES:
        _create_index_if_missing(session, name, ", ".join(cols), unique=unique)


def _indexes_on(session, table: str, columns: tuple[str, ...]) -> list[str]:
    """Every index on ``table`` naming one of ``columns``. Discovered, not listed.

    Read UP FRONT, before any DDL: the single-column ORM ``index=True`` family and
    this migration's explicit names are created by different code paths, and a
    downgrade that drops only the ones it knows leaves an object behind on SQLite
    and dies on the next column change.
    """
    from sqlalchemy import inspect

    if not _table_present(session):
        return []
    found: list[str] = []
    for index in inspect(_own_connection(session)).get_indexes(table):
        if any(col in (index.get("column_names") or ()) for col in columns):
            found.append(str(index["name"]))
    return found


def downgrade(session) -> None:
    """Indexes FIRST, then the table. Never the other way round.

    SQLite refuses to drop a column an index still names, and PostgreSQL keeps a
    dangling index happily; the only order that is clean on both is
    index-then-table.

    Catalog reads go through :func:`_own_connection` here too: a downgrade that
    enumerated indexes through the engine would not see the ones this very
    transaction created.
    """
    from sqlalchemy import text

    present = _index_names(session)
    for name, _table, cols, _unique in _INDEXES:
        for candidate in {name, *_indexes_on(session, _TABLE, cols)}:
            if candidate in present:
                session.execute(text(f'DROP INDEX IF EXISTS "{candidate}"'))
    # Any index the discovery above did not enumerate (an operator's own, say)
    # still names a dropped column, so sweep the table before dropping it.
    for extra in present:
        session.execute(text(f'DROP INDEX IF EXISTS "{extra}"'))
    if _table_present(session):
        session.execute(text(f'DROP TABLE IF EXISTS "{_TABLE}"'))
