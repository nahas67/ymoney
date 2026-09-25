"""Durable campaign derive orchestration (extracted from the API handler).

The ``campaign.derive`` job handler in ``app/api/v1/campaigns.py`` stays thin
(auth/request/response + enqueue); the resumable discovery -> select ->
shorts -> variants -> plan chain lives here as the testable
:func:`run_derive_campaign` function.
"""

from __future__ import annotations

from typing import Any


def run_derive_campaign(session, campaign, plan: dict, ctx, *,
                        jobs_service=None, emit=None) -> dict:
    """Run the full derive chain for one campaign. Returns a summary dict.

    ``session`` is an open SQLAlchemy session owning ``campaign``; ``plan``
    is the mutable campaign-plan dict (sidecar copy); ``ctx`` is the job
    context. ``jobs_service`` (cancellation checks) and ``emit`` (campaign
    events) default to the real implementations when omitted so the API
    handler stays a one-line call.
    """
    from sqlalchemy import select

    from app.engine.campaign.derive_chain import (
        _covered_ranges,
    )
    from app.engine.campaign.derive_chain import (
        derive_moment as _derive_moment,
    )
    from app.engine.campaign.derive_chain import (
        discover_moments as _discover_moments,
    )
    from app.engine.campaign.derive_chain import (
        range_covered as _range_covered,
    )
    from app.engine.campaign.derive_chain import (
        select_moments as _select_moments,
    )
    from app.engine.campaign.metadata import PlatformMetadataGenerator
    from app.engine.campaign.publish_flow import build_publishing_plan
    from app.engine.campaign.variants import build_variant, default_store
    from app.models.base import utcnow

    if jobs_service is None:
        from app.services import jobs as jobs_service  # type: ignore[no-redef]
    if emit is None:
        from app.engine.campaign.publish_flow import (
            emit_campaign_event as emit,  # type: ignore[no-redef]
        )

    s = session
    stages = ["discovery", "select", "shorts", "variants", "plan"]
    sidecar = dict(campaign.kpis_json or {})
    # Work on the caller's plan dict in place so progress is observable.
    if plan is None:
        plan = {}

    def _progress(stage: str, done: int, total: int) -> None:
        plan["status"] = "RUNNING"
        plan["progress"] = {"stage": stage, "completed": done, "total": total}
        sidecar["campaign_plan"] = plan
        campaign.kpis_json = dict(sidecar)
        try:
            from app.models.campaign import CampaignPlan

            plan_row = s.scalar(
                select(CampaignPlan).where(CampaignPlan.campaign_id == campaign.id))
            if plan_row is not None:
                plan_row.status = "RUNNING"
                plan_row.progress_json = dict(plan["progress"])
        except Exception:
            pass
        s.flush()

    from app.models import ContentItem, ContentTimeline

    master = s.get(ContentItem, plan.get("master_content_id", "")) if plan.get("master_content_id") else None
    platforms = plan.get("target_platforms") or (campaign.platforms_json or [])
    desired = int(plan.get("desired_shorts", 0) or 0)
    cta_kind = plan.get("cta_kind", "FOLLOW")

    _progress("discovery", 0, max(desired * len(platforms), 1))
    jobs_service.check_cancelled(ctx)
    moments = _discover_moments(s, campaign.workspace_id, master, max(desired * 3, desired + 2))
    _progress("discovery", len(moments), max(desired * len(platforms), 1))
    jobs_service.check_cancelled(ctx)
    selected = _select_moments(s, campaign, moments, desired)
    _progress("select", len(selected), max(desired, 1))

    # shorts (resumable: reuse existing campaign shorts, derive the rest;
    # one bad moment never kills the campaign)
    existing = s.scalars(
        select(ContentItem).where(
            ContentItem.workspace_id == campaign.workspace_id,
            ContentItem.campaign_id == campaign.id,
            ContentItem.derivation_type == "short",
        )
    ).all()
    shorts = list(existing)
    covered = _covered_ranges(s, shorts)
    derived_errors: list[dict] = []
    for moment in selected:
        if len(shorts) >= desired:
            break
        if _range_covered(moment, covered):
            continue
        jobs_service.check_cancelled(ctx)
        try:
            (child,) = _derive_moment(s, campaign, master, moment, plan)
            shorts.append(child)
            covered.append((float(moment["start"]), float(moment["end"])))
            emit(campaign.workspace_id, "campaign.short_created",
                 f"Short {child.id} derived", campaign_id=campaign.id,
                 short_content_id=child.id)
        except Exception as exc:
            derived_errors.append({
                "moment": {k: moment.get(k) for k in ("start", "end", "hook")},
                "error": f"{type(exc).__name__}: {exc}",
            })
    if derived_errors:
        plan["derived_errors"] = derived_errors
    _progress("shorts", len(shorts), max(desired, len(shorts)))

    # variants + metadata (metadata-only shares the base timeline)
    gen = PlatformMetadataGenerator()
    store = default_store(s)
    total_variants = len(shorts) * len(platforms)
    made = 0
    for short in shorts:
        for platform in platforms:
            jobs_service.check_cancelled(ctx)
            metadata = gen.generate(topic=short.topic, platform=platform,
                                    cta_kind=cta_kind,
                                    master_content_id=master.id if master else "")
            build_variant(s, campaign.workspace_id, short.id, platform, None,
                          cta_kind, campaign_id=campaign.id, metadata=metadata,
                          store=store)
            made += 1
            emit(campaign.workspace_id, "campaign.variant_created",
                 f"Variant for {short.id} on {platform}",
                 campaign_id=campaign.id, short_content_id=short.id,
                 platform=platform)
    _progress("variants", made, max(total_variants, 1))

    # plan (stored in sidecar until Lane A table lands)
    refs = [{"variant_id": sh.id, "platform": p} for sh in shorts for p in platforms]
    items = build_publishing_plan(
        refs, utcnow(), 1.0,
        master_ref={"content_id": master.id if master else "master",
                    "platform": "youtube_longform"},
        autonomy=campaign.automation_level,
    )
    plan["publishing_items"] = items
    plan["status"] = "READY"
    plan["progress"] = {"stage": "plan", "completed": made, "total": max(total_variants, 1)}
    sidecar["campaign_plan"] = plan
    campaign.kpis_json = dict(sidecar)
    campaign.status = "READY"
    try:
        from app.models.campaign import CampaignPlan, PublishingPlan

        plan_row = s.scalar(
            select(CampaignPlan).where(CampaignPlan.campaign_id == campaign.id))
        if plan_row is not None:
            plan_row.status = "READY"
            plan_row.progress_json = dict(plan["progress"])
        existing_pp = s.scalar(
            select(PublishingPlan).where(PublishingPlan.campaign_id == campaign.id))
        if existing_pp is None:
            s.add(PublishingPlan(workspace_id=campaign.workspace_id,
                                 campaign_id=campaign.id,
                                 items_json=items, status="READY"))
        else:
            existing_pp.items_json = items
            existing_pp.status = "READY"
    except Exception:
        pass
    from app.engine.campaign.publish_flow import schedule_plan

    sched = schedule_plan(session=s, workspace_id=campaign.workspace_id,
                          campaign_id=campaign.id, items=items)
    plan["schedule"] = sched
    from app.engine.campaign.qc import campaign_qc, short_qc

    short_results: dict[str, Any] = {}
    for short in shorts:
        jobs_service.check_cancelled(ctx)
        tl = s.query(ContentTimeline).filter(
            ContentTimeline.content_item_id == short.id,
            ContentTimeline.workspace_id == campaign.workspace_id,
        ).order_by(ContentTimeline.version.desc()).first()
        if tl is None:
            short_results[short.id] = {"result": "FAIL", "checks": {},
                                       "error": "no timeline"}
            continue
        short_results[short.id] = short_qc(tl.tracks_json or {}, None)
    plan["short_qc"] = short_results
    camp_qc = campaign_qc(s, campaign.id)
    plan["campaign_qc"] = camp_qc
    from app.engine.campaign.costs import track_campaign_cost

    track_campaign_cost(s, campaign.workspace_id, campaign.id,
                        "derivation", 0.0,
                        detail={"shorts": len(shorts), "variants": made})
    s.flush()
    emit(campaign.workspace_id, "campaign.ready",
         f"Campaign {campaign.id} ready ({made} variants)",
         campaign_id=campaign.id)
    return {"shorts": len(shorts), "variants": made, "stages": stages}
