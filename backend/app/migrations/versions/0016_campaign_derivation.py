"""Upgrade 0016: campaign derivation tables + post lineage columns (Work 04 Lane A)."""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS campaign_plans (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            campaign_id VARCHAR(36) NOT NULL UNIQUE REFERENCES campaigns(id) ON DELETE CASCADE,
            master_content_id VARCHAR(36) NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            target_platforms JSON NOT NULL DEFAULT '[]',
            desired_shorts INTEGER NOT NULL DEFAULT 8,
            duration_min FLOAT NOT NULL DEFAULT 20.0,
            duration_max FLOAT NOT NULL DEFAULT 55.0,
            diversity_config JSON NOT NULL DEFAULT '{}',
            posting_window JSON NOT NULL DEFAULT '{}',
            frequency VARCHAR(20) NOT NULL DEFAULT 'daily',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            progress_json JSON NOT NULL DEFAULT '{}',
            cost_usd FLOAT NOT NULL DEFAULT 0.0,
            error TEXT NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_campaign_plans_ws
        ON campaign_plans (workspace_id)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS platform_variants (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            campaign_id VARCHAR(36) NOT NULL,
            short_content_id VARCHAR(36) NOT NULL
                REFERENCES content_items(id) ON DELETE CASCADE,
            platform VARCHAR(30) NOT NULL DEFAULT '',
            aspect_ratio VARCHAR(10) NOT NULL DEFAULT '9:16',
            timeline_id VARCHAR(36),
            metadata_json JSON NOT NULL DEFAULT '{}',
            cover_asset_id VARCHAR(36),
            safe_zone_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            publishing_job_id VARCHAR(36),
            published_post_id VARCHAR(36)
        )
    """))
    session.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_variant_short_platform
        ON platform_variants (short_content_id, platform)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_variant_campaign
        ON platform_variants (campaign_id)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS publishing_plans (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            campaign_id VARCHAR(36) NOT NULL UNIQUE,
            items_json JSON NOT NULL DEFAULT '[]',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
        )
    """))
    for _col in ("platform_variant_id VARCHAR(36)", "campaign_id VARCHAR(36)"):
        try:
            session.execute(text(f"ALTER TABLE published_posts ADD COLUMN {_col}"))
        except Exception as exc:  # noqa: BLE001 — duplicate-column means applied
            if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                raise
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_published_posts_campaign
        ON published_posts (campaign_id)
    """))
