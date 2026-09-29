"""UGC pipeline (Work 07 Lane C): nine presets, editor-compatible canonical
timeline, claim-safety QC, regeneration edit-guard, workspace isolation.

Test doubles only — the shared `FakeLLMTransport` (conftest) drives strategy/
script/b-roll prompts, and `MockTTSProvider` is injected at the voice boundary
below. The product ships no mock path and nothing here touches the network.
"""
from __future__ import annotations

import os

import pytest

from app.engine.timeline import (
    add_clip,
    create_empty,
    render_manifest,
    validate_timeline,
)
from app.engine.ugc import (
    PRESET_DEFAULTS,
    QC_STATUSES,
    UGC_PRESETS,
    UGCBlockedError,
    UGCError,
    UGCVideoPipeline,
    normalize_brief,
    run_ugc_qc,
)

EXPECTED_PRESETS = (
    "PRODUCT_DEMO",
    "TESTIMONIAL",
    "REVIEW",
    "UNBOXING",
    "REACTION",
    "PROBLEM_SOLUTION",
    "FOUNDER_STYLE",
    "TALKING_HEAD",
    "BEFORE_AFTER",
)


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_tts(monkeypatch):
    """Voice-stage double: the default edge provider would reach the network."""
    from app.providers import tts as tts_mod

    monkeypatch.setattr(
        tts_mod, "get_tts_provider", lambda *a, **k: tts_mod.MockTTSProvider()
    )
    return tts_mod.MockTTSProvider


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, tag: str) -> dict:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": f"{tag}{os.urandom(4).hex()}@test.local",
              "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "ws": data["workspace"]["id"],
    }


def _asset(db_session, ws_id: str) -> str:
    """A workspace product upload (the only visuals a UGC brief may reference)."""
    from app.models.assets import MediaAsset

    row = MediaAsset(
        workspace_id=ws_id, type="image", origin="upload", provider="test",
        storage_key=f"uploads/product_{os.urandom(3).hex()}.png",
        mime_type="image/png",
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def _brief(topic: str = "Aurora Lamp", *, asset_ids: list[str] | None = None,
           **extra) -> dict:
    body: dict = {"topic": topic, "audience": "home decor buyers", "tone": "friendly"}
    if asset_ids:
        body["product_assets"] = list(asset_ids)
    body.update(extra)
    return body


def _create_project(client, ctx: dict, preset: str, brief: dict,
                    *, run: bool = True) -> dict:
    r = client.post(
        f"/api/v1/workspaces/{ctx['ws']}/ugc/projects",
        headers=ctx["headers"],
        json={"preset": preset, "brief": brief, "run": run},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _doc(**clips) -> dict:
    """Minimal editor-valid timeline doc for QC unit tests."""
    doc = create_empty("ws-test", aspect="9:16", duration_seconds=8.0)
    add_clip(doc, track="voice", clip_id="v_0", name="narration",
             start=0.0, duration=8.0, source={"asset_id": "a-voice"})
    add_clip(doc, track="caption", clip_id="c_0", name="narration",
             start=0.0, duration=8.0)
    add_clip(doc, track="broll", clip_id="b_0", name="product",
             start=0.0, duration=8.0, source={"asset_id": clips.get("visual", "a-visual")})
    add_clip(doc, track="text", clip_id="hook", name="hook: opening",
             start=0.0, duration=3.0, text={"content": "opening", "size": 64})
    add_clip(doc, track="text", clip_id="cta", name="cta: follow",
             start=5.0, duration=3.0, text={"content": "follow", "size": 56})
    return doc


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


def test_nine_presets_registered(client):
    ctx = _register(client, "ugcp")
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/ugc/presets",
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 9
    assert tuple(item["key"] for item in body["items"]) == EXPECTED_PRESETS
    # module surface matches the route surface
    assert UGC_PRESETS == EXPECTED_PRESETS
    assert set(PRESET_DEFAULTS) == set(EXPECTED_PRESETS)
    for item in body["items"]:
        assert item["duration_seconds"] >= 20  # documented 20-90s clamp
        assert item["hook_type"] and item["format"]


def test_unknown_preset_is_rejected_with_listing(client):
    ctx = _register(client, "ugcx")
    r = client.post(
        f"/api/v1/workspaces/{ctx['ws']}/ugc/projects",
        headers=ctx["headers"],
        json={"preset": "TIKTOK_MAGIC", "brief": {"topic": "x"}, "run": False},
    )
    assert r.status_code == 422, r.text
    assert "unknown UGC preset" in r.json()["detail"]
    assert "PRODUCT_DEMO" in r.json()["detail"] and "BEFORE_AFTER" in r.json()["detail"]
    with pytest.raises(UGCError, match="unknown UGC preset"):
        normalize_brief("TIKTOK_MAGIC", {})


def test_all_nine_presets_execute_to_a_timeline(client, db_session):
    """Every registered preset runs the full flow and yields an editable timeline."""
    ctx = _register(client, "ugc9")
    asset_id = _asset(db_session, ctx["ws"])
    for preset in EXPECTED_PRESETS:
        body = _create_project(client, ctx, preset, _brief(asset_ids=[asset_id]))
        assert body["ran"] is True, preset
        assert body["timeline_id"], preset
        assert body["qc"]["status"] in QC_STATUSES, preset
        assert body["project"]["preset"] == preset
        assert body["project"]["status"] in ("READY", "REVIEW_REQUIRED",
                                             "BLOCKED", "RENDERED"), preset
        # TESTIMONIAL without source material warns; never fabricates a quote
        expected = "PASS_WITH_WARNINGS" if preset == "TESTIMONIAL" else "PASS"
        assert body["qc"]["status"] == expected, (preset, body["qc"])


# ---------------------------------------------------------------------------
# editor-compatible canonical timeline
# ---------------------------------------------------------------------------


def test_pipeline_creates_editor_compatible_timeline(client, db_session):
    ctx = _register(client, "ugct")
    asset_id = _asset(db_session, ctx["ws"])

    body = _create_project(client, ctx, "PRODUCT_DEMO",
                           _brief(asset_ids=[asset_id]))
    timeline_id = body["timeline_id"]
    assert timeline_id, body
    assert body["qc"]["status"] in QC_STATUSES

    # the canonical editor load route returns the generated tracks
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}",
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text
    tl = r.json()
    assert tl["id"] == timeline_id and tl["version"] >= 1
    kinds = {t["kind"] for t in tl["tracks"]}
    assert {"voice", "caption", "text"} <= kinds
    assert tl["duration_seconds"] > 0

    # same validation the editor API runs on load/save
    doc = {"tracks": tl["tracks"], "duration_seconds": tl["duration_seconds"],
           "fps": tl["fps"], "aspect_ratio": tl["aspect_ratio"]}
    validate_timeline(doc)

    # render manifest (editor render gate) describes a real build
    r = client.get(
        f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}/manifest",
        headers=ctx["headers"])
    assert r.status_code == 200, r.text
    manifest = r.json()
    assert manifest["clip_count"] > 0 and manifest["manifest_hash"]
    assert render_manifest(doc)["manifest_hash"] == manifest["manifest_hash"]

    # editable narrative units (scenes) were created for the timeline
    r = client.get(
        f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}/scenes",
        headers=ctx["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] >= 1

    # the editor accepts the generated doc back (save round-trip)
    r = client.put(
        f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}",
        headers=ctx["headers"],
        json={"tracks": tl["tracks"], "duration_seconds": tl["duration_seconds"]},
    )
    assert r.status_code == 200, r.text

    # reuse map: ScriptAgent + BrandDNA strategy + TTS + b-roll planner, all
    # recorded in lineage — one video engine, no second one.
    lineage = body["project"]["lineage"]
    assert lineage["script_source"] == "script_agent"
    assert lineage["script"]
    assert lineage["strategy"]["aspect_ratio"] in ("9:16", "16:9", "1:1", "4:5")
    assert len(lineage["voice"]) >= 1
    assert all(seg["asset_id"] for seg in lineage["voice"])
    assert lineage["broll_plan"], "b-roll planner must contribute a scene plan"
    assert lineage["manifest_hash"] and lineage["generated_version"] >= 1
    assert lineage["content_item_id"]
    assert body["qc"]["checks"]["product_assets"]["status"] == "pass"
    assert body["qc"]["checks"]["hook_present"]["status"] == "pass"
    assert body["qc"]["checks"]["cta_present"]["status"] == "pass"
    assert body["qc"]["checks"]["timeline_complete"]["status"] == "pass"


# ---------------------------------------------------------------------------
# claim safety: testimonial + numeric/absolute claims
# ---------------------------------------------------------------------------


def test_fabricated_testimonial_claim_fails_qc_and_blocks_render(client, db_session):
    """A quote with no source_quote is NEVER shipped: QC FAIL → render 409."""
    ctx = _register(client, "ugcq")
    asset_id = _asset(db_session, ctx["ws"])
    script = '"It changed my life overnight" said one customer about the Aurora Lamp.'
    brief = _brief(asset_ids=[asset_id], script=script)

    body = _create_project(client, ctx, "PRODUCT_DEMO", brief)

    assert body["qc"]["status"] == "FAIL", body["qc"]
    claim = body["qc"]["checks"]["unsupported_claims"]
    assert claim["status"] == "fail"
    assert "source_quote" in claim["detail"]
    # everything else passed — the claims gate is what blocked it
    assert body["qc"]["checks"]["product_assets"]["status"] == "pass"
    assert body["qc"]["checks"]["timeline_complete"]["status"] == "pass"
    assert body["project"]["status"] == "BLOCKED"
    assert body["project"]["render_asset_ref"] == ""
    # the user brief is stored exactly as supplied — nothing added or rewritten
    assert body["project"]["brief"] == brief

    # rendering a QC-FAIL project refuses loudly (409), never silently ships
    r = client.post(
        f"/api/v1/workspaces/{ctx['ws']}/ugc/projects/{body['project']['id']}/render",
        headers=ctx["headers"], json={})
    assert r.status_code == 409, r.text
    assert "QC FAIL" in r.json()["detail"]

    # unit-level: the report marks FAIL as blocking
    report = run_ugc_qc(preset="TESTIMONIAL", brief={"topic": "Aurora Lamp"},
                        script=script, doc=_doc())
    assert report.status == "FAIL" and report.blocking is True
    assert report.checks["unsupported_claims"]["status"] == "fail"


def test_sourced_testimonial_quote_passes_claim_gate(client, db_session):
    """The same quote with a user-supplied source_quote is accepted."""
    ctx = _register(client, "ugcs")
    asset_id = _asset(db_session, ctx["ws"])
    script = '"It changed my life overnight", said Sam about the Aurora Lamp.'

    body = _create_project(
        client, ctx, "TESTIMONIAL",
        _brief(asset_ids=[asset_id], script=script,
               testimonials=[{"name": "Sam",
                              "source_quote": "It changed my life overnight"}]))

    assert body["qc"]["status"] in ("PASS", "PASS_WITH_WARNINGS"), body["qc"]
    assert body["qc"]["checks"]["unsupported_claims"]["status"] == "pass"
    assert body["qc"]["checks"]["testimonial_sources"]["status"] == "pass"
    assert body["project"]["status"] == "READY"


def test_unsupported_numeric_claim_requires_review():
    """A number absent from the brief → REVIEW_REQUIRED (human gate, not a ship)."""
    script = "This tool makes your workflow 300% faster, guaranteed."
    report = run_ugc_qc(preset="PRODUCT_DEMO",
                        brief={"topic": "Aurora Lamp"},
                        script=script, doc=_doc())
    claim = report.checks["unsupported_claims"]
    assert claim["status"] == "review"
    assert "300%" in claim["detail"] or "faster" in claim["detail"]
    assert report.status == "REVIEW_REQUIRED"
    assert report.blocking is False  # review ≠ fail: it must not render silently

    # the same claim IS supported when the brief states it
    backed = run_ugc_qc(
        preset="PRODUCT_DEMO",
        brief={"topic": "Aurora Lamp",
               "claims": ["300% faster than the old workflow"]},
        script="This tool makes your workflow 300% faster.", doc=_doc())
    assert backed.checks["unsupported_claims"]["status"] == "pass"


# ---------------------------------------------------------------------------
# product asset presence QC
# ---------------------------------------------------------------------------


