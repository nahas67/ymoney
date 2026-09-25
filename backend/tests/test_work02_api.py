"""Work 02 API: operations batches, versions/restore, render, export, media file."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"e2{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _mk_timeline(client, ws_id, headers):
    from app.engine.timeline import add_clip, create_empty

    doc = create_empty(ws_id, duration_seconds=20.0)
    add_clip(doc, track="video", clip_id="v1", name="A", start=0.0, duration=10.0)
    add_clip(doc, track="video", clip_id="v2", name="B", start=10.0, duration=10.0)
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers,
                    json={"name": "ep", "duration_seconds": 20.0,
                          "tracks": doc["tracks"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_operations_apply_and_bump_version(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    tl = _mk_timeline(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/operations",
                    headers=headers, json={
                        "base_version": tl["version"],
                        "operations": [
                            {"type": "split_item", "track": "video",
                             "clip_id": "v1", "at": 4.0},
                            {"type": "update_volume", "track": "video",
                             "clip_id": "v2", "volume": 0.5},
                        ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == tl["version"] + 1
    clips = next(t for t in body["tracks"] if t["kind"] == "video")["clips"]
    assert [c["id"] for c in clips] == ["v1", "v1__b", "v2"]

    # stale base rejected, nothing applied
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/operations",
                    headers=headers, json={
                        "base_version": tl["version"],
                        "operations": [{"type": "delete_item", "track": "video",
                                        "clip_id": "v1"}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["expected_version"] == tl["version"] + 1
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}", headers=headers)
    assert len(next(t for t in r.json()["tracks"] if t["kind"] == "video")["clips"]) == 3


def test_operations_reject_bad_ops(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    tl = _mk_timeline(client, ws_id, headers)
    url = f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/operations"

    r = client.post(url, headers=headers,
                    json={"base_version": tl["version"],
                          "operations": [{"type": "teleport"}]})
    assert r.status_code == 422, r.text
    r = client.post(url, headers=headers,
                    json={"base_version": tl["version"],
                          "operations": [{"type": "move_to_track", "from_track": "video",
                                          "to_track": "text", "clip_id": "v1"}]})
    assert r.status_code == 422, r.text
    # cross-workspace blocked
    ws2, h2 = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws2}/timelines/{tl['id']}/operations",
                    headers=h2, json={"base_version": 1, "operations": []})
    assert r.status_code == 404, r.text


def test_versions_list_and_restore(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    tl = _mk_timeline(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/versions",
                    headers=headers, json={"label": "v2"})
    v2 = r.json()["id"]
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{v2}/operations",
                    headers=headers, json={
                        "base_version": r.json()["version"],
                        "operations": [{"type": "delete_item", "track": "video",
                                        "clip_id": "v2"}]})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/versions",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["versions"]) == 2  # v1 + v2 (operations edit in place)

    # restore v1 content onto the tip → new version with 2 video clips
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/timelines/{r.json()['versions'][-1]['id']}"
        f"/versions/{tl['id']}/restore", headers=headers, json={})
    assert r.status_code == 200, r.text
    clips = next(t for t in r.json()["tracks"] if t["kind"] == "video")["clips"]
    assert [c["id"] for c in clips] == ["v1", "v2"]


def test_export_otio_download_and_fcpxml_availability(tmp_path, monkeypatch):
    import json as _json

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    tl = _mk_timeline(client, ws_id, headers)

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/export/otio",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert _json.loads(r.text)["OTIO_SCHEMA"] == "Timeline.1"

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tl['id']}/export/fcpxml",
                   headers=headers)
    assert r.status_code in (200, 501), r.text
    if r.status_code == 501:
        assert r.json()["detail"]["status"] == "NOT_AVAILABLE"
    else:
        assert "<fcpxml" in r.text


def test_render_route_and_media_file_serve(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from app.db import session_scope
    from app.models.assets import MediaAsset
    from app.services.storage import STORAGE_ROOT

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)

    root = Path.cwd() / STORAGE_ROOT / ws_id
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=320x576:rate=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(root / "src.mp4")],
                   check=True, capture_output=True, timeout=120)
    with session_scope() as s:
        row = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                         storage_key="src.mp4")
        s.add(row)
        s.flush()
        aid = row.id

    from app.engine.timeline import add_clip, create_empty

    doc = create_empty(ws_id, duration_seconds=4.0)
    add_clip(doc, track="video", clip_id="a", name="A", start=0.0, duration=4.0,
             source={"asset_id": aid})
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers,
                    json={"name": "r", "duration_seconds": 4.0, "tracks": doc["tracks"]})
    tid = r.json()["id"]

    from app.providers.video_engine.timeline_render import resolve_font

    assert resolve_font() is not None, \
        "render font required (install fonts-dejavu or set YMONEY_FONT_FILE)"
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tid}/render",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["width"] == 1080
    out_id = r.json()["asset_id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/media/{out_id}/file", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"

    ws2, h2 = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws2}/assets/media/{out_id}/file", headers=h2)
    assert r.status_code == 404
