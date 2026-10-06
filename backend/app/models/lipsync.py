"""Lip-sync job ORM model (Work 07 Lane B: replaceable lip-sync layer).

Owned exclusively by Lane B (Work 07). One row per submitted lip-sync job;
heavy inference runs in an isolated worker (`engine.lipsync.worker`), so this
row is the durable record of status, progress, failure and GPU cost.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED | TIMEOUT
LIPSYNC_STATUSES = (
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
)


class LipSyncJob(Base, PKMixin, TimestampMixin):
    """A single lip-sync render request driven by the local worker queue."""

    __tablename__ = "lipsync_jobs"
    __table_args__ = (
        # 0034 creates exactly these two composites, and they are the question an
        # operator actually asks ("this workspace's renders with unknown
        # exposure"). The single-column ``index=True`` below answers the other
        # one. Work 16 §1: both were on ``Base.metadata`` for neither.
        Index("ix_lipsync_cost_outcome", "workspace_id", "cost_outcome"),
        Index("ix_lipsync_execution_outcome", "workspace_id", "execution_outcome"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="unavailable", index=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    result_asset_ref: Mapped[str] = mapped_column(String(1024), default="")
    cost_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # inputs + adapter linkage (beyond the minimal contract: needed to run)
    video_ref: Mapped[str] = mapped_column(String(1024), default="")
    audio_ref: Mapped[str] = mapped_column(String(1024), default="")
    opts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    adapter_job_id: Mapped[str] = mapped_column(String(80), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # --- Work 15.8 §7: three facts that used to be conflated into two -------
    # `status` above is the BUSINESS status (QUEUED|RUNNING|SUCCEEDED|FAILED|
    # CANCELLED|TIMEOUT) and stays exactly that: the render pipeline, the API and
    # the frontend all read it and must not start interpreting a state they have
    # no business interpreting.
    #
    # Before this, "may this job already have been billed?" lived ONLY inside
    # `cost_json`, so an operator could not filter for it -- they had to fetch
    # every row and parse arbitrary JSON to find the ones that matter. These two
    # columns hold the CANONICAL vocabularies (SubmissionState and CostOutcome)
    # rather than new spellings:
    #
    #   status=FAILED, execution_outcome=SUBMISSION_UNKNOWN,
    #   cost_outcome=UNKNOWN_EXPOSURE
    #
    # is the honest rendering of one event: the job failed to produce a video,
    # the submit may already have been billed, and nobody knows how much.
    execution_outcome: Mapped[str] = mapped_column(String(24), default="",
                                                   index=True,
                                                   server_default=text("''"))
    cost_outcome: Mapped[str] = mapped_column(String(24), default="", index=True,
                                              server_default=text("''"))


__all__ = ["LIPSYNC_STATUSES", "LipSyncJob"]
