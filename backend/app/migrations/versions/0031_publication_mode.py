"""Upgrade 0031: Work 14 publication mode (LIVE / MOCK / HANDOFF / UNAVAILABLE).

Before this, a publication was recorded as ``published_posts.is_mock``
(boolean), so a *user handoff* (media prepared, a human publishes it) was
indistinguishable from a live autonomous post. That is exactly the confusion
Work 14 §5/§9 forbids: Snapchat handoff must never be reported as PUBLISHED.

Adds to ``published_posts``:
  * ``publication_mode`` -- LIVE | MOCK | HANDOFF | UNAVAILABLE.
  * ``handoff_payload``  -- JSON: where the prepared media lives, the share
    link, and the instructions the operator follows.
  * ``handoff_completed_at`` -- when the user reported finishing the publish.

Every step is guarded by catalog reads, so a fresh install (create_all first)
and an already-migrated install both replay as no-ops. The backfill is
conservative and lossless: existing rows are LIVE when they carry a remote id
and are not mock, MOCK otherwise. No existing column is dropped or narrowed, and
``is_mock`` is KEPT (existing readers still use it) rather than being replaced,
because dropping it would break every current caller for no gain.
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, has_column, table_exists

MODES = ("LIVE", "MOCK", "HANDOFF", "UNAVAILABLE")


def upgrade(session) -> None:
    if not table_exists(session, "published_posts"):
        return  # fresh install: create_all already made the full table

    from sqlalchemy import text

    # ``handoff_completed_at`` was spelled ``DATETIME``. That is a SQLite-only
    # type name: SQLite gives it NUMERIC affinity like any unrecognised type,
    # but PostgreSQL has no ``datetime`` type at all and raises 42704 ("type
    # datetime does not exist") the moment this ALTER actually runs -- which is
    # exactly the upgrade path this migration exists for. ``TIMESTAMP`` is what
    # ``create_all`` already builds on both backends and carries the same NUMERIC
    # affinity in SQLite, so the resulting schema is unchanged either way.
    add_columns_if_missing(
        session,
        "published_posts",
        [
            ("publication_mode", "VARCHAR(12) DEFAULT 'LIVE'"),
            ("handoff_payload", "JSON"),
            ("handoff_completed_at", "TIMESTAMP"),
        ],
        when_absent_table="raise",
    )

    # Backfill: a row with a remote id that is not mock is LIVE; everything
    # else is MOCK. UNKNOWN/absent remote ids must not claim LIVE.
    session.execute(text(
        "UPDATE published_posts SET publication_mode = "
        "CASE WHEN is_mock THEN 'MOCK' "
        "WHEN remote_post_id IS NOT NULL AND remote_post_id <> '' "
        "THEN 'LIVE' ELSE 'UNAVAILABLE' END "
        "WHERE publication_mode IS NULL"))
    # Rows that were created without going through the new path (is_mock NULL)
    # are MOCK, not LIVE: a missing mock flag is not evidence of a live post.
    session.execute(text(
        "UPDATE published_posts SET publication_mode = 'MOCK' "
        "WHERE publication_mode IS NULL"))


def downgrade(session) -> None:
    from sqlalchemy import text

    for name in ("publication_mode", "handoff_payload", "handoff_completed_at"):
        if has_column(session, "published_posts", name):
            session.execute(text(
                f'ALTER TABLE published_posts DROP COLUMN "{name}"'))