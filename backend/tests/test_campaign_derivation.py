"""Work 04 Lane A: campaign derivation — lineage, diversity, repair, hooks, QC, costs."""
from __future__ import annotations

import uuid

import pytest


def _workspace(session):
    from app.models import Workspace

    ws = Workspace(name="Campaign WS", slug=f"cmp-{uuid.uuid4().hex[:8]}", niche="money")
    session.add(ws)
    session.flush()
    return ws.id


def _campaign(session, ws_id):
    from app.models import Campaign

    campaign = Campaign(workspace_id=ws_id, name="Master push", goal="subs")
    session.add(campaign)
    session.flush()
    return campaign


def _master(session, ws_id, campaign_id):
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, Scene

    master = ContentItem(
        workspace_id=ws_id, campaign_id=campaign_id,
        topic="master personal finance video", status="PUBLISHED",
    )
    session.add(master)
    session.flush()

    doc = create_empty(ws_id, duration_seconds=300.0, aspect="16:9")
    add_clip(doc, track="video", clip_id="v1", name="master shot",
             start=0.0, duration=300.0, source={"file": "master.mp4"})
    for i, (s, text) in enumerate([
        (0.0, "Welcome to the money masterclass today."),
        (30.0, "First rule is to pay yourself before bills."),
        (90.0, "Second rule compounds every single year."),
        (150.0, "Third rule avoids lifestyle inflation traps."),
        (210.0, "Final rule is to automate all investing."),
    ]):
        add_clip(doc, track="caption", clip_id=f"c{i}", name=text,
                 start=s, duration=20.0)
    timeline = ContentTimeline(
        workspace_id=ws_id, content_item_id=master.id, name="main",
        fps=30.0, duration_seconds=300.0, tracks_json=doc, version=1,
    )
    session.add(timeline)
    for i, (s, e) in enumerate([(0.0, 60.0), (60.0, 120.0), (120.0, 180.0),
                                (180.0, 240.0), (240.0, 300.0)]):
        session.add(Scene(
            workspace_id=ws_id, content_item_id=master.id, timeline_id=None,
            chapter_id=f"ch-{i % 3}", index=i, title=f"part {i}",
            script_segment=f"master segment {i} about money rules",
            start_seconds=s, end_seconds=e,
        ))
    session.flush()
    return master


def _moments():
    return [
        {"start": 28.0, "end": 58.0, "topic": "pay yourself first",
         "hook_text": "Why do paychecks vanish by Friday?",
         "cta_text": "Follow for rule two",
         "transcript": "First rule is to pay yourself before bills."},
        {"start": 88.0, "end": 118.0, "topic": "compounding yearly",
         "hook_text": "This habit beat 200 stock picks",
         "cta_text": "Follow for rule three",
         "transcript": "Second rule compounds every single year."},
        {"start": 148.0, "end": 178.0, "topic": "lifestyle inflation",
         "hook_text": "Raises can make you poorer",
         "cta_text": "Follow for the finale",
         "transcript": "Third rule avoids lifestyle inflation traps."},
    ]


def test_plan_build_and_idempotent(db_session):
    from app.engine.campaign.plan import PlanError, build_derivation_plan
    from app.models.campaign import CampaignPlan

    ws_id = _workspace(db_session)
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)

    plan = build_derivation_plan(
        db_session, campaign.id, master.id, ["YouTube", "tiktok"],
        desired_shorts=3, goal="growth",
    )
    assert plan.target_platforms == ["tiktok", "youtube"]
    assert plan.desired_shorts == 3
    assert plan.status == "DRAFT"

    again = build_derivation_plan(
        db_session, campaign.id, master.id, ["tiktok"], desired_shorts=5,
    )
    assert again.id == plan.id
    rows = db_session.query(CampaignPlan).filter(
        CampaignPlan.campaign_id == campaign.id).all()
    assert len(rows) == 1

    with pytest.raises(PlanError):
        build_derivation_plan(db_session, campaign.id, master.id, [])
    with pytest.raises(PlanError):
        build_derivation_plan(db_session, "missing", master.id, ["tiktok"])
    with pytest.raises(PlanError):
        build_derivation_plan(db_session, campaign.id, master.id, ["tiktok"],
                              desired_shorts=0)


