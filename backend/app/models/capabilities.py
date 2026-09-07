"""Persistent authorization and observability models for agent capabilities."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin, utcnow


class CapabilityPermission(Base, PKMixin, TimestampMixin):
    __tablename__ = "capability_permissions"
    __table_args__ = (Index("ix_capability_permissions_workspace", "workspace_id", "capability_type", "capability_key"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    capability_type: Mapped[str] = mapped_column(String(16))  # skill|tool|permission
    capability_key: Mapped[str] = mapped_column(String(120))
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ToolCallAudit(Base, PKMixin, TimestampMixin):
    __tablename__ = "tool_call_audits"
    __table_args__ = (Index("ix_tool_call_audits_workspace_time", "workspace_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(40), index=True)
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="STARTED")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(default=0.0)
    actual_cost_usd: Mapped[float] = mapped_column(default=0.0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
