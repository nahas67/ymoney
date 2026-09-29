"""Work 10 knowledge graph + GlobalMemory + source connectors.

Tables (mirrored idempotently in migrations/versions/0027_knowledge.py):
  * knowledge_memories  -- GlobalMemory record: typed, provenance-tracked
                           workspace knowledge with lifecycle status +
                           freshness band
  * knowledge_evidence  -- one provenance link per memory (what backs it)
  * knowledge_nodes     -- knowledge-graph nodes
                           (Brand|Campaign|Content|Scene|Topic|Entity|Source|
                           Claim|AudienceInsight|CommunityInsight|Experiment|
                           CreativeLesson|Publication)
  * knowledge_edges     -- typed, weighted relationships between nodes
  * source_connectors   -- registered external source connectors
                           (config_json holds secrets and is NEVER returned
                           raw by the API)
  * source_documents    -- ingested source documents, deduped per connector

Design rules (Work 10 foundation):
  * Isolation: every row carries workspace_id; queries always filter by it.
  * Provenance: memories record source_ids + evidence_ids; rows stored
    without either are treated as UNVERIFIED by the service layer.
  * Dedupe is a SERVICE concern: (workspace_id, type, topic_key,
    content_hash) is deliberately NOT DB-unique, so conflicting facts can
    coexist side by side (never deleted or overwritten).
  * Lifecycle is column state only: SUPERSEDED/CONFLICTED/DISABLED rows are
    preserved forever; supersession points at superseded_by.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class KnowledgeMemory(Base, PKMixin, TimestampMixin):
    """GlobalMemory record: one typed, provenance-tracked workspace memory."""

    __tablename__ = "knowledge_memories"
    __table_args__ = (
        Index("ix_kmem_ws_type", "workspace_id", "type"),
        Index("ix_kmem_ws_topic_key", "workspace_id", "topic_key"),
        Index("ix_kmem_ws_status", "workspace_id", "status"),
        Index("ix_kmem_ws_hash", "workspace_id", "content_hash"),
        # NOTE: deliberately no UNIQUE on (workspace_id, type, topic_key,
        # content_hash) -- conflicting facts must coexist; dedupe happens in
        # the service layer (GlobalMemory.store idempotency).
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    brand_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # one of TYPES (engine/knowledge/memory.py)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    topic: Mapped[str] = mapped_column(String(200), default="")
    # normalized dedupe key (lowercased/hashed topic)
    topic_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    scope: Mapped[str] = mapped_column(String(120), default="")
    platform: Mapped[str] = mapped_column(String(40), default="")

    # provenance: originating source_documents.id / evidence ids
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # age band FRESH|AGING|STALE (recomputed on read by freshness.py)
    freshness: Mapped[str] = mapped_column(String(20), default="FRESH")
    # lifecycle: ACTIVE|CONFLICTED|SUPERSEDED|UNVERIFIED|DISABLED
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    # agent key or "user"
    origin: Mapped[str] = mapped_column(String(80), default="")
    # sha256 of normalized content (dedupe / conflict detection)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    conflict_group: Mapped[str] = mapped_column(String(64), default="", index=True)

    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # historical usefulness (feeds retrieval ranking)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # service-owned extra relations; never API-writable
    related_json: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeEvidence(Base, PKMixin, TimestampMixin):
    """One provenance link: what backs a memory (unique per kind+ref)."""

    __tablename__ = "knowledge_evidence"
    __table_args__ = (
        UniqueConstraint("memory_id", "kind", "ref_id", name="uq_evidence_ref"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_memories.id", ondelete="CASCADE"), index=True
    )
    # evidence_record|publication|interaction|source_document|agent_run|user|
    # metric|research_claim
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(250), default="")
    # source_documents.id when applicable
    source_id: Mapped[str] = mapped_column(String(36), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KnowledgeNode(Base, PKMixin, TimestampMixin):
    """Knowledge-graph node (node_type + node_key unique per workspace)."""

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "node_type", "node_key", name="uq_node_key"
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    node_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # domain row id ("" for Topic/Entity)
    ref_id: Mapped[str] = mapped_column(String(36), default="")
    node_key: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(200), default="")
    topic_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeEdge(Base, PKMixin, TimestampMixin):
    """Typed, weighted relationship between two knowledge-graph nodes."""

    __tablename__ = "knowledge_edges"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "from_node_id",
            "to_node_id",
            "relationship",
            name="uq_edge_relationship",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    from_node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    to_node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # CONTENT_ABOUT_TOPIC|CLAIM_SUPPORTED_BY|DERIVED_FROM|MENTS_ENTITY|
    # PERFORMED_ON|LEARNED_FROM|COMMUNITY_REQUESTED|EXPERIMENT_TESTED|
    # BRAND_USES|SOURCE_REFERENCES
    relationship: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceConnector(Base, PKMixin, TimestampMixin):
    """Registered external source connector (one row per kind+name)."""

    __tablename__ = "source_connectors"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "kind", "name", name="uq_connector_kind_name"
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # AVAILABLE|UNAVAILABLE|DISABLED|ERROR
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    unavailable_reason: Mapped[str] = mapped_column(String(200), default="")
    # secrets live here -- redacted (never returned raw) by the API
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # durable incremental-sync cursor (survives job restarts)
    last_cursor: Mapped[str] = mapped_column(Text, default="")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)


class SourceDocument(Base, PKMixin, TimestampMixin):
    """Ingested source document, deduped per (workspace, connector, remote_id)."""

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "connector_id", "remote_id", name="uq_document_remote"
        ),
        Index("ix_kdoc_ws_state", "workspace_id", "state"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    connector_id: Mapped[str] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="CASCADE"), index=True
    )
    remote_id: Mapped[str] = mapped_column(String(200), nullable=False)

    title: Mapped[str] = mapped_column(Text, default="")
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    # full text (capped by adapters at MAX_SOURCE_BYTES)
    content: Mapped[str] = mapped_column(Text, default="")
    # storage_key / s3://... / "" for text-only
    asset_reference: Mapped[str] = mapped_column(String(500), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    cursor: Mapped[str] = mapped_column(String(200), default="")
    # active|updated|deleted
    state: Mapped[str] = mapped_column(String(20), default="active")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


__all__ = [
    "KnowledgeEdge",
    "KnowledgeEvidence",
    "KnowledgeMemory",
    "KnowledgeNode",
    "SourceConnector",
    "SourceDocument",
]
