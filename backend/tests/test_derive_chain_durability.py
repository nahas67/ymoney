"""Work 06 Lane A: derive-chain durability — stage commits, no nested-write
stalls, kill-mid-chain resume without duplicates."""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

PLATFORMS = ["youtube_shorts", "tiktok"]


class _Jobs:
    """jobs_service double; fail_after=N raises _Cancelled on the N+1th check."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls = 0
        self.fail_after = fail_after

    def check_cancelled(self, ctx) -> None:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            from app.services.jobs import _Cancelled

            raise _Cancelled()


def _workspace(session):
    from app.models import Workspace

    ws = Workspace(name="Derive WS", slug=f"drv-{uuid.uuid4().hex[:8]}", niche="money")
    session.add(ws)
    session.flush()
    return ws.id


def _master(session, ws_id, campaign_id, duration=300.0):
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, Scene

    master = ContentItem(
        workspace_id=ws_id, campaign_id=campaign_id,
        topic="master personal finance video", status="PUBLISHED")
    session.add(master)
    session.flush()
    doc = create_empty(ws_id, duration_seconds=duration, aspect="16:9")
    add_clip(doc, track="video", clip_id="v1", name="master shot",
             start=0.0, duration=duration, source={"file": "master.mp4"})
    for i, (s, text) in enumerate([
        (0.0, "Welcome to the money masterclass today."),
        (30.0, "First rule is to pay yourself before bills."),
        (90.0, "Second rule compounds every single year."),
        (150.0, "Third rule avoids lifestyle inflation traps."),
        (210.0, "Final rule is to automate all investing."),
    ]):
        add_clip(doc, track="caption", clip_id=f"c{i}", name=text,
                 start=s, duration=20.0)
    session.add(ContentTimeline(
        workspace_id=ws_id, content_item_id=master.id, name="main",
        fps=30.0, duration_seconds=duration, tracks_json=doc, version=1))
    for i, (s, e) in enumerate([(0.0, 60.0), (60.0, 120.0), (120.0, 180.0),
                                (180.0, 240.0), (240.0, 300.0)]):
        session.add(Scene(
            workspace_id=ws_id, content_item_id=master.id, chapter_id=f"ch-{i % 3}",
            index=i, title=f"part {i}", script_segment=f"master segment {i}",
            start_seconds=s, end_seconds=e))
    session.flush()
    return master


def _campaign(session, ws_id, master, desired=3):
    from app.models import Campaign
    from app.models.campaign import CampaignPlan

    campaign = Campaign(
        workspace_id=ws_id, name="Durability push", goal="subs",
        platforms_json=list(PLATFORMS))
    session.add(campaign)
    session.flush()
    plan = {
        "master_content_id": master.id,
        "target_platforms": list(PLATFORMS),
        "desired_shorts": desired,
        "cta_kind": "FOLLOW",
        "status": "DRAFT",
        "progress": {"completed": 0, "total": desired * len(PLATFORMS)},
    }
    campaign.kpis_json = {"campaign_plan": dict(plan)}
    session.add(CampaignPlan(
        workspace_id=ws_id, campaign_id=campaign.id,
        master_content_id=master.id, goal="subs",
        target_platforms=list(PLATFORMS), desired_shorts=desired,
        status="DRAFT"))
    session.flush()
    return campaign, plan


def _shorts(session, ws_id, campaign_id):
    from sqlalchemy import select

    from app.models import ContentItem

    return list(session.scalars(select(ContentItem).where(
        ContentItem.workspace_id == ws_id,
        ContentItem.campaign_id == campaign_id,
        ContentItem.derivation_type == "short")).all())


def _variants(session, ws_id, campaign_id):
    from sqlalchemy import select

    from app.models.campaign import PlatformVariant

    return list(session.scalars(select(PlatformVariant).where(
        PlatformVariant.workspace_id == ws_id,
        PlatformVariant.campaign_id == campaign_id)).all())


def test_no_emit_inside_open_transaction(db_session):
    """Events fire only after stage commits (no nested-write stall pattern)."""
    from app.engine.campaign.derive import run_derive_campaign

    ws_id = _workspace(db_session)
    campaign, _ = _campaign(db_session, ws_id, _master(db_session, ws_id, "x"))
    plan = dict((campaign.kpis_json or {})["campaign_plan"])
    emitted: list[tuple] = []

    def _guarded_emit(workspace_id, kind, message, **data):
        assert not db_session.in_transaction(), (
            f"emit {kind!r} ran inside an open transaction — nested writers stall")
        emitted.append((kind, data))

    out = run_derive_campaign(
        db_session, campaign, plan, SimpleNamespace(),
        jobs_service=_Jobs(), emit=_guarded_emit)
    assert out["shorts"] == 3, out
    assert out["variants"] == 6, out
    kinds = [k for k, _ in emitted]
    assert kinds.count("campaign.short_created") == 3
    assert kinds.count("campaign.variant_created") == 6
    assert kinds[-1] == "campaign.ready"


def test_kill_mid_chain_resume_without_duplicates(db_session):
    """Stop mid-shorts (cancel), then resume: completes, no duplicates."""
    from app.engine.campaign.derive import run_derive_campaign
    from app.services.jobs import _Cancelled

    ws_id = _workspace(db_session)
    campaign, _ = _campaign(db_session, ws_id, _master(db_session, ws_id, "x"))
    plan = dict((campaign.kpis_json or {})["campaign_plan"])
    ctx = SimpleNamespace()
    emitted: list[tuple] = []

    def _quiet_emit(workspace_id, kind, message, **data):
        emitted.append(kind)

    # Checks: 1 pre-discover, 1 post-discover, then one per moment.
    # fail_after=4 dies on the 3rd moment: 2 shorts committed, 3rd untouched.
    try:
        run_derive_campaign(
            db_session, campaign, plan, ctx,
            jobs_service=_Jobs(fail_after=4), emit=_quiet_emit)
        raise AssertionError("expected cancellation")
    except _Cancelled:
        pass
    partial = _shorts(db_session, ws_id, campaign.id)
    assert len(partial) == 2, [s.topic for s in partial]

    # Resume from the durable sidecar, like the job handler does.
    db_session.refresh(campaign)
    resumed_plan = dict((campaign.kpis_json or {})["campaign_plan"])
    out = run_derive_campaign(
        db_session, campaign, resumed_plan, ctx,
        jobs_service=_Jobs(), emit=_quiet_emit)
    assert out["shorts"] == 3, out
    assert out["variants"] == 6, out
    final = _shorts(db_session, ws_id, campaign.id)
    topics = [s.topic for s in final]
    assert len(set(topics)) == 3, topics
    variants = _variants(db_session, ws_id, campaign.id)
    assert len(variants) == 6
    pairs = [(v.short_content_id, v.platform) for v in variants]
    assert len(set(pairs)) == 6

    # Idempotent re-run: nothing duplicated, schedule fully reused.
    again = run_derive_campaign(
        db_session, campaign, dict((campaign.kpis_json or {})["campaign_plan"]),
        ctx, jobs_service=_Jobs(), emit=_quiet_emit)
    assert again["shorts"] == 3 and again["variants"] == 6
    assert len(_shorts(db_session, ws_id, campaign.id)) == 3
    assert len(_variants(db_session, ws_id, campaign.id)) == 6


def test_derive_wall_time_with_real_emit(db_session):
    """Full chain with the REAL event pipeline stays far under the stall bound.

    Pre-fix each nested write busy-waited ~5s on the outer SQLite write lock
    (7+ emits plus advisory persists ≈ 40s+ here); post-fix the chain runs in
    seconds. Bound is generous to avoid flakes — it guards the pattern, not
    the hardware.
    """
    from app.engine.campaign.derive import run_derive_campaign

    ws_id = _workspace(db_session)
    campaign, _ = _campaign(
        db_session, ws_id, _master(db_session, ws_id, "x"), desired=2)
    plan = dict((campaign.kpis_json or {})["campaign_plan"])
    started = time.monotonic()
    out = run_derive_campaign(
        db_session, campaign, plan, SimpleNamespace(), jobs_service=_Jobs())
    elapsed = time.monotonic() - started
    assert out == {"shorts": 2, "variants": 4,
                   "stages": ["discovery", "select", "shorts", "variants", "plan"]}
    assert elapsed < 60, f"derive took {elapsed:.1f}s — nested-write stall?"