def test_product_asset_presence_qc():
    # declared in the brief, missing from the timeline → FAIL (never invents)
    report = run_ugc_qc(preset="PRODUCT_DEMO",
                        brief={"topic": "Aurora Lamp", "product_assets": ["ghost-1"]},
                        script="A clean product demo.", doc=_doc())
    assert report.checks["product_assets"]["status"] == "fail"
    assert report.status == "FAIL"

    # declared and present → pass
    report = run_ugc_qc(
        preset="PRODUCT_DEMO",
        brief={"topic": "Aurora Lamp", "product_assets": ["a-visual"]},
        script="A clean product demo.", doc=_doc(visual="a-visual"))
    assert report.checks["product_assets"]["status"] == "pass"
    assert report.status == "PASS"

    # nothing declared → warning only (the claim gates still run)
    report = run_ugc_qc(preset="PRODUCT_DEMO", brief={"topic": "Aurora Lamp"},
                        script="A clean product demo.", doc=_doc())
    assert report.checks["product_assets"]["status"] == "warning"
    assert report.status == "PASS_WITH_WARNINGS"  # warning rolls up, never plain PASS


def test_missing_product_asset_fails_project(client, db_session):
    """A brief pointing at an asset that is not in this workspace is blocked."""
    ctx = _register(client, "ugca")
    body = _create_project(client, ctx, "UNBOXING",
                           _brief(asset_ids=["ghost-asset-not-uploaded"]))
    assert body["qc"]["checks"]["product_assets"]["status"] == "fail"
    assert "not in timeline" in body["qc"]["checks"]["product_assets"]["detail"]
    assert body["qc"]["status"] == "FAIL"
    assert body["project"]["status"] == "BLOCKED"


def test_short_script_still_builds_valid_timeline(client, db_session):
    """Regression: sub-6s narration must not overlap hook/CTA clips."""
    ctx = _register(client, "ugcshort")
    asset_id = _asset(db_session, ctx["ws"])
    body = _create_project(
        client, ctx, "TALKING_HEAD",
        _brief(asset_ids=[asset_id], script="Wow, this lamp is great."))
    assert body["timeline_id"], body
    r = client.get(
        f"/api/v1/workspaces/{ctx['ws']}/timelines/{body['timeline_id']}",
        headers=ctx["headers"])
    assert r.status_code == 200, r.text
    tl = r.json()
    validate_timeline({"tracks": tl["tracks"],
                       "duration_seconds": tl["duration_seconds"],
                       "fps": tl["fps"], "aspect_ratio": tl["aspect_ratio"]})
    text_clips = [c for t in tl["tracks"] if t["kind"] == "text" for c in t["clips"]]
    ids = {c["id"] for c in text_clips}
    assert {"hook", "cta"} <= ids
    assert body["qc"]["checks"]["timeline_complete"]["status"] == "pass"


# ---------------------------------------------------------------------------
# regeneration never clobbers a manual edit
# ---------------------------------------------------------------------------


def test_regenerate_refuses_after_manual_edit(client, db_session):
    from app.models import ContentTimeline, UgcProjectRow

    ctx = _register(client, "ugcr")
    asset_id = _asset(db_session, ctx["ws"])
    body = _create_project(client, ctx, "REVIEW", _brief(asset_ids=[asset_id]))
    timeline_id = body["timeline_id"]
    project_id = body["project"]["id"]

    # a human edits the generated timeline in the editor
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}",
                   headers=ctx["headers"])
    tracks = r.json()["tracks"]
    edited = False
    for track in tracks:
        if track["kind"] == "caption" and track["clips"]:
            track["clips"][0]["name"] = "edited by a human"
            edited = True
            break
    assert edited, "generated timeline must carry caption clips to edit"
    r = client.put(f"/api/v1/workspaces/{ctx['ws']}/timelines/{timeline_id}",
                   headers=ctx["headers"],
                   json={"tracks": tracks,
                         "duration_seconds": r.json()["duration_seconds"]})
    assert r.status_code == 200, r.text

    # regeneration must refuse instead of overwriting that edit
    row = db_session.get(UgcProjectRow, project_id)
    out = UGCVideoPipeline(db_session, ctx["ws"], row).regenerate()
    assert out["refused"] is True
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["timeline_id"] == timeline_id
    assert row.status == "REVIEW_REQUIRED"
    refusal = dict(row.qc_json or {}).get("regeneration") or {}
    assert refusal["refused"] is True and refusal["current_manifest_hash"]
    db_session.commit()

    # no child timeline was written and the human edit survived
    rows = db_session.query(ContentTimeline).filter_by(
        content_item_id=row.lineage_json["content_item_id"]).all()
    assert len(rows) == 1
    assert rows[0].id == timeline_id
    clips = [c for t in rows[0].tracks_json["tracks"] for c in t["clips"]]
    assert any(c["name"] == "edited by a human" for c in clips)
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/ugc/projects/{project_id}",
                   headers=ctx["headers"])
    assert r.status_code == 200
    assert r.json()["timeline_id"] == timeline_id


