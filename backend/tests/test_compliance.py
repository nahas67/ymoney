"""E5 trust tests: spec preflight, reused-content risk, gate wiring, audit export."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.providers.compliance import preflight, reused_content_score


def _has_ffmpeg() -> bool:
    import shutil

    return bool(shutil.which("ffmpeg"))


def _make_video(dest: Path, w: int = 1080, h: int = 1920, seconds: int = 30) -> None:
    # Preflight only reads duration, dimensions, size, and container. A one-fps
    # solid-color source preserves those properties without rendering 6,000
    # full-resolution testsrc frames for the 200-second duration case.
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r=1:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-an", str(dest)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]


@pytest.mark.slow
@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_preflight_passes_good_vertical(tmp_path):
    src = tmp_path / "good.mp4"
    _make_video(src)
    for platform in ("youtube", "tiktok", "facebook", "instagram"):
        pf = preflight(str(src), platform)
        assert pf["passed"], (platform, pf["checks"])


@pytest.mark.slow
@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_preflight_blocks_overtime_and_landscape(tmp_path):
    long_v = tmp_path / "long.mp4"
    _make_video(long_v, seconds=200)
    assert preflight(str(long_v), "youtube")["passed"] is False  # 3-min Shorts cap
    assert preflight(str(long_v), "facebook")["passed"] is False  # 90s Reels cap
    wide = tmp_path / "wide.mp4"
    _make_video(wide, w=1920, h=1080)
    assert preflight(str(wide), "facebook")["passed"] is False  # 9:16 required
    assert preflight(str(wide), "youtube")["passed"] is True  # horizontal OK w/ warn


def test_preflight_unknown_platform_and_missing_file(tmp_path):
    assert preflight(str(tmp_path / "nope.mp4"), "youtube")["passed"] is False
    assert preflight(str(tmp_path / "nope.mp4"), "myspace")["passed"] is False


def test_reused_score_rates_repeats_high(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import ContentItem, Workspace

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        ws = Workspace(name="reuse-ws", slug="reuse-ws", niche="money")
        s.add(ws)
        s.flush()
        ws_id = ws.id
        s.add(ContentItem(workspace_id=ws_id, topic="how to save money fast", status="PUBLISHED"))
        s.flush()
    try:
        fresh = reused_content_score("how to save money fast", "short script here", ws_id, [])
        assert fresh["score"] >= 65 and fresh["level"] == "high"
        novel = reused_content_score(
            "quantum error correction breakthrough",
            "word " * 60, ws_id, ["chart", "lab", "qubit"])
        assert novel["score"] < 35 and novel["level"] == "low"
    finally:
        import shutil as _sh

        _sh.rmtree(tmp_path / "data", ignore_errors=True)


def test_compliance_agent_mock_path_skips_preflight(tmp_path, monkeypatch):
    from app.engine.agents import compliance as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-c",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.ComplianceOfficerAgent().review(
        ctx, video_path="mock:whatever", platforms=["youtube", "tiktok"],
        topic="brand new topic nobody covered", script="word " * 60,
        metadata_by_platform={"youtube": {"is_ai_generated": True},
                              "tiktok": {"is_ai_generated": True}},
        visual_keywords=["chart"])
    assert out["passed"] is True and out["require_human"] is False
    assert out["spec_failed"] == []


def test_compliance_skill_agent_registered():
    from app.engine.agents.registry import AGENTS
    from app.engine.capabilities import get_skill, get_tool

    assert get_skill("compliance_review").required_tools == ("review_compliance",)
    assert "compliance:review" in get_tool("review_compliance").permissions
    assert AGENTS["compliance"].meta.title == "Compliance Officer"
    assert len(AGENTS) == 23


def test_approve_enqueues_upload_and_audit_exports(tmp_path, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.main import create_app
    from app.models import ContentItem, Video, VideoVariant

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"cmp{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    media_dir = Path("data/videos") / ws_id
    media_dir.mkdir(parents=True, exist_ok=True)
    _make_video(media_dir / "final.mp4", seconds=10)

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="audit me", status="QC")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id, label="v1",
                               script="word " * 60, selected=True)
        s.add(variant)
        s.flush()
        video = Video(variant_id=variant.id, workspace_id=ws_id, engine="ffmpeg_avatar",
                      status="READY", file_path=str(media_dir / "final.mp4"))
        s.add(video)
        s.flush()
        cid = item.id

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/actions",
                    headers=headers, json={"action": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["queued_upload"] is True

    # double-approve stays idempotent (same key → no second job)
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/actions",
                    headers=headers, json={"action": "approve"})
    assert r.status_code == 200
    assert r.json()["queued_upload"] is False

    r = client.get(f"/api/v1/workspaces/{ws_id}/content/{cid}/audit", headers=headers)
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert set(bundle) >= {"content", "decision_why", "strategy", "research", "variants",
                           "videos", "quality_checks", "publishing_jobs",
                           "published_posts", "event_trail"}
    assert bundle["content"]["topic"] == "audit me"
    assert len(bundle["variants"]) == 1 and len(bundle["videos"]) == 1
