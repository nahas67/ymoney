"""Upgrade 0018: decision audit log (Work 05 Lane A)."""

from app.migrations.ddl import add_columns_if_missing


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS decision_records (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL DEFAULT '',
            mode VARCHAR(20) NOT NULL DEFAULT 'SHADOW',
            requested_provider VARCHAR(30) NOT NULL DEFAULT '',
            actual_provider VARCHAR(30) NOT NULL DEFAULT '',
            model VARCHAR(120) NOT NULL DEFAULT '',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            cost_usd FLOAT NOT NULL DEFAULT 0.0,
            fallback_reason VARCHAR(500) NOT NULL DEFAULT '',
            input_json JSON NOT NULL DEFAULT '{}',
            output_json JSON NOT NULL DEFAULT '{}',
            agree BOOLEAN,
            shadow_json JSON NOT NULL DEFAULT '{}',
            extra_json JSON NOT NULL DEFAULT '{}',
            notes TEXT NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_decision_ws_kind
        ON decision_records (workspace_id, kind)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_decision_ws_mode
        ON decision_records (workspace_id, mode)
    """))
    # The old guard was ``try: ALTER ... ADD COLUMN ... except: ignore
    # 'duplicate'``. Harmless on SQLite, which leaves the transaction usable
    # after a failed statement; on PostgreSQL the duplicate-column error ABORTS
    # the transaction and every later statement dies with 25P02. ``create_all``
    # runs before migrations and already builds the current shape, so these two
    # columns are always present on a fresh install -- the ALTER could only
    # ever fail. A catalog check cannot fail, so nothing is caught.
    # ``when_absent_table="raise"`` keeps the old loud behaviour: the previous
    # handler re-raised anything that was not a duplicate/exists error.
    add_columns_if_missing(
        session,
        "decision_records",
        [
            ("extra_json", "JSON NOT NULL DEFAULT '{}'"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
        ],
        when_absent_table="raise",
    )