"""Upgrade 0028: collaboration foundation (Work 11 Lane F).

Tables (mirrors app/models/collab.py):
  projects, project_members, project_targets, reviews, review_assignments,
  review_decisions, revision_requests, comments, export_profiles,
  export_jobs, project_archives, notifications, retention_policies.

Append-only and idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so
it replays as a no-op even when the ORM already created the tables
(Base.metadata.create_all runs first in the runner). No existing table is
altered -- every Work 01-10 schema stays untouched.

Note (documented deviation from the contracts §2 sketch): every table also
carries `updated_at` (and retention_policies carries `created_at`) because
all 13 models use the PKMixin + TimestampMixin pair mandated by contracts §2
-- the same convention as models/knowledge.py.
"""

from __future__ import annotations


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS projects (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            archived_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_projects_workspace_id ON projects (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_projects_created_at ON projects (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_projects_ws_status ON projects (workspace_id, status)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS project_members (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(32) NOT NULL DEFAULT 'VIEWER',
            CONSTRAINT uq_project_member UNIQUE (project_id, user_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_project_members_project_id ON project_members (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_members_user_id ON project_members (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_members_created_at ON project_members (created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_member ON project_members (project_id, user_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS project_targets (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(36) NOT NULL,
            CONSTRAINT uq_project_target UNIQUE (project_id, target_type, target_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_project_targets_project_id ON project_targets (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_targets_created_at ON project_targets (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_ptarget_lookup ON project_targets (target_type, target_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_target ON project_targets (project_id, target_type, target_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS reviews (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id VARCHAR(36) REFERENCES projects(id) ON DELETE CASCADE,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(36) NOT NULL,
            title VARCHAR(200) NOT NULL DEFAULT '',
            state VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
            bound_version VARCHAR(64),
            bound_manifest_hash VARCHAR(64),
            stale BOOLEAN NOT NULL DEFAULT FALSE,
            stale_detected_at TIMESTAMP,
            created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            closed_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_reviews_workspace_id ON reviews (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_reviews_project_id ON reviews (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_reviews_created_at ON reviews (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reviews_ws_created ON reviews (workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reviews_ws_state ON reviews (workspace_id, state)",
        "CREATE INDEX IF NOT EXISTS ix_reviews_target ON reviews (target_type, target_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS review_assignments (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            review_id VARCHAR(36) NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assigned_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_review_assignments_review_id ON review_assignments (review_id)",
        "CREATE INDEX IF NOT EXISTS ix_review_assignments_user_id ON review_assignments (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_review_assignments_created_at ON review_assignments (created_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS review_decisions (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            review_id VARCHAR(36) NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            decision VARCHAR(32) NOT NULL,
            bound_version VARCHAR(64),
            bound_manifest_hash VARCHAR(64),
            body TEXT NOT NULL DEFAULT ''
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_review_decisions_review_id ON review_decisions (review_id)",
        "CREATE INDEX IF NOT EXISTS ix_review_decisions_user_id ON review_decisions (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_review_decisions_created_at ON review_decisions (created_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS revision_requests (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id VARCHAR(36) REFERENCES projects(id) ON DELETE CASCADE,
            review_id VARCHAR(36) REFERENCES reviews(id) ON DELETE CASCADE,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(36) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'OPEN',
            items_json JSON NOT NULL DEFAULT '[]',
            created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            resolved_at TIMESTAMP,
            resolved_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_revision_requests_workspace_id ON revision_requests (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_revision_requests_project_id ON revision_requests (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_revision_requests_review_id ON revision_requests (review_id)",
        "CREATE INDEX IF NOT EXISTS ix_revision_requests_created_at ON revision_requests (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_revision_requests_target ON revision_requests (target_type, target_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS comments (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id VARCHAR(36) REFERENCES projects(id) ON DELETE CASCADE,
            parent_id VARCHAR(36) REFERENCES comments(id) ON DELETE CASCADE,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(36) NOT NULL,
            anchor_json JSON NOT NULL DEFAULT '{}',
            body TEXT NOT NULL,
            author_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mentions_json JSON NOT NULL DEFAULT '[]',
            version_ref VARCHAR(64),
            resolved_at TIMESTAMP,
            resolved_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_comments_workspace_id ON comments (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_comments_project_id ON comments (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_comments_parent_id ON comments (parent_id)",
        "CREATE INDEX IF NOT EXISTS ix_comments_author_id ON comments (author_id)",
        "CREATE INDEX IF NOT EXISTS ix_comments_created_at ON comments (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_comments_ws_created ON comments (workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_comments_target ON comments (target_type, target_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS export_profiles (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) REFERENCES workspaces(id) ON DELETE CASCADE,
            name VARCHAR(120) NOT NULL,
            preset VARCHAR(32) NOT NULL,
            config_json JSON NOT NULL DEFAULT '{}',
            is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
            created_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_export_profiles_workspace_id ON export_profiles (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_export_profiles_created_at ON export_profiles (created_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS export_jobs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            profile_id VARCHAR(36) REFERENCES export_profiles(id) ON DELETE SET NULL,
            format VARCHAR(16) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(36) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
            progress INTEGER NOT NULL DEFAULT 0,
            artifact_asset_id VARCHAR(36) REFERENCES media_assets(id) ON DELETE SET NULL,
            checksum VARCHAR(64) NOT NULL DEFAULT '',
            verification_json JSON NOT NULL DEFAULT '{}',
            error TEXT,
            job_id VARCHAR(36),
            attempt INTEGER NOT NULL DEFAULT 0,
            created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_workspace_id ON export_jobs (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_profile_id ON export_jobs (profile_id)",
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_artifact_asset_id ON export_jobs (artifact_asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_job_id ON export_jobs (job_id)",
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_created_at ON export_jobs (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_export_jobs_ws_state ON export_jobs (workspace_id, state)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS project_archives (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            mode VARCHAR(32) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
            manifest_json JSON NOT NULL DEFAULT '{}',
            artifact_path TEXT,
            checksum VARCHAR(64) NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            finished_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_project_archives_workspace_id ON project_archives (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_archives_project_id ON project_archives (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_archives_created_at ON project_archives (created_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS notifications (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind VARCHAR(64) NOT NULL,
            payload_json JSON NOT NULL DEFAULT '{}',
            read_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_notifications_workspace_id ON notifications (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON notifications (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON notifications (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_ws_user_read ON notifications (workspace_id, user_id, read_at)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_ws_created ON notifications (workspace_id, created_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS retention_policies (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            audit_retention_days INTEGER,
            render_retention_days INTEGER,
            temp_asset_retention_days INTEGER,
            export_retention_days INTEGER,
            updated_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
            CONSTRAINT uq_retention_workspace UNIQUE (workspace_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_retention_policies_created_at ON retention_policies (created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_retention_workspace ON retention_policies (workspace_id)",
    ):
        session.execute(text(stmt))
