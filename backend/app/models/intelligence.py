"""Intelligence persistence (Work 05).

Lane A: decision audit log. Lane C: browser runs + verification evidence
ledger (append-only, sha256 hash chain).
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class DecisionRecordRow(Base, PKMixin, TimestampMixin):
    """One audited decision call (inputs sanitized before storage)."""

    __tablename__ = "decision_records"
    __table_args__ = (
        Index("ix_decision_ws_kind", "workspace_id", "kind"),
        Index("ix_decision_ws_mode", "workspace_id", "mode"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="")
    mode: Mapped[str] = mapped_column(String(20), default="SHADOW")
    requested_provider: Mapped[str] = mapped_column(String(30), default="")
    actual_provider: Mapped[str] = mapped_column(String(30), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fallback_reason: Mapped[str] = mapped_column(String(500), default="")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    agree: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    shadow_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Reserved for Lane B/C extension without a new migration.
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")


__all__ = ["BrowserRun", "DecisionRecordRow", "EvidenceRecord"]


class BrowserRun(Base, PKMixin, TimestampMixin):
    """One research-FIRST browsing session (goal, steps, evidence, cost)."""

    __tablename__ = "browser_runs"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    goal: Mapped[str] = mapped_column(Text, default="")
    # QUEUED|RUNNING|COMPLETED|ABORTED|CANCELLED|FAILED
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", index=True)
    steps_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class EvidenceRecord(Base, PKMixin, TimestampMixin):
    """Append-only verification evidence with a sha256 hash chain
    (digest/prev_digest); rows are never updated or deleted by the app."""

    __tablename__ = "evidence_records"
    __table_args__ = (
        Index("ix_evidence_ws_kind", "workspace_id", "kind"),
        Index("ix_evidence_subject", "subject_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # video|publication|campaign|research
    kind: Mapped[str] = mapped_column(String(30), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # COMPLETED|FAILED|RUNNING|UNKNOWN (what happened — separate axis)
    execution_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    # VERIFIED|PARTIALLY_VERIFIED|NOT_VERIFIED|BLOCKED (what was proven)
    verification_status: Mapped[str] = mapped_column(String(20), default="NOT_VERIFIED",
                                                     index=True)
    checks_json: Mapped[list] = mapped_column(JSON, default=list)
    digest: Mapped[str] = mapped_column(String(64), default="")
    prev_digest: Mapped[str] = mapped_column(String(64), default="")
