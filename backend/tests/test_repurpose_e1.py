"""E1 link-to-shorts tests: ranking (offline deterministic), presets, cutting.

ffmpeg-dependent tests skip when ffmpeg is missing (CI); pure-python ranking
and preset tests always run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.providers import clips as clips_mod
from app.providers.clips import (
    CAPTION_PRESETS,
    ClipRepurposer,
    caption_style,
)


def _has_ffmpeg() -> bool:
    return clips_mod.ffmpeg_available()


def _make_source(tmp_path: Path, seconds: int = 30) -> Path:
    src = tmp_path / "source.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=640x480:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]
    return src


# -- pure python: always run -------------------------------------------------


def test_caption_presets_cover_minimal_pop_karaoke():
    assert set(CAPTION_PRESETS) == {"minimal", "pop", "karaoke"}
    for name, style in CAPTION_PRESETS.items():
        assert "FontSize=" in style, name
        assert "Alignment=2" in style, name
    assert caption_style("POP") == CAPTION_PRESETS["pop"]
    assert caption_style("unknown-preset") == CAPTION_PRESETS["minimal"]


def test_trim_to_best_breaks_ties_by_start():
    from app.providers.clips import ViralMoment, trim_to_best

    moments = [
        ViralMoment(start=40.0, end=60.0, score=80.0, hook="b"),
        ViralMoment(start=0.0, end=20.0, score=80.0, hook="a"),
        ViralMoment(start=20.0, end=40.0, score=90.0, hook="best"),
    ]
    top = trim_to_best(moments, 2)
    assert [m.hook for m in top] == ["best", "a"]
    assert trim_to_best(moments, 0)[0].hook == "best"  # bad input degrades to 1


def test_rank_moments_tie_order_deterministic():
    rep = ClipRepurposer()
    base = [
        {"start": 0.0, "end": 20.0, "text": "markets rally as investors cheer gains today."},
        {"start": 20.0, "end": 40.0, "text": "markets rally as investors cheer gains today."},
    ]
    assert [(m.start, m.score) for m in rep.rank_moments(base, 5)] == [
        (m.start, m.score) for m in rep.rank_moments(list(reversed(base)), 5)
    ]


def test_moment_count_target_clamps_and_env_overrides(monkeypatch):
    from app.providers.clips import moment_count_target

    assert moment_count_target(5) == 5
    assert moment_count_target(0) == 1
    assert moment_count_target(100) == 12
    assert moment_count_target("nope") == 5
    monkeypatch.setenv("LINKMINER_MAX_MOMENTS", "2")
    assert moment_count_target(5) == 2
    monkeypatch.setenv("LINKMINER_MIN_MOMENTS", "4")
    monkeypatch.delenv("LINKMINER_MAX_MOMENTS")
    assert moment_count_target(1) == 4
    monkeypatch.setenv("LINKMINER_MAX_MOMENTS", "bogus")
    assert moment_count_target(5) == 5


def test_heuristic_rank_orders_hooks_first():
    from app.providers.clips import _heuristic_rank

    windows = [
        {"start": 0.0, "end": 20.0, "text": "welcome back to the show today we talk about markets"},
        {"start": 20.0, "end": 40.0, "text": "What if nobody told you the secret truth about money? 3 mistakes!"},
    ]
    ranked = _heuristic_rank(windows)
    assert ranked[0].start == 20.0
    assert ranked[0].score > ranked[1].score
    assert ranked[0].hook
    assert ranked[0].reason


def test_candidate_windows_merge_and_clamp():
    from app.providers.clips import _candidate_windows

    segs = [{"start": float(i * 5), "end": float(i * 5 + 5), "text": f"point number {i}."} for i in range(12)]
    out = _candidate_windows(segs)
    assert out
    assert all(w["end"] - w["start"] >= 5 for w in out)
    assert all(w["end"] - w["start"] <= 60.0 for w in out)


def test_rank_moments_empty_without_transcript():
    rep = ClipRepurposer()
    assert rep.rank_moments([], 3) == []


def test_rank_moments_offline_deterministic():
    rep = ClipRepurposer()
    segs = [
        {"start": 0.0, "end": 20.0, "text": "hello and welcome to this episode about savings"},
        {"start": 20.0, "end": 45.0, "text": "Stop making this mistake! Nobody talks about the secret fee?"},
    ]
    first = rep.rank_moments(segs, 5)
    second = rep.rank_moments(segs, 5)
    assert [(m.start, m.score) for m in first] == [(m.start, m.score) for m in second]
    assert first[0].score >= 50


def test_status_reports_new_capabilities():
    status = ClipRepurposer().status()
    assert set(status) >= {"yt_dlp", "ffmpeg", "whisper", "scene_detect",
                           "face_track", "caption_presets"}
    assert "minimal" in status["caption_presets"]


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_probe_local_file_ok_and_missing(tmp_path):
    import subprocess as _sp

    from app.providers.clips import probe_source_quality

    src = tmp_path / "s.mp4"
    proc = _sp.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x480:rate=30:duration=5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:200]
    good = probe_source_quality(str(src))
    assert good["probe"] == "ok" and good["mode"] == "local"
    assert good["height"] == 480

    missing = probe_source_quality(str(tmp_path / "nope.mp4"))
    assert missing["probe"] == "unknown" and missing["warning"]

    assert probe_source_quality("")["probe"] == "unknown"


def test_probe_without_ytdlp_degrades(monkeypatch):
    from app.providers import clips as clips_mod
    from app.providers.clips import probe_source_quality

    monkeypatch.setattr(clips_mod, "yt_dlp_available", lambda: False)
    out = probe_source_quality("https://example.com/v.mp4")
    assert out["probe"] == "unknown" and "yt-dlp" in out["warning"]


def test_probe_parses_remote_json(monkeypatch):
    import json as _json
    import subprocess as _sp

    from app.providers import clips as clips_mod
    from app.providers.clips import probe_source_quality

    def _fake_run(cmd, **kw):
        assert cmd[0] == "yt-dlp" and "--dump-single-json" in cmd
        payload = {"duration": 600, "formats": [
            {"height": 480, "vcodec": "avc1"},
            {"height": 1080, "vcodec": "avc1"},
            {"height": 0, "vcodec": "none"},
        ]}
        return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload).encode(), stderr=b"")

    monkeypatch.setattr(clips_mod, "yt_dlp_available", lambda: True)
    monkeypatch.setattr(clips_mod.subprocess, "run", _fake_run)
    good = probe_source_quality("https://example.com/v.mp4")
    assert (good["max_height"], good["duration"], good["probe"]) == (1080, 600, "ok")
    assert not good["warning"]

    def _fake_low(cmd, **kw):
        payload = {"duration": 60, "formats": [{"height": 480, "vcodec": "avc1"}]}
        return _sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload).encode(), stderr=b"")

    monkeypatch.setattr(clips_mod.subprocess, "run", _fake_low)
    low = probe_source_quality("https://example.com/v.mp4")
    assert low["max_height"] == 480 and "480" in low["warning"]


def test_probe_endpoint_and_mine_wiring(tmp_path, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.engine.agents import repurpose as rep_mod
    from app.main import create_app
    from app.providers.clips import SourceInfo
    from app.services.jobs import JobContext

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"prb{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ws_id = r.json()["workspace"]["id"]

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/repurpose/probe",
                    headers=headers, json={"url": str(tmp_path / "nope.mp4")})
    assert r.status_code == 200, r.text
    assert r.json()["probe"] == "unknown"

    f = tmp_path / "s.mp4"
    f.write_bytes(b"\x00" * 1024)

    class _Rep:
        def acquire(self, source, ws):
            return SourceInfo(title="t", duration=10.0, width=640, height=480, local_path=f)
        def transcribe_segments(self, info):
            return []
        def detect_scenes(self, info):
            return []
        def rank_moments(self, base, n, ws):
            return []

    monkeypatch.setattr(rep_mod, "get_repurposer", lambda: _Rep())
    ctx = JobContext(job_id="j1", type="test", workspace_id=ws_id,
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = rep_mod.LinkMinerAgent().mine(ctx, source=str(f))
    assert out["source_quality"]["probe"] == "ok"
    assert out["source_quality"]["mode"] == "local"


def test_vf_chain_center_and_face_modes():
    rep = ClipRepurposer()
    center = rep._vf_chain(True, None)
    assert "scale=1080:1920" in center and "crop=" in center
    tracked = rep._vf_chain(True, 0.7)
    assert "scale=1080:1920" in tracked
    assert rep._vf_chain(False, None) == ""


# -- ffmpeg-gated ---------------------------------------------------------------


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_cut_segments_vertical_with_captions(tmp_path):
    src = _make_source(tmp_path)
    rep = ClipRepurposer(work_root=tmp_path / "work")
    info = rep.acquire(str(src), "ws-test")
    assert info.duration and info.duration >= 25
    clips = rep.cut_segments(
        info, "ws-test", segments=[(0.0, 8.0), (10.0, 18.0)],
        caption_preset="pop",
        captions={1: "hello world", 2: "second clip here"},
    )
    assert len(clips) == 2
    assert clips[0].width == 1080 and clips[0].height == 1920
    assert clips[0].preset == "pop"


@pytest.mark.slow
@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_agent_mine_and_assemble_on_local_file(tmp_path, monkeypatch):
    from app.services.jobs import JobContext

    src = _make_source(tmp_path, seconds=40)
    monkeypatch.chdir(tmp_path)
    from app.engine.agents.repurpose import LinkMinerAgent, RepurposeEditorAgent

    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-e1",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    mined = LinkMinerAgent().mine(ctx, source=str(src), max_moments=2)
    assert "moments" in mined
    assert mined["source_title"]
    out = RepurposeEditorAgent().assemble(
        ctx, source=str(src), moments=mined["moments"],
        max_clips=2, caption_preset="minimal",
    )
    assert len(out["clips"]) <= 2
    for c in out["clips"]:
        assert Path(c["path"]).exists()
        assert {"score", "hook", "reason"} <= set(c)
