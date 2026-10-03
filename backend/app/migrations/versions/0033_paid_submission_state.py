"""Upgrade 0033: paid-submission state (Work 15.5).

The critical change is NOT a new column. It is a **backfill of meaning**.

``videos.status`` was documented as ``RENDERING|READY|FAILED``, and
``VideoProductionAgent._resolve_existing`` wrote a deterministic ``FAILED`` when
a submit was interrupted before the engine's task id was persisted. But an
interrupted submit is not a failure: the engine may have accepted and BILLED the
job, and writing FAILED both loses the reference and invites a re-buy.

This migration adds a dedicated ``submission_state`` column carrying the
seven-state paid-job contract, and it backfills existing rows conservatively:

* a row with an ``engine_task_id`` clearly got past submission
* a row with no task id and status FAILED is indistinguishable from a genuine
  rejection, so it is left as-is rather than guessed at

A column is preferred over widening ``status`` because ``status`` is read by
the render pipeline, the API, and the frontend, and those must not start
handling a state they do not understand.

These are money records. The column names, types, defaults and the seven-state
vocabulary are load-bearing: ``UNKNOWN``-shaped states here are what stop a
second purchase, so nothing here may be renamed, retyped or defaulted away.
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, table_exists

_NEW_COLUMNS = (
    # The seven-state paid-job contract from Work 15.5 §7.
    ("submission_state", "VARCHAR(24) NOT NULL DEFAULT 'PREPARED'"),
    # The provider's durable id, when one was confirmed. Kept separate from
    # engine_task_id so a paid AI-provider job is reconcilable independently of
    # the local render engine.
    ("provider_task_id", "VARCHAR(160) NOT NULL DEFAULT ''"),
    # Why the state is what it is, for audit.
    ("submission_detail", "VARCHAR(600) NOT NULL DEFAULT ''"),
    # Sent upstream as the provider's idempotency key so a lost response can be
    # retried WITHOUT buying a second job.
    ("idempotency_key", "VARCHAR(80) NOT NULL DEFAULT ''"),
)

#: (index name, table, columns) -- workspace_id/submission_state verified
#: against ``app.models.Video``: both columns are declared, so this CREATE INDEX
#: resolves on both backends. It is still guarded on table presence because
#: ``CREATE INDEX`` against a missing table is a hard error on PostgreSQL.
_INDEXES = (
    ("ix_video_submission_state", "videos", "workspace_id, submission_state"),
)


def _inspector(session):
    from sqlalchemy import inspect

    # ``Session.bind`` is deprecated in SQLAlchemy 2.x; ``get_bind()`` is the
    # supported accessor and is what ``app.migrations.ddl`` uses too.
    return inspect(session.get_bind())


def _columns(session, table: str) -> set[str]:
    from app.migrations.ddl import columns_of

    return columns_of(session, table)


def upgrade(session) -> None:
    from sqlalchemy import text

    # ``create_all`` runs before migrations, so on a fresh install every column
    # below already exists and this adds nothing; the guard exists for installs
    # that predate the column. It is a catalog read, not a caught error: on
    # PostgreSQL a swallowed duplicate-column error would ABORT the transaction
    # and every later statement in this migration would die with 25P02.
    add_columns_if_missing(session, "videos", list(_NEW_COLUMNS))
    for name, table, columns in _INDEXES:
        if not table_exists(session, table):
            continue
        session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({columns})'))

    if not table_exists(session, "videos"):
        return

    # Backfill, conservatively. A row that has an engine task id demonstrably
    # got past submission, so it is REMOTE_ID_CONFIRMED. Everything else stays
    # PREPARED: guessing FAILED-vs-UNKNOWN from a status we cannot interpret
    # would repeat the exact bug this migration exists to fix.
    session.execute(text(
        "UPDATE videos SET submission_state = 'REMOTE_ID_CONFIRMED' "
        "WHERE submission_state = 'PREPARED' "
        "AND engine_task_id IS NOT NULL AND engine_task_id != ''"))
    session.execute(text(
        "UPDATE videos SET submission_state = 'PROCESSING' "
        "WHERE submission_state = 'REMOTE_ID_CONFIRMED' "
        "AND status = 'RENDERING'"))
    session.execute(text(
        "UPDATE videos SET submission_state = 'SUCCEEDED' "
        "WHERE submission_state IN ('REMOTE_ID_CONFIRMED','PROCESSING') "
        "AND status = 'READY'"))
    session.execute(text(
        "UPDATE videos SET submission_state = 'FAILED' "
        "WHERE submission_state IN ('REMOTE_ID_CONFIRMED','PROCESSING') "
        "AND status = 'FAILED'"))


def _drop_indexes_on(session, table: str, column: str) -> None:
    """Drop every index referencing ``table.column``.

    SQLite refuses ``ALTER TABLE ... DROP COLUMN`` while any index still
    references the column, and the names are not predictable: the ORM's
    ``index=True`` on ``submission_state`` creates ``ix_videos_submission_state``
    while the explicit DDL above creates ``ix_video_submission_state``. Dropping
    only the explicit one makes the downgrade fail outright, so discover them.
    """
    from sqlalchemy import text

    inspector = _inspector(session)
    if table not in inspector.get_table_names():
        return
    for index in inspector.get_indexes(table):
        if column in (index.get("column_names") or ()):
            session.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))


def downgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("DROP INDEX IF EXISTS ix_video_submission_state"))
    # Indexes FIRST: SQLite raises "error in index ... after drop column".
    for name, _ddl_type in _NEW_COLUMNS:
        _drop_indexes_on(session, "videos", name)
    for name, _ddl_type in _NEW_COLUMNS:
        if name in _columns(session, "videos"):
            session.execute(text(f'ALTER TABLE videos DROP COLUMN "{name}"'))
