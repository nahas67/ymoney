"""Upgrade 0015: long-form projects/chapters + scene.chapter_id (Work 03)."""

from app.migrations.ddl import add_columns_if_missing, create_index_if_missing, has_column


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
    # The ordering column has two spellings in this schema and the index has to
    # name whichever one the table actually has. ``LongFormChapter`` declares it
    # as ``index`` (a Python attribute named after the SQL keyword), so
    # ``create_all`` -- which the runner does BEFORE migrations -- builds the
    # table with ``index`` and its own ``Index("ix_chapter_project", ...)``. This
    # migration's ``CREATE TABLE IF NOT EXISTS`` is then a no-op, so ``idx``
    # exists only when the table came from the migration path instead.
    #
    # SQLite short-circuits ``CREATE INDEX IF NOT EXISTS`` on the index NAME and
    # never validates the column list, so the mismatch stayed hidden for the
    # whole life of this migration. PostgreSQL resolves the column list FIRST and
    # raises 42703 even though the index already exists. Read the real name.
    order_col = "index" if has_column(session, "longform_chapters", "index") else "idx"
    create_index_if_missing(
        session, "ix_chapter_project", "longform_chapters",
        f"project_id, {order_col}",
    )
    # The old guard was ``try/except`` around each ALTER, ignoring "duplicate
    # column". Harmless on SQLite; on PostgreSQL the swallowed error aborts the
    # transaction (25P02) and every statement after it fails. A catalog check
    # cannot fail, so the migration stays replay-safe on both backends.
    # ``scenes`` is created by migration 0014, so it must exist here -- the old
    # code re-raised a missing-table error, and this preserves that.
    add_columns_if_missing(
        session,
        "scenes",
        [
            ("chapter_id", "VARCHAR(36)"),
            ("beats_json", "JSON NOT NULL DEFAULT '[]'"),
        ],
        when_absent_table="raise",
    )
