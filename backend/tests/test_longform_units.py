"""Work 03 units: request validation, strategy, chapters, script, verify, scenes, assets, proxy, pipeline gates."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"lf{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _project(client, ws_id, headers, **kw):
    body = {"topic": "How rivers shape cities", "content_format": "EXPLAINER",
            "target_duration_seconds": 300, "autonomy": "MANUAL"}
    body.update(kw)
    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects",
                    headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_create_validation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    base = {"topic": "Rivers shape cities"}
    for bad in [{"content_format": "OPERA"}, {"target_duration_seconds": 60},
                {"target_duration_seconds": 9999}, {"aspect_ratio": "21:9"},
                {"autonomy": "CHAOS"}, {"budget_strategy": "LUXURY"}]:
        r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects",
                        headers=headers, json={**base, **bad})
        assert r.status_code == 422, (bad, r.text)
    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects",
                    headers=headers, json={**base, "topic": "ab"})
    assert r.status_code == 422


def test_strategy_and_chapter_math(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.longform import stages_early as early
    from app.models import LongFormProject

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        from app.models import Workspace

        ws = Workspace(name="W", slug=f"w-{uuid.uuid4().hex[:8]}", niche="t")
        s.add(ws)
        s.flush()
        p = LongFormProject(workspace_id=ws.id, topic="Rivers shape cities",
                            content_format="DOCUMENTARY", target_duration_seconds=600)
        s.add(p)
        s.flush()
        pid = p.id
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        assert early.stage_strategy(s, p, None).startswith("DOCUMENTARY")
        assert p.strategy_json["narrative_roles"][0] == "Cold Open"
        s.commit()
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        summary = early.stage_outline(s, p, None)
        s.commit()
        assert "10 chapters" in summary
    with session_scope() as s:
        from app.models import LongFormChapter

        chs = s.query(LongFormChapter).filter(
            LongFormChapter.project_id == pid).order_by(LongFormChapter.index).all()
        total = sum(c.target_duration_seconds for c in chs)
        assert abs(total - 600) <= len(chs)  # rounding only
        assert sum(c.target_words for c in chs) > 1000


def test_script_links_claims_and_estimates(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.longform import stages_early as early
    from app.models import LongFormProject

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        from app.models import Workspace

        ws = Workspace(name="W", slug=f"w-{uuid.uuid4().hex[:8]}", niche="t")
        s.add(ws)
        s.flush()
        p = LongFormProject(workspace_id=ws.id, topic="Rivers shape cities",
                            content_format="EXPLAINER", target_duration_seconds=300)
        s.add(p)
        s.flush()
        pid = p.id
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        early.stage_strategy(s, p, None)
        early.stage_outline(s, p, None)
        p.research_json = {"claims": [
            {"claim": "Rivers deposit silt", "status": "VERIFIED", "confidence": 0.9},
            {"claim": "Cities love rivers?", "status": "CONTESTED", "confidence": 0.4}]}
        s.commit()
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        summary = early.stage_script(s, p, None)
        s.commit()
        assert "segments" in summary and p.script_json["words_total"] > 500
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        report = early.stage_verify(s, p, None)
        s.commit()
        assert p.fact_report_json["risky_claims"] >= 1  # contested surfaced, not dropped
        assert "checked" in report


def test_scene_grouping_no_slivers_and_beats_typed(tmp_path, monkeypatch):
    from app.engine.longform import stages_late as late

    segs = [{"id": f"s{i:02d}", "narration": "word " * 60, "type": "explain"}
            for i in range(6)]
    groups = late._group_segments(segs)
    assert all(sum(len(s["narration"].split()) * 60.0 / 150 for s in g) >= 20.0
               for g in groups)
    beats = late._visual_beats("NARRATION_BROLL", 30.0, 0, 0)
    assert all(2.0 <= b["duration"] <= 6.0 for b in beats)
    assert all(b["kind"] in ("broll", "image", "graphic") for b in beats)
    # beats are metadata, not rows: no VisualBeat model exists
    import app.models as models

    assert not hasattr(models, "VisualBeat")


def test_asset_choice_and_ai_video_unavailable(tmp_path, monkeypatch):
    import pytest

    from app.engine.longform import stages_late as late
    from app.providers.longform_assets import get_asset_provider

    assert late._asset_choice("ECONOMY", "x") == ("stock", ["local", "graphic"])
    assert late._asset_choice("PREMIUM", "x")[0] == "ai_video"
    with pytest.raises(Exception, match="Wan2.2 unevaluated"):
        get_asset_provider("ai_video")


def test_pronunciation_applied(tmp_path, monkeypatch):
    from app.engine.longform.stages_voice_timeline import _apply_pronunciation

    out = _apply_pronunciation("NVIDIA Blackwell is here", {"NVIDIA": "en-VID-ee-uh"})
    assert "en-VID-ee-uh Blackwell" in out


def test_pipeline_gates(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    pid = _project(client, ws_id, headers, autonomy="REVIEW",
                   target_duration_seconds=300)

    from app.db import session_scope
    from app.engine.longform.pipeline import advance, run_stage
    from app.models import LongFormProject

    monkeypatch.chdir(tmp_path)
    # run research + strategy + outline directly
    with session_scope() as s:
        p = s.get(LongFormProject, pid)
        p.research_json = {"claims": []}
        s.commit()
    out = run_stage(pid, ws_id)
    assert out["stage"] == "RESEARCH"
    out = run_stage(pid, ws_id)
    assert out["stage"] == "STRATEGY"
    # REVIEW pauses after SCRIPT: run outline then script, expect WAITING_REVIEW
    out = run_stage(pid, ws_id)
    assert out["stage"] == "OUTLINE"
    out = run_stage(pid, ws_id)
    assert out["stage"] == "SCRIPT" and out["status"] == "WAITING_REVIEW"
    # advance resumes
    out = advance(pid, ws_id)
    assert out["stage"] == "VERIFY"
    # cancel stops the chain
    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects/{pid}/cancel",
                    headers=headers)
    assert r.status_code == 200
    out = run_stage(pid, ws_id)
    assert out["status"] == "CANCELLED"


def test_manual_never_auto_advances_and_cross_ws_404(tmp_path, monkeypatch):
    from app.services.jobs import JobContext

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    ws2, h2 = _register(client)
    pid = _project(client, ws_id, headers, autonomy="MANUAL")

    from app.engine.longform.pipeline import run_stage

    monkeypatch.chdir(tmp_path)
    ctx = JobContext(job_id="j", type="longform.stage", workspace_id=ws_id,
                     cycle_id=None, payload={"project_id": pid, "stage": "RESEARCH"},
                     attempt=1, cancelled=lambda: False)
    out = run_stage(pid, ws_id, job_ctx=ctx)
    assert out["status"] == "WAITING_REVIEW"  # job did not advance MANUAL
    r = client.get(f"/api/v1/workspaces/{ws2}/long-form/projects/{pid}/progress",
                   headers=h2)
    assert r.status_code == 404


def test_estimate_shape(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    pid = _project(client, ws_id, headers, target_duration_seconds=600,
                   budget_strategy="BALANCED")
    r = client.get(f"/api/v1/workspaces/{ws_id}/long-form/projects/{pid}/estimate",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["estimates_usd"]["tts"] > 0
    assert "assumptions" in r.json()


def test_proxy_generates_and_final_uses_original(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from app.db import session_scope
    from app.engine.longform import proxy as proxy_mod
    from app.models import Workspace
    from app.models.assets import MediaAsset
    from app.providers.video_engine.timeline_render import resolve_clip_source
    from app.services.storage import STORAGE_ROOT

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        ws = Workspace(name="W", slug=f"w-{uuid.uuid4().hex[:8]}", niche="t")
        s.add(ws)
        s.flush()
        ws_id = ws.id
    root = Path.cwd() / STORAGE_ROOT / ws_id
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=640x360:rate=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(root / "orig.mp4")],
                   check=True, capture_output=True, timeout=120)
    with session_scope() as s:
        row = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                         storage_key="orig.mp4")
        s.add(row)
        s.flush()
        aid = row.id
    with session_scope() as s:
        proxy = proxy_mod.generate_proxy(s, ws_id, aid)
        s.commit()
        assert proxy.meta_json["original_asset_id"] == aid
        assert (proxy.width or 0) <= 720
    with session_scope() as s:
        # finals resolve the ORIGINAL even when a proxy exists
        resolved = resolve_clip_source(ws_id, s, {"asset_id": aid})
        assert resolved is not None and resolved.name == "orig.mp4"


def test_repair_route_and_regenerate_guard(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentTimeline, LongFormProject

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    pid = _project(client, ws_id, headers, autonomy="MANUAL")
    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        doc = create_empty(ws_id, duration_seconds=4.0)
        add_clip(doc, track="video", clip_id="a", name="A", start=0.0, duration=4.0,
                 source={"asset_id": "missing-asset"})
        tl = ContentTimeline(workspace_id=ws_id, name="t", duration_seconds=4.0,
                             tracks_json=doc)
        s.add(tl)
        s.flush()
        p = s.get(LongFormProject, pid)
        p.timeline_id = tl.id
        s.commit()
    r = client.post(f"/api/v1/workspaces/{ws_id}/long-form/projects/{pid}/repair",
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["repaired"] == ["a"]
    # repair bumped the timeline to v2, so the manual-edit guard fires: 409
    # without confirm, 200 with confirm (snapshot first, never destructive)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/long-form/projects/{pid}/regenerate-timeline",
        headers=headers, json={"confirm": False})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["timeline_version"] == 2
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/long-form/projects/{pid}/regenerate-timeline",
        headers=headers, json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["snapshot_version_id"]
