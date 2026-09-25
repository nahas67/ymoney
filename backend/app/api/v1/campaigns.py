"""Master-to-shorts campaign endpoints (Work 04, Lane B).

Mounted alongside the existing ``campaigns_router`` (app/api/v1/content.py:
list/create/detail) with NON-overlapping sub-paths — inspect that router
before adding routes here. All endpoints enforce workspace isolation with
404-on-cross-workspace semantics and role gates via require_workspace_role.

Lane A owns campaign_plans / platform_variants / publishing_plans tables;
this module reads them by reflection when present and falls back to the
Campaign.kpis_json sidecar + existing ContentItem rows otherwise.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db, session_scope
from app.engine.campaign.publish_flow import register_publish_handlers
from app.models import Campaign, ContentItem, ContentTimeline, QualityCheck, Video, VideoVariant
from app.services import jobs as jobs_service
from app.services.auth_service import require_workspace_role

register_publish_handlers()

campaign_flows_router = APIRouter(
    prefix="/workspaces/{workspace_id}/campaigns", tags=["video-campaigns"]
)
campaign_shorts_router = APIRouter(
    prefix="/workspaces/{workspace_id}/content", tags=["video-campaigns"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _campaign_or_404(db, campaign_id: str, ws_id: str) -> Campaign:
    row = db.get(Campaign, campaign_id)
    if not row or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="campaign not found")
    return row


def _content_or_404(db, content_id: str, ws_id: str) -> ContentItem:
    row = db.get(ContentItem, content_id)
    if not row or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="content not found")
    return row


def _sidecar(campaign: Campaign) -> dict:
    return dict(getattr(campaign, "kpis_json", None) or {})


def _save_sidecar(db, campaign: Campaign, patch: dict) -> None:
    data = _sidecar(campaign)
    data.update(patch)
    campaign.kpis_json = data
    db.flush()


def _shorts(db, campaign_id: str, ws_id: str) -> list[ContentItem]:
    return db.scalars(
        select(ContentItem).where(
            ContentItem.workspace_id == ws_id,
            ContentItem.campaign_id == campaign_id,
            ContentItem.derivation_type == "short",
        )
    ).all()


def _read_plan_row(campaign_id: str) -> dict | None:
    """Lane A publishing_plans row; None when not migrated/present."""
    try:
        from sqlalchemy import select as _select

        from app.db import session_scope as _scope
        from app.models.campaign import PublishingPlan

        with _scope() as s:
            row = s.scalar(_select(PublishingPlan).where(PublishingPlan.campaign_id == campaign_id))
            if not row:
                return None
            return {"status": row.status, "items": list(row.items_json or [])}
    except Exception:
        return None


def _read_variants(short_ids: list[str]) -> list[dict]:
    """Lane A platform_variants rows; [] when not migrated/present."""
    if not short_ids:
        return []
    try:
        from sqlalchemy import select as _select

        from app.db import session_scope as _scope
        from app.models.campaign import PlatformVariant

        with _scope() as s:
            rows = s.scalars(
                _select(PlatformVariant).where(PlatformVariant.short_content_id.in_(short_ids))
            ).all()
            return [{
                "id": r.id,
                "short_content_id": r.short_content_id,
                "platform": r.platform,
                "aspect_ratio": r.aspect_ratio,
                "timeline_id": r.timeline_id,
                "shares_base_timeline": r.timeline_id is None,
                "metadata": dict(r.metadata_json or {}),
                "status": r.status,
            } for r in rows]
    except Exception:
        return []


def _upsert_campaign_plan(db, *, workspace_id: str, campaign_id: str, **fields) -> None:
    """Create-or-update the Lane A CampaignPlan row (best-effort)."""
    try:
        from app.models.campaign import CampaignPlan

        row = db.scalar(select(CampaignPlan).where(CampaignPlan.campaign_id == campaign_id))
        if row is None:
            row = CampaignPlan(workspace_id=workspace_id, campaign_id=campaign_id,
                               master_content_id=fields.get("master_content_id", ""))
            db.add(row)
            db.flush()
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        db.flush()
    except Exception:
        pass


def _short_dto(item: ContentItem) -> dict:
    return {
        "id": item.id,
        "topic": item.topic,
        "status": item.status,
        "derivation_type": item.derivation_type,
        "parent_content_id": item.parent_content_id,
        "root_content_id": item.root_content_id,
        "campaign_id": item.campaign_id,
    }


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateFromMasterBody(BaseModel):
    master_content_id: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    goal: str = Field(default="", max_length=2000)
    target_platforms: list[str] = Field(default_factory=list)
    desired_shorts: int = Field(default=3, ge=1, le=20)
    cta_kind: str = Field(default="FOLLOW", max_length=30)


class GenerateMoreBody(BaseModel):
    n: int = Field(default=1, ge=1, le=10)
    exclude: list[str] = Field(default_factory=list)


class ScheduleBody(BaseModel):
    start_date: datetime | None = None
    interval_days: float = Field(default=1.0, ge=0.25, le=30)


class PublishBody(BaseModel):
    variant_ids: list[str] = Field(default_factory=list)
    only_approved: bool = Field(default=True)


class PlatformVariantBody(BaseModel):
    platform: str = Field(min_length=1)
    cta_kind: str = Field(default="FOLLOW", max_length=30)


# ---------------------------------------------------------------------------
# Campaign flows
# ---------------------------------------------------------------------------

@campaign_flows_router.post("/from-master", status_code=201)
def vc_create_from_master(
    body: CreateFromMasterBody,
    ws=Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.engine.campaign.metadata import CTA_KINDS
    from app.engine.campaign.platforms import CAMPAIGN_PLATFORMS
    from app.engine.campaign.publish_flow import emit_campaign_event

    master = _content_or_404(db, body.master_content_id, ws.id)
    bad = [p for p in body.target_platforms if p not in CAMPAIGN_PLATFORMS]
    if bad:
        raise HTTPException(status_code=400, detail=f"unsupported platforms: {bad}")
    if body.cta_kind not in CTA_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown CTA kind {body.cta_kind!r}")
    platforms = body.target_platforms or list(CAMPAIGN_PLATFORMS)
    row = Campaign(
        workspace_id=ws.id,
        name=body.name or f"Campaign from {master.topic[:60]}",
        goal=body.goal,
        status="DRAFT",
        target_videos=body.desired_shorts * len(platforms),
        platforms_json=platforms,
        automation_level="SEMI_AUTONOMOUS",
    )
    db.add(row)
    db.flush()
    _save_sidecar(db, row, {
        "campaign_plan": {
            "master_content_id": master.id,
            "goal": body.goal,
            "target_platforms": platforms,
            "desired_shorts": body.desired_shorts,
            "cta_kind": body.cta_kind,
            "status": "DRAFT",
            "progress": {"completed": 0, "total": body.desired_shorts * len(platforms)},
        }
    })
    _upsert_campaign_plan(db, workspace_id=ws.id, campaign_id=row.id,
                          master_content_id=master.id, goal=body.goal,
                          target_platforms=platforms, desired_shorts=body.desired_shorts,
                          status="DRAFT")
    db.commit()
    emit_campaign_event(ws.id, "campaign.created",
                        f"Campaign {row.id} created from master {master.id}",
                        campaign_id=row.id, master_content_id=master.id)
    return {"id": row.id, "plan": _sidecar(row)["campaign_plan"]}


@campaign_flows_router.get("/{campaign_id}/aggregate")
def vc_aggregate(campaign_id: str, ws=Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id, ws.id)
    plan = _sidecar(campaign).get("campaign_plan") or {}
    master = None
    if plan.get("master_content_id"):
        master = db.get(ContentItem, plan["master_content_id"])
        if master is not None and master.workspace_id != ws.id:
            master = None
    shorts = _shorts(db, campaign.id, ws.id)
    variants = _read_variants([s.id for s in shorts])
    stored_plan = _read_plan_row(campaign.id)
    return {
        "campaign": {"id": campaign.id, "name": campaign.name, "status": campaign.status,
                     "platforms": campaign.platforms_json or []},
        "master": _short_dto(master) if master else None,
        "shorts": [_short_dto(s) for s in shorts],
        "variants": variants,
        "plan": stored_plan or plan,
        "progress": vc_progress_inner(db, campaign, shorts),
        "qc": _qc_summary(db, shorts),
        "costs": _cost_summary(db, campaign.id, ws.id),
    }


def vc_progress_inner(db, campaign: Campaign, shorts: list[ContentItem] | None = None) -> dict:
    shorts = shorts if shorts is not None else _shorts(db, campaign.id, campaign.workspace_id)
    plan = _sidecar(campaign).get("campaign_plan") or {}
    desired = int(plan.get("desired_shorts", 0) or 0)
    platforms = plan.get("target_platforms") or (campaign.platforms_json or [])
    total = (desired * len(platforms)) if desired else max(len(shorts), 0)
    variants = _read_variants([s.id for s in shorts])
    done = sum(1 for v in variants if v.get("status") in ("READY", "SCHEDULED", "PUBLISHED"))
    if not variants:
        done = sum(1 for s in shorts if s.status in ("READY", "APPROVED", "PUBLISHED", "SCHEDULED"))
        total = max(total, len(shorts))
    return {"completed": done, "total": total}


@campaign_flows_router.get("/{campaign_id}/content")
def vc_content(campaign_id: str, ws=Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id, ws.id)
    shorts = _shorts(db, campaign.id, ws.id)
    return {"items": [_short_dto(s) for s in shorts]}


@campaign_flows_router.get("/{campaign_id}/progress")
def vc_progress(campaign_id: str, ws=Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id, ws.id)
    return vc_progress_inner(db, campaign)


def _qc_summary(db, shorts: list[ContentItem]) -> dict:
    items = []
    for short in shorts:
        variants = db.scalars(
            select(VideoVariant).where(VideoVariant.content_item_id == short.id)
        ).all()
        best: QualityCheck | None = None
        for vv in variants:
            vids = db.scalars(select(Video).where(Video.variant_id == vv.id)).all()
            for v in vids:
                checks = db.scalars(
                    select(QualityCheck).where(QualityCheck.video_id == v.id)
                    .order_by(QualityCheck.created_at.desc())
                ).all()
                if checks and (best is None or checks[0].overall > best.overall):
                    best = checks[0]
        items.append({
            "short_content_id": short.id,
            "overall": best.overall if best else None,
            "passed": bool(best.passed) if best else None,
        })
    scored = [i for i in items if i["overall"] is not None]
    return {
        "items": items,
        "avg_overall": (sum(i["overall"] for i in scored) / len(scored)) if scored else None,
    }


def _cost_summary(db, campaign_id: str, ws_id: str) -> dict:
    from app.models import CostEntry

    rows = db.scalars(select(CostEntry).where(CostEntry.workspace_id == ws_id)).all()
    total, n = 0.0, 0
    for row in rows:
        detail = row.detail_json or {}
        if detail.get("campaign_id") == campaign_id:
            total += float(row.amount_usd or 0.0)
            n += 1
    return {"campaign_id": campaign_id, "entries": n, "total_usd": round(total, 4)}


@campaign_flows_router.get("/{campaign_id}/qc")
def vc_qc(campaign_id: str, ws=Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id, ws.id)
    return _qc_summary(db, _shorts(db, campaign.id, ws.id))


@campaign_flows_router.post("/{campaign_id}/derive", status_code=202)
def vc_derive(campaign_id: str, ws=Depends(require_workspace_role("admin")), db=Depends(get_db)):
    from app.engine.campaign.publish_flow import emit_campaign_event

    campaign = _campaign_or_404(db, campaign_id, ws.id)
    job_id = jobs_service.enqueue(
        "campaign.derive",
        {"campaign_id": campaign.id},
        workspace_id=ws.id,
        priority=50,
        max_retries=2,
        idempotency_key=f"campaign-derive-{campaign.id}",
    )
    plan = _sidecar(campaign).get("campaign_plan") or {}
    plan["status"] = "RUNNING"
    _save_sidecar(db, campaign, {"campaign_plan": plan})
    _upsert_campaign_plan(db, workspace_id=ws.id, campaign_id=campaign.id, status="RUNNING")
    db.commit()
    emit_campaign_event(ws.id, "campaign.derivation_started",
                        f"Derivation started for campaign {campaign.id}",
                        campaign_id=campaign.id)
    return {"job_id": job_id, "status": "RUNNING"}


@campaign_flows_router.post("/{campaign_id}/generate-more", status_code=201)
def vc_generate_more(
    body: GenerateMoreBody,
    campaign_id: str,
    ws=Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.engine.campaign.publish_flow import emit_campaign_event

    campaign = _campaign_or_404(db, campaign_id, ws.id)
    plan = _sidecar(campaign).get("campaign_plan") or {}
    master_id = plan.get("master_content_id", "")
    master = db.get(ContentItem, master_id) if master_id else None
    topic = master.topic if master else campaign.name
    excluded = set(body.exclude or [])
    existing_topics = {
        s.topic for s in db.scalars(
            select(ContentItem).where(
                ContentItem.workspace_id == ws.id, ContentItem.campaign_id == campaign.id)
        ).all()
    }
    created = []
    i = 0
    while len(created) < body.n:
        i += 1
        if i > body.n * 10 + 10:  # uniqueness loop guard
            break
        title = f"{topic} — angle {len(existing_topics) + 1}"
        if title in existing_topics or title in excluded:
            continue
        row = ContentItem(
            workspace_id=ws.id,
            campaign_id=campaign.id,
            topic=title,
            status="IDEA",
            parent_content_id=master_id or None,
            root_content_id=(master.root_content_id if master and master.root_content_id else master_id) or None,
            derivation_type="short",
        )
        db.add(row)
        db.flush()
        existing_topics.add(title)
        created.append(row.id)
    db.commit()
    # Announce only after commit (a second session mid-transaction can
    # lock SQLite; emission itself is best-effort).
    for short_id in created:
        emit_campaign_event(ws.id, "campaign.short_created", f"Short {short_id} added",
                            campaign_id=campaign.id, short_content_id=short_id)
    return {"created": created}


@campaign_flows_router.post("/{campaign_id}/schedule")
def vc_schedule(
    body: ScheduleBody,
    campaign_id: str,
    ws=Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from datetime import UTC

    from app.engine.campaign.publish_flow import (
        build_publishing_plan,
        emit_campaign_event,
        schedule_plan,
    )

    campaign = _campaign_or_404(db, campaign_id, ws.id)
    plan = _sidecar(campaign).get("campaign_plan") or {}
    shorts = _shorts(db, campaign.id, ws.id)
    if not shorts:
        raise HTTPException(status_code=409, detail="no shorts to schedule — derive first")
    platforms = plan.get("target_platforms") or (campaign.platforms_json or [])
    start = body.start_date or datetime.now(UTC)
    refs = [
        {"variant_id": s.id, "platform": p, "short_content_id": s.id}
        for s in shorts for p in platforms
    ]
    items = build_publishing_plan(
        refs, start, body.interval_days,
        master_ref={"content_id": plan.get("master_content_id", "master"),
                    "platform": "youtube_longform"},
        autonomy=campaign.automation_level,
    )
    sched_items = [
        {"short_content_id": it["variant_id"], "platform": it["platform"],
         "planned_at": it["planned_at"]}
        for it in items if not it.get("is_master")
    ]
    # Master-first: the master entry is scheduled first so CTA URLs resolve.
    # Reuse the plan's master platform label so derive + schedule agree
    # (idempotent reuse instead of a duplicate master entry).
    master_item = next((it for it in items if it.get("is_master")), None)
    sched_items.insert(0, {
        "short_content_id": plan.get("master_content_id", ""),
        "platform": (master_item or {}).get("platform", "youtube_longform"),
        "planned_at": items[0]["planned_at"] if items else start.isoformat(),
    })
    result = schedule_plan(db, workspace_id=ws.id, campaign_id=campaign.id, items=sched_items)
    plan["publishing_items"] = items
    plan["status"] = "READY"
    _save_sidecar(db, campaign, {"campaign_plan": plan})
    try:
        from app.models.campaign import PublishingPlan

        existing = db.scalar(
            select(PublishingPlan).where(PublishingPlan.campaign_id == campaign.id))
        if existing is None:
            db.add(PublishingPlan(workspace_id=ws.id, campaign_id=campaign.id,
                                  items_json=items, status="READY"))
        else:
            existing.items_json = items
            existing.status = "READY"
        db.flush()
    except Exception:
        pass
    db.commit()
    emit_campaign_event(ws.id, "campaign.scheduled",
                        f"Campaign {campaign.id} scheduled ({len(result['created'])} new entries)",
                        campaign_id=campaign.id)
    return {"items": items, "schedule": result}


@campaign_flows_router.post("/{campaign_id}/publish", status_code=202)
def vc_publish(
    body: PublishBody,
    campaign_id: str,
    ws=Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.engine.campaign.publish_flow import dependencies_satisfied, publish_due

    campaign = _campaign_or_404(db, campaign_id, ws.id)
    plan = _sidecar(campaign).get("campaign_plan") or {}
    items = list(plan.get("publishing_items") or [])
    if body.variant_ids:
        items = [i for i in items if i.get("variant_id") in set(body.variant_ids)]
    # Master-first: shorts publish only after their dependencies are live.
    published = _published_variant_ids(db, ws.id, campaign.id)
    enqueued, skipped = [], []
    for item in items:
        if item.get("is_master"):
            continue
        if body.only_approved and item.get("approval_state") != "APPROVED":
            skipped.append({"variant_id": item["variant_id"], "reason": "awaiting approval"})
            continue
        if item.get("publication_state") == "PUBLISHED":
            skipped.append({"variant_id": item["variant_id"], "reason": "already published"})
            continue
        if not dependencies_satisfied(item, published):
            skipped.append({"variant_id": item["variant_id"], "reason": "waiting on master"})
            continue
        job_id = publish_due(
            db, workspace_id=ws.id, campaign_id=campaign.id,
            variant_id=str(item["variant_id"]), platform=str(item["platform"]),
            short_content_id=str(item.get("variant_id", "")),
            payload={"metadata": item.get("metadata") or {}},
        )
        if job_id:
            enqueued.append({"variant_id": item["variant_id"], "job_id": job_id})
        else:
            skipped.append({"variant_id": item["variant_id"], "reason": "already queued"})
    db.commit()
    return {"enqueued": enqueued, "skipped": skipped}


def _published_variant_ids(db, ws_id: str, campaign_id: str) -> set[str]:
    from app.models import PublishedPost

    plan_master = ""
    rows = db.scalars(
        select(PublishedPost).where(PublishedPost.workspace_id == ws_id)
    ).all()
    found = {r.video_id for r in rows if r.video_id}
    if plan_master:
        found.add(plan_master)
    # Master counts as published when its own post row exists.
    _ = campaign_id
    return found


@campaign_flows_router.post("/{campaign_id}/cancel")
def vc_cancel(campaign_id: str, ws=Depends(require_workspace_role("admin")), db=Depends(get_db)):
    from app.services.events import record_event

    campaign = _campaign_or_404(db, campaign_id, ws.id)
    campaign.status = "CANCELLED"
    plan = _sidecar(campaign).get("campaign_plan") or {}
    plan["status"] = "CANCELLED"
    _save_sidecar(db, campaign, {"campaign_plan": plan})
    _upsert_campaign_plan(db, workspace_id=ws.id, campaign_id=campaign.id,
                          status="FAILED", error="cancelled by operator")
    db.commit()
    record_event(ws.id, "campaign.failed", f"Campaign {campaign.id} cancelled by operator",
                 level="warning", source="campaign", data={"campaign_id": campaign.id})
    return {"id": campaign.id, "status": "CANCELLED"}


# ---------------------------------------------------------------------------
# Single-content operations (siblings always preserved)
# ---------------------------------------------------------------------------

@campaign_shorts_router.post("/{content_id}/regenerate")
def vc_regenerate_short(
    content_id: str, ws=Depends(require_workspace_role("admin")), db=Depends(get_db)
):
    item = _content_or_404(db, content_id, ws.id)
    item.lineage_version = int(item.lineage_version or 1) + 1
    item.status = "IDEA"
    item.error = ""
    db.commit()
    return {"id": item.id, "status": item.status, "lineage_version": item.lineage_version}


@campaign_shorts_router.post("/{content_id}/platform-variants")
def vc_regenerate_platform_variant(
    body: PlatformVariantBody,
    content_id: str,
    ws=Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.engine.campaign.metadata import CTA_KINDS, PlatformMetadataGenerator
    from app.engine.campaign.platforms import CAMPAIGN_PLATFORMS
    from app.engine.campaign.publish_flow import emit_campaign_event
    from app.engine.campaign.variants import (
        build_variant,
        default_store,
        regenerate_variant,
        variant_payload,
    )

    item = _content_or_404(db, content_id, ws.id)
    if body.platform not in CAMPAIGN_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"unsupported platform {body.platform!r}")
    if body.cta_kind not in CTA_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown CTA kind {body.cta_kind!r}")
    store = default_store(db)
    gen = PlatformMetadataGenerator()
    metadata = gen.generate(topic=item.topic, platform=body.platform,
                            cta_kind=body.cta_kind, master_content_id=item.root_content_id or "")
    try:
        rec = regenerate_variant(db, ws.id, item.id, body.platform,
                                 metadata=metadata, store=store)
    except LookupError:
        rec = build_variant(db, ws.id, item.id, body.platform, None, body.cta_kind,
                            campaign_id=item.campaign_id or "", metadata=metadata, store=store)
    db.commit()
    emit_campaign_event(ws.id, "campaign.variant_created",
                        f"Variant {rec.id} ({body.platform}) regenerated",
                        campaign_id=item.campaign_id or "", short_content_id=item.id,
                        platform=body.platform)
    return variant_payload(rec)


# ---------------------------------------------------------------------------
# Durable derive chain: discovery -> select -> shorts -> variants -> plan.
# Resumable: each step skips work that already exists.
# ---------------------------------------------------------------------------

def register_campaign_handlers() -> None:
    if "campaign.derive" in jobs_service._handlers:
        return

    @jobs_service.handler("campaign.derive")
    def _run_campaign_derive(ctx) -> dict:
        from app.db import session_scope as _scope
        from app.engine.campaign.derive import run_derive_campaign

        campaign_id = (ctx.payload or {}).get("campaign_id", "")
        with _scope() as s:
            campaign = s.get(Campaign, campaign_id)
            if not campaign or campaign.workspace_id != (ctx.workspace_id or ""):
                raise RuntimeError("campaign not found for derive job")
            sidecar = dict(campaign.kpis_json or {})
            plan = dict(sidecar.get("campaign_plan") or {})
            return run_derive_campaign(s, campaign, plan, ctx)


register_campaign_handlers()
