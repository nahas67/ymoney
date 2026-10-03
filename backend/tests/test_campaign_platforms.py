"""Lane B: platform profiles, variants, metadata, preflight, plan, API.

Deterministic, no network: publisher factory is stubbed where touched and
ffprobe-dependent checks are injected or skipped honestly.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta


def _register(client, email=None):
    email = email or f"cb{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _make_master(ws_id):
    from app.db import session_scope
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, Scene

    with session_scope() as s:
        row = ContentItem(workspace_id=ws_id, topic="Save 20% of every paycheck", status="READY")
        s.add(row)
        s.flush()
        mid = row.id
        doc = create_empty(ws_id, duration_seconds=120.0, aspect="16:9")
        add_clip(doc, track="video", clip_id="v1", name="master shot",
                 start=0.0, duration=120.0, source={"file": "master.mp4"})
        captions = [
            (0.0, "Want to know the secret to saving money every month?"),
            (20.0, "First, pay yourself twenty percent before any bills."),
            (40.0, "Second, automate the transfer so you never miss it."),
            (60.0, "Why do most budgets fail within ninety days?"),
            (80.0, "Third, track every dollar with a simple money habit."),
            (100.0, "Start today and watch savings grow year after year."),
        ]
        for i, (st, text) in enumerate(captions):
            add_clip(doc, track="caption", clip_id=f"c{i}", name=text,
                     start=st, duration=20.0)
        s.add(ContentTimeline(
            workspace_id=ws_id, content_item_id=mid, name="main",
            fps=30.0, duration_seconds=120.0, tracks_json=doc, version=1))
        for i, (st, e) in enumerate([(0.0, 60.0), (60.0, 120.0)]):
            s.add(Scene(
                workspace_id=ws_id, content_item_id=mid, timeline_id=None,
                chapter_id=f"ch-{i}", index=i, title=f"part {i}",
                script_segment=f"master segment {i} about saving",
                start_seconds=st, end_seconds=e))
        s.flush()
    return mid


# ---------------------------------------------------------------------------
# Platform profiles
# ---------------------------------------------------------------------------

def test_profiles_validate_bad_aspect_and_duration():
    from app.engine.campaign.platforms import CAMPAIGN_PLATFORMS, validate_against_profile

    assert set(CAMPAIGN_PLATFORMS) == {
        "youtube_shorts", "tiktok", "instagram_reels", "facebook_reels",
        # Work 09 §15: LinkedIn/X joined the campaign target set
        "linkedin", "x",
        # Work 14: expanded distribution (Threads, Pinterest, Bluesky, and
        # Snapchat-as-a-handoff).
        "threads", "pinterest", "bluesky", "snapchat",
    }
    good = {"title": "Save 20% of every paycheck", "description": "A simple habit.",
            "hashtags": ["#money"]}
    assert validate_against_profile("tiktok", 45.0, "9:16", good) == []

    issues = validate_against_profile("tiktok", 45.0, "16:9", good)
    assert any("aspect" in i for i in issues)

    issues = validate_against_profile("tiktok", 900.0, "9:16", good)
    assert any("exceeds" in i for i in issues)

    issues = validate_against_profile("youtube_shorts", 45.0, "9:16",
                                      {"title": "", "description": "", "hashtags": []})
    assert any("missing title" in i for i in issues)
    assert any("missing description" in i for i in issues)

    long_title = {"title": "x" * 200, "description": "ok", "hashtags": []}
    assert any("exceeds" in i for i in validate_against_profile("youtube_shorts", 30.0, "9:16", long_title))


def test_unknown_platform_raises():
    import pytest

    from app.engine.campaign.platforms import get_profile, validate_against_profile

    with pytest.raises(KeyError):
        get_profile("myspace")
    with pytest.raises(KeyError):
        validate_against_profile("myspace", 10.0, "9:16", {})


# ---------------------------------------------------------------------------
# Metadata: distinct per platform, master ref not URL
# ---------------------------------------------------------------------------

def test_metadata_differs_per_platform():
    from app.engine.campaign.metadata import PlatformMetadataGenerator
    from app.engine.campaign.platforms import CAMPAIGN_PLATFORMS

    gen = PlatformMetadataGenerator()
    bundles = gen.generate_all(topic="Save 20% of every paycheck",
                               script_excerpt="pay yourself first every month")
    # Work 09 §15: one bundle per campaign platform (now 6 incl. linkedin/x)
    assert set(bundles) == set(CAMPAIGN_PLATFORMS)
    assert len(bundles) == len(CAMPAIGN_PLATFORMS)
    assert {b["platform"] for b in bundles.values()} == set(CAMPAIGN_PLATFORMS)

    # Work 09: every campaign platform has its own voice, so all 6 bundles
    # are pairwise distinct (titles, captions AND hashtag sets).
    all_plats = list(CAMPAIGN_PLATFORMS)
    titles = [bundles[p]["title"] for p in all_plats]
    assert len(set(titles)) == len(all_plats), titles
    captions = [bundles[p]["caption"] for p in all_plats]
    assert len(set(captions)) == len(all_plats), captions
    tag_sets = [tuple(bundles[p]["hashtags"]) for p in all_plats]
    assert len(set(tag_sets)) == len(all_plats), tag_sets


def test_watch_full_video_has_master_ref_not_url():
    from app.engine.campaign.metadata import PlatformMetadataGenerator

    gen = PlatformMetadataGenerator()
    bundle = gen.generate(topic="Budget basics", platform="youtube_shorts",
                          cta_kind="WATCH_FULL_VIDEO", master_content_id="master-123")
    assert bundle["master_content_id"] == "master-123"
    assert bundle["master_url"] == ""
    blob = " ".join(str(v) for v in bundle.values())
    assert "http" not in blob


# ---------------------------------------------------------------------------
# Variants: metadata-only default, timeline copy on visual diff, siblings safe
# ---------------------------------------------------------------------------

def test_variant_shares_timeline_by_default_and_copies_on_diff(workspace_with_user):
    from app.db import session_scope
    from app.engine.campaign.variants import (
        build_variant,
        default_store,
        needs_timeline_copy,
        reframe_intent,
    )
    from app.models import ContentItem

    assert needs_timeline_copy(platform="tiktok") is False
    assert needs_timeline_copy(platform="tiktok", caption_shift=True) is True
    assert needs_timeline_copy(platform="tiktok", cta_overlay=True) is True
    assert needs_timeline_copy(platform="tiktok", base_aspect="16:9") is True
    intent = reframe_intent(platform="tiktok")
    assert intent["to_aspect"] == "9:16" and intent["from_aspect"] == "9:16"

    ws_id = workspace_with_user["workspace"]
    with session_scope() as s:
        short = ContentItem(workspace_id=ws_id, topic="real short",
                            status="IDEA", derivation_type="short")
        s.add(short)
        s.flush()
        short_id = short.id

    # real Lane A store: metadata-only shares the base timeline
    with session_scope() as s:
        store = default_store(s)
        meta = {"title": "t", "description": "d"}
        rec = build_variant(s, ws_id, short_id, "tiktok", None, "FOLLOW",
                            campaign_id="camp-1", metadata=meta, store=store)
        assert rec.timeline_id is None
        assert rec.aspect_ratio == "9:16"
        assert rec.status == "READY"

        rec2 = build_variant(s, ws_id, short_id, "instagram_reels",
                             {"duration_seconds": 30.0}, "COMMENT",
                             campaign_id="camp-1", metadata=meta,
                             caption_shift=True, store=store)
        assert rec2.timeline_id, "visual diff requires a timeline copy"
        # siblings preserved: the tiktok variant is untouched
        assert store.get(short_id, "tiktok").timeline_id is None


def test_regenerate_variant_preserves_siblings(db_session):
    from app.engine.campaign.variants import (
        MemoryVariantStore,
        build_variant,
        regenerate_variant,
    )

    store = MemoryVariantStore()
    meta = {"title": "t", "description": "d"}
    build_variant(db_session, "ws-1", "short-9", "tiktok", None, "FOLLOW", metadata=meta, store=store)
    build_variant(db_session, "ws-1", "short-9", "youtube_shorts", None, "SUBSCRIBE",
                  metadata=meta, store=store)
    rec = regenerate_variant(db_session, "ws-1", "short-9", "tiktok",
                             metadata={"title": "new", "description": "new"}, store=store)
    assert rec.status == "DRAFT"
    assert rec.metadata_json["title"] == "new"
    sibling = store.get("short-9", "youtube_shorts")
    assert sibling.metadata_json["title"] == "t"
    assert sibling.status == "READY"


# ---------------------------------------------------------------------------
# Covers: safe-zone-aware, no image pipeline
# ---------------------------------------------------------------------------

def test_cover_spec_stays_in_safe_zone():
    from app.engine.campaign.covers import build_cover_spec

    for platform in ("youtube_shorts", "tiktok", "instagram_reels", "facebook_reels"):
        spec = build_cover_spec(topic="Save 20% of every paycheck", platform=platform,
                                title="Save 20% of every paycheck")
        box, text = spec["safe_box"], spec["text_box"]
        assert text["x"] >= box["x"] and text["y"] >= box["y"]
        assert text["x"] + text["w"] <= box["x"] + box["w"]
        assert text["y"] + text["h"] <= box["y"] + box["h"]
        assert spec["source_thumbnail"] == ""
    yt = build_cover_spec(topic="t", platform="youtube_shorts")
    ig = build_cover_spec(topic="t", platform="instagram_reels")
    assert yt["behavior"] == "poster-frame" and ig["behavior"] == "cover-selectable"


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def test_preflight_catches_bad_aspect_missing_metadata_duplicates():
    from app.engine.campaign.publish_flow import PreflightInput, preflight

    bad = PreflightInput(platform="tiktok", aspect="16:9", duration=45.0,
                         metadata={"title": "", "description": ""},
                         compliance_state="pass", account_id="",
                         content_hash="abc123",
                         published_refs=[{"content_hash": "abc123", "platform": "tiktok",
                                          "account_id": ""}])
    issues = preflight(bad, account_connected=False)
    assert any("aspect" in i for i in issues)
    assert any("missing title" in i for i in issues)
    assert any("duplicate" in i for i in issues)
    assert any("no connected" in i for i in issues)

    blocked = PreflightInput(platform="youtube_shorts", aspect="9:16", duration=30.0,
                             metadata={"title": "t", "description": "d"},
                             compliance_state="fail", approval_satisfied=False)
    issues = preflight(blocked, account_connected=True)
    assert any("compliance" in i for i in issues)
    assert any("approval" in i for i in issues)

    ok = PreflightInput(platform="youtube_shorts", aspect="9:16", duration=30.0,
                        metadata={"title": "t", "description": "d"},
                        compliance_state="pass", approval_satisfied=True)
    assert preflight(ok, account_connected=True) == []


def test_preflight_undecodable_file_blocked(tmp_path):
    from app.engine.campaign.publish_flow import PreflightInput, preflight

    bad_file = tmp_path / "broken.mp4"
    bad_file.write_bytes(b"not a video")
    inp = PreflightInput(platform="tiktok", aspect="9:16", duration=30.0,
                         metadata={"title": "t", "description": "d"},
                         file_path=str(bad_file))
    issues = preflight(inp, account_connected=True)
    assert any("decode" in i or "ffprobe" in i for i in issues)


# ---------------------------------------------------------------------------
# Plan pacing + master-first dependencies (pure)
# ---------------------------------------------------------------------------

def test_plan_dependency_ordering_and_pacing():
    from app.engine.campaign.publish_flow import build_publishing_plan, dependencies_satisfied

    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    refs = [{"variant_id": f"v{i}", "platform": "tiktok"} for i in range(3)]
    items = build_publishing_plan(refs, start, 1.0,
                                  master_ref={"content_id": "master-1",
                                              "platform": "youtube_longform"})
    assert items[0]["depends_on"] == [] and items[0]["is_master"] is True
    shorts = items[1:]
    assert all(it["depends_on"] == ["master-1"] for it in shorts)
    times = [datetime.fromisoformat(it["planned_at"]) for it in items]
    assert len(set(times)) == len(times), "never all-at-once"
    assert times == sorted(times)
    assert dependencies_satisfied(shorts[0], set()) is False
    assert dependencies_satisfied(shorts[0], {"master-1"}) is True


# ---------------------------------------------------------------------------
# Idempotency: same key twice -> one job
# ---------------------------------------------------------------------------

def test_publish_idempotency_same_key_one_job(workspace_with_user):
    from sqlalchemy import select

    from app.db import session_scope
    from app.engine.campaign.publish_flow import publish_due
    from app.models import Job

    ws_id = workspace_with_user["workspace"]
    first = publish_due(None, workspace_id=ws_id, campaign_id="camp-x",
                        variant_id="var-1", platform="tiktok")
    assert first
    second = publish_due(None, workspace_id=ws_id, campaign_id="camp-x",
                         variant_id="var-1", platform="tiktok")
    assert second is None
    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.idempotency_key == "camp-var-1-tiktok",
                              Job.workspace_id == ws_id)).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Webhooks: campaign events queued through existing infra
# ---------------------------------------------------------------------------

def test_campaign_webhook_events_queued(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.engine.campaign.publish_flow import CAMPAIGN_EVENT_KINDS, emit_campaign_event
    from app.main import create_app
    from app.models import Job
    from app.services.webhooks import WEBHOOK_EVENTS

    for kind in CAMPAIGN_EVENT_KINDS:
        assert kind in WEBHOOK_EVENTS, kind

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers,
                    json={"url": "http://127.0.0.1:9/hook", "events": ["campaign.created"]})
    assert r.status_code == 200, r.text
    sub_id = r.json()["id"]
    payload = emit_campaign_event(ws_id, "campaign.created", "hello",
                                  campaign_id="camp-1")
    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.type == "webhook.dispatch",
                              Job.workspace_id == ws_id)).all()
        assert len(rows) == 1
        assert rows[0].idempotency_key == f"wh-{payload['id']}-{sub_id}"


# ---------------------------------------------------------------------------
# API: aggregate/progress/404s + schedule/publish gates
# ---------------------------------------------------------------------------

def test_campaign_api_flow_and_cross_workspace_404(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)
    master_id = _make_master(ws_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/from-master", headers=headers,
                    json={"master_content_id": master_id, "goal": "grow",
                          "target_platforms": ["tiktok", "youtube_shorts"],
                          "desired_shorts": 2})
    assert r.status_code == 201, r.text
    camp_id = r.json()["id"]

    # derive chain runs durably through the registered handler
    from app.services import jobs as jobs_service

    handler = jobs_service._handlers["campaign.derive"]
    ctx = jobs_service.JobContext(job_id="test-derive", type="campaign.derive",
                                  workspace_id=ws_id, cycle_id=None,
                                  payload={"campaign_id": camp_id}, attempt=1,
                                  cancelled=lambda: False)
    out = handler(ctx)
    assert out["shorts"] == 2

    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/aggregate", headers=headers)
    assert r.status_code == 200, r.text
    agg = r.json()
    assert len(agg["shorts"]) == 2
    # real Lane A rows: 2 shorts x 2 platforms, metadata-only shares timelines
    assert len(agg["variants"]) == 4, agg["variants"]
    assert {v["platform"] for v in agg["variants"]} == {"tiktok", "youtube_shorts"}
    assert all(v["timeline_id"] is None for v in agg["variants"])
    assert agg["progress"]["total"] >= 2

    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/content", headers=headers)
    assert r.status_code == 200 and len(r.json()["items"]) == 2

    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/progress", headers=headers)
    prog = r.json()
    assert isinstance(prog["completed"], int) and isinstance(prog["total"], int)

    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/qc", headers=headers)
    assert r.status_code == 200 and len(r.json()["items"]) == 2

    # generate-more adds one short, siblings preserved
    short_ids_before = {s["id"] for s in agg["shorts"]}
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/generate-more",
                    headers=headers, json={"n": 1, "exclude": []})
    assert r.status_code == 201, r.text
    assert len(r.json()["created"]) == 1
    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/content", headers=headers)
    after = {s["id"] for s in r.json()["items"]}
    assert short_ids_before < after

    # schedule writes entries; publish respects approval (SEMI -> skipped)
    start = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/schedule",
                    headers=headers, json={"start_date": start, "interval_days": 1.0})
    assert r.status_code == 200, r.text
    assert len(r.json()["schedule"]["created"]) >= 1

    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/publish",
                    headers=headers, json={})
    assert r.status_code == 202, r.text
    assert r.json()["enqueued"] == []
    assert all(s["reason"] in ("awaiting approval", "waiting on master")
               for s in r.json()["skipped"])

    # single-short regenerate preserves siblings (version bumps exactly once
    # from whatever it was: lineage_version is sibling-position at creation)
    one = next(iter(short_ids_before))
    from app.db import session_scope
    from app.models import ContentItem

    with session_scope() as _s:
        _before = _s.get(ContentItem, one).lineage_version
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{one}/regenerate", headers=headers)
    assert r.status_code == 200 and r.json()["lineage_version"] == _before + 1

    # single-platform variant regen
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{one}/platform-variants",
                    headers=headers, json={"platform": "tiktok"})
    assert r.status_code == 200, r.text
    assert r.json()["platform"] == "tiktok"

    # cancel + cross-workspace isolation
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/cancel", headers=headers)
    assert r.status_code == 200
    _, _, headers2 = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/aggregate", headers=headers2)
    assert r.status_code in (403, 404)
    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/progress", headers=headers2)
    assert r.status_code in (403, 404)
