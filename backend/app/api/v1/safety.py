"""Safety Center + Decision Intelligence APIs.

- GET/PUT  /workspaces/{id}/safety          — safety settings with safe defaults
- GET      /workspaces/{id}/decision        — live NEXT BEST ACTION preview with full WHY
- POST     /workspaces/{id}/autopilot/simulate — run N fully-mocked cycles (SIMULATION)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db import get_db
from app.engine.decision import decide_next_best_action, get_safety_settings
from app.models import (
    AgentRun,
    ContentItem,
    CostEntry,
    Cycle,
    PostMetric,
    PublishedPost,
    Workspace,
)
from app.services.auth_service import require_workspace_role

safety_router = APIRouter(prefix="/workspaces/{workspace_id}/safety", tags=["safety"])


class SafetyBody(BaseModel):
    daily_budget_usd: float | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    per_video_budget_usd: float | None = Field(default=None, ge=0)
    max_videos_per_day: int | None = Field(default=None, ge=1, le=100)
    max_uploads_per_hour: int | None = Field(default=None, ge=1, le=60)
    min_qc_score: int | None = Field(default=None, ge=0, le=100)
    max_render_attempts: int | None = Field(default=None, ge=1, le=5)
    max_consecutive_failures: int | None = Field(default=None, ge=1, le=10)
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    require_human_review_risk_above: float | None = Field(default=None, ge=0, le=100)
    produce_score_threshold: float | None = Field(default=None, ge=0, le=100)
    max_concurrent_renders: int | None = Field(default=None, ge=1, le=8)


@safety_router.get("")
def get_safety(ws: Workspace = Depends(require_workspace_role("viewer"))):
    merged = get_safety_settings(ws.settings_json or {})
    return {"safety": merged}


@safety_router.put("")
def put_safety(
    body: SafetyBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    current = dict((ws.settings_json or {}).get("safety", {}))
    updates = body.model_dump(exclude_none=True)
    for k, v in updates.items():
        current[k] = v
    merged_settings = dict(ws.settings_json or {})
    merged_settings["safety"] = current
    ws.settings_json = merged_settings
    db.commit()
    return {"safety": get_safety_settings(merged_settings)}


# ---------------------------------------------------------------------------
# Decision preview (WHY panel data source)
# ---------------------------------------------------------------------------

decision_router = APIRouter(prefix="/workspaces/{workspace_id}/decision", tags=["decision"])


@decision_router.get("")
def next_best_action_preview(ws: Workspace = Depends(require_workspace_role("viewer"))):
    """Live NEXT BEST ACTION with the complete WHY breakdown."""
    decision = decide_next_best_action(ws.id)
    return decision.why()


# ---------------------------------------------------------------------------
# Cost intelligence
# ---------------------------------------------------------------------------

cost_intel_router = APIRouter(prefix="/workspaces/{workspace_id}/costs", tags=["costs"])


@cost_intel_router.get("/intelligence")
def cost_intelligence(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Per-cycle / per-video / per-platform / per-agent / efficiency economics."""
    total = db.scalar(
        select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(CostEntry.workspace_id == ws.id)
    ) or 0.0

    cycles_completed = db.scalar(
        select(func.count()).select_from(Cycle).where(
            Cycle.workspace_id == ws.id, Cycle.status == "COMPLETED"
        )
    ) or 0
    videos_built = db.scalar(
        select(func.count()).select_from(ContentItem).where(ContentItem.workspace_id == ws.id)
    ) or 0
    published = db.scalars(select(PublishedPost).where(PublishedPost.workspace_id == ws.id)).all()
    post_ids = [p.id for p in published]

    views = 0
    if post_ids:
        metric_rows = db.scalars(
            select(PostMetric).where(PostMetric.post_id.in_(post_ids)).order_by(PostMetric.captured_at.asc())
        ).all()
        latest = {}
        for m in metric_rows:
            latest[m.post_id] = m
        views = sum(m.views for m in latest.values())

    by_category = dict(db.execute(
        select(CostEntry.category, func.sum(CostEntry.amount_usd))
        .where(CostEntry.workspace_id == ws.id)
        .group_by(CostEntry.category)
    ).all())

    by_agent = {
        row[0]: round(float(row[1] or 0), 4)
        for row in db.execute(
            select(AgentRun.agent_key, func.sum(AgentRun.cost_usd))
            .where(AgentRun.workspace_id == ws.id)
            .group_by(AgentRun.agent_key)
        ).all()
    }

    by_platform: dict[str, int] = {}
    for p in published:
        by_platform[p.platform] = by_platform.get(p.platform, 0) + 1

    def _div(a, b):
        return round(a / b, 4) if b else None

    return {
        "total_cost_usd": round(float(total), 4),
        "per_cycle_usd": _div(float(total), cycles_completed),
        "per_video_usd": _div(float(total), videos_built),
        "per_publication_usd": _div(float(total), len(published)),
        "cost_per_1000_views_usd": _div(float(total) * 1000, views) if views else None,
        "by_category": {k: round(float(v or 0), 4) for k, v in by_category.items()},
        "by_agent": by_agent,
        "publications_by_platform": by_platform,
        "totals": {
            "cycles": cycles_completed,
            "videos_built": videos_built,
            "posts_published": len(published),
            "views": views,
        },
        # estimated return requires real revenue data — never fabricated.
        "estimated_return_usd": None,
        "estimated_return_note": "requires monetization API access; not simulated",
    }


# Simulation mode removed — production only (see ARCHITECTURE_V2.md).



from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.db import get_db
from app.services.auth_service import require_workspace_role

safety_router = APIRouter(prefix="/workspaces/{workspace_id}/safety", tags=["safety"])


class SafetyBody(BaseModel):
    daily_budget_usd: float | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    per_video_budget_usd: float | None = Field(default=None, ge=0)
    max_videos_per_day: int | None = Field(default=None, ge=1, le=100)
    max_uploads_per_hour: int | None = Field(default=None, ge=1, le=60)
    min_qc_score: int | None = Field(default=None, ge=0, le=100)
    max_render_attempts: int | None = Field(default=None, ge=1, le=5)
    max_consecutive_failures: int | None = Field(default=None, ge=1, le=10)
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    require_human_review_risk_above: float | None = Field(default=None, ge=0, le=100)
    produce_score_threshold: float | None = Field(default=None, ge=0, le=100)
    max_concurrent_renders: int | None = Field(default=None, ge=1, le=8)


@safety_router.get("")
def get_safety(ws: Workspace = Depends(require_workspace_role("viewer"))):
    merged = get_safety_settings(ws.settings_json or {})
    return {"safety": merged}


@safety_router.put("")
def put_safety(
    body: SafetyBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    current = dict((ws.settings_json or {}).get("safety", {}))
    updates = body.model_dump(exclude_none=True)
    for k, v in updates.items():
        current[k] = v
    merged_settings = dict(ws.settings_json or {})
    merged_settings["safety"] = current
    ws.settings_json = merged_settings
    db.commit()
    return {"safety": get_safety_settings(merged_settings)}


# ---------------------------------------------------------------------------
# Decision preview (WHY panel data source)
# ---------------------------------------------------------------------------

decision_router = APIRouter(prefix="/workspaces/{workspace_id}/decision", tags=["decision"])


@decision_router.get("")
def next_best_action_preview(ws: Workspace = Depends(require_workspace_role("viewer"))):
    """Live NEXT BEST ACTION with the complete WHY breakdown."""
    decision = decide_next_best_action(ws.id)
    return decision.why()


# ---------------------------------------------------------------------------
# Cost intelligence
# ---------------------------------------------------------------------------

cost_intel_router = APIRouter(prefix="/workspaces/{workspace_id}/costs", tags=["costs"])


@cost_intel_router.get("/intelligence")
def cost_intelligence(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Per-cycle / per-video / per-platform / per-agent / efficiency economics."""
    total = db.scalar(
        select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(CostEntry.workspace_id == ws.id)
    ) or 0.0

    cycles_completed = db.scalar(
        select(func.count()).select_from(Cycle).where(
            Cycle.workspace_id == ws.id, Cycle.status == "COMPLETED"
        )
    ) or 0
    videos_built = db.scalar(
        select(func.count()).select_from(ContentItem).where(ContentItem.workspace_id == ws.id)
    ) or 0
    published = db.scalars(select(PublishedPost).where(PublishedPost.workspace_id == ws.id)).all()
    post_ids = [p.id for p in published]

    views = 0
    if post_ids:
        metric_rows = db.scalars(
            select(PostMetric).where(PostMetric.post_id.in_(post_ids)).order_by(PostMetric.captured_at.asc())
        ).all()
        latest = {}
        for m in metric_rows:
            latest[m.post_id] = m
        views = sum(m.views for m in latest.values())

    by_category = dict(db.execute(
        select(CostEntry.category, func.sum(CostEntry.amount_usd))
        .where(CostEntry.workspace_id == ws.id)
        .group_by(CostEntry.category)
    ).all())

    by_agent = {
        row[0]: round(float(row[1] or 0), 4)
        for row in db.execute(
            select(AgentRun.agent_key, func.sum(AgentRun.cost_usd))
            .where(AgentRun.workspace_id == ws.id)
            .group_by(AgentRun.agent_key)
        ).all()
    }

    by_platform: dict[str, int] = {}
    for p in published:
        by_platform[p.platform] = by_platform.get(p.platform, 0) + 1

    def _div(a, b):
        return round(a / b, 4) if b else None

    return {
        "total_cost_usd": round(float(total), 4),
        "per_cycle_usd": _div(float(total), cycles_completed),
        "per_video_usd": _div(float(total), videos_built),
        "per_publication_usd": _div(float(total), len(published)),
        "cost_per_1000_views_usd": _div(float(total) * 1000, views) if views else None,
        "by_category": {k: round(float(v or 0), 4) for k, v in by_category.items()},
        "by_agent": by_agent,
        "publications_by_platform": by_platform,
        "totals": {
            "cycles": cycles_completed,
            "videos_built": videos_built,
            "posts_published": len(published),
            "views": views,
        },
        # estimated return requires real revenue data — never fabricated.
        "estimated_return_usd": None,
        "estimated_return_note": "requires monetization API access; not simulated",
    }


# ---------------------------------------------------------------------------
# Simulation mode
# ---------------------------------------------------------------------------