def test_regenerate_refuses_version_mismatch(client, db_session):
    from app.models import ContentTimeline, UgcProjectRow

    ctx = _register(client, "ugcv")
    body = _create_project(client, ctx, "BEFORE_AFTER", _brief())
    project_id = body["project"]["id"]
    timeline_id = body["timeline_id"]
    assert timeline_id

    # an editor version bump alone is enough to refuse (hash may still match)
    row = db_session.get(UgcProjectRow, project_id)
    tl_row = db_session.get(ContentTimeline, timeline_id)
    tl_row.version = int(tl_row.version or 1) + 1
    db_session.commit()

    out = UGCVideoPipeline(db_session, ctx["ws"], row).regenerate()
    assert out["refused"] is True and out["status"] == "REVIEW_REQUIRED"
    assert row.status == "REVIEW_REQUIRED"
    assert row.timeline_id == timeline_id
    db_session.commit()

    # untouched: same row, bumped version, still exactly one timeline
    db_session.expire_all()
    assert db_session.get(ContentTimeline, timeline_id).version == 2
    assert db_session.query(ContentTimeline).count() >= 1
    assert row.qc_json["regeneration"]["expected_version"] == 1


# ---------------------------------------------------------------------------
# render path (QC-gated)
# ---------------------------------------------------------------------------


def test_render_produces_asset_and_reruns_qc(db_session, tmp_path):
    from app.models import MediaAsset, UgcProjectRow, Workspace

    ws = Workspace(name="UGC render", slug=f"ugc-r-{os.urandom(3).hex()}",
                   niche="AI money")
    db_session.add(ws)
    db_session.flush()
    asset = MediaAsset(workspace_id=ws.id, type="image", origin="upload",
                       provider="test", storage_key="uploads/p.png",
                       mime_type="image/png")
    db_session.add(asset)
    db_session.flush()

    row = UgcProjectRow(workspace_id=ws.id, preset="PRODUCT_DEMO",
                        brief_json=_brief(asset_ids=[asset.id]), status="DRAFT")
    db_session.add(row)
    db_session.flush()

    out_file = tmp_path / "render.mp4"
    out_file.write_bytes(b"\x00\x00fake-mp4")

    def fake_render(doc: dict) -> dict:
        validate_timeline(doc)
        return {"path": str(out_file), "duration_seconds": 12.0,
                "width": 720, "height": 1280, "warnings": ["test render"]}

    pipe = UGCVideoPipeline(db_session, ws.id, row, render_fn=fake_render)
    result = pipe.run(render=True)
    db_session.commit()

    rendered = result["render"]
    assert rendered["asset_id"] and rendered["storage_key"]
    assert row.status == "RENDERED"
    assert row.render_asset_ref == rendered["asset_id"]
    assert rendered["qc"]["status"] in ("PASS", "PASS_WITH_WARNINGS")
    assert rendered["timeline_id"] == row.timeline_id

    media = db_session.get(MediaAsset, rendered["asset_id"])
    assert media.workspace_id == ws.id
    assert media.meta_json["timeline_id"] == row.timeline_id
    assert media.meta_json["warnings"] == ["test render"]


