"""Upgrade 0019: performance intelligence tables (Work 06 Lane A).

retention_points + creative_features + performance_observations. Additive
only; guarded so replays and parallel-lane migrations stay no-ops.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS retention_points (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            post_id VARCHAR(36) REFERENCES published_posts(id) ON DELETE CASCADE,
            campaign_id VARCHAR(36),
            short_content_id VARCHAR(36),
            checkpoint VARCHAR(20) NOT NULL DEFAULT '',
            value FLOAT NOT NULL DEFAULT 0.0,
            source VARCHAR(40) NOT NULL DEFAULT '',
            captured_at TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS creative_features (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            content_item_id VARCHAR(36) NOT NULL UNIQUE,
            features_json JSON NOT NULL DEFAULT '{}',
            extracted_at TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS performance_observations (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            subject_type VARCHAR(20) NOT NULL DEFAULT '',
            subject_id VARCHAR(36) NOT NULL DEFAULT '',
            metric VARCHAR(60) NOT NULL DEFAULT '',
            value FLOAT NOT NULL DEFAULT 0.0,
            platform VARCHAR(30) NOT NULL DEFAULT '',
            scope_json JSON NOT NULL DEFAULT '{}',
            evidence_json JSON NOT NULL DEFAULT '{}',
            observed_at TIMESTAMP
        )
    """))
    for _idx in (
        "CREATE INDEX IF NOT EXISTS ix_retention_ws_post "
        "ON retention_points (workspace_id, post_id)",
        "CREATE INDEX IF NOT EXISTS ix_retention_ws_short "
        "ON retention_points (workspace_id, short_content_id)",
        "CREATE INDEX IF NOT EXISTS ix_retention_ws_campaign "
        "ON retention_points (workspace_id, campaign_id)",
        "CREATE INDEX IF NOT EXISTS ix_creative_features_ws "
        "ON creative_features (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_perf_obs_ws_subject "
        "ON performance_observations (workspace_id, subject_type, subject_id)",
        "CREATE INDEX IF NOT EXISTS ix_perf_obs_ws_metric "
        "ON performance_observations (workspace_id, metric)",
    ):
        session.execute(text(_idx))
    # Guarded ALTERs for databases created before a column existed.
    for _table, _col in (
        ("retention_points", "source VARCHAR(40) NOT NULL DEFAULT ''"),
        ("retention_points", "captured_at TIMESTAMP"),
        ("creative_features", "extracted_at TIMESTAMP"),
        ("performance_observations", "platform VARCHAR(30) NOT NULL DEFAULT ''"),
        ("performance_observations", "scope_json JSON NOT NULL DEFAULT '{}'"),
        ("performance_observations", "evidence_json JSON NOT NULL DEFAULT '{}'"),
        ("performance_observations", "observed_at TIMESTAMP"),
    ):
        try:
            session.execute(text(f"ALTER TABLE {_table} ADD COLUMN {_col}"))
        except Exception as exc:  # noqa: BLE001 — duplicate-column means applied
            if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                raise
