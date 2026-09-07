"""Persist capability authorization and tool execution audit data."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def upgrade(session: Session) -> None:
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS capability_permissions (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            capability_type VARCHAR(16) NOT NULL,
            capability_key VARCHAR(120) NOT NULL,
            allowed BOOLEAN NOT NULL DEFAULT 1,
            updated_by VARCHAR(36)
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_capability_permissions_workspace
        ON capability_permissions (workspace_id, capability_type, capability_key)
    """))
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS tool_call_audits (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            agent_key VARCHAR(40) NOT NULL,
            tool_name VARCHAR(120) NOT NULL,
            job_id VARCHAR(36),
            cycle_id VARCHAR(36),
            status VARCHAR(16) NOT NULL DEFAULT 'STARTED',
            input_summary TEXT NOT NULL DEFAULT '',
            output_summary TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER,
            estimated_cost_usd FLOAT NOT NULL DEFAULT 0,
            actual_cost_usd FLOAT NOT NULL DEFAULT 0,
            metadata_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_tool_call_audits_workspace_time
        ON tool_call_audits (workspace_id, created_at)
    """))
