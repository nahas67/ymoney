"""Upgrade 0008: one credential row per (provider, scope).

ApiCredential lookups resolve the newest row for a key, so duplicate rows
created before any write-time dedupe made reads ambiguous. This migration
collapses existing duplicates (newest wins) and adds partial unique indexes
so the database itself prevents the ambiguity from returning: one row per
provider inside each workspace, and one global (workspace_id IS NULL) row
per provider. Partial indexes work identically on SQLite and Postgres.
"""

from sqlalchemy import text


def upgrade(session) -> None:
    # Collapse duplicates in deterministic newest-first order so the index
    # creation below cannot fail on an older installation.
    rows = session.execute(
        text(
            "SELECT id, provider, workspace_id FROM api_credentials "
            "ORDER BY provider, workspace_id, updated_at DESC, created_at DESC"
        )
    ).fetchall()
    seen: set[tuple[str, str | None]] = set()
    doomed: list[str] = []
    for row_id, provider, workspace_id in rows:
        key = (provider, workspace_id)
        if key in seen:
            doomed.append(row_id)
        else:
            seen.add(key)
    for row_id in doomed:
        session.execute(
            text("DELETE FROM api_credentials WHERE id = :id"), {"id": row_id}
        )

    # NULLs are distinct in both SQLite and Postgres unique semantics, so the
    # global scope needs its own partial index rather than a plain column set.
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_credentials_provider_ws "
            "ON api_credentials (provider, workspace_id) "
            "WHERE workspace_id IS NOT NULL"
        )
    )
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_credentials_provider_global "
            "ON api_credentials (provider) WHERE workspace_id IS NULL"
        )
    )
