"""Upgrade 0025: creative_commands audit ledger (Work 08 Lane B).

Lane A owns 0024 (brand), this lane owns 0025 only; max existing is 0024.
Append-only and idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so
it replays as a no-op even when the ORM already created the table
(Base.metadata.create_all runs first in the runner).

Table (mirrors app/models/creative.py — CreativeCommandRow):
  * creative_commands — one row per NL parse / preview / apply / rejection:
    typed command payload, preview change-set (diff + estimates), lifecycle
    status (parsed|previewed|applied|rejected|undone|stale), the timeline
    version the action was based on (stale-preview detection), and the apply
    result (previous/new timeline id + version) that ``undo`` restores from.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS creative_commands (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            timeline_id VARCHAR(36) NOT NULL DEFAULT '',
            actor VARCHAR(60) NOT NULL DEFAULT 'user',
            source_user_id VARCHAR(36),
            text_input TEXT NOT NULL,
            commands_json JSON NOT NULL DEFAULT '[]',
            change_set_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'parsed',
            parent_version INTEGER,
            result_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_creative_commands_workspace_id
        ON creative_commands (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_creative_commands_timeline_id
        ON creative_commands (timeline_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_creative_ws_status
        ON creative_commands (workspace_id, status)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_creative_ws_timeline
        ON creative_commands (workspace_id, timeline_id)
    """))
