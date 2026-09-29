"""Work 06 Lane A: retention ingest/normalize/map/detect + honest UNAVAILABLE."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime


def _workspace(session):
    from app.models import Workspace

    ws = Workspace(name="Retention WS", slug=f"ret-{uuid.uuid4().hex[:8]}", niche="money")
    session.add(ws)
    session.flush()
    return ws.id


def _short_fixture(session, ws_id, campaign_id="camp-1"):
    """Short with 30s timeline, caption clips and two chaptered scenes."""
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, Scene

    short = ContentItem(
        workspace_id=ws_id, campaign_id=campaign_id, topic="hook test short",
        status="READY", strategy_json={"hook_type": "question"},
        parent_content_id="master-1", root_content_id="master-1",
        derivation_type="short",
    )
    session.add(short)
    session.flush()
    doc = create_empty(ws_id, duration_seconds=30.0, aspect="9:16")
    add_clip(doc, track="caption", clip_id="c1", name="hook line one",
             start=0.0, duration=3.0)
    add_clip(doc, track="caption", clip_id="c2", name="second line here",
             start=3.0, duration=4.0)
    add_clip(doc, track="caption", clip_id="c3", name="payoff line here",
             start=16.0, duration=4.0)
    session.add(ContentTimeline(
        workspace_id=ws_id, content_item_id=short.id, name="main",
        fps=30.0, duration_seconds=30.0, tracks_json=doc, version=1))
    session.add(Scene(
        workspace_id=ws_id, content_item_id=short.id, index=0, title="setup",
        chapter_id="ch-a", script_segment="hook line one second line",
        start_seconds=0.0, end_seconds=15.0,
        beats_json=[{"start": 1.0, "end": 2.0, "kind": "hook-beat"}]))
    session.add(Scene(
        workspace_id=ws_id, content_item_id=short.id, index=1, title="payoff",
        chapter_id="ch-b", script_segment="payoff line here",
        start_seconds=15.0, end_seconds=30.0))
    session.flush()
    return short.id


def test_ingest_curve_mapping_accuracy(db_session):
    from app.engine.performance.retention import RetentionAnalyzer
    from app.models import Scene
    from app.models.performance import PerformanceObservation, RetentionPoint

    ws_id = _workspace(db_session)
    short_id = _short_fixture(db_session, ws_id)
    analyzer = RetentionAnalyzer(db_session, ws_id)
    curve = [{"t": 0.0, "v": 1.0}, {"t": 3.0, "v": 0.9}, {"t": 7.5, "v": 0.85},
             {"t": 15.0, "v": 0.7}, {"t": 22.5, "v": 0.6}, {"t": 30.0, "v": 0.55}]
    out = analyzer.ingest(short_content_id=short_id, campaign_id="camp-1",
                          curve=curve, source="fixture")
    assert out["status"] == "AVAILABLE"
    for cp in ("1s", "3s", "25%", "50%", "75%", "100%"):
        assert cp in out["curve"], out
    assert out["curve"]["100%"] == 0.55

    mapping = out["mapping"]
    assert mapping["1s"]["is_hook"] is True
    assert mapping["1s"]["mapped"] is True
    assert mapping["1s"]["chapter_id"] == "ch-a"
    assert mapping["50%"]["chapter_id"] == "ch-b"
    assert mapping["1s"]["caption_state"]["active"] is True
    assert mapping["1s"]["visual_beats"], mapping["1s"]

    rows = db_session.query(RetentionPoint).filter(
        RetentionPoint.short_content_id == short_id).all()
    assert len(rows) == 6
    scenes = db_session.query(Scene).filter(
        Scene.content_item_id == short_id).order_by(Scene.index).all()
    assert (scenes[0].performance_json or {}).get("retention", {}).get("1s") == 0.9667
    obs = db_session.query(PerformanceObservation).filter(
        PerformanceObservation.subject_id == short_id).all()
    assert obs and obs[0].metric == "retention_curve"


def test_analyze_unavailable_honest_with_coarse_proxy(db_session):
    from app.engine.performance.retention import RetentionAnalyzer
    from app.models import ContentItem, PostMetric, PublishedPost

    ws_id = _workspace(db_session)
    short_id = _short_fixture(db_session, ws_id)
    short = db_session.get(ContentItem, short_id)
    post = PublishedPost(
        workspace_id=ws_id, content_item_id=short_id, video_id=short_id,
        platform="youtube", remote_post_id="r1", title="t",
        campaign_id="camp-1")
    db_session.add(post)
    db_session.flush()
    db_session.add(PostMetric(
        post_id=post.id, views=1000, likes=100,
        avg_view_duration_seconds=12.0, completion_rate=0.4,
        captured_at=datetime.now(UTC)))
    db_session.flush()

    out = RetentionAnalyzer(db_session, ws_id).analyze(post_id=post.id)
    assert out["status"] == "UNAVAILABLE"
    assert out["curve"] == {}
    assert "granular" in out["reason"]
    proxy = out["coarse_proxy"]
    assert proxy["label"] == "coarse_proxy" and proxy["available"] is True
    assert proxy["completion_rate"] == 0.4
    assert short.topic  # fixture sanity


def test_drop_and_rewatch_detection():
    from app.engine.performance.retention import RetentionAnalyzer

    drops = RetentionAnalyzer.detect_drops(
        {"1s": 1.0, "3s": 0.9, "25%": 0.5, "50%": 0.48,
         "75%": 0.47, "100%": 0.46}, None)
    kinds = [d for d in drops if d["type"] == "threshold_drop"]
    assert kinds and kinds[0]["from"] == "3s" and kinds[0]["to"] == "25%"
    assert kinds[0]["loss"] == 0.4

    slopes = RetentionAnalyzer.detect_drops(
        {}, [(0.0, 1.0), (1.0, 0.9), (10.0, 0.2)])
    assert any(d["type"] == "steep_slope" for d in slopes)

    rewatches = RetentionAnalyzer.detect_rewatches(
        [(0.0, 0.9), (2.0, 0.6), (4.0, 0.8), (6.0, 0.5)])
    assert len(rewatches) == 1 and rewatches[0]["t"] == 4.0
    assert RetentionAnalyzer.detect_rewatches(
        [(0.0, 1.0), (1.0, 0.9), (2.0, 0.8)]) == []


def test_retention_workspace_isolation(db_session):
    from app.engine.performance.retention import RetentionAnalyzer

    ws_a = _workspace(db_session)
    ws_b = _workspace(db_session)
    short_id = _short_fixture(db_session, ws_a)
    RetentionAnalyzer(db_session, ws_a).ingest(
        short_content_id=short_id, curve=[(0.0, 1.0), (30.0, 0.5)],
        source="fixture")
    db_session.commit()
    # Same rows queried from another workspace: honestly UNAVAILABLE.
    out = RetentionAnalyzer(db_session, ws_b).analyze(short_content_id=short_id)
    assert out["status"] == "UNAVAILABLE"
    assert out["coarse_proxy"] == {"label": "coarse_proxy", "available": False}


def test_performance_routes_overview_retention_compare():
    import uuid as _uuid
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"perf{_uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ws_id = r.json()["workspace"]["id"]

    from app.db import session_scope
    from app.models import Campaign, ContentItem, PostMetric, PublishedPost

    with session_scope() as s:
        campaign = Campaign(workspace_id=ws_id, name="Perf push", goal="subs",
                            platforms_json=["youtube_shorts"])
        s.add(campaign)
        s.flush()
        shorts = []
        for i, hook in enumerate(("question", "number")):
            row = ContentItem(
                workspace_id=ws_id, campaign_id=campaign.id,
                topic=f"short hook {i}", status="READY",
                strategy_json={"hook_type": hook},
                parent_content_id="master-1", root_content_id="master-1",
                derivation_type="short")
            s.add(row)
            s.flush()
            shorts.append(row)
        posts = []
        for i, short in enumerate(shorts):
            post = PublishedPost(
                workspace_id=ws_id, content_item_id=short.id,
                video_id=short.id, platform="youtube",
                remote_post_id=f"rp-{i}", title=f"short {i}",
                campaign_id=campaign.id)
            s.add(post)
            s.flush()
            posts.append(post)
            s.add(PostMetric(
                post_id=post.id, views=1000 * (i + 1), likes=100,
                avg_view_duration_seconds=10.0, completion_rate=0.5,
                captured_at=_dt.now(_utc)))
        camp_id = campaign.id
        post_id = posts[0].id

    r = client.get(f"/api/v1/workspaces/{ws_id}/performance/overview",
                   params={"campaign_id": camp_id}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["rollup"]["totals"]["views"] == 3000

    r = client.get(f"/api/v1/workspaces/{ws_id}/performance/retention",
                   params={"post_id": post_id}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "UNAVAILABLE"
    assert body["coarse_proxy"]["label"] == "coarse_proxy"

    r = client.get(f"/api/v1/workspaces/{ws_id}/performance/compare",
                   params={"campaign_id": camp_id, "group_by": "hook"},
                   headers=headers)
    assert r.status_code == 200, r.text
    groups = {g["group"]: g for g in r.json()["groups"]}
    assert groups["question"]["n"] == 1 and groups["number"]["n"] == 1

    r = client.get(f"/api/v1/workspaces/{ws_id}/performance/compare",
                   params={"campaign_id": camp_id, "group_by": "bogus"},
                   headers=headers)
    assert r.status_code == 400

    # Cross-workspace isolation: another user cannot reach this campaign.
    r2 = client.post("/api/v1/auth/register",
                     json={"email": f"perf2{_uuid.uuid4().hex[:8]}@test.local",
                           "password": "supersecret123"})
    headers2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    r = client.get(f"/api/v1/workspaces/{ws_id}/performance/overview",
                   params={"campaign_id": camp_id}, headers=headers2)
    assert r.status_code in (403, 404), r.text
