"""Upgrade 0020: creative experiments table (Work 06 Lane B).

Reserves 0019 for Lane A performance models; this lane owns 0020 only.
Idempotent: CREATE TABLE IF NOT EXISTS + guarded index.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS experiments (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL DEFAULT 'HOOK',
            hypothesis TEXT NOT NULL DEFAULT '',
            control_json JSON NOT NULL DEFAULT '{}',
            variants_json JSON NOT NULL DEFAULT '[]',
            platform VARCHAR(30) NOT NULL DEFAULT '',
            primary_metric VARCHAR(60) NOT NULL DEFAULT 'views',
            secondary_metrics JSON NOT NULL DEFAULT '[]',
            minimum_sample INTEGER NOT NULL DEFAULT 60,
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            result_json JSON NOT NULL DEFAULT '{}',
            confidence VARCHAR(40) NOT NULL DEFAULT '',
            started_at TIMESTAMP,
            ended_at TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_experiments_ws_status
        ON experiments (workspace_id, status)
    """))
