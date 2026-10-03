"""Upgrade 0009: add request_id columns for correlation tracing."""

from app.migrations.ddl import add_column_if_missing


def upgrade(session) -> None:
    # Single-quoted string literal: double quotes are identifiers in
    # PostgreSQL, so DEFAULT "" is a certain PG failure (W11.5 F1).
    #
    # Guarded by a catalog read, not try/except -- on PostgreSQL the swallowed
    # duplicate-column error would abort the transaction (25P02) and take every
    # subsequent statement down with it.
    for table in ("agent_runs", "events"):
        add_column_if_missing(session, table, "request_id", "VARCHAR(32) DEFAULT ''")