def test_derive_shorts_lineage(db_session):
    from app.engine.campaign.plan import build_derivation_plan
    from app.engine.campaign.shorts import derive_shorts
    from app.engine.timeline import validate_timeline
    from app.models import ContentTimeline, Scene

    ws_id = _workspace(db_session)
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)
    plan = build_derivation_plan(
        db_session, campaign.id, master.id, ["youtube", "tiktok"], desired_shorts=3)

    shorts = derive_shorts(db_session, ws_id, campaign.id, master.id, _moments(), plan)
    assert len(shorts) == 3
    for short in shorts:
        assert short.parent_content_id == master.id
        assert short.root_content_id == master.id
        assert short.campaign_id == campaign.id
        assert short.derivation_type == "short"
        timeline = db_session.query(ContentTimeline).filter(
            ContentTimeline.content_item_id == short.id).one()
        assert timeline.tracks_json["aspect_ratio"] == "9:16"
        validate_timeline(timeline.tracks_json)  # raises when invalid
        video = next(t for t in timeline.tracks_json["tracks"] if t["kind"] == "video")
        assert video["clips"], "short must carry sliced video"
        assert video["clips"][0]["source"]["master_range"]["start"] >= 0
        scene = db_session.query(Scene).filter(
            Scene.content_item_id == short.id).one()
        assert scene.timeline_id == timeline.id
        assert scene.parent_scene_id is not None  # linked to a master scene
    assert plan.progress_json["shorts_derived"] == 3


def test_short_timeline_references_master_range_and_keeps_master(db_session):
    import copy

    from app.engine.campaign.shorts import build_short_timeline

    ws_id = _workspace(db_session)
    master = _master(db_session, ws_id, _campaign(db_session, ws_id).id)
    from app.models import ContentTimeline

    row = db_session.query(ContentTimeline).filter(
        ContentTimeline.content_item_id == master.id).one()
    before = copy.deepcopy(row.tracks_json)
    doc = build_short_timeline(
        db_session, ws_id, row.tracks_json, 28.0, 58.0,
        hook_text="Why do paychecks vanish?", cta_text="Follow along",
    )
    assert doc["campaign"]["master_range"] == {"start": 28.0, "end": 58.0}
    assert abs(doc["duration_seconds"] - 30.0) < 1e-9  # full precision, no round()
    caption_names = [
        c["name"] for t in doc["tracks"] if t["kind"] == "caption" for c in t["clips"]
    ]
    assert caption_names, "short keeps master caption coverage"
    assert row.tracks_json == before  # master doc untouched


def test_diversity_spreads_chapters_and_guards_overlap():
    from app.engine.campaign.diversity import CampaignDiversitySelector, transcript_overlap

    dup_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    candidates = [
        {"id": "m1", "virality_score": 95.0, "topic": "pay yourself first",
         "hook": "Why do paychecks vanish by Friday?", "chapter": "ch-0",
         "transcript": dup_text, "scene_ids": ["s1"]},
        {"id": "m2", "virality_score": 94.0, "topic": "pay yourself first repeat",
         "hook": "Paychecks vanish every single Friday", "chapter": "ch-0",
         "transcript": dup_text, "scene_ids": ["s1"]},
        {"id": "m3", "virality_score": 80.0, "topic": "compounding yearly gains",
         "hook": "This habit beat two hundred stock picks", "chapter": "ch-1",
         "transcript": "compound interest grows wealth slowly over decades",
         "scene_ids": ["s3"]},
        {"id": "m4", "virality_score": 79.0, "topic": "lifestyle inflation trap",
         "hook": "Raises can make you poorer over time", "chapter": "ch-2",
         "transcript": "lifestyle creep eats every raise luxury cars dinners",
         "scene_ids": ["s4"]},
        {"id": "m5", "virality_score": 70.0, "topic": "automate all investing",
         "hook": "Set one transfer and retire richer", "chapter": "ch-0",
         "transcript": "automatic transfers build portfolios without willpower",
         "scene_ids": ["s5"]},
    ]
    picked = CampaignDiversitySelector().select(candidates, 3)
    assert len(picked) == 3
    ids = {c["id"] for c in picked}
    assert not ({"m1", "m2"} <= ids), "near-duplicate transcripts never co-selected"
    assert len({c["chapter"] for c in picked}) >= 2, "selection spreads chapters"
    texts = [c["transcript"] for c in picked]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            assert transcript_overlap(texts[i], texts[j]) <= 0.8


