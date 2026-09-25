"""Work 02: scene sync — broll plans, repurpose segments, captions, resync."""
from __future__ import annotations

import uuid


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentTimeline, Workspace

    with session_scope() as s:
        ws = Workspace(name="S WS", slug=f"s-{uuid.uuid4().hex[:8]}", niche="t")
        s.add(ws)
        s.flush()
        ws_id = ws.id
        doc = create_empty(ws_id, duration_seconds=12.0)
        add_clip(doc, track="video", clip_id="v", name="V", start=0.0, duration=12.0)
        add_clip(doc, track="voice", clip_id="n", name="N", start=0.0, duration=12.0)
        row = ContentTimeline(workspace_id=ws_id, name="ep", duration_seconds=12.0,
                              tracks_json=doc)
        s.add(row)
        s.flush()
        tid = row.id
    return ws_id, tid


def test_broll_plan_sync_real_plan(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine import scene_sync as sync_mod
    from app.models.assets import Scene
    from app.providers.broll import plan_scenes

    ws_id, tid = _setup(tmp_path, monkeypatch)
    plan = plan_scenes("money habits", ["budget"], 3, ws_id)  # real fn, template lane
    assert len(plan) == 3
    with session_scope() as s:
        rows = sync_mod.sync_from_broll_plan(
            s, workspace_id=ws_id, timeline_id=tid, content_item_id=None,
            plan=plan, total_duration=12.0)
        s.commit()
        assert [r.index for r in rows] == [0, 1, 2]
    with session_scope() as s:
        scenes = s.query(Scene).filter(Scene.timeline_id == tid).order_by(Scene.index).all()
        assert [(sc.start_seconds, sc.end_seconds) for sc in scenes] == [
            (0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
        assert "money habits" in scenes[0].visual_intent


def test_repurpose_segments_and_caption_attach(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine import scene_sync as sync_mod
    from app.models.assets import Scene

    ws_id, tid = _setup(tmp_path, monkeypatch)
    with session_scope() as s:
        rows = sync_mod.sync_from_segments(
            s, workspace_id=ws_id, timeline_id=tid, content_item_id=None,
            segments=[(0.0, 5.0), (5.0, 12.0)], source="repurpose")
        s.commit()
        assert len(rows) == 2
        touched = sync_mod.attach_captions_to_scenes(
            s, workspace_id=ws_id, timeline_id=tid,
            caption_clips=[{"name": "save more", "start": 1.0, "duration": 2.0},
                           {"name": "invest", "start": 6.0, "duration": 2.0}])
        s.commit()
        assert touched == 2
    with session_scope() as s:
        scenes = s.query(Scene).filter(Scene.timeline_id == tid).order_by(Scene.index).all()
        assert [c["text"] for c in scenes[0].captions_json] == ["save more"]
        assert [c["text"] for c in scenes[1].captions_json] == ["invest"]
        mapping = sync_mod.scene_clip_map(s, workspace_id=ws_id, timeline_id=tid,
                                           tracks_doc=s.get(
                                               __import__("app.models", fromlist=["ContentTimeline"]).ContentTimeline, tid).tracks_json)
        assert mapping[0]["clips"]["video"] == ["v"]


def test_resync_after_trim(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine import scene_sync as sync_mod
    from app.engine.timeline_ops import apply_operations
    from app.models import ContentTimeline
    from app.models.assets import Scene

    ws_id, tid = _setup(tmp_path, monkeypatch)
    with session_scope() as s:
        sync_mod.sync_from_segments(
            s, workspace_id=ws_id, timeline_id=tid, content_item_id=None,
            segments=[(0.0, 12.0)], source="repurpose")
        s.commit()
    with session_scope() as s:
        row = s.get(ContentTimeline, tid)
        doc = dict(row.tracks_json)
        new_doc = apply_operations(doc, [
            {"type": "trim_item", "track": "video", "clip_id": "v",
             "edge": "start", "start": 2.0},
            {"type": "trim_item", "track": "voice", "clip_id": "n",
             "edge": "start", "start": 2.0},
        ])
        row.tracks_json = new_doc
        updated = sync_mod.resync_scene_ranges(
            session=s, workspace_id=ws_id, timeline_id=tid, tracks_doc=new_doc)
        s.commit()
        assert updated == 1
    with session_scope() as s:
        sc = s.query(Scene).filter(Scene.timeline_id == tid).one()
        assert (sc.start_seconds, sc.end_seconds) == (2.0, 12.0)


def test_broll_agent_sync_adapter(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.agents import broll as agent_mod
    from app.models.assets import Scene
    from app.services.jobs import JobContext

    ws_id, tid = _setup(tmp_path, monkeypatch)
    ctx = JobContext(job_id="j1", type="test", workspace_id=ws_id,
                     cycle_id=None, payload={"timeline_id": tid},
                     attempt=1, cancelled=lambda: False)
    out = agent_mod.BrollResearcherAgent().research(
        ctx, topic="money habits", keywords=["budget"], n_scenes=2)
    assert out["synced_scene_ids"] and len(out["synced_scene_ids"]) == 2
    with session_scope() as s:
        assert s.query(Scene).filter(Scene.timeline_id == tid).count() == 2


def test_repurpose_agent_sync_adapter(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.agents import repurpose as rep_mod
    from app.models.assets import Scene
    from app.providers.clips import SourceInfo
    from app.services.jobs import JobContext

    ws_id, tid = _setup(tmp_path, monkeypatch)

    class _Rep:
        def acquire(self, source, ws):
            return SourceInfo(title="t", duration=30.0, width=1, height=1,
                              local_path=tmp_path / "s.mp4")

        def cut_segments(self, *a, **k):
            from app.providers.clips import ClipResult
            return [ClipResult(path=str(tmp_path / "c1.mp4"), start=2.0, end=9.0,
                               duration=7.0, score=80.0, hook="hook here",
                               reason="r", preset="minimal")]

    monkeypatch.setattr(rep_mod, "get_repurposer", lambda: _Rep())
    ctx = JobContext(job_id="j1", type="test", workspace_id=ws_id,
                     cycle_id=None, payload={"timeline_id": tid},
                     attempt=1, cancelled=lambda: False)
    out = rep_mod.RepurposeEditorAgent().assemble(ctx, source="x")
    assert out["synced_scene_ids"] and len(out["synced_scene_ids"]) == 1
    with session_scope() as s:
        sc = s.query(Scene).filter(Scene.timeline_id == tid).one()
        assert (sc.start_seconds, sc.end_seconds) == (2.0, 9.0)
