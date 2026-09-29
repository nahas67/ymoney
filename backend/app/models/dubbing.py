"""Dubbing plan ORM model (Work 07 Lane B: speaker-aware dubbing plans).

Owned exclusively by Lane B. The typed plan lives in
`app.engine.dubbing.plan.DubbingPlan` and is stored verbatim as `plan_json`;
the columns keep list/filter queries cheap without parsing every plan.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# DRAFT | READY | REVIEW
DUBBING_PLAN_STATUSES = ("DRAFT", "READY", "REVIEW")


class DubbingPlanRow(Base, PKMixin, TimestampMixin):
    """A persisted, speaker-aware dubbing plan for one workspace."""

    __tablename__ = "dubbing_plans"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    source_ref: Mapped[str] = mapped_column(String(512), default="")
    target_language: Mapped[str] = mapped_column(String(16), default="", index=True)
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0)


__all__ = ["DUBBING_PLAN_STATUSES", "DubbingPlanRow"]
