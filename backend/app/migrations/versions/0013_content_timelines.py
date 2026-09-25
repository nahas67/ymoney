"""Upgrade 0013: canonical editorial timelines (Phase 1 foundation)."""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS content_timelines (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            content_item_id VARCHAR(36),
            video_id VARCHAR(36),
            name VARCHAR(200) NOT NULL DEFAULT 'main',
            fps FLOAT NOT NULL DEFAULT 30.0,
            duration_seconds FLOAT NOT NULL DEFAULT 0.0,
            tracks_json JSON NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1,
            parent_timeline_id VARCHAR(36)
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_timeline_ws
        ON content_timelines (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_timeline_content
        ON content_timelines (content_item_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_timeline_video
        ON content_timelines (video_id)
    """))
