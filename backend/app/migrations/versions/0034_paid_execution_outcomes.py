"""Upgrade 0034: structural paid-execution and cost outcomes (Work 15.8 §6/§7).

Three facts about one paid operation used to be recorded in two, or in JSON:

1. **lip-sync kept its money story in ``cost_json``.** An operator asking "which
   lip-sync jobs may already have been billed?" had to fetch every row and parse
   arbitrary JSON, because ``status`` (the business vocabulary the render
   pipeline and the UI both read) cannot carry an ambiguity without breaking
   every reader of it. So lip-sync gets two ADDITIVE columns holding the
   CANONICAL vocabularies already in the codebase:
   ``lipsync_jobs.execution_outcome`` (``SubmissionState``) and
   ``lipsync_jobs.cost_outcome`` (``CostOutcome``).

2. **the render lane had no structural cost outcome.** ``videos.submission_state``
   already *is* the structural execution outcome, so this migration does NOT add
   a second one -- two spellings of "may have been billed" is how two dashboards
   disagree. What it adds is ``videos.cost_outcome`` (the money half), plus
   ``videos.submission_operation_id`` and ``videos.submission_attempted_at``,
   which make the record correlatable: before this, nothing linked a Video row
   to the ledger row for the same submit, so a crash between the two writes made
   the pairing unrecoverable, and ``created_at`` (row creation) was standing in
   for "the request actually left".

Nothing here guesses. Rows that predate these columns keep empty values: an empty
``cost_outcome`` means "this operation was recorded before the distinction
existed", and rewriting it to ``NOT_APPLICABLE`` would claim a knowledge nobody
had. A migration that invents a value is a migration that can be wrong.

Reversible: the downgrade discovers and drops every index referencing each
column BEFORE the column, because SQLite refuses ``DROP COLUMN`` while an index
still names it (``versions/0033`` found this the hard way).
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, table_exists

#: (table, column, DDL type) for every column this migration adds.
#:
#: MONEY RECORDS. The names, widths, defaults and the empty-string-means-
#: unknown convention are load-bearing: ``cost_outcome == ''`` is how a row says
#: "this was recorded before the distinction existed", and
#: ``execution_outcome == ''`` is how lip-sync says the same thing. Do not
#: rename, re-type, or default any of these to a non-empty value.
_NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # videos -- the render lane's durable paid-submission record (Work 15.8 §6).
    ("videos", "cost_outcome", "VARCHAR(24) NOT NULL DEFAULT ''"),
    ("videos", "submission_operation_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("videos", "submission_attempted_at", "TIMESTAMP NULL"),
    # lipsync_jobs -- structural separation of business/execution/cost (15.8 §7).
    ("lipsync_jobs", "execution_outcome", "VARCHAR(24) NOT NULL DEFAULT ''"),
    ("lipsync_jobs", "cost_outcome", "VARCHAR(24) NOT NULL DEFAULT ''"),
)

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Explicit composite indexes for the two questions an operator actually
    # asks: "what may have been billed, workspace X?" and "what is this one
    # operation's record?". The single-column ORM `index=True` names are created
    # separately and are discovered on downgrade. Every column here was checked
    # against the ORM (``Video.workspace_id``/``cost_outcome``/
    # ``submission_operation_id``, ``LipSyncJob.workspace_id``/
    # ``execution_outcome``/``cost_outcome``): SQLite never validates the column
    # list of an existing index name, so a wrong spelling would stay hidden
    # there while PostgreSQL raises 42703.
    ("ix_video_cost_outcome", "videos", ("workspace_id", "cost_outcome")),
    ("ix_video_submission_operation", "videos", ("submission_operation_id",)),
    ("ix_lipsync_execution_outcome", "lipsync_jobs",
     ("workspace_id", "execution_outcome")),
    ("ix_lipsync_cost_outcome", "lipsync_jobs", ("workspace_id", "cost_outcome")),
)


def _columns(session, table: str) -> set[str]:
    from app.migrations.ddl import columns_of

    return columns_of(session, table)


def upgrade(session) -> None:
    from sqlalchemy import text

    # The old guard was ``if table not in _columns(session, table): continue`` --
    # it compared a TABLE name against the table's COLUMN names, which is never
    # true, so it skipped every column and every index below on every database.
    # ``create_all`` runs before migrations and hides that on a fresh install,
    # but on any install that actually needs these columns the paid-record
    # columns were silently never created. Guard on table presence, and guard
    # each column on a catalog read rather than by catching the duplicate error
    # (on PostgreSQL a swallowed error ABORTS the transaction and every later
    # statement dies with 25P02).
    for table, name, ddl_type in _NEW_COLUMNS:
        if table_exists(session, table):
            add_columns_if_missing(session, table, [(name, ddl_type)])
    for name, table, cols in _INDEXES:
        if not table_exists(session, table):
            continue  # table absent (a trimmed deployment); nothing to extend
        session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{name}" '
            f'ON "{table}" ({", ".join(cols)})'))


def _drop_indexes_on(session, table: str, column: str) -> None:
    """Drop every index referencing ``table.column``.

    SQLite raises ``error in index ... after drop column``, and the names are not
    predictable: the ORM's ``index=True`` on ``cost_outcome`` creates
    ``ix_videos_cost_outcome`` while the explicit DDL above creates
    ``ix_video_cost_outcome``. Dropping only the known one makes the downgrade
    fail outright, so discover them.
    """
    from sqlalchemy import inspect, text

    if not table_exists(session, table):
        return
    for index in inspect(session.get_bind()).get_indexes(table):
        if column in (index.get("column_names") or ()):
            session.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))


def downgrade(session) -> None:
    from sqlalchemy import text

    for name, _table, _cols in _INDEXES:
        session.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    # Indexes FIRST, then columns.
    for table, name, _ddl_type in _NEW_COLUMNS:
        _drop_indexes_on(session, table, name)
    for table, name, _ddl_type in _NEW_COLUMNS:
        if name in _columns(session, table):
            session.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{name}"'))