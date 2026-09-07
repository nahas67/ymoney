"""Tests for the upgraded ffmpeg_avatar engine helpers (no ffmpeg, no network)."""
from __future__ import annotations

from pathlib import Path

from app.providers.video_engine.base import STATE_FAILED, RenderHandle
from app.providers.video_engine.ffmpeg_avatar import (
    _escape_subtitles_path,
    _pick_bgm,
    _proportional_segments,
    _srt_time,
    build_captions,
    pick_encoder,
)


def test_srt_time_format():
    assert _srt_time(0) == "00:00:00,000"
    assert _srt_time(3661.5) == "01:01:01,500"
    assert _srt_time(59.999) == "00:00:59,999"


def test_proportional_segments_chunk_phrases():
    script = ("This is the first sentence about AI tools. "
              "And here comes the second thought with more words in it! "
              "Short end.")
    segs = _proportional_segments(script, 30.0)
    assert segs, "segments produced"
    total_words = len(script.split())
    chunked_words = sum(len(s[2].split()) for s in segs)
    assert chunked_words == total_words
    # monotonic, full coverage of the timeline
    assert segs[0][0] == 0.0
    assert abs(segs[-1][1] - 30.0) < 0.5
    for a, b in zip(segs, segs[1:]):
        assert abs(a[1] - b[0]) < 1e-6


def test_build_captions_writes_valid_srt(tmp_path: Path):
    srt = tmp_path / "cap.srt"
    ok = build_captions("Hello world from the caption test. Second phrase here!",
                        10.0, srt)
    assert ok and srt.exists()
    text = srt.read_text(encoding="utf-8")
    assert "1\n" in text and "-->" in text
    assert text.count("-->") >= 2


def test_build_captions_empty_script_returns_false(tmp_path: Path):
    assert build_captions("   ", 10.0, tmp_path / "x.srt") is False


def test_escape_subtitles_path_windows():
    p = Path("C:/tmp/dir with space/cap.srt")
    esc = _escape_subtitles_path(p)
    assert "\\:" in esc  # colon escaped for the filter
    assert "'" not in esc or "\\'" in esc


def test_pick_encoder_returns_supported():
    enc = pick_encoder()
    assert enc in ("h264_qsv", "h264_nvenc", "h264_amf", "libx264")


def test_pick_bgm_none_when_dir_missing(tmp_path, monkeypatch):
    from app.providers.video_engine import ffmpeg_avatar as mod

    monkeypatch.setattr(mod, "BGM_DIR", tmp_path / "nope")
    assert _pick_bgm() is None


def test_restart_marks_inflight_manifest_as_failed(tmp_path, monkeypatch):
    """A task owned by a dead process must not remain falsely processing."""
    import json

    from app.providers.video_engine import ffmpeg_avatar as mod
    from app.providers.video_engine.ffmpeg_avatar import FFmpegAvatarEngine

    monkeypatch.setattr(mod, "WORK_DIR", tmp_path)
    task_id = "ffa-restart-test"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    (task_dir / "job.json").write_text(json.dumps({
        "task_id": task_id,
        "status": "processing",
        "progress": 45,
        "subject": "restart test",
        "pid": 999999,
    }), encoding="utf-8")

    engine = FFmpegAvatarEngine()
    status = engine.status(RenderHandle(task_id, engine.engine_name))
    assert status.state == STATE_FAILED
    assert "application restart" in status.error
