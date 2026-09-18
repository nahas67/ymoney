"""Publishing, analytics, agents, activity (SSE), system health, logs, costs."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core import security
from app.core.config import settings
from app.db import get_db
from app.engine.agents.registry import AGENT_META
from app.models import (
    AgentConfig,
    AgentRun,
    ContentItem,
    CostEntry,
    EventLog,
    LearningPattern,
    MemoryRecord,
    PostMetric,
    PublishedPost,
    PublishingJob,
    SocialAccount,
    SystemLog,
    Workspace,
)
from app.models.base import utcnow
from app.services import jobs as jobs_service
from app.services import memory as memory_service
from app.services.auth_service import require_workspace_role

# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

publishing_router = APIRouter(prefix="/workspaces/{workspace_id}/publishing", tags=["publishing"])


class ConnectAccountBody(BaseModel):
    platform: str = Field(pattern="^(youtube|tiktok|facebook|instagram)$")
    display_name: str = Field(default="", max_length=200)
    access_token: str = Field(default="", max_length=8000)
    refresh_token: str = Field(default="", max_length=8000)
    external_id: str = Field(default="", max_length=200)


@publishing_router.get("/accounts")
def list_accounts(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(select(SocialAccount).where(SocialAccount.workspace_id == ws.id)).all()
    return {
        "items": [
            {
                "id": a.id,
                "platform": a.platform,
                "display_name": a.display_name,
                "external_id": a.external_id[:6] + "…" if a.external_id else "",
                "status": a.status,
                "token_expires_at": a.token_expires_at.isoformat() + "Z" if a.token_expires_at else None,
            }
            for a in rows
        ]
    }


@publishing_router.post("/accounts", summary="Connect an account (tokens stored encrypted)")
def connect_account(body: ConnectAccountBody, ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db)):
    row = SocialAccount(
        workspace_id=ws.id,
        platform=body.platform,
        display_name=body.display_name or body.platform,
        external_id=body.external_id,
        access_token_enc=security.encrypt_secret(body.access_token),
        refresh_token_enc=security.encrypt_secret(body.refresh_token),
        status="connected" if body.access_token or body.refresh_token else "error",
    )
    db.add(row)
    db.commit()
    from app.services.events import record_event

    record_event(
        ws.id,
        "account.connected",
        f"{body.platform} account connected"
        + (" (mock mode active — publishing stays simulated)" if settings.mock_publishing else ""),
        level="info",
        source="publishing",
    )
    return {"id": row.id}


@publishing_router.delete("/accounts/{account_id}")
def disconnect_account(account_id: str, ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db)):
    row = db.get(SocialAccount, account_id)
    if not row or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="account not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# OAuth connect flows (real platform authorization)
# ---------------------------------------------------------------------------


@publishing_router.get("/oauth/youtube/start")
def oauth_youtube_start(ws: Workspace = Depends(require_workspace_role("admin"))):
    from app.services import oauth_service

    try:
        return oauth_service.youtube_start(ws.id)
    except oauth_service.OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@publishing_router.get("/oauth/tiktok/start")
def oauth_tiktok_start(ws: Workspace = Depends(require_workspace_role("admin"))):
    from app.services import oauth_service

    try:
        return oauth_service.tiktok_start(ws.id)
    except oauth_service.OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@publishing_router.get("/oauth/facebook/start")
def oauth_facebook_start(ws: Workspace = Depends(require_workspace_role("admin"))):
    from app.services import oauth_service

    try:
        return oauth_service.meta_start(ws.id, "facebook")
    except oauth_service.OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@publishing_router.get("/oauth/instagram/start")
def oauth_instagram_start(ws: Workspace = Depends(require_workspace_role("admin"))):
    from app.services import oauth_service

    try:
        return oauth_service.meta_start(ws.id, "instagram")
    except oauth_service.OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@publishing_router.get("/oauth/youtube/callback")
def oauth_youtube_callback(workspace_id: str, code: str = "", state: str = "", error: str = ""):
    """Browser-facing callback. Returns a tiny HTML page that closes the popup."""
    from fastapi.responses import HTMLResponse

    from app.services import oauth_service

    ok, message = True, "YouTube connected — you can close this window."
    if error:
        ok, message = False, f"Authorization failed: {error}"
    else:
        try:
            oauth_service.youtube_callback(workspace_id, code, state)
        except oauth_service.OAuthError as exc:
            ok, message = False, str(exc)

    color = "#10b981" if ok else "#ef4444"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>YMONEY</title>
        <style>body{{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#09090b;color:#f4f4f5}}
        .box{{text-align:center}}.dot{{width:12px;height:12px;border-radius:50%;background:{color};display:inline-block;margin-right:8px}}</style></head>
        <body><div class="box"><h2><span class="dot"></span>{'Connected' if ok else 'Connection failed'}</h2>
        <p style="color:#8b8b93">{message}</p>
        <script>setTimeout(()=>window.close(),1500);</script></div></body></html>"""
    )


