"""Upgrade 0021: localization tables (Work 07 Lane A).

localized_contents + glossary_terms + localization_qc_reports. Additive only;
CREATE TABLE IF NOT EXISTS + guarded indexes so replays and parallel-lane
migrations stay no-ops.
"""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS localized_contents (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            source_content_id VARCHAR(36) NOT NULL,
            child_content_id VARCHAR(36),
            timeline_id VARCHAR(36),
            language VARCHAR(10) NOT NULL DEFAULT '',
            locale VARCHAR(20) NOT NULL DEFAULT '',
            translation_version INTEGER NOT NULL DEFAULT 1,
            lineage_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            error VARCHAR(2000) NOT NULL DEFAULT ''
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS glossary_terms (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            term VARCHAR(200) NOT NULL,
            replacement VARCHAR(200) NOT NULL DEFAULT '',
            target_languages JSON NOT NULL DEFAULT '[]',
            kind VARCHAR(20) NOT NULL DEFAULT 'terminology',
            case_sensitive BOOLEAN NOT NULL DEFAULT 0
        )
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS localization_qc_reports (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            localized_content_id VARCHAR(36) NOT NULL
                REFERENCES localized_contents(id) ON DELETE CASCADE,
            status VARCHAR(30) NOT NULL DEFAULT 'REVIEW_REQUIRED',
            checks_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    for _idx in (
        "CREATE INDEX IF NOT EXISTS ix_localized_contents_workspace_id "
        "ON localized_contents (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_localized_ws_lang "
        "ON localized_contents (workspace_id, language)",
        "CREATE INDEX IF NOT EXISTS ix_localized_source "
        "ON localized_contents (source_content_id)",
        "CREATE INDEX IF NOT EXISTS ix_localized_contents_child_content_id "
        "ON localized_contents (child_content_id)",
        "CREATE INDEX IF NOT EXISTS ix_localized_contents_timeline_id "
        "ON localized_contents (timeline_id)",
        "CREATE INDEX IF NOT EXISTS ix_localized_contents_status "
        "ON localized_contents (status)",
        "CREATE INDEX IF NOT EXISTS ix_glossary_terms_workspace_id "
        "ON glossary_terms (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_glossary_ws_term "
        "ON glossary_terms (workspace_id, term)",
        "CREATE INDEX IF NOT EXISTS ix_localization_qc_reports_workspace_id "
        "ON localization_qc_reports (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_locqc_ws_content "
        "ON localization_qc_reports (workspace_id, localized_content_id)",
        "CREATE INDEX IF NOT EXISTS ix_localization_qc_reports_localized_content_id "
        "ON localization_qc_reports (localized_content_id)",
    ):
        session.execute(text(_idx))
