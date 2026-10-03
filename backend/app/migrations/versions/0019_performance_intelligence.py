"""Upgrade 0019: performance intelligence tables (Work 06 Lane A).

retention_points + creative_features + performance_observations. Additive
only; guarded so replays and parallel-lane migrations stay no-ops.
"""

from app.migrations.ddl import add_columns_if_missing


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
    # Column names verified against the ORM (``app/models``): workspace_id /
    # post_id / short_content_id / campaign_id on retention_points, subject_type
    # / subject_id / metric on performance_observations. SQLite never validates
    # the column list of an existing index name, so a wrong spelling would have
    # stayed hidden there; these all match.
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
    # Guarded ALTERs for databases created before a column existed. The old
    # guard was ``try/except`` ignoring "duplicate": SQLite leaves the
    # transaction usable after a failed statement so that was harmless, while
    # on PostgreSQL the swallowed duplicate-column error ABORTS the
    # transaction and every statement after it dies with 25P02. Reading the
    # catalog cannot fail, so nothing is caught. ``when_absent_table="raise"``
    # preserves the old loud behaviour for a missing table -- each of these is
    # created by the CREATE TABLE above, so absence means a broken schema.
    add_columns_if_missing(
        session, "retention_points",
        [
            ("source", "VARCHAR(40) NOT NULL DEFAULT ''"),
            ("captured_at", "TIMESTAMP"),
        ],
        when_absent_table="raise",
    )
    add_columns_if_missing(
        session, "creative_features",
        [("extracted_at", "TIMESTAMP")],
        when_absent_table="raise",
    )
    add_columns_if_missing(
        session, "performance_observations",
        [
            ("platform", "VARCHAR(30) NOT NULL DEFAULT ''"),
            ("scope_json", "JSON NOT NULL DEFAULT '{}'"),
            ("evidence_json", "JSON NOT NULL DEFAULT '{}'"),
            ("observed_at", "TIMESTAMP"),
        ],
        when_absent_table="raise",
    )