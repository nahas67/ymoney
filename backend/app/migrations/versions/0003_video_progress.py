"""Upgrade 0003: video render progress column."""

from app.migrations.ddl import add_column_if_missing


def upgrade(session) -> None:
    # Guarded by a catalog read, not by catching the ALTER's failure: on
    # PostgreSQL a failed statement aborts the whole transaction, so the
    # runner would then die with 25P02 on every statement after it.
    add_column_if_missing(session, "videos", "progress", "INTEGER DEFAULT 0")
