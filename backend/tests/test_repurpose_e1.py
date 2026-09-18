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
    ViralMoment,
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
