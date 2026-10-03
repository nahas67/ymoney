"""Work 04 acceptance: master -> 5 diverse Shorts -> 4-platform variants ->
metadata -> publishing plan -> schedule -> simulated publish -> metrics.

Deterministic and offline: lavfi fixture media, heuristic rank_moments,
MockPublisher (MOCK_PUBLISHING=true in conftest). Renders real MP4s and
verifies them with ffprobe. Marked slow (renders).
"""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

# Slow: fixture encode + 2 real MP4 renders + full derive chain (~4 min).
pytestmark = [pytest.mark.slow, pytest.mark.timeout(590)]

PLATFORMS = ["youtube_shorts", "tiktok", "instagram_reels", "facebook_reels"]

CAPTIONS = [
    (0.0, "Want to know the secret to saving money every single month?"),
    (10.0, "First, pay yourself twenty percent before you pay any bills."),
    (20.0, "Second, automate the transfer on payday without thinking twice."),
    (30.0, "Why do most household budgets collapse within ninety days?"),
    (40.0, "Third, track every dollar with one simple daily money habit."),
    (50.0, "Fourth, kill lifestyle inflation before it eats your raise."),
    (60.0, "What if compounding did the heavy lifting for thirty years?"),
    (70.0, "Fifth, invest the difference in a boring index fund today."),
    (80.0, "Start tonight and watch your savings grow year after year."),
]


def _register(client, email=None):
    email = email or f"ce{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _master_media(root: Path) -> Path:
    out = root / "master.mp4"
    v = subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        "testsrc=duration=90:size=640x360:rate=15",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(out)],
                       capture_output=True, text=True, timeout=180)
    assert v.returncode == 0, v.stderr[-500:]
    return out


def _ffprobe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-300:]
    return json.loads(p.stdout)


def _frame_mean(path: Path, at: float) -> float:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(at), "-i", str(path),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, timeout=60)
    assert p.returncode == 0
    data = p.stdout
    return sum(data) / max(len(data), 1)


def _make_master_fixture(ws_id: str):
    """Master ContentItem + 90s media + timeline + captions + 3 scenes."""
    from app.db import session_scope
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, MediaAsset
    from app.models.assets import Scene
    from app.services.storage import STORAGE_ROOT

    root = Path.cwd() / STORAGE_ROOT / ws_id
    root.mkdir(parents=True, exist_ok=True)
    _master_media(root)
    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                           storage_key="master.mp4", mime_type="video/mp4",
                           duration_seconds=90.0, width=640, height=360)
        s.add(asset)
        s.flush()
        master = ContentItem(workspace_id=ws_id, topic="Save money every month",
                             status="PUBLISHED")
        s.add(master)
        s.flush()
        doc = create_empty(ws_id, duration_seconds=90.0, aspect="16:9")
        add_clip(doc, track="video", clip_id="mv", name="master shot",
                 start=0.0, duration=90.0, source={"asset_id": asset.id})
        for i, (st, text) in enumerate(CAPTIONS):
            add_clip(doc, track="caption", clip_id=f"mc{i}", name=text,
                     start=st, duration=10.0)
        s.add(ContentTimeline(workspace_id=ws_id, content_item_id=master.id,
                              name="main", fps=30.0, duration_seconds=90.0,
                              tracks_json=doc, version=1))
        for i, (st, e) in enumerate([(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)]):
            s.add(Scene(workspace_id=ws_id, content_item_id=master.id,
                        chapter_id=f"ch-{i}", index=i, title=f"part {i}",
                        script_segment=" ".join(t for _, t in CAPTIONS[i * 3:(i + 1) * 3]),
                        start_seconds=st, end_seconds=e))
        s.flush()
        return master.id, asset.id


def _run_derive(ws_id: str, camp_id: str) -> dict:
    from app.services import jobs as jobs_service

    handler = jobs_service._handlers["campaign.derive"]
    ctx = jobs_service.JobContext(job_id="e2e-derive", type="campaign.derive",
                                  workspace_id=ws_id, cycle_id=None,
                                  payload={"campaign_id": camp_id}, attempt=1,
                                  cancelled=lambda: False)
    return handler(ctx)


