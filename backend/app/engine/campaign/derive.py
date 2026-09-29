"""Durable campaign derive orchestration (extracted from the API handler).

The ``campaign.derive`` job handler in ``app/api/v1/campaigns.py`` stays thin
(auth/request/response + enqueue); the resumable discovery -> select ->
shorts -> variants -> plan chain lives here as the testable
:func:`run_derive_campaign` function.

Transaction discipline (Work 06, Lane A): every stage commits at its
boundary — discovery / select / each short / each variant / plan / schedule /
QC — and campaign events are emitted only AFTER the commit that durably
stores what they announce. Nested-session writers (event emission, webhook
fan-out, intelligence advisory persists, LLM cost tracking) must never run
while this session holds an open write transaction: on SQLite that stalls
each nested write on a ~5s busy wait (the 242s E2E). Crash safety is
preserved: each short/variant commits atomically (savepoint per unit, so a
failed unit rolls back alone), committed prefixes resume-skip via the
existing idempotency (covered ranges, variant upserts, idempotent
scheduling), and half-written units are never committed.
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

    Commits at each stage boundary; safe to call inside an outer
    ``session_scope`` (the scope's exit commit becomes a no-op).
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

    # -- stage-commit plumbing ------------------------------------------------
    # Events are queued and flushed only after the commit that durably stores
    # what they announce, so nested-session writers never contend this
    # session's write transaction.
    pending_events: list[tuple] = []

    def _queue_emit(workspace_id: str, kind: str, message: str, **data) -> None:
        pending_events.append((workspace_id, kind, message, data))

    def _checkpoint() -> None:
        """Commit stage work, then emit queued events outside the transaction."""
        s.commit()
        while pending_events:
            workspace_id, kind, message, data = pending_events.pop(0)
            emit(workspace_id, kind, message, **data)

    def _atomic(work):
        """Run one unit atomically: failure rolls back the unit alone."""
        save = s.begin_nested()
        try:
            out = work()
        except Exception:
            save.rollback()
            raise
        else:
            save.commit()
            return out

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
        _checkpoint()

    from app.models import ContentItem, ContentTimeline

    master = s.get(ContentItem, plan.get("master_content_id", "")) if plan.get("master_content_id") else None
    platforms = plan.get("target_platforms") or (campaign.platforms_json or [])
    desired = int(plan.get("desired_shorts", 0) or 0)
    cta_kind = plan.get("cta_kind", "FOLLOW")

    _progress("discovery", 0, max(desired * len(platforms), 1))
    jobs_service.check_cancelled(ctx)
    # Committed above: the LLM cost-tracking nested write inside discovery
    # no longer contends this session.
    moments = _discover_moments(s, campaign.workspace_id, master, max(desired * 3, desired + 2))
    _progress("discovery", len(moments), max(desired * len(platforms), 1))
    jobs_service.check_cancelled(ctx)
    # Committed above: the intelligence advisory persist inside selection
    # runs outside this session's write transaction.
    selected = _select_moments(s, campaign, moments, desired)
    _progress("select", len(selected), max(desired, 1))

    # shorts (resumable: reuse existing campaign shorts, derive the rest;
    # one bad moment never kills the campaign; each short commits atomically)
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
            (child,) = _atomic(
                lambda: _derive_moment(s, campaign, master, moment, plan))
            shorts.append(child)
            covered.append((float(moment["start"]), float(moment["end"])))
            _queue_emit(campaign.workspace_id, "campaign.short_created",
                        f"Short {child.id} derived", campaign_id=campaign.id,
                        short_content_id=child.id)
            _checkpoint()
        except Exception as exc:
            derived_errors.append({
                "moment": {k: moment.get(k) for k in ("start", "end", "hook")},
                "error": f"{type(exc).__name__}: {exc}",
            })
    if derived_errors:
        plan["derived_errors"] = derived_errors
    _progress("shorts", len(shorts), max(desired, len(shorts)))

    # variants + metadata (metadata-only shares the base timeline).
    # Each variant commits atomically; resume upserts idempotently.
    gen = PlatformMetadataGenerator()
    store = default_store(s)
    # Brand DNA (Work 08 Lane C): one policy resolution PER PLATFORM, reused
    # for every short — variant metadata carries required disclaimers,
    # platform-level overrides and the effective_config_id provenance.
    brand_meta: dict[str, dict] = {}
    try:
        from app.engine.brand_templates import brand_gate, brand_variant_metadata

        for _platform in platforms:
            _gate = brand_gate(s, campaign.workspace_id, campaign_id=campaign.id,
                               platform=_platform,
                               artifact={"content_format": "short"})
            _fragment = brand_variant_metadata(_gate, platform=_platform)
            if _fragment:
                brand_meta[_platform] = _fragment
    except Exception:  # noqa: BLE001 — brand never breaks derivation
        brand_meta = {}
    total_variants = len(shorts) * len(platforms)
    made = 0
    for short in shorts:
        for platform in platforms:
            jobs_service.check_cancelled(ctx)
            metadata = gen.generate(topic=short.topic, platform=platform,
                                    cta_kind=cta_kind,
                                    master_content_id=master.id if master else "")
            if brand_meta.get(platform):
                metadata = {**metadata, **brand_meta[platform]}

            def _one_variant(
                short_id: str = short.id, platform_name: str = platform,
                bundle: dict = metadata,
            ):
                return build_variant(s, campaign.workspace_id, short_id,
                                     platform_name, None, cta_kind,
                                     campaign_id=campaign.id, metadata=bundle,
                                     store=store)

            _atomic(_one_variant)
            made += 1
            _queue_emit(campaign.workspace_id, "campaign.variant_created",
                        f"Variant for {short.id} on {platform}",
                        campaign_id=campaign.id, short_content_id=short.id,
                        platform=platform)
            _checkpoint()
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
    s.flush()
    _checkpoint()
    from app.engine.campaign.publish_flow import schedule_plan

    sched = schedule_plan(session=s, workspace_id=campaign.workspace_id,
                          campaign_id=campaign.id, items=items)
    _checkpoint()
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
    sidecar["campaign_plan"] = plan
    campaign.kpis_json = dict(sidecar)
    from app.engine.campaign.costs import track_campaign_cost

    track_campaign_cost(s, campaign.workspace_id, campaign.id,
                        "derivation", 0.0,
                        detail={"shorts": len(shorts), "variants": made})
    s.flush()
    _queue_emit(campaign.workspace_id, "campaign.ready",
                f"Campaign {campaign.id} ready ({made} variants)",
                campaign_id=campaign.id)
    _checkpoint()
    return {"shorts": len(shorts), "variants": made, "stages": stages}