def _oauth_callback_page(ok: bool, message: str):
    from fastapi.responses import HTMLResponse

    color = "#10b981" if ok else "#ef4444"
    title = "Connected" if ok else "Connection failed"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>YMONEY</title>
        <style>body{{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#09090b;color:#f4f4f5}}
        .box{{text-align:center}}.dot{{width:12px;height:12px;border-radius:50%;background:{color};display:inline-block;margin-right:8px}}</style></head>
        <body><div class="box"><h2><span class="dot"></span>{title}</h2>
        <p style="color:#8b8b93">{message}</p>
        <script>setTimeout(()=>window.close(),1500);</script></div></body></html>"""
    )


@publishing_router.get("/oauth/tiktok/callback")
def oauth_tiktok_callback(workspace_id: str, code: str = "", state: str = "", error: str = ""):
    from app.services import oauth_service

    if error:
        return _oauth_callback_page(False, f"Authorization failed: {error}")
    try:
        oauth_service.tiktok_callback(workspace_id, code, state)
        return _oauth_callback_page(True, "TikTok connected — you can close this window.")
    except oauth_service.OAuthError as exc:
        return _oauth_callback_page(False, str(exc))


@publishing_router.get("/oauth/facebook/callback")
def oauth_facebook_callback(workspace_id: str, code: str = "", state: str = "", error: str = ""):
    from app.services import oauth_service

    if error:
        return _oauth_callback_page(False, f"Authorization failed: {error}")
    try:
        oauth_service.meta_callback(workspace_id, "facebook", code, state)
        return _oauth_callback_page(True, "Facebook connected — you can close this window.")
    except oauth_service.OAuthError as exc:
        return _oauth_callback_page(False, str(exc))


@publishing_router.get("/oauth/instagram/callback")
def oauth_instagram_callback(workspace_id: str, code: str = "", state: str = "", error: str = ""):
    from app.services import oauth_service

    if error:
        return _oauth_callback_page(False, f"Authorization failed: {error}")
    try:
        oauth_service.meta_callback(workspace_id, "instagram", code, state)
        return _oauth_callback_page(True, "Instagram connected — you can close this window.")
    except oauth_service.OAuthError as exc:
        return _oauth_callback_page(False, str(exc))


@publishing_router.get("/jobs")
def list_publishing_jobs(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db), limit: int = 100):
    from app.models.content import ContentItem, Video, VideoVariant

    rows = db.execute(
        select(PublishingJob, ContentItem.topic, ContentItem.id.label("cid"))
        .join(Video, PublishingJob.video_id == Video.id)
        .join(VideoVariant, Video.variant_id == VideoVariant.id)
        .outerjoin(ContentItem, VideoVariant.content_item_id == ContentItem.id)
        .where(PublishingJob.workspace_id == ws.id)
        .order_by(PublishingJob.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "id": j.id,
                "platform": j.platform,
                "status": j.status,
                "remote_url": j.remote_url,
                "remote_post_id": j.remote_post_id,
                "attempt": j.attempt,
                "error": j.error[:300],
                "content_item_id": cid,
                "content_topic": topic[:120] if topic else None,
                "scheduled_at": j.scheduled_at.isoformat() + "Z" if j.scheduled_at else None,
                "published_at": j.published_at.isoformat() + "Z" if j.published_at else None,
                "created_at": j.created_at.isoformat() + "Z",
            }
            for j, topic, cid in rows
        ]
    }


@publishing_router.get("/posts")
def list_published_posts(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db), limit: int = 100):
    rows = db.scalars(
        select(PublishedPost).where(PublishedPost.workspace_id == ws.id).order_by(PublishedPost.published_at.desc()).limit(limit)
    ).all()
    latest_metrics = {}
    if rows:
        metrics = db.scalars(
            select(PostMetric).where(PostMetric.post_id.in_([p.id for p in rows])).order_by(PostMetric.captured_at.asc())
        ).all()
        for m in metrics:
            latest_metrics[m.post_id] = m
    return {
        "items": [
            {
                "id": p.id,
                "platform": p.platform,
                "title": p.title,
                "remote_url": p.remote_url,
                "published_at": p.published_at.isoformat() + "Z" if p.published_at else None,
                "is_mock": p.is_mock,
                "metrics": {
                    "views": latest_metrics[p.id].views if p.id in latest_metrics else 0,
                    "likes": latest_metrics[p.id].likes if p.id in latest_metrics else 0,
                    "comments": latest_metrics[p.id].comments if p.id in latest_metrics else 0,
                    "completion_rate": round(latest_metrics[p.id].completion_rate, 3) if p.id in latest_metrics else None,
                },
            }
            for p in rows
        ]
    }


# ---------------------------------------------------------------------------
# Analytics overview + learning patterns
# ---------------------------------------------------------------------------

analytics_router = APIRouter(prefix="/workspaces/{workspace_id}/analytics", tags=["analytics"])


def _latest_metric_map(db, post_ids):
    if not post_ids:
        return {}
    out = {}
    for m in db.scalars(
        select(PostMetric).where(PostMetric.post_id.in_(post_ids)).order_by(PostMetric.captured_at.asc())
    ):
        out[m.post_id] = m
    return out


@analytics_router.get("/overview", summary="Channel-level performance snapshot")
def analytics_overview(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    posts = db.scalars(select(PublishedPost).where(PublishedPost.workspace_id == ws.id)).all()
    metrics = _latest_metric_map(db, [p.id for p in posts])
    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "followers_gained": 0}
    per_platform: dict[str, dict] = {}
    for p in posts:
        m = metrics.get(p.id)
        if not m:
            continue
        totals["views"] += m.views
        totals["likes"] += m.likes
        totals["comments"] += m.comments
        totals["shares"] += m.shares
        totals["followers_gained"] += m.followers_gained
        slot = per_platform.setdefault(p.platform, {"posts": 0, "views": 0})
        slot["posts"] += 1
        slot["views"] += m.views
    content_count = db.scalar(
        select(func.count()).select_from(ContentItem).where(ContentItem.workspace_id == ws.id)
    )
    cost_total = db.scalar(select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(CostEntry.workspace_id == ws.id))
    best_post = max(
        ((p, m) for p, m in zip(posts, [metrics.get(p.id) for p in posts]) if m),
        key=lambda pm: pm[1].views,
        default=(None, None),
    )
    return {
        "totals": totals,
        "posts_published": len(posts),
        "content_items": content_count or 0,
        "cost_total_usd": round(float(cost_total or 0.0), 4),
        "per_platform": per_platform,
        "best_post": (
            {
                "platform": best_post[0].platform,
                "title": best_post[0].title,
                "views": best_post[1].views,
            }
            if best_post[0]
            else None
        ),
        "mock_analytics": settings.mock_analytics,
    }


@analytics_router.get("/patterns", summary="Learned performance patterns")
def learning_patterns(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(
        select(LearningPattern).where(LearningPattern.workspace_id == ws.id).order_by(LearningPattern.updated_at.desc())
    ).all()
    return {
        "items": [
            {
                "pattern_key": p.pattern_key,
                "description": p.description,
                "improvement_pct": p.observed_improvement_pct,
                "confidence": p.confidence,
                "sample_size": p.sample_size,
                "active": p.active,
                "updated_at": p.updated_at.isoformat() + "Z",
            }
            for p in rows
        ]
    }


@analytics_router.get("/breakdowns", summary="Performance breakdowns by topic, hook style, and duration")
def analytics_breakdowns(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Group measured posts by observable content features (topic, hook style,
    duration bucket). Mock and real posts are counted separately — never mixed."""
    from app.models.content import ContentItem, Video, VideoVariant

    posts = db.scalars(select(PublishedPost).where(PublishedPost.workspace_id == ws.id)).all()
    metrics = _latest_metric_map(db, [p.id for p in posts])

    video_ids = [p.video_id for p in posts if p.video_id]
    videos = (
        {v.id: v for v in db.scalars(select(Video).where(Video.id.in_(video_ids))).all()}
        if video_ids
        else {}
    )
    variant_ids = [v.variant_id for v in videos.values() if v.variant_id]
    variants = (
        {vr.id: vr for vr in db.scalars(select(VideoVariant).where(VideoVariant.id.in_(variant_ids))).all()}
        if variant_ids
        else {}
    )
    ci_ids = [vr.content_item_id for vr in variants.values() if vr.content_item_id]
    items = (
        {c.id: c for c in db.scalars(select(ContentItem).where(ContentItem.id.in_(ci_ids))).all()}
        if ci_ids
        else {}
    )

    def bucket_duration(sec: float | None) -> str:
        if sec is None:
            return "unknown"
        if sec <= 20:
            return "≤20s"
        if sec <= 35:
            return "21–35s"
        if sec <= 60:
            return "36–60s"
        return ">60s"

    def hook_style(hook: str) -> str:
        h = (hook or "").strip()
        if not h:
            return "unknown"
        if h.endswith("?"):
            return "question"
        first = h.split()[0].lower() if h.split() else ""
        if first in ("stop", "never", "always", "don't", "dont"):
            return "command"
        if any(c.isdigit() for c in h[:20]):
            return "number"
        return "statement"

    groups: dict[str, dict[str, dict]] = {
        "by_topic": {},
        "by_hook_style": {},
        "by_duration": {},
    }

    def add(bucket: dict, key: str, m, is_mock: bool):
        slot = bucket.setdefault(key, {"posts": 0, "mock_posts": 0, "views": 0, "likes": 0, "engagement_sum": 0.0})
        if m is None:
            return
        slot["posts"] += 1
        if is_mock:
            slot["mock_posts"] += 1
        slot["views"] += m.views
        slot["likes"] += m.likes
        eng = (m.likes + m.comments + m.shares) / m.views if m.views else 0.0
        slot["engagement_sum"] += eng

    for p in posts:
        m = metrics.get(p.id)
        video = videos.get(p.video_id)
        variant = variants.get(video.variant_id) if video else None
        item = items.get(variant.content_item_id) if variant else None
        topic = (item.topic if item else p.title or "(untitled)")[:80]
        duration = video.duration_seconds if video else None
        add(groups["by_topic"], topic, m, p.is_mock)
        add(groups["by_hook_style"], hook_style(variant.hook if variant else ""), m, p.is_mock)
        add(groups["by_duration"], bucket_duration(duration), m, p.is_mock)

    def finish(bucket: dict) -> list[dict]:
        out = []
        for key, s in bucket.items():
            n = s["posts"]
            out.append({
                "key": key,
                "posts": n,
                "mock_posts": s["mock_posts"],
                "total_views": s["views"],
                "avg_views": round(s["views"] / n) if n else 0,
                "engagement_pct": round(100 * s["engagement_sum"] / n, 2) if n else 0.0,
            })
        out.sort(key=lambda r: r["total_views"], reverse=True)
        return out

    return {
        "by_topic": finish(groups["by_topic"]),
        "by_hook_style": finish(groups["by_hook_style"]),
        "by_duration": finish(groups["by_duration"]),
        "posts_with_metrics": sum(1 for p in posts if metrics.get(p.id)),
        "mock_analytics": settings.mock_analytics,
    }


# ---------------------------------------------------------------------------
# Agents center
# ---------------------------------------------------------------------------

agents_router = APIRouter(prefix="/workspaces/{workspace_id}/agents", tags=["agents"])


class AgentConfigBody(BaseModel):
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=120)
    prompt_override: str | None = Field(default=None, max_length=8000)
    timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    cost_limit_usd: float | None = None


@agents_router.get("/capabilities", summary="List registered agent skills and tools")
def list_agent_capabilities(ws: Workspace = Depends(require_workspace_role("viewer"))):
    from app.engine.agents.registry import agent_catalog
    from app.engine.capabilities import skill_catalog, tool_catalog

    return {
        "agents": agent_catalog(),
        "skills": skill_catalog(),
        "tools": tool_catalog(),
    }


@agents_router.get("/tool-audits", summary="List tool execution audit records")
def list_tool_audits(ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db), limit: int = 100):
    from app.models import ToolCallAudit
    rows = db.scalars(
        select(ToolCallAudit).where(ToolCallAudit.workspace_id == ws.id)
        .order_by(ToolCallAudit.created_at.desc()).limit(min(limit, 500))
    ).all()
    return {"items": [{
        "id": row.id, "agent_key": row.agent_key, "tool_name": row.tool_name,
        "status": row.status, "job_id": row.job_id, "cycle_id": row.cycle_id,
        "input_summary": row.input_summary, "output_summary": row.output_summary,
        "error": row.error, "duration_ms": row.duration_ms,
        "estimated_cost_usd": row.estimated_cost_usd, "actual_cost_usd": row.actual_cost_usd,
        "created_at": row.created_at.isoformat() + "Z",
    } for row in rows]}