def test_campaign_e2e_master_to_attributed_metrics(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)
    master_id, _ = _make_master_fixture(ws_id)

    # --- campaign + derive through the real durable chain ---
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/from-master", headers=headers,
                    json={"master_content_id": master_id, "goal": "subs",
                          "target_platforms": PLATFORMS, "desired_shorts": 5})
    assert r.status_code == 201, r.text
    camp_id = r.json()["id"]

    out = _run_derive(ws_id, camp_id)
    assert out["shorts"] == 5, out
    assert out["variants"] == 20, out

    # --- lineage + diversity ---
    from app.db import session_scope
    from app.models import ContentItem
    from app.models.campaign import PlatformVariant

    with session_scope() as s:
        shorts = s.scalars(
            select(ContentItem).where(
                ContentItem.workspace_id == ws_id,
                ContentItem.campaign_id == camp_id,
                ContentItem.derivation_type == "short")).all()
        assert len(shorts) == 5
        for sh in shorts:
            assert sh.parent_content_id == master_id
            assert sh.root_content_id == master_id
            assert sh.campaign_id == camp_id
        variants = s.scalars(select(PlatformVariant).where(
            PlatformVariant.workspace_id == ws_id,
            PlatformVariant.campaign_id == camp_id)).all()
        assert len(variants) == 20
        by_short: dict[str, set[str]] = {}
        for v in variants:
            assert v.status in ("READY", "DRAFT")
            by_short.setdefault(v.short_content_id, set()).add(v.platform)
        assert all(p == set(PLATFORMS) for p in by_short.values())

    from app.engine.campaign.diversity import transcript_overlap
    from app.models.assets import Scene

    with session_scope() as s:
        hook_texts, chapters = [], set()
        for sh in shorts:
            row = s.query(Scene).filter(Scene.content_item_id == sh.id).all()
            assert row, f"short {sh.id} has no scene"
            hook_texts.append(sh.topic)
            for sc in row:
                if sc.chapter_id:
                    chapters.add(sc.chapter_id)
        assert len(chapters) >= 2, chapters
        for i in range(len(hook_texts)):
            for j in range(i + 1, len(hook_texts)):
                assert transcript_overlap(hook_texts[i], hook_texts[j]) < 0.8

    # --- metadata differs per platform ---
    with session_scope() as s:
        first = shorts[0].id
        titles = {v.platform: (v.metadata_json or {}).get("title", "")
                  for v in variants if v.short_content_id == first}
        assert len(set(titles.values())) == 4, titles
        for v in variants:
            md = v.metadata_json or {}
            assert md.get("hashtags"), v.id
            assert md.get("cta_kind")

    # --- render two shorts to real MP4s ---
    from app.models import ContentTimeline
    from app.providers.video_engine.timeline_render import render_timeline

    rendered: dict[str, Path] = {}
    with session_scope() as s:
        docs = []
        for sh in shorts[:2]:
            tl = s.query(ContentTimeline).filter(
                ContentTimeline.content_item_id == sh.id,
                ContentTimeline.workspace_id == ws_id,
            ).order_by(ContentTimeline.version.desc()).first()
            assert tl is not None
            docs.append((sh.id, dict(tl.tracks_json or {})))
    for sid, doc in docs:
        with session_scope() as s:
            res = render_timeline(ws_id, s, doc, out_name=f"short-{sid[:8]}.mp4")
        path = Path(res["path"])
        assert path.exists() and path.stat().st_size > 10_000
        meta = _ffprobe(path)
        kinds = {st.get("codec_type") for st in meta.get("streams", [])}
        assert {"video", "audio"} <= kinds
        dur = float(meta.get("format", {}).get("duration", 0))
        assert 15.0 <= dur <= 70.0, dur
        vstream = next(st for st in meta["streams"] if st.get("codec_type") == "video")
        assert (vstream.get("width"), vstream.get("height")) == (1080, 1920)
        assert vstream.get("codec_name") == "h264"
        astream = next(st for st in meta["streams"] if st.get("codec_type") == "audio")
        assert astream.get("codec_name") == "aac"
        assert _frame_mean(path, min(2.0, dur / 2)) > 5.0  # not a black slug
        rendered[sid] = path

    # --- publishing plan: master-first deps + pacing ---
    from app.models.campaign import PublishingPlan

    with session_scope() as s:
        plan = s.scalar(select(PublishingPlan).where(
            PublishingPlan.campaign_id == camp_id))
        assert plan is not None and plan.status == "READY"
        items = list(plan.items_json or [])
        assert len(items) == 21  # master slot + 20 variants
        assert items[0].get("is_master") is True
        shorts_items = [i for i in items if not i.get("is_master")]
        assert all(i.get("depends_on") for i in shorts_items)
        dates = sorted(i["planned_at"] for i in shorts_items)
        assert dates[-1] > dates[0]  # paced, never all-at-once

    # --- schedule (idempotent; derive already scheduled, so this reuses) ---
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/schedule",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    sched = r.json()["schedule"]
    assert sched["created"] == []
    assert len(sched["reused"]) == 21
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/schedule",
                    headers=headers, json={})
    assert r.json()["schedule"]["created"] == []
    assert len(r.json()["schedule"]["reused"]) == 21

    # --- publish gating: shorts wait on master ---
    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/publish",
                    headers=headers, json={"only_approved": False})
    assert r.status_code == 202, r.text
    assert r.json()["enqueued"] == []
    assert all(s["reason"] == "waiting on master" for s in r.json()["skipped"][:5])

    # --- simulated publish via MockPublisher through the real job handler ---
    from app.models import PublishedPost
    from app.providers.publishers import factory as publisher_factory
    from app.providers.publishers.mock import MockPublisher
    from app.services import jobs as jobs_service

    monkeypatch.setattr(publisher_factory, "get_publisher",
                        lambda platform, **kw: MockPublisher())
    pub_handler = jobs_service._handlers["campaign.publish"]

    def _publish(variant_id, platform, short_id, path):
        ctx = jobs_service.JobContext(
            job_id=f"e2e-pub-{variant_id[:8]}-{platform}", type="campaign.publish",
            workspace_id=ws_id, cycle_id=None,
            payload={"campaign_id": camp_id, "variant_id": variant_id,
                     "platform": platform, "short_content_id": short_id,
                     "platform_variant_id": variant_id,
                     "video_path": str(path), "metadata": {"title": "t"}},
            attempt=1, cancelled=lambda: False)
        return pub_handler(ctx)

    with session_scope() as s:
        master_post = PublishedPost(
            workspace_id=ws_id, content_item_id=master_id, video_id=master_id,
            platform="youtube_longform", remote_post_id="yt-master-1",
            remote_url="https://youtube.test/watch?v=master1", title="master")
        s.add(master_post)
        s.flush()
    with session_scope() as s:
        tiktok_variants = [v for v in variants if v.platform == "tiktok"][:2]
        yt_variants = [v for v in variants if v.platform == "youtube_shorts"][:2]
    results = []
    for v in tiktok_variants + yt_variants:
        sid = next(sh.id for sh in shorts if sh.id == v.short_content_id)
        results.append(_publish(v.id, v.platform, sid, rendered.get(sid, Path("x.mp4"))))
    assert all(r.get("published") for r in results), results
    # idempotent re-run: same remote rows reused, no duplicates
    for v in tiktok_variants[:1]:
        sid = next(sh.id for sh in shorts if sh.id == v.short_content_id)
        _publish(v.id, v.platform, sid, rendered.get(sid, Path("x.mp4")))
    with session_scope() as s:
        posts = s.scalars(select(PublishedPost).where(
            PublishedPost.workspace_id == ws_id,
            PublishedPost.campaign_id == camp_id)).all()
        assert len(posts) == 4
        for p in posts:
            assert p.platform_variant_id and p.remote_post_id.startswith("mock-")

    # W11.5 D-F1: the SUCCESS path must also move the variant to PUBLISHED.
    # It never asserted this, which is how a real defect hid: `_mark_variant`
    # opened its OWN session while the publish transaction already held
    # SQLite's write lock, so the nested write timed out on busy_timeout and
    # was swallowed by a bare `except` -- leaving status at READY while the
    # publish reported success (and burning 5s per publish). The FAILED path
    # was asserted below and passed, because nothing is flushed there yet.
    with session_scope() as s:
        published_ids = {p.platform_variant_id for p in posts}
        now_published = s.scalars(select(PlatformVariant).where(
            PlatformVariant.id.in_(published_ids))).all()
        assert len(now_published) == 4, [v.id for v in now_published]
        for v in now_published:
            assert v.status == "PUBLISHED", f"{v.id} left at {v.status}"
            assert v.published_post_id, f"{v.id} has no published_post_id"

    # --- failed publication isolates siblings ---
    def _boom(platform, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(publisher_factory, "get_publisher", _boom)
    with session_scope() as s:
        victim = [v for v in variants if v.platform == "instagram_reels"][0]
        # W11.5 D-F1: read the sibling snapshot from the DB, not from the
        # `variants` list loaded before the successful publishes. Now that the
        # success path really writes PUBLISHED (it used to silently fail), the
        # stale in-memory rows said READY while the table said PUBLISHED, and
        # this sibling-isolation assertion failed for the wrong reason.
        before = sorted(
            (v.id, v.status)
            for v in s.scalars(select(PlatformVariant).where(
                PlatformVariant.workspace_id == ws_id,
                PlatformVariant.campaign_id == camp_id)).all()
            if v.id != victim.id
        )
    ctx = jobs_service.JobContext(
        job_id="e2e-pub-fail", type="campaign.publish", workspace_id=ws_id,
        cycle_id=None, payload={"campaign_id": camp_id, "variant_id": victim.id,
                                "platform": "instagram_reels",
                                "short_content_id": victim.short_content_id,
                                "platform_variant_id": victim.id,
                                "video_path": "", "metadata": {}},
        attempt=1, cancelled=lambda: False)
    with pytest.raises(RuntimeError):
        pub_handler(ctx)
    with session_scope() as s:
        after = sorted((v.id, v.status) for v in s.scalars(select(PlatformVariant).where(
            PlatformVariant.workspace_id == ws_id,
            PlatformVariant.campaign_id == camp_id)).all() if v.id != victim.id)
        assert before == after
        failed = s.get(PlatformVariant, victim.id)
        assert failed.status == "FAILED"

    # --- metrics + attribution up the whole chain ---
    from app.engine.campaign.analytics import (
        chapter_performance,
        compare_platforms,
        resolve_attribution,
        rollup_campaign,
    )
    from app.models import PostMetric

    with session_scope() as s:
        posts = s.scalars(select(PublishedPost).where(
            PublishedPost.workspace_id == ws_id,
            PublishedPost.campaign_id == camp_id)).all()
        views = {"tiktok": 1000, "youtube": 2500}
        for p in posts:
            v = views.get(p.platform, 100)
            s.add(PostMetric(post_id=p.id, views=v, likes=v // 10,
                             comments=v // 50, shares=v // 20, saves=v // 40,
                             watch_time_seconds=float(v * 12),
                             avg_view_duration_seconds=12.0,
                             completion_rate=0.6, captured_at=datetime.now(UTC)))
        s.flush()
        camp = rollup_campaign(s, ws_id, camp_id)
        assert camp["totals"]["views"] == 2 * (1000 + 2500)
        assert camp["post_count"] == 4
        comp = compare_platforms(s, ws_id, camp_id)
        assert comp["youtube"]["views"] > comp["tiktok"]["views"]
        chain = resolve_attribution(s, ws_id, posts[0].id)
        assert chain["campaign_id"] == camp_id
        assert chain["master_id"] == master_id
        assert chain["short_id"] == posts[0].content_item_id
        assert chain["variant_id"] == posts[0].platform_variant_id
        assert chain["post_id"] == posts[0].id
        chap = chapter_performance(s, ws_id, camp_id)
        assert isinstance(chap, list) and chap

    # --- regenerate one short preserves siblings; generate-more excludes ---
    with session_scope() as s:
        target = shorts[0].id
        sibling_ids = sorted(sh.id for sh in shorts[1:])
        sibling_versions = {sh.id: sh.lineage_version for sh in shorts[1:]}
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{target}/regenerate",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    with session_scope() as s:
        sibs = s.scalars(select(ContentItem).where(
            ContentItem.id.in_(sibling_ids))).all()
        assert sorted(x.id for x in sibs) == sibling_ids
        assert all(x.lineage_version == sibling_versions[x.id] for x in sibs)

    r = client.post(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/generate-more",
                    headers=headers, json={"n": 2})
    assert r.status_code == 201, r.text
    with session_scope() as s:
        all_shorts = s.scalars(select(ContentItem).where(
            ContentItem.workspace_id == ws_id, ContentItem.campaign_id == camp_id,
            ContentItem.derivation_type == "short")).all()
        assert len(all_shorts) == 7
        topics = [x.topic for x in all_shorts]
        assert len(set(topics)) == len(topics)

    r2 = client.post("/api/v1/auth/register",
                     json={"email": f"ce2{uuid.uuid4().hex[:8]}@test.local", "password": "supersecret123"})
    headers2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    r = client.get(f"/api/v1/workspaces/{ws_id}/campaigns/{camp_id}/aggregate",
                   headers=headers2)
    assert r.status_code in (403, 404)
