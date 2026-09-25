"""Upgrade 0012: outbound webhook subscriptions (encrypted signing secrets)."""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            url VARCHAR(2000) NOT NULL DEFAULT '',
            secret_enc TEXT NOT NULL DEFAULT '',
            events_json JSON NOT NULL DEFAULT '[]',
            active BOOLEAN NOT NULL DEFAULT 1
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_webhook_subscriptions_workspace
        ON webhook_subscriptions (workspace_id, active)
    """))
