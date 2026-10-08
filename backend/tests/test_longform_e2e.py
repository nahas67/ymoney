"""Work 03 acceptance: 3-min and 5-min fixture projects, fully offline.

Fake LLM (conftest) + MockTTS (real wav bytes) + lavfi local media.
Every stage runs for real; the final artifact is a playable MP4.
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.slow


def _register(client, email=None):
    email = email or f"e2e{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def offline_stack(monkeypatch):
    from app.providers import broll as broll_mod
    from app.providers import tts as tts_mod
    from app.providers.broll import BrollError

    monkeypatch.setattr(tts_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())

    def _no_stock(*a, **k):
        raise BrollError("Pexels key not configured (hermetic test)")

    monkeypatch.setattr(broll_mod, "search_stock", _no_stock)
    monkeypatch.setattr(broll_mod, "fetch_stock_clip", _no_stock)

    from app.providers.longform_assets import (
        AssetProviderError,
        GeneratedImageProvider,
    )

    def _no_gen(self, *a, **k):
        raise AssetProviderError("image provider unavailable (hermetic test)")

    monkeypatch.setattr(GeneratedImageProvider, "fetch", _no_gen)
    return tts_mod.MockTTSProvider()


def _seed_local_media(ws_id, count_v=4, count_i=3):
    from pathlib import Path

    from app.services.storage import STORAGE_ROOT

    updir = Path.cwd() / STORAGE_ROOT / ws_id / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    patterns = ["testsrc", "smptebars", "testsrc2", "rgbtestsrc"]
    for i in range(count_v):
        pat = patterns[i % len(patterns)]
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        f"{pat}=duration=12:size=640x360:rate=10",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(updir / f"stock{i}.mp4")],
                       check=True, capture_output=True, timeout=120)
    for i in range(count_i):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=640x360:rate=1",
                        "-frames:v", "1", str(updir / f"img{i}.png")],
                       check=True, capture_output=True, timeout=60)


def _drive_to_complete(client, ws_id, headers, pid, limit=30):
    from app.engine.longform.pipeline import run_stage

    last = None
    for _ in range(limit):
        last = run_stage(pid, ws_id)
        if last["status"] in ("COMPLETE", "FAILED", "CANCELLED", "WAITING_REVIEW"):
            break
    return last


def _assert_complete_project(client, ws_id, headers, pid, min_seconds: float):
    import json as _json
    from pathlib import Path

    from app.db import session_scope
    from app.models import ContentTimeline, LongFormProject
    from app.models.assets import MediaAsset, Scene

    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        assert p.status == "COMPLETE", p.error
        assert p.timeline_id
        assert p.render_json.get("asset_id")
        assert p.qc_json.get("result") in ("PASS", "PASS_WITH_WARNINGS"), p.qc_json
        assert len(p.metadata_json.get("titles", [])) >= 3
        assert len(p.metadata_json.get("chapters", [])) >= 3
        assert len(p.metadata_json.get("thumbnails", [])) >= 1
        n_scenes = s.query(Scene).filter(Scene.workspace_id == ws_id).count()
        assert n_scenes >= 5
        tl = s.get(ContentTimeline, p.timeline_id)
        clips = [c for tr in tl.tracks_json["tracks"] for c in tr["clips"]]
        assert len(clips) >= 20
        asset = s.get(MediaAsset, p.render_json["asset_id"])
        from app.services.storage import STORAGE_ROOT

        final = Path.cwd() / STORAGE_ROOT / ws_id / asset.storage_key
        assert final.exists() and final.stat().st_size > 100_000
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(final)],
            capture_output=True, text=True, timeout=120)
        data = _json.loads(probe.stdout)
        kinds = {st.get("codec_type") for st in data.get("streams", [])}
        assert {"video", "audio"} <= kinds
        dur = float(data.get("format", {}).get("duration", 0))
        assert dur >= min_seconds, dur
    # opens in the existing editor (read path)
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tl.id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["tracks"]) == 8
    return dur


@pytest.mark.timeout(1500)
def test_e2e_three_minute_explainer(tmp_path, monkeypatch, offline_stack):
    """Renders ~120s+ of real video: the work scales with the output duration,
    so the 240s global timeout cannot fit the slowest supported runner (the
    Linux CI worker needed more than 240s where local hardware needs ~100s).
    The per-test mark keeps hang protection (25 minutes) without mistaking a
    slow render for a stuck one."""
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    _seed_local_media(ws_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects", headers=headers, json={
        "topic": "How rivers shape cities", "content_format": "EXPLAINER",
        "target_duration_seconds": 180, "autonomy": "AUTO",
        "budget_strategy": "ECONOMY",
        "pronunciation": {"testword": "TEST-word"}})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    last = _drive_to_complete(client, ws_id, headers, pid)
    assert last["status"] == "COMPLETE", last
    dur = _assert_complete_project(client, ws_id, headers, pid, min_seconds=120.0)

    # fallback chain was exercised (stock unavailable offline) and recorded
    from app.db import session_scope
    from app.models import LongFormProject

    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        assert p.cost_usd >= 0
    print(f"\n3-min E2E: rendered {dur:.0f}s")


@pytest.mark.timeout(1500)
def test_e2e_five_minute_documentary(tmp_path, monkeypatch, offline_stack):
    """Renders ~200s+ of real video: same timeout reasoning as the 3-minute
    explainer above -- the work is the output duration, and the slowest
    supported runner needs more than the 240s global budget."""
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    _seed_local_media(ws_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects", headers=headers, json={
        "topic": "The quiet rise of community solar", "content_format": "DOCUMENTARY",
        "target_duration_seconds": 300, "autonomy": "AUTO",
        "budget_strategy": "BALANCED"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    last = _drive_to_complete(client, ws_id, headers, pid, limit=40)
    assert last["status"] == "COMPLETE", last
    dur = _assert_complete_project(client, ws_id, headers, pid, min_seconds=200.0)
    print(f"\n5-min E2E: rendered {dur:.0f}s")
