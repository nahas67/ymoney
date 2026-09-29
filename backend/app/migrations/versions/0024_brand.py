"""Upgrade 0024: brand system (Work 08 Lane A).

Tables (mirrors app/models/brand.py): brands, brand_dna, brand_assets,
brand_presets, brand_overrides, brand_effective_configs. Append-only and
idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so it replays as a
no-op even when the ORM already created the tables (Base.metadata.create_all
runs first in the runner).

No existing table is altered -- the white-label /brand routes (Work 01) and
every other lane's schema stay untouched.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brands (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL DEFAULT '',
            is_default BOOLEAN NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brands_workspace_id ON brands (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_ws_status ON brands (workspace_id, status)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brands_is_default ON brands (is_default)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_dna (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            brand_id VARCHAR(36) REFERENCES brands(id) ON DELETE CASCADE,
            dna_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_dna_workspace_id ON brand_dna (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_dna_brand_id ON brand_dna (brand_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_dna_ws_brand ON brand_dna (workspace_id, brand_id)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_assets (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            brand_id VARCHAR(36) NOT NULL DEFAULT '' REFERENCES brands(id) ON DELETE CASCADE,
            asset_role VARCHAR(30) NOT NULL DEFAULT 'logo',
            media_asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            label VARCHAR(160) NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_assets_workspace_id ON brand_assets (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_assets_brand_id ON brand_assets (brand_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_assets_media_asset_id ON brand_assets (media_asset_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_asset_ws_role ON brand_assets (workspace_id, asset_role)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_asset_brand_role ON brand_assets (brand_id, asset_role)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_presets (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL DEFAULT '',
            builtin BOOLEAN NOT NULL DEFAULT 0,
            preset_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_presets_workspace_id ON brand_presets (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_preset_ws_builtin ON brand_presets (workspace_id, builtin)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_overrides (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            subject_type VARCHAR(20) NOT NULL DEFAULT 'campaign',
            subject_id VARCHAR(60) NOT NULL DEFAULT '',
            override_json JSON NOT NULL DEFAULT '{}',
            updated_by VARCHAR(60) NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_overrides_workspace_id
        ON brand_overrides (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS uq_brand_override_subject
        ON brand_overrides (workspace_id, subject_type, subject_id)
    """))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_effective_configs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            subject_type VARCHAR(20) NOT NULL DEFAULT 'workspace',
            subject_id VARCHAR(36) NOT NULL DEFAULT '',
            platform VARCHAR(30),
            effective_json JSON NOT NULL DEFAULT '{}',
            dna_version VARCHAR(64) NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_effective_configs_workspace_id
        ON brand_effective_configs (workspace_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_eff_ws_subject
        ON brand_effective_configs (workspace_id, subject_type, subject_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_eff_ws_platform
        ON brand_effective_configs (workspace_id, platform)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_brand_effective_configs_platform
        ON brand_effective_configs (platform)
    """))
