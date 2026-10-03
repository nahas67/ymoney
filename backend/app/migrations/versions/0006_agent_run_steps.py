"""Upgrade 0006: agent-run step tracing (subagent decomposition visibility)."""

from app.migrations.ddl import add_column_if_missing


def upgrade(session) -> None:
    # Catalog-guarded, never try/except: catching the duplicate-column error
    # would abort the transaction on PostgreSQL (25P02) for the rest of the
    # migration. ``JSON`` is a real type on PostgreSQL and an accepted
    # affinity on SQLite, so one definition serves both.
    add_column_if_missing(session, "agent_runs", "steps_json", "JSON")
