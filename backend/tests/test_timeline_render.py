"""Edited-timeline render: trim/split/reorder/captions/audio land in a real MP4."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _run(*args: str) -> None:
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stderr or "")[-500:]


@pytest.fixture()
def ws_media(tmp_path, monkeypatch):
    import uuid

    from app.db import session_scope
    from app.models import Workspace
    from app.models.assets import MediaAsset
    from app.services.storage import STORAGE_ROOT

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        ws = Workspace(name="R WS", slug=f"r-{uuid.uuid4().hex[:8]}", niche="t")
        s.add(ws)
        s.flush()
        ws_id = ws.id
    root = Path.cwd() / STORAGE_ROOT / ws_id
    root.mkdir(parents=True, exist_ok=True)

    def make(name: str, *ffmpeg_args: str) -> str:
        _run("ffmpeg", "-y", *ffmpeg_args, str(root / name))
        return name

    keys = {
        "vidA": make("a.mp4", "-f", "lavfi", "-i",
                     "testsrc=duration=5:size=320x576:rate=10",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p"),
        "vidB": make("b.mp4", "-f", "lavfi", "-i",
                     "smptebars=duration=5:size=320x576:rate=10",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p"),
        "aud": make("n.m4a", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-c:a", "aac"),
        "img": make("p.png", "-f", "lavfi", "-i", "testsrc=size=320x200:rate=1",
                    "-frames:v", "1"),
    }
    ids = {}
    with session_scope() as s:
        for key, rel in keys.items():
            row = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                             storage_key=rel)
            s.add(row)
            s.flush()
            ids[key] = row.id
    return ws_id, ids


def _frame_mean(path: str, at: float) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(at), "-i", path,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=60)
    assert proc.returncode == 0
    data = proc.stdout
    return sum(data) / max(len(data), 1)


def test_edited_timeline_renders_real_mp4(ws_media):
    from app.db import session_scope
    from app.engine.timeline import add_clip, create_empty
    from app.providers.video_engine.timeline_render import render_timeline, resolve_font

    assert resolve_font() is not None, \
        "render font required (install fonts-dejavu or set YMONEY_FONT_FILE)"
    ws_id, ids = ws_media
    t = create_empty(ws_id, duration_seconds=6.0, aspect="9:16")
    # trim: first 3s of output come from seconds 1-4 of A
    add_clip(t, track="video", clip_id="a", name="A", start=0.0, duration=3.0,
             source={"asset_id": ids["vidA"]}, source_start=1.0)
    # reorder: B occupies the second half
    add_clip(t, track="video", clip_id="b", name="B", start=3.0, duration=3.0,
             source={"asset_id": ids["vidB"]})
    # cutaway overlay wins 2-4s
    add_clip(t, track="broll", clip_id="c", name="cut", start=2.0, duration=2.0,
             source={"asset_id": ids["img"]})
    add_clip(t, track="voice", clip_id="n", name="narr", start=0.0, duration=6.0,
             source={"asset_id": ids["aud"]}, volume=0.5)
    add_clip(t, track="caption", clip_id="cc", name="hello world", start=1.0, duration=2.0)
    add_clip(t, track="text", clip_id="tx", name="SALE", start=2.0, duration=3.0,
             text={"content": "SALE", "size": 72})

    with session_scope() as s:
        out = render_timeline(ws_id, s, t, out_name="edit.mp4")
    path = out["path"]
    assert Path(path).exists() and Path(path).stat().st_size > 10_000
    assert abs((out["duration_seconds"] or 0) - 6.0) < 0.6
    assert (out["width"], out["height"]) == (1080, 1920)
    assert out["warnings"] == []

    # audio stream really present
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, timeout=60)
    import json as _json

    codecs = [s.get("codec_type") for s in _json.loads(probe.stdout)["streams"]]
    assert "video" in codecs and "audio" in codecs

    # edits visible: A-region, cutaway-region, B-region all differ; none is black slug
    m1, m3, m5 = _frame_mean(path, 1.0), _frame_mean(path, 3.0), _frame_mean(path, 5.0)
    assert abs(m1 - m3) > 2.0 and abs(m3 - m5) > 2.0
    assert m1 > 5.0 and m5 > 5.0


def test_resolver_ignores_raw_paths_and_cross_workspace(ws_media):
    from app.db import session_scope
    from app.providers.video_engine.timeline_render import resolve_clip_source

    ws_id, ids = ws_media
    with session_scope() as s:
        assert resolve_clip_source(ws_id, s, {"file_path": "C:/Windows/win.ini"}) is None
        assert resolve_clip_source(ws_id, s, {"file_path": "/etc/passwd"}) is None
        assert resolve_clip_source("other-ws", s, {"asset_id": ids["vidA"]}) is None
        assert resolve_clip_source(ws_id, s, {"asset_id": ids["vidA"]}) is not None
        assert resolve_clip_source(ws_id, s, {"asset_id": "missing"}) is None
