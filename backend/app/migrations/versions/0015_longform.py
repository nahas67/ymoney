"""Upgrade 0015: long-form projects/chapters + scene.chapter_id (Work 03)."""


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS longform_projects (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            topic VARCHAR(400) NOT NULL,
            content_format VARCHAR(30) NOT NULL DEFAULT 'EXPLAINER',
            target_duration_seconds INTEGER NOT NULL DEFAULT 600,
            target_audience VARCHAR(200) NOT NULL DEFAULT '',
            language VARCHAR(20) NOT NULL DEFAULT 'en',
            tone VARCHAR(60) NOT NULL DEFAULT 'confident, direct',
            aspect_ratio VARCHAR(10) NOT NULL DEFAULT '16:9',
            campaign_id VARCHAR(36),
            content_item_id VARCHAR(36),
            timeline_id VARCHAR(36),
            autonomy VARCHAR(10) NOT NULL DEFAULT 'AUTO',
            budget_strategy VARCHAR(10) NOT NULL DEFAULT 'BALANCED',
            voice_name VARCHAR(120) NOT NULL DEFAULT '',
            pronunciation_json JSON NOT NULL DEFAULT '{}',
            stage VARCHAR(20) NOT NULL DEFAULT 'CREATED',
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            stage_progress_json JSON NOT NULL DEFAULT '{}',
            cost_usd FLOAT NOT NULL DEFAULT 0.0,
            error TEXT NOT NULL DEFAULT '',
            strategy_json JSON NOT NULL DEFAULT '{}',
            research_json JSON NOT NULL DEFAULT '{}',
            script_json JSON NOT NULL DEFAULT '{}',
            fact_report_json JSON NOT NULL DEFAULT '{}',
            asset_plan_json JSON NOT NULL DEFAULT '{}',
            voice_json JSON NOT NULL DEFAULT '{}',
            qc_json JSON NOT NULL DEFAULT '{}',
            metadata_json JSON NOT NULL DEFAULT '{}',
            render_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_longform_ws ON longform_projects (workspace_id)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS longform_chapters (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES longform_projects(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL DEFAULT 0,
            title VARCHAR(200) NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            narrative_role VARCHAR(40) NOT NULL DEFAULT '',
            target_duration_seconds INTEGER NOT NULL DEFAULT 60,
            target_words INTEGER NOT NULL DEFAULT 150,
            entry_transition VARCHAR(20) NOT NULL DEFAULT 'cut',
            exit_transition VARCHAR(20) NOT NULL DEFAULT 'cut',
            retention_device VARCHAR(200) NOT NULL DEFAULT '',
            script_json JSON NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'PLANNED'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_chapter_project
        ON longform_chapters (project_id, idx)
    """))
    for _col in ("chapter_id VARCHAR(36)", "beats_json JSON NOT NULL DEFAULT '[]'"):
        try:
            session.execute(text(f"ALTER TABLE scenes ADD COLUMN {_col}"))
        except Exception as exc:  # noqa: BLE001 — duplicate-column means applied
            if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                raise
