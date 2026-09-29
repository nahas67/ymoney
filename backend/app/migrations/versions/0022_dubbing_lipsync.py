"""Upgrade 0022: dubbing plans + lip-sync jobs (Work 07 Lane B).

Lane A owns 0021; this lane owns 0022 only. Append-only and idempotent:
CREATE TABLE IF NOT EXISTS + guarded indexes, so it replays as a no-op even
when the ORM already created the tables (Base.metadata.create_all runs first
in the runner).
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS dubbing_plans (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            source_ref VARCHAR(512) NOT NULL DEFAULT '',
            target_language VARCHAR(16) NOT NULL DEFAULT '',
            plan_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            needs_review BOOLEAN NOT NULL DEFAULT FALSE,
            review_count INTEGER NOT NULL DEFAULT 0
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_dubbing_plans_workspace_id
        ON dubbing_plans (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_dubbing_plans_status
        ON dubbing_plans (status)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS lipsync_jobs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            provider VARCHAR(40) NOT NULL DEFAULT 'unavailable',
            status VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
            progress FLOAT NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            result_asset_ref VARCHAR(1024) NOT NULL DEFAULT '',
            cost_json JSON NOT NULL DEFAULT '{}',
            video_ref VARCHAR(1024) NOT NULL DEFAULT '',
            audio_ref VARCHAR(1024) NOT NULL DEFAULT '',
            opts_json JSON NOT NULL DEFAULT '{}',
            adapter_job_id VARCHAR(80) NOT NULL DEFAULT '',
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_lipsync_jobs_workspace_id
        ON lipsync_jobs (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_lipsync_jobs_status
        ON lipsync_jobs (status)
    """))
