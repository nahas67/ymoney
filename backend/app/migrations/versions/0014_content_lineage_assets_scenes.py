"""Upgrade 0014: content lineage + media assets + scenes (Work 01)."""

from app.migrations.ddl import add_columns_if_missing


def upgrade(session) -> None:
    from sqlalchemy import text

    # Replay-safe on every backend, but NOT by catching the error. The old guard
    # was ``try: ALTER ... except: ignore 'duplicate column'``. SQLite leaves the
    # transaction usable after a failed statement so that was harmless; on
    # PostgreSQL the failed ALTER aborts the transaction, the handler swallows it,
    # and every later statement in this migration dies with 25P02 -- with the
    # real error never surfacing. Reading the catalog cannot fail, so nothing is
    # caught and nothing is poisoned.
    #
    # ``when_absent_table="raise"`` keeps the old loud behaviour: the previous
    # ``except`` clause re-raised anything that was not a duplicate column, so a
    # missing ``content_items`` used to fail here rather than be skipped.
    add_columns_if_missing(
        session,
        "content_items",
        [
            ("parent_content_id", "VARCHAR(36)"),
            ("root_content_id", "VARCHAR(36)"),
            ("derivation_type", "VARCHAR(30)"),
            ("lineage_version", "INTEGER NOT NULL DEFAULT 1"),
        ],
        when_absent_table="raise",
    )
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_content_parent
        ON content_items (parent_content_id)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS media_assets (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            type VARCHAR(20) NOT NULL DEFAULT 'other',
            origin VARCHAR(20) NOT NULL DEFAULT 'upload',
            provider VARCHAR(60) NOT NULL DEFAULT '',
            storage_key TEXT NOT NULL DEFAULT '',
            mime_type VARCHAR(100) NOT NULL DEFAULT '',
            duration_seconds FLOAT,
            width INTEGER,
            height INTEGER,
            frame_rate FLOAT,
            codec VARCHAR(40) NOT NULL DEFAULT '',
            audio_codec VARCHAR(40) NOT NULL DEFAULT '',
            sample_rate INTEGER,
            channels INTEGER,
            file_size INTEGER,
            checksum VARCHAR(128) NOT NULL DEFAULT '',
            meta_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_asset_ws_type
        ON media_assets (workspace_id, type)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS scenes (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            content_item_id VARCHAR(36),
            timeline_id VARCHAR(36),
            idx INTEGER NOT NULL DEFAULT 0,
            title VARCHAR(200) NOT NULL DEFAULT '',
            script_segment TEXT NOT NULL DEFAULT '',
            narration TEXT NOT NULL DEFAULT '',
            visual_intent TEXT NOT NULL DEFAULT '',
            start_seconds FLOAT NOT NULL DEFAULT 0.0,
            end_seconds FLOAT NOT NULL DEFAULT 0.0,
            assets_json JSON NOT NULL DEFAULT '[]',
            captions_json JSON NOT NULL DEFAULT '[]',
            performance_json JSON NOT NULL DEFAULT '{}',
            parent_scene_id VARCHAR(36)
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_scene_content ON scenes (content_item_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_scene_timeline ON scenes (timeline_id)
    """))