def test_repair_extends_drops_and_labels_generated():
    from app.engine.campaign.repair import ClipContextRepair, split_sentences

    segments = [
        {"start": 0.0, "end": 10.0,
         "text": 'She said "buy low. sell high." Then he left the room entirely.'},
        {"start": 10.0, "end": 30.0,
         "text": "Later the market recovered slowly. Everyone stayed calm."},
    ]
    repairer = ClipContextRepair()

    extended = repairer.repair(start=1.0, end=29.0, segments=segments,
                               hook_text="Markets punish panic", cta_text="Follow on")
    assert extended["extended"] is True
    assert '"buy low. sell high."' in extended["text"], "quoted speech never altered"
    assert [g["kind"] for g in extended["generated"]] == ["hook", "cta"]
    assert all(g["generated"] is True for g in extended["generated"])
    assert extended["generated"][0]["text"] == "Markets punish panic"

    dropped = repairer.repair(start=9.0, end=29.0, segments=segments)
    assert dropped["dropped_leading"] == 1
    assert "Then he left" not in dropped["text"]

    # Sentence splitter never breaks inside quotes.
    assert "buy low." not in split_sentences('She said "buy low. sell high." Done.')


def test_hooks_have_valid_types_and_no_deception():
    from app.engine.campaign.hooks import HOOK_TYPES, optimize_hook

    out = optimize_hook(
        "You won't believe this guaranteed shocking trick!", topic="budgeting")
    assert out["hook_type"] in HOOK_TYPES
    lowered = out["optimized_hook"].lower()
    assert "guaranteed" not in lowered
    assert "won't believe" not in lowered
    assert out["original_hook"].startswith("You won't believe")
    assert out["predicted_score"] > 0

    assert optimize_hook("Why does money vanish?", topic="x")["hook_type"] == "QUESTION"
    assert optimize_hook("5 rules that beat 200 funds", topic="x")["hook_type"] == "NUMBER"


def test_short_qc_pass_and_fail_paths(db_session):
    from app.engine.campaign.qc import short_qc
    from app.engine.campaign.shorts import build_short_timeline

    ws_id = _workspace(db_session)
    master = _master(db_session, ws_id, _campaign(db_session, ws_id).id)
    from app.models import ContentTimeline

    row = db_session.query(ContentTimeline).filter(
        ContentTimeline.content_item_id == master.id).one()
    good = build_short_timeline(
        db_session, ws_id, row.tracks_json, 28.0, 58.0,
        hook_text="Why do paychecks vanish?", cta_text="Follow along")
    result = short_qc(good, {"has_audio": True, "max_transcript_overlap": 0.2})
    assert result["result"] == "PASS", result["checks"]
    assert set(result["checks"]) == {
        "duration", "aspect", "audio", "captions", "hook",
        "safe_zone", "duplicate_overlap",
    }

    no_captions = build_short_timeline(
        db_session, ws_id, row.tracks_json, 28.0, 58.0, hook_text="Hook here")
    no_captions["tracks"] = [t for t in no_captions["tracks"] if t["kind"] != "caption"]
    bad = short_qc(no_captions, {"has_audio": False, "max_transcript_overlap": 0.9})
    assert bad["result"] == "FAIL"
    assert bad["checks"]["captions"]["status"] == "fail"
    assert bad["checks"]["audio"]["status"] == "fail"
    assert bad["checks"]["duplicate_overlap"]["status"] == "fail"

    wrong_aspect = dict(good)
    wrong_aspect["aspect_ratio"] = "16:9"
    assert short_qc(wrong_aspect, {"has_audio": True})["result"] == "FAIL"


def test_campaign_qc_and_costs(db_session):
    from app.engine.campaign.costs import track_campaign_cost
    from app.engine.campaign.plan import build_derivation_plan
    from app.engine.campaign.qc import campaign_qc
    from app.engine.campaign.shorts import derive_shorts
    from app.models import CostEntry

    ws_id = _workspace(db_session)
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)
    plan = build_derivation_plan(
        db_session, campaign.id, master.id, ["youtube", "tiktok"], desired_shorts=3)
    derive_shorts(db_session, ws_id, campaign.id, master.id, _moments(), plan)

    report = campaign_qc(db_session, campaign.id)
    assert report["counts"]["shorts"] == 3
    assert report["counts"]["timelines"] == 3
    assert report["diversity"]["pairwise_overlap_max"] <= 0.8
    assert report["diversity"]["chapters_covered"] >= 1
    assert report["result"] in ("PASS", "PASS_WITH_WARNINGS")

    total = track_campaign_cost(db_session, ws_id, campaign.id, "video", 1.25,
                                {"engine": "mock"})
    assert total == pytest.approx(1.25)
    total = track_campaign_cost(db_session, ws_id, campaign.id, "llm", 0.10, None)
    assert total == pytest.approx(1.35)
    entries = db_session.query(CostEntry).filter(
        CostEntry.workspace_id == ws_id).all()
    assert sum(e.amount_usd for e in entries) == pytest.approx(1.35)
    assert all(e.detail_json.get("campaign_id") == campaign.id for e in entries)