def test_qc_fail_blocks_render_before_any_file_work(db_session, tmp_path):
    from app.models import UgcProjectRow, Workspace

    ws = Workspace(name="UGC blocked", slug=f"ugc-b-{os.urandom(3).hex()}",
                   niche="AI money")
    db_session.add(ws)
    db_session.flush()

    row = UgcProjectRow(workspace_id=ws.id, preset="TESTIMONIAL",
                        brief_json={"topic": "X"}, status="BLOCKED",
                        qc_json={"status": "FAIL",
                                 "checks": {"unsupported_claims": {"status": "fail"}},
                                 "preset": "TESTIMONIAL", "report_type": "ugc"})
    db_session.add(row)
    db_session.flush()
    called = []

    def should_not_run(doc):
        called.append(doc)
        return {"path": str(tmp_path / "x.mp4")}

    pipe = UGCVideoPipeline(db_session, ws.id, row, render_fn=should_not_run)
    with pytest.raises(UGCBlockedError, match="BLOCKED by QC FAIL"):
        pipe.render()
    assert called == []  # the render engine was never touched


# ---------------------------------------------------------------------------
# honest failures + QC vocabulary
# ---------------------------------------------------------------------------


def test_failed_brief_reports_honest_error(client):
    ctx = _register(client, "ugcf")
    r = client.post(
        f"/api/v1/workspaces/{ctx['ws']}/ugc/projects",
        headers=ctx["headers"],
        json={"preset": "PRODUCT_DEMO", "brief": {}, "run": True})
    assert r.status_code == 422, r.text
    assert "topic or product name" in r.json()["detail"]

    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/ugc/projects",
                   headers=ctx["headers"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "FAILED"
    assert "UGCError" in items[0]["lineage"].get("error", "")


def test_qc_status_vocabulary():
    from app.engine.ugc import AvatarQCReport, UGCQCReport, run_avatar_qc

    assert QC_STATUSES == ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED", "FAIL")

    report = run_ugc_qc(preset="PRODUCT_DEMO", brief={"topic": "X"},
                        script="", doc=_doc())
    assert report.status in QC_STATUSES
    assert UGCQCReport.from_dict(report.to_dict()).status == report.status
    # an unknown stored status degrades to PASS — never blocks by accident
    assert UGCQCReport.from_dict({"status": "banana"}).status == "PASS"

    # avatar QC: a non-authorized consent snapshot fails the output gate
    avatar = run_avatar_qc(output_path="", audio_duration=None,
                           lineage={"consent": {"state": "revoked"}})
    assert avatar.checks["consent"]["status"] == "fail"
    assert avatar.status == "FAIL" and avatar.blocking is True
    assert AvatarQCReport.from_dict(avatar.to_dict()).flags == avatar.flags


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_ugc_isolation(client):
    owner = _register(client, "ugco")
    other = _register(client, "ugcw")

    r = client.post(
        f"/api/v1/workspaces/{owner['ws']}/ugc/projects",
        headers=owner["headers"],
        json={"preset": "PRODUCT_DEMO", "brief": {"topic": "Aurora Lamp"},
              "run": False})
    assert r.status_code == 200, r.text
    project_id = r.json()["project"]["id"]

    # another user cannot enter the owner's workspace (403, no details)
    r = client.get(
        f"/api/v1/workspaces/{owner['ws']}/ugc/projects/{project_id}",
        headers=other["headers"])
    assert r.status_code == 403, r.text

    # their own workspace path for that project is 404 (no existence hint)
    for method, url in (
        ("get", f"/api/v1/workspaces/{other['ws']}/ugc/projects/{project_id}"),
        ("post", f"/api/v1/workspaces/{other['ws']}/ugc/projects/{project_id}/render"),
    ):
        kwargs = {"headers": other["headers"]}
        if method == "post":
            kwargs["json"] = {}
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 404, (method, r.text)

    # the foreign workspace's own list stays empty
    r = client.get(f"/api/v1/workspaces/{other['ws']}/ugc/projects",
                   headers=other["headers"])
    assert r.status_code == 200 and r.json()["total"] == 0

    # the owner still sees exactly their project
    r = client.get(f"/api/v1/workspaces/{owner['ws']}/ugc/projects",
                   headers=owner["headers"])
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == project_id
