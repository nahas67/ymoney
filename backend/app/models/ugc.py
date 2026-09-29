"""UGC projects (Work 07 Lane C): brief → editable timeline → render → QC.

`ugc_projects` carries the user brief (audience, tone, product assets,
variants), the canonical ContentTimeline it produced, the rendered MediaAsset
ref, and the QC report. Generated artifacts (script, strategy, stage lineage,
the manifest hash that protects manual edits from regeneration) live in
`lineage_json` so `brief_json` stays exactly what the user supplied.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# DRAFT | RUNNING | READY | REVIEW_REQUIRED | BLOCKED | RENDERED | FAILED
UGC_STATUSES = (
    "DRAFT", "RUNNING", "READY", "REVIEW_REQUIRED", "BLOCKED", "RENDERED", "FAILED",
)


class UgcProjectRow(Base, PKMixin, TimestampMixin):
    __tablename__ = "ugc_projects"
    __table_args__ = (
        Index("ix_ugc_ws_status", "workspace_id", "status"),
        Index("ix_ugc_ws_preset", "workspace_id", "preset"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # PRODUCT_DEMO | TESTIMONIAL | REVIEW | UNBOXING | REACTION |
    # PROBLEM_SOLUTION | FOUNDER_STYLE | TALKING_HEAD | BEFORE_AFTER
    preset: Mapped[str] = mapped_column(String(40), default="")
    brief_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    render_asset_ref: Mapped[str] = mapped_column(Text, default="")
    # UGCQCReport {status, checks, ...}
    qc_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # stage artifacts: script, strategy, presenter, broll plan, voice segments,
    # generated manifest hash + timeline version (regeneration edit-guard)
    lineage_json: Mapped[dict] = mapped_column(JSON, default=dict)
