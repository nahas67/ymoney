"""Upgrade 0023: avatar profiles/outputs + ugc projects (Work 07 Lane C).

Lane A owns 0021, Lane B owns 0022; this lane owns 0023 only. Append-only and
idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so it replays as a
no-op even when the ORM already created the tables (Base.metadata.create_all
runs first in the runner).

Tables (mirrors app/models/avatar.py + app/models/ugc.py):
  * avatar_profiles — typed profile payload, source portrait, CONSENT gate
    (authorized|pending|revoked + evidence JSON), provider, status.
  * avatar_outputs  — one render: output MediaAsset ref + full lineage
    (source asset, provider/backend, consent snapshot, driving audio, QC).
  * ugc_projects    — user brief → canonical ContentTimeline → render → QC.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS avatar_profiles (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL DEFAULT '',
            profile_json JSON NOT NULL DEFAULT '{}',
            source_asset_ref TEXT NOT NULL,
            consent_state VARCHAR(20) NOT NULL DEFAULT 'pending',
            consent_json JSON NOT NULL DEFAULT '{}',
            provider VARCHAR(40) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_profiles_workspace_id
        ON avatar_profiles (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_profile_ws_status
        ON avatar_profiles (workspace_id, status)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_profile_consent
        ON avatar_profiles (workspace_id, consent_state)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS avatar_outputs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            profile_id VARCHAR(36) NOT NULL,
            output_asset_ref TEXT NOT NULL,
            lineage_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'ready'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_outputs_workspace_id
        ON avatar_outputs (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_output_ws_status
        ON avatar_outputs (workspace_id, status)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_avatar_output_profile
        ON avatar_outputs (profile_id)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS ugc_projects (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            preset VARCHAR(40) NOT NULL DEFAULT '',
            brief_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            timeline_id VARCHAR(36),
            render_asset_ref TEXT NOT NULL,
            qc_json JSON NOT NULL DEFAULT '{}',
            lineage_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_ugc_projects_workspace_id
        ON ugc_projects (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_ugc_projects_timeline_id
        ON ugc_projects (timeline_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_ugc_ws_status
        ON ugc_projects (workspace_id, status)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_ugc_ws_preset
        ON ugc_projects (workspace_id, preset)
    """))
