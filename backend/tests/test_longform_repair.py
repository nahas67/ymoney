"""Work 03: bounded repair loop + chunked-render resume."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path


def _ws(session):
    from app.models import Workspace

    ws = Workspace(name="W", slug=f"w-{uuid.uuid4().hex[:8]}", niche="t")
    session.add(ws)
    session.flush()
    return ws.id


def _lavfi(path: Path, *args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args, str(path)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stderr or "")[-300:]


def test_repair_replaces_missing_visuals_and_fails_voice_loudly(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.longform.repair import repair_timeline_assets
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentTimeline, LongFormProject
    from app.models.assets import MediaAsset

    monkeypatch.chdir(tmp_path)
    from app.services.storage import STORAGE_ROOT

    with session_scope() as s:
        ws_id = _ws(s)
        root = Path.cwd() / STORAGE_ROOT / ws_id
        root.mkdir(parents=True, exist_ok=True)
        _lavfi(root / "ok.mp4", "-f", "lavfi", "-i",
               "testsrc=duration=2:size=320x240:rate=10",
               "-c:v", "libx264", "-pix_fmt", "yuv420p")
        _lavfi(root / "gone.mp4", "-f", "lavfi", "-i",
               "testsrc=duration=2:size=320x240:rate=10",
               "-c:v", "libx264", "-pix_fmt", "yuv420p")
        _lavfi(root / "narr.wav", "-f", "lavfi", "-i", "sine=duration=2",
               "-c:a", "pcm_s16le")
        ok = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                        storage_key="ok.mp4")
        gone = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                          storage_key="gone.mp4")
        voice = MediaAsset(workspace_id=ws_id, type="voice", origin="generated",
                           storage_key="narr.wav")
        s.add_all([ok, gone, voice])
        s.flush()
        doc = create_empty(ws_id, duration_seconds=4.0)
        add_clip(doc, track="video", clip_id="a", name="A", start=0.0, duration=2.0,
                 source={"asset_id": ok.id})
        add_clip(doc, track="broll", clip_id="b", name="B", start=2.0, duration=2.0,
                 source={"asset_id": gone.id})
        add_clip(doc, track="voice", clip_id="n", name="N", start=0.0, duration=2.0,
                 source={"asset_id": voice.id})
        tl = ContentTimeline(workspace_id=ws_id, name="t", duration_seconds=4.0,
                             tracks_json=doc)
        s.add(tl)
        s.flush()
        p = LongFormProject(workspace_id=ws_id, topic="repair test",
                            timeline_id=tl.id)
        s.add(p)
        s.flush()
        pid, tid = p.id, tl.id
    # delete one visual + the voice file from disk
    (root / "gone.mp4").unlink()
    (root / "narr.wav").unlink()
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        report = repair_timeline_assets(s, p)
        s.commit()
        assert report["repaired"] == ["b"]
        assert report["unrepairable"] == ["n"]  # voice: loud, never silent filler
        assert any("graphic" in d for d in report["details"])
    with session_scope() as s:
        tl = s.get(ContentTimeline, tid)
        assert tl.version == 2  # repair is versioned, not silent
        broll = next(t for t in tl.tracks_json["tracks"] if t["kind"] == "broll")
        assert broll["clips"][0]["source"]["asset_id"] != gone.id


def test_chunked_render_resumes_from_cache(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.longform import stages_finish as finish
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentTimeline, LongFormChapter, LongFormProject
    from app.models.assets import MediaAsset

    monkeypatch.chdir(tmp_path)
    from app.services.storage import STORAGE_ROOT

    with session_scope() as s:
        ws_id = _ws(s)
        root = Path.cwd() / STORAGE_ROOT / ws_id
        root.mkdir(parents=True, exist_ok=True)
        _lavfi(root / "v.mp4", "-f", "lavfi", "-i",
               "testsrc=duration=10:size=320x240:rate=10",
               "-c:v", "libx264", "-pix_fmt", "yuv420p")
        _lavfi(root / "a.wav", "-f", "lavfi", "-i", "sine=duration=10",
               "-c:a", "pcm_s16le")
        v = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                       storage_key="v.mp4")
        a = MediaAsset(workspace_id=ws_id, type="voice", origin="generated",
                       storage_key="a.wav")
        s.add_all([v, a])
        s.flush()
        p = LongFormProject(workspace_id=ws_id, topic="resume test")
        s.add(p)
        s.flush()
        ch = LongFormChapter(project_id=p.id, index=0, title="Only",
                             script_json={"measured_start": 0.0, "measured_end": 8.0})
        s.add(ch)
        s.flush()
        doc = create_empty(ws_id, duration_seconds=8.0)
        add_clip(doc, track="video", clip_id="v", name="V", start=0.0, duration=8.0,
                 source={"asset_id": v.id})
        add_clip(doc, track="voice", clip_id="n", name="N", start=0.0, duration=8.0,
                 source={"asset_id": a.id})
        tl = ContentTimeline(workspace_id=ws_id, name="t", duration_seconds=8.0,
                             tracks_json=doc)
        s.add(tl)
        s.flush()
        p.timeline_id = tl.id
        pid = p.id
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        first = finish.stage_render(s, p, None)
        s.commit()
        assert "1 chunks assembled" in first
        from app.services.storage import STORAGE_ROOT as _SR

        workdir = _SR / ws_id / "longform" / pid / "chunks"
        files = sorted(workdir.glob("chunk_*.mp4"))
        assert len(files) == 1
        mtime = files[0].stat().st_mtime
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        second = finish.stage_render(s, p, None)
        s.commit()
        assert "1 cached" in second  # re-encode skipped, concat reused
        assert sorted(workdir.glob("chunk_*.mp4"))[0].stat().st_mtime == mtime
