"""Upgrade 0027: knowledge graph + GlobalMemory + source connectors (Work 10).

Tables (mirrors app/models/knowledge.py):
  knowledge_memories, knowledge_evidence, knowledge_nodes, knowledge_edges,
  source_connectors, source_documents.

Append-only and idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so
it replays as a no-op even when the ORM already created the tables
(Base.metadata.create_all runs first in the runner). No existing table is
altered -- every Work 01-09 schema stays untouched.
"""

from __future__ import annotations


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_memories (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            brand_id VARCHAR(36),
            type VARCHAR(40) NOT NULL,
            content TEXT NOT NULL,
            topic VARCHAR(200) NOT NULL DEFAULT '',
            topic_key VARCHAR(64) NOT NULL DEFAULT '',
            scope VARCHAR(120) NOT NULL DEFAULT '',
            platform VARCHAR(40) NOT NULL DEFAULT '',
            source_ids JSON NOT NULL DEFAULT '[]',
            evidence_ids JSON NOT NULL DEFAULT '[]',
            confidence FLOAT NOT NULL DEFAULT 0.5,
            freshness VARCHAR(20) NOT NULL DEFAULT 'FRESH',
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            origin VARCHAR(80) NOT NULL DEFAULT '',
            content_hash VARCHAR(64) NOT NULL DEFAULT '',
            conflict_group VARCHAR(64) NOT NULL DEFAULT '',
            last_verified_at TIMESTAMP,
            superseded_by VARCHAR(36),
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TIMESTAMP,
            related_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    # NOTE: no UNIQUE on (workspace_id, type, topic_key, content_hash) --
    # conflicting facts coexist; dedupe is a service-level concern.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_memories_workspace_id ON knowledge_memories (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_memories_type ON knowledge_memories (type)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_memories_topic_key ON knowledge_memories (topic_key)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_memories_content_hash ON knowledge_memories (content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_memories_conflict_group ON knowledge_memories (conflict_group)",
        "CREATE INDEX IF NOT EXISTS ix_kmem_ws_type ON knowledge_memories (workspace_id, type)",
        "CREATE INDEX IF NOT EXISTS ix_kmem_ws_topic_key ON knowledge_memories (workspace_id, topic_key)",
        "CREATE INDEX IF NOT EXISTS ix_kmem_ws_status ON knowledge_memories (workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_kmem_ws_hash ON knowledge_memories (workspace_id, content_hash)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_evidence (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            memory_id VARCHAR(36) NOT NULL REFERENCES knowledge_memories(id) ON DELETE CASCADE,
            kind VARCHAR(40) NOT NULL,
            ref_id VARCHAR(250) NOT NULL DEFAULT '',
            source_id VARCHAR(36) NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            captured_at TIMESTAMP,
            CONSTRAINT uq_evidence_ref UNIQUE (memory_id, kind, ref_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_evidence_workspace_id ON knowledge_evidence (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_evidence_memory_id ON knowledge_evidence (memory_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_ref ON knowledge_evidence (memory_id, kind, ref_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            node_type VARCHAR(30) NOT NULL,
            ref_id VARCHAR(36) NOT NULL DEFAULT '',
            node_key VARCHAR(120) NOT NULL,
            label VARCHAR(200) NOT NULL DEFAULT '',
            topic_key VARCHAR(64) NOT NULL DEFAULT '',
            meta_json JSON NOT NULL DEFAULT '{}',
            CONSTRAINT uq_node_key UNIQUE (workspace_id, node_type, node_key)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_workspace_id ON knowledge_nodes (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_node_type ON knowledge_nodes (node_type)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_topic_key ON knowledge_nodes (topic_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_key ON knowledge_nodes (workspace_id, node_type, node_key)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            from_node_id VARCHAR(36) NOT NULL,
            to_node_id VARCHAR(36) NOT NULL,
            relationship VARCHAR(40) NOT NULL,
            weight FLOAT NOT NULL DEFAULT 1.0,
            evidence_ids JSON NOT NULL DEFAULT '[]',
            meta_json JSON NOT NULL DEFAULT '{}',
            CONSTRAINT uq_edge_relationship UNIQUE
                (workspace_id, from_node_id, to_node_id, relationship)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_workspace_id ON knowledge_edges (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_from_node_id ON knowledge_edges (from_node_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_to_node_id ON knowledge_edges (to_node_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_relationship ON knowledge_edges (relationship)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_relationship ON knowledge_edges (workspace_id, from_node_id, to_node_id, relationship)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS source_connectors (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            kind VARCHAR(40) NOT NULL,
            name VARCHAR(120) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE',
            unavailable_reason VARCHAR(200) NOT NULL DEFAULT '',
            config_json JSON NOT NULL DEFAULT '{}',
            last_cursor TEXT NOT NULL DEFAULT '',
            last_sync_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            doc_count INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT uq_connector_kind_name UNIQUE (workspace_id, kind, name)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_source_connectors_workspace_id ON source_connectors (workspace_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_kind_name ON source_connectors (workspace_id, kind, name)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS source_documents (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            connector_id VARCHAR(36) NOT NULL REFERENCES source_connectors(id) ON DELETE CASCADE,
            remote_id VARCHAR(200) NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            mime_type VARCHAR(100) NOT NULL DEFAULT '',
            remote_created_at TIMESTAMP,
            remote_updated_at TIMESTAMP,
            author VARCHAR(200) NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            asset_reference VARCHAR(500) NOT NULL DEFAULT '',
            checksum VARCHAR(64) NOT NULL DEFAULT '',
            cursor VARCHAR(200) NOT NULL DEFAULT '',
            state VARCHAR(20) NOT NULL DEFAULT 'active',
            first_seen_at TIMESTAMP,
            last_seen_at TIMESTAMP,
            meta_json JSON NOT NULL DEFAULT '{}',
            CONSTRAINT uq_document_remote UNIQUE
                (workspace_id, connector_id, remote_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_source_documents_workspace_id ON source_documents (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_source_documents_connector_id ON source_documents (connector_id)",
        "CREATE INDEX IF NOT EXISTS ix_kdoc_ws_state ON source_documents (workspace_id, state)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_remote ON source_documents (workspace_id, connector_id, remote_id)",
    ):
        session.execute(text(stmt))
