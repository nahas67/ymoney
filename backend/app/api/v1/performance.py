"""Performance intelligence routes (Work 06, Lane A).

- ``GET /performance/overview`` — campaign rollup (reuses campaign analytics).
- ``GET /performance/retention`` — curve + timeline mapping + drops, or
  honest ``UNAVAILABLE`` when no granular retention exists.
- ``GET /performance/compare`` — group measured posts by observable creative
  dimensions (hook/caption/duration/voice/broll/posting-window), always with
  ``n`` per group. Correlational only — never causal.
"""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_db
from app.models.identity import Workspace
from app.services.auth_service import require_workspace_role

performance_router = APIRouter(
    prefix="/workspaces/{workspace_id}/performance", tags=["performance"])

COMPARE_GROUPS = ("hook", "caption", "duration", "voice", "broll", "posting-window")


@performance_router.get("/overview", summary="Campaign performance rollup")
def performance_overview(
    campaign_id: str = Query(...),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.engine.campaign.analytics import compare_platforms, rollup_campaign
    from app.models import Campaign

    campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {
        "campaign_id": campaign_id,
        "rollup": rollup_campaign(db, ws.id, campaign_id),
        "platforms": compare_platforms(db, ws.id, campaign_id),
    }


@performance_router.get("/retention", summary="Retention curve + timeline mapping")
def performance_retention(
    post_id: str | None = Query(None),
    short_id: str | None = Query(None),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.engine.performance.retention import RetentionAnalyzer
    from app.models import ContentItem, PublishedPost

    if post_id:
        post = db.get(PublishedPost, post_id)
        if post is None or post.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="post not found")
    if short_id:
        short = db.get(ContentItem, short_id)
        if short is None or short.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="short not found")
    if not post_id and not short_id:
        raise HTTPException(
            status_code=400, detail="post_id or short_id is required")
    return RetentionAnalyzer(db, ws.id).analyze(
        post_id=post_id, short_content_id=short_id)


@performance_router.get("/compare", summary="Compare groups by creative dimension")
def performance_compare(
    campaign_id: str = Query(...),
    group_by: str = Query("hook"),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.models import Campaign

    if group_by not in COMPARE_GROUPS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown group_by {group_by!r}; pick from {list(COMPARE_GROUPS)}")
    campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="campaign not found")
    return _compare(db, ws.id, campaign_id, group_by)


def _compare(db, workspace_id: str, campaign_id: str, group_by: str) -> dict:
    from sqlalchemy import select

    from app.engine.campaign.analytics import campaign_posts, latest_metrics
    from app.models import ContentItem, ContentTimeline

    posts = campaign_posts(db, workspace_id, campaign_id)
    metrics = latest_metrics(db, [p.id for p in posts])
    shorts = {s.id: s for s in db.scalars(select(ContentItem).where(
        ContentItem.workspace_id == workspace_id,
        ContentItem.campaign_id == campaign_id)).all()}
    timelines: dict[str, dict] = {}
    for sid in {p.content_item_id for p in posts if p.content_item_id}:
        tl = db.scalar(select(ContentTimeline).where(
            ContentTimeline.workspace_id == workspace_id,
            ContentTimeline.content_item_id == sid,
        ).order_by(ContentTimeline.version.desc()))
        timelines[sid or ""] = (tl.tracks_json or {}) if tl else {}
    features = _lane_b_features(db, workspace_id, list(shorts))
    groups: dict[str, dict] = {}
    for post in posts:
        metric = metrics.get(post.id)
        if metric is None:
            continue
        short = shorts.get(post.content_item_id or "")
        key = _group_key(group_by, post, short,
                         timelines.get(post.content_item_id or "", {}),
                         features.get(post.content_item_id or "", {}))
        slot = groups.setdefault(key, {"n": 0, "views": 0, "completions": []})
        slot["n"] += 1
        slot["views"] += metric.views or 0
        slot["completions"].append(float(metric.completion_rate or 0.0))
    items = []
    for key in sorted(groups):
        slot = groups[key]
        completions = slot.pop("completions")
        items.append({
            "group": key, "n": slot["n"], "views": slot["views"],
            "avg_completion": (round(sum(completions) / len(completions), 4)
                               if completions else 0.0),
            "low_sample": slot["n"] < 2,
        })
    return {
        "campaign_id": campaign_id, "group_by": group_by, "groups": items,
        "causal": False,
        "note": "correlational only; groups with n<2 are low-sample",
    }


def _lane_b_features(db, workspace_id: str, short_ids: list[str]) -> dict:
    """Lane B creative features when extracted; {} otherwise (never required)."""
    if not short_ids:
        return {}
    try:
        from sqlalchemy import select

        from app.models.performance import CreativeFeature

        rows = db.scalars(select(CreativeFeature).where(
            CreativeFeature.workspace_id == workspace_id,
            CreativeFeature.content_item_id.in_(short_ids))).all()
        return {r.content_item_id: dict(r.features_json or {}) for r in rows}
    except Exception:
        return {}


def _group_key(group_by: str, post, short, timeline_doc: dict,
               features: dict) -> str:
    if group_by == "hook":
        if features.get("hook_type"):
            return str(features["hook_type"])
        strategy = (short.strategy_json if short is not None else {}) or {}
        return str(strategy.get("hook_type") or "unknown")
    if group_by == "caption":
        if features.get("caption_style"):
            return str(features["caption_style"])
        n_caps = sum(1 for t in (timeline_doc.get("tracks") or [])
                     if t.get("kind") == "caption" for _ in (t.get("clips") or []))
        return "captioned" if n_caps else "uncaptioned"
    if group_by == "duration":
        seconds = float((timeline_doc.get("duration_seconds") or 0.0) or 0.0)
        if features.get("duration_seconds") is not None:
            with contextlib.suppress(TypeError, ValueError):
                seconds = float(features["duration_seconds"])
        if seconds < 25:
            return "<25s"
        if seconds < 40:
            return "25-40s"
        if seconds <= 60:
            return "40-60s"
        return "60s+"
    if group_by == "voice":
        if features.get("voice") is not None:
            return str(features["voice"])
        kinds = {t.get("kind") for t in (timeline_doc.get("tracks") or [])}
        return "voiced" if "voice" in kinds else "unvoiced"
    if group_by == "broll":
        if features.get("broll") is not None:
            return str(features["broll"])
        kinds = {t.get("kind") for t in (timeline_doc.get("tracks") or [])}
        return "broll" if "broll" in kinds else "no-broll"
    # posting-window: hour bucket of the publication time.
    published = getattr(post, "published_at", None)
    hour = published.hour if published is not None else -1
    if 6 <= hour < 12:
        return "morning 6-12"
    if 12 <= hour < 18:
        return "afternoon 12-18"
    if 18 <= hour < 24:
        return "evening 18-24"
    if 0 <= hour < 6:
        return "night 0-6"
    return "unscheduled"


__all__ = ["COMPARE_GROUPS", "performance_router"]
