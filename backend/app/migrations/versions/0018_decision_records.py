"""Upgrade 0018: decision audit log (Work 05 Lane A)."""


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
    for _col in ("extra_json JSON NOT NULL DEFAULT '{}'", "notes TEXT NOT NULL DEFAULT ''"):
        try:
            session.execute(text(f"ALTER TABLE decision_records ADD COLUMN {_col}"))
        except Exception as exc:  # noqa: BLE001 — duplicate-column means applied
            if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                raise