@agents_router.get("/config")
def list_agent_configs(workspace_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    from app.engine.agents.registry import AGENT_META

    rows = {
        r.agent_key: r
        for r in db.scalars(select(AgentConfig).where(AgentConfig.workspace_id == ws.id)).all()
    }
    items = []
    for key, meta in AGENT_META.items():
        cfg = rows.get(key)
        items.append(
            {
                "key": key,
                "title": meta.title,
                "description": meta.description,
                **meta.capability_catalog(),
                "enabled": cfg.enabled if cfg else True,
                "model": (cfg.model if cfg else "") or "",
                "timeout_seconds": cfg.timeout_seconds if cfg else 300,
                "cost_limit_usd": cfg.cost_limit_usd if cfg else None,
            }
        )
    return {"items": items}


@agents_router.get("/{agent_key}")
def agent_detail(
    workspace_id: str,
    agent_key: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    """Per-agent detail: catalog info + config + run history + aggregates."""
    from app.engine.agents.registry import AGENT_META

    if agent_key not in AGENT_META:
        raise HTTPException(status_code=404, detail="unknown agent")
    meta = AGENT_META[agent_key]
    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.workspace_id == ws.id, AgentRun.agent_key == agent_key)
        .order_by(AgentRun.created_at.desc())
        .limit(50)
    ).all()
    stats = db.execute(
        select(
            func.count().label("runs"),
            func.sum(AgentRun.status == "FAILED").label("failures"),
            func.avg(AgentRun.duration_ms).label("avg_ms"),
            func.sum(AgentRun.cost_usd).label("cost"),
        ).where(AgentRun.workspace_id == ws.id, AgentRun.agent_key == agent_key)
    ).one()
    cfg_row = db.scalar(
        select(AgentConfig).where(
            AgentConfig.workspace_id == ws.id, AgentConfig.agent_key == agent_key
        )
    )
    total_runs = int(stats.runs or 0)
    failures = int(stats.failures or 0)
    return {
        "key": meta.key,
        "title": meta.title,
        "description": meta.description,
        **meta.capability_catalog(),
        "enabled": cfg_row.enabled if cfg_row else True,
        "model": (cfg_row.model if cfg_row else "") or "",
        "timeout_seconds": cfg_row.timeout_seconds if cfg_row else 300,
        "cost_limit_usd": cfg_row.cost_limit_usd if cfg_row else None,
        "stats": {
            "runs": total_runs,
            "failure_rate": round(failures / total_runs, 3) if total_runs else 0.0,
            "avg_duration_ms": int(stats.avg_ms) if stats.avg_ms is not None else None,
            "total_cost_usd": round(float(stats.cost or 0.0), 4),
        },
        "runs": [
            {
                "id": r.id,
                "task_type": r.task_type,
                "status": r.status,
                "input_summary": (r.input_summary or "")[:200],
                "output_summary": (r.output_summary or "")[:300],
                "duration_ms": r.duration_ms,
                "cost_usd": r.cost_usd,
                "error": (r.error or "")[:300],
                "steps": ((r.steps_json or {}).get("steps", []) if r.steps_json else [])[:40],
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in runs
        ],
    }


@agents_router.put("/config/{agent_key}")
def update_agent_config(
    workspace_id: str,
    agent_key: str,
    body: AgentConfigBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.engine.agents.registry import AGENT_META

    if agent_key not in AGENT_META:
        raise HTTPException(status_code=404, detail="unknown agent")
    row = db.scalar(
        select(AgentConfig).where(AgentConfig.workspace_id == ws.id, AgentConfig.agent_key == agent_key)
    )
    if not row:
        row = AgentConfig(workspace_id=ws.id, agent_key=agent_key)
        db.add(row)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.model is not None:
        row.model = body.model
    if body.prompt_override is not None:
        row.prompt_override = body.prompt_override[:8000]
    if body.timeout_seconds is not None:
        row.timeout_seconds = body.timeout_seconds
    if body.cost_limit_usd is not None:
        row.cost_limit_usd = body.cost_limit_usd
    db.commit()
    return {"ok": True}


@agents_router.get("")
def agent_status(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    stats_rows = db.execute(
        select(
            AgentRun.agent_key,
            func.count().label("runs"),
            func.sum(AgentRun.status == "FAILED").label("failures"),
            func.avg(AgentRun.duration_ms).label("avg_ms"),
            func.sum(AgentRun.cost_usd).label("cost"),
        )
        .where(AgentRun.workspace_id == ws.id)
        .group_by(AgentRun.agent_key)
    ).all()
    by_key = {r.agent_key: r for r in stats_rows}
    cfg_rows = {
        c.agent_key: c
        for c in db.scalars(select(AgentConfig).where(AgentConfig.workspace_id == ws.id)).all()
    }
    items = []
    for key, meta in AGENT_META.items():
        r = by_key.get(key)
        runs = int(r.runs or 0) if r else 0
        failures = int(r.failures or 0) if r else 0
        cfg = cfg_rows.get(key)
        current = db.scalar(
            select(AgentRun)
            .where(AgentRun.workspace_id == ws.id, AgentRun.agent_key == key, AgentRun.status == "RUNNING")
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        items.append(
            {
                "key": key,
                "title": meta.title,
                "description": meta.description,
                **meta.capability_catalog(),
                "enabled": cfg.enabled if cfg else True,
                "status": "busy" if current else "idle",
                "current_task": current.task_type if current else None,
                "runs": runs,
                "failure_rate": round(failures / runs, 3) if runs else 0.0,
                "avg_duration_ms": int(r.avg_ms or 0) if r and r.avg_ms is not None else None,
                "total_cost_usd": round(float(r.cost or 0.0), 4) if r else 0.0,
            }
        )
    recent = db.scalars(
        select(AgentRun).where(AgentRun.workspace_id == ws.id).order_by(AgentRun.created_at.desc()).limit(30)
    ).all()
    return {
        "items": items,
        "recent_runs": [
            {
                "agent_key": run.agent_key,
                "task_type": run.task_type,
                "status": run.status,
                "duration_ms": run.duration_ms,
                "output_summary": run.output_summary[:200],
                "created_at": run.created_at.isoformat() + "Z",
            }
            for run in recent
        ],
    }


# ---------------------------------------------------------------------------
# Activity feed (SSE + recent)
# ---------------------------------------------------------------------------

activity_router = APIRouter(prefix="/workspaces/{workspace_id}/activity", tags=["activity"])


@activity_router.get("/recent")
def recent_activity(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db), limit: int = 50):
    rows = db.scalars(
        select(EventLog).where(EventLog.workspace_id == ws.id).order_by(EventLog.created_at.desc()).limit(min(limit, 200))
    ).all()
    return {
        "items": [
            {
                "id": e.id,
                "level": e.level,
                "source": e.source,
                "kind": e.kind,
                "message": e.message,
                "data": e.data_json or {},
                "created_at": e.created_at.isoformat() + "Z",
            }
            for e in reversed(rows)
        ]
    }


@activity_router.get("/stream")
async def activity_stream(
    workspace_id: str,
    request: Request,
    token: str | None = None,
    db=Depends(get_db),
):
    """Server-Sent Events stream of live workspace activity.

    EventSource cannot send Authorization headers, so the bearer token may be
    supplied as ?token= as an alternative to the header. Header auth still
    takes precedence when present.
    """
    from sqlalchemy import select as _select

    from app.core.security import decode_access_token
    from app.models import User, WorkspaceMember
    from app.services.events import subscribe, unsubscribe

    bearer_token: str | None = token
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip() or bearer_token
    payload = decode_access_token(bearer_token) if bearer_token else None
    user = db.get(User, payload.get("sub", "")) if payload else None
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="not authenticated")
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="workspace not found")
    member = db.scalar(
        _select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if member is None and not user.is_superuser:
        raise HTTPException(status_code=403, detail="not a workspace member")

    token_q = await subscribe(ws.id)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(token_q.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await unsubscribe(ws.id, token_q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Jobs / system / logs / costs
# ---------------------------------------------------------------------------

system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/health")
def health():
    from app.providers.video_engine.factory import get_video_engine

    try:
        engine = get_video_engine()
        engine_ok = engine.health()
        engine_name = engine.engine_name
        engine_version = engine.version()
    except Exception:
        engine_ok = False
        engine_name = (settings.video_engine or "unknown").lower()
        engine_version = None
    db_ok = True
    try:
        from sqlalchemy import text as _t

        from app.db import session_scope

        with session_scope() as s:
            s.execute(_t("SELECT 1"))
    except Exception:
        db_ok = False
    llm_ok = True if settings.mock_llm else _llm_probe()
    tts_status = {}
    try:
        from app.providers.tts import tts_provider_status

        tts_status = tts_provider_status()
    except Exception:
        tts_status = {"provider": "unknown", "healthy": False}
    return {
        "status": "healthy" if (db_ok and engine_ok) else "degraded",
        "database": db_ok,
        "video_engine": engine_ok,
        "video_engine_name": engine_name,
        "video_engine_version": engine_version,
        "llm_provider": llm_ok,
        "tts": tts_status,
        "publishers": _publisher_status(),
        "mocks": {
            "llm": bool(settings.mock_llm),
            "trends": bool(settings.mock_trends),
            "publishing": bool(settings.mock_publishing),
            "analytics": bool(settings.mock_analytics),
            "video_engine": engine_name in ("mock", "simulation"),
        },
        "time": utcnow().isoformat() + "Z",
    }


def _llm_probe() -> bool:
    """Cheap reachability probe of the configured LLM endpoint."""
    import httpx

    try:
        resp = httpx.get(f"{settings.openai_base_url.rstrip('/')}/models", timeout=5)
        return resp.status_code < 500
    except httpx.HTTPError:
        return False


def _publisher_status() -> dict:
    from app.providers.publishers.factory import relay_ready

    relay = relay_ready()
    from sqlalchemy import func, select

    from app.db import get_db
    from app.models import SocialAccount

    connected: dict = {}
    try:
        db = next(get_db())
        try:
            rows = (
                db.execute(
                    select(SocialAccount.platform, func.count())
                    .where(SocialAccount.status == "connected")
                    .group_by(SocialAccount.platform)
                ).all()
            )
            connected = {p: c for p, c in rows}
        finally:
            db.close()
    except Exception:
        connected = {}
    out = {}
    for name, detail in [
        ("youtube", "OAuth upload (youtube.upload scope)"),
        ("tiktok", "content posting API / relay"),
        ("facebook", "graph API / relay"),
        ("instagram", "relay only"),
    ]:
        has_account = connected.get(name, 0) > 0
        real_path = has_account or relay
        out[name] = {
            "mode": "mock" if (settings.mock_publishing and not real_path) else "real",
            "ready": bool(real_path),
            "detail": (
                f"connected account ×{connected[name]}" if has_account
                else "via Upload-Post relay" if relay
                else "not configured — connect account or add relay key"
            ),
            "via_relay": relay and not has_account,
        }
    return out


@system_router.get("/readiness")
def system_readiness():
    """Real production readiness: probes LLM auth, video engine, ffmpeg,
    storage, trends and publishing configuration. No fake providers."""
    from app.services.readiness import run_readiness

    return run_readiness()


# ---------------------------------------------------------------------------
# Memory (spec #25)
# ---------------------------------------------------------------------------

memory_router = APIRouter(prefix="/workspaces/{workspace_id}/memory", tags=["memory"])


class MemoryStoreBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    type: str = Field(default=MemoryRecord.TYPE_SEMANTIC, max_length=20)
    scope: str = Field(default="", max_length=120)
    source: str = Field(default="user", max_length=80)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    ttl_hours: float | None = Field(default=None, gt=0)


class MemoryRetrieveBody(BaseModel):
    type: str | None = Field(default=None, max_length=20)
    scope: str | None = Field(default=None, max_length=120)
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


@memory_router.get("", summary="List memories (targeted, capped)")
def list_memories(
    type: str | None = None,
    scope: str | None = None,
    q: str | None = None,
    limit: int = 20,
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    try:
        items = memory_service.retrieve(ws.id, type=type, scope=scope, query=q, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"items": items, "count": len(items), "is_mock": False}


@memory_router.post("/store", summary="Store a memory record")
def store_memory(
    body: MemoryStoreBody,
    ws: Workspace = Depends(require_workspace_role("member")),
):
    try:
        rec_id = memory_service.store(
            ws.id,
            content=body.content,
            type=body.type,
            source=body.source or "user",
            confidence=body.confidence,
            importance=body.importance,
            scope=body.scope,
            ttl_hours=body.ttl_hours,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"id": rec_id, "stored": True}


@memory_router.post("/retrieve", summary="Targeted retrieval (filters required by design)")
def retrieve_memory(
    body: MemoryRetrieveBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    if not (body.type or body.scope or body.query):
        raise HTTPException(status_code=422, detail="provide at least one filter: type, scope, or query")
    try:
        items = memory_service.retrieve(ws.id, type=body.type, scope=body.scope, query=body.query, limit=body.limit)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"items": items, "count": len(items), "is_mock": False}


@memory_router.delete("/{memory_id}", summary="Delete a memory record")
def delete_memory(
    memory_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    rec = db.scalar(
        select(MemoryRecord).where(MemoryRecord.id == memory_id, MemoryRecord.workspace_id == ws.id)
    )
    if rec is None:
        raise HTTPException(status_code=404, detail="memory not found")
    db.delete(rec)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

logs_router = APIRouter(prefix="/workspaces/{workspace_id}/logs", tags=["logs"])


class LogQuery(BaseModel):
    category: str | None = None
    level: str | None = None


@logs_router.get("")
def query_logs(
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
    category: str | None = None,
    level: str | None = None,
    search: str | None = None,
    limit: int = 100,
):
    q = select(SystemLog).where(SystemLog.workspace_id == ws.id).order_by(SystemLog.created_at.desc()).limit(min(limit, 500))
    if category:
        q = q.where(SystemLog.category == category)
    if level:
        q = q.where(SystemLog.level == level)
    if search:
        q = q.where(SystemLog.message.ilike(f"%{search}%"))
    rows = db.scalars(q).all()
    return {
        "items": [
            {
                "id": lg.id,
                "category": lg.category,
                "level": lg.level,
                "message": lg.message,
                "context": lg.context_json or {},
                "created_at": lg.created_at.isoformat() + "Z",
            }
            for lg in rows
        ]
    }


jobs_router = APIRouter(prefix="/workspaces/{workspace_id}/jobs", tags=["jobs"])


@jobs_router.get("")
def list_jobs(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    status_filter: str | None = None,
    limit: int = 100,
):
    return {"items": jobs_service.list_jobs(workspace_id=ws.id, status=status_filter, limit=limit)}


@jobs_router.post("/{job_id}/cancel")
def cancel_job(job_id: str, ws: Workspace = Depends(require_workspace_role("admin"))):
    job = jobs_service.get_job(job_id)
    if not job or job.get("workspace_id") != ws.id:
        raise HTTPException(status_code=404, detail="job not found")
    ok = jobs_service.cancel_job(job_id)
    return {"cancelled": ok}


costs_router = APIRouter(prefix="/workspaces/{workspace_id}/costs", tags=["costs"])


@costs_router.get("")
def cost_summary(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    day_ago = utcnow() - timedelta(hours=24)
    by_cat = db.execute(
        select(CostEntry.category, func.sum(CostEntry.amount_usd))
        .where(CostEntry.workspace_id == ws.id, CostEntry.created_at >= day_ago)
        .group_by(CostEntry.category)
    ).all()
    spent_24h = float(sum((float(a or 0) for _, a in by_cat)))
    ok, remaining = True, settings.daily_budget_usd - spent_24h
    return {
        "last_24h_by_category": {c: round(float(a or 0), 4) for c, a in by_cat},
        "spent_last_24h_usd": round(spent_24h, 4),
        "daily_budget_usd": settings.daily_budget_usd,
        "per_video_budget_usd": settings.per_video_budget_usd,
        "within_budget": remaining > 0,
        "remaining_usd": round(remaining, 4),
    }
