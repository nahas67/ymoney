"""Upgrade 0017: intelligence evidence tables (Work 05 Lane C).

browser_runs + evidence_records. Additive only; guarded so replays and
parallel-lane migrations stay no-ops.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS browser_runs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            goal TEXT NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
            steps_json JSON NOT NULL DEFAULT '[]',
            evidence_json JSON NOT NULL DEFAULT '{}',
            cost_usd FLOAT NOT NULL DEFAULT 0.0
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_browser_runs_ws
        ON browser_runs (workspace_id)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence_records (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind VARCHAR(30) NOT NULL DEFAULT '',
            subject_id VARCHAR(36) NOT NULL DEFAULT '',
            execution_status VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN',
            verification_status VARCHAR(20) NOT NULL DEFAULT 'NOT_VERIFIED',
            checks_json JSON NOT NULL DEFAULT '[]',
            digest VARCHAR(64) NOT NULL DEFAULT '',
            prev_digest VARCHAR(64) NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_evidence_records_ws
        ON evidence_records (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_evidence_ws_kind
        ON evidence_records (workspace_id, kind)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_evidence_subject
        ON evidence_records (subject_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_evidence_records_status
        ON evidence_records (verification_status)
    """))
