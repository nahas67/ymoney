"""Upgrade 0011: workspace-scoped third-party API keys (hash-only storage)."""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS workspace_api_keys (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(120) NOT NULL DEFAULT '',
            prefix VARCHAR(16) NOT NULL,
            key_hash VARCHAR(128) NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            revoked BOOLEAN NOT NULL DEFAULT FALSE,
            last_used_at TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_workspace_api_keys_workspace
        ON workspace_api_keys (workspace_id, revoked)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_workspace_api_keys_hash
        ON workspace_api_keys (key_hash)
    """))
