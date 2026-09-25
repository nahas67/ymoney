"""Long-form project models: dedicated pipeline, canonical timeline output.

A project moves through durable stages (see engine/longform/pipeline.py);
every artifact is JSON on the project row or first-class rows (chapters,
scenes). The finished timeline is a normal ContentTimeline — editable in the
Work 02 editor, rendered by the Work 02 renderer.
"""

from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

FORMATS = ("EXPLAINER", "DOCUMENTARY", "VIDEO_ESSAY", "EDUCATIONAL", "TUTORIAL",
           "NEWS_ANALYSIS", "FACELESS", "LISTICLE", "PRODUCT_EXPLAINER",
           "STORYTELLING", "PODCAST_STYLE")
STAGES = ("CREATED", "RESEARCH", "STRATEGY", "OUTLINE", "SCRIPT", "VERIFY",
          "SCENE_PLAN", "ASSET_PLAN", "ASSET_ACQUIRE", "VOICE", "TIMELINE",
          "QC", "RENDER", "METADATA", "DONE")
STATUS = ("DRAFT", "RUNNING", "WAITING_REVIEW", "CANCELLED", "FAILED", "COMPLETE")
AUTONOMY = ("AUTO", "REVIEW", "MANUAL")
BUDGETS = ("ECONOMY", "BALANCED", "PREMIUM")


class LongFormProject(Base, PKMixin, TimestampMixin):
    __tablename__ = "longform_projects"
    __table_args__ = (Index("ix_longform_ws", "workspace_id"),)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(String(400), nullable=False)
    content_format: Mapped[str] = mapped_column(String(30), default="EXPLAINER")
    target_duration_seconds: Mapped[int] = mapped_column(Integer, default=600)
    target_audience: Mapped[str] = mapped_column(String(200), default="")
    language: Mapped[str] = mapped_column(String(20), default="en")
    tone: Mapped[str] = mapped_column(String(60), default="confident, direct")
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="16:9")
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    autonomy: Mapped[str] = mapped_column(String(10), default="AUTO")
    budget_strategy: Mapped[str] = mapped_column(String(10), default="BALANCED")
    voice_name: Mapped[str] = mapped_column(String(120), default="")
    pronunciation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(20), default="CREATED", index=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    stage_progress_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    # stage artifacts (typed dicts, see engine/longform/*)
    strategy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    research_json: Mapped[dict] = mapped_column(JSON, default=dict)
    script_json: Mapped[dict] = mapped_column(JSON, default=dict)
    fact_report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    asset_plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    voice_json: Mapped[dict] = mapped_column(JSON, default=dict)
    qc_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    render_json: Mapped[dict] = mapped_column(JSON, default=dict)


class LongFormChapter(Base, PKMixin, TimestampMixin):
    __tablename__ = "longform_chapters"
    __table_args__ = (Index("ix_chapter_project", "project_id", "index"),)

    project_id: Mapped[str] = mapped_column(
        ForeignKey("longform_projects.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    narrative_role: Mapped[str] = mapped_column(String(40), default="")
    target_duration_seconds: Mapped[int] = mapped_column(Integer, default=60)
    target_words: Mapped[int] = mapped_column(Integer, default=150)
    entry_transition: Mapped[str] = mapped_column(String(20), default="cut")
    exit_transition: Mapped[str] = mapped_column(String(20), default="cut")
    retention_device: Mapped[str] = mapped_column(String(200), default="")
    script_json: Mapped[dict] = mapped_column(JSON, default=dict)  # segments[]
    status: Mapped[str] = mapped_column(String(20), default="PLANNED")
