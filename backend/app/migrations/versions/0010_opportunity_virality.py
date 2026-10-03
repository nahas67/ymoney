"""Upgrade 0010: virality column on opportunities (breakout potential, informational)."""

from app.migrations.ddl import add_column_if_missing


def upgrade(session) -> None:
    # Catalog-guarded rather than try/except: on PostgreSQL the duplicate-column
    # error would abort the transaction and turn every later statement in this
    # migration into 25P02.
    add_column_if_missing(session, "opportunities", "virality", "FLOAT DEFAULT 0.0")
