"""Work 01 API: lineage/derive, media assets, scenes, timeline events."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"w1{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _mk_content(client, ws_id, headers, topic="master", status="PUBLISHED"):
    from app.db import session_scope
    from app.models import ContentItem

    with session_scope() as s:
        row = ContentItem(workspace_id=ws_id, topic=topic, status=status)
        s.add(row)
        s.flush()
        return row.id


def test_derive_and_lineage_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    mid = _mk_content(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{mid}/derive",
                    headers=headers, json={"derivation_type": "short", "topic": "short 1"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["root_id"] == mid

    r = client.get(f"/api/v1/workspaces/{ws_id}/content/{mid}/lineage", headers=headers)
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()["children"]] == [cid]

    r = client.get(f"/api/v1/workspaces/{ws_id}/content/{cid}/lineage", headers=headers)
    assert [a["id"] for a in r.json()["ancestors"]] == [mid]

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{mid}/derive",
                    headers=headers, json={"derivation_type": "teleport"})
    assert r.status_code == 422, r.text

    # event recorded
    r = client.get(f"/api/v1/workspaces/{ws_id}/activity/recent?limit=40", headers=headers)
    assert r.status_code == 200, r.text
    assert any(e.get("kind") == "content.derived" for e in r.json().get("items", []))


def test_lineage_cross_workspace_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws1, h1 = _register(client)
    ws2, h2 = _register(client)
    mid = _mk_content(client, ws1, h1)
    r = client.get(f"/api/v1/workspaces/{ws2}/content/{mid}/lineage", headers=h2)
    assert r.status_code == 404, r.text


def test_media_register_get_list_and_key_guard(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/media", headers=headers, json={
        "type": "voice", "origin": "render", "provider": "edge-tts",
        "storage_key": "audio/narr_1.mp3", "mime_type": "audio/mpeg",
        "duration_seconds": 12.5, "checksum": "abc"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert r.json()["storage_key"] == "audio/narr_1.mp3"

    for evil in ["../other/x.mp3", "/abs/x.mp3", "..\\win.mp3", ""]:
        r = client.post(f"/api/v1/workspaces/{ws_id}/assets/media", headers=headers,
                        json={"type": "audio", "storage_key": evil})
        assert r.status_code == 422, (evil, r.text)

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/media", headers=headers,
                    json={"type": "spaceship", "storage_key": "a.mp3"})
    assert r.status_code == 422, r.text

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/media/{aid}", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/media", headers=headers)
    assert r.json()["total"] == 1


def test_media_cross_workspace_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws1, h1 = _register(client)
    ws2, h2 = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws1}/assets/media", headers=h1,
                    json={"type": "image", "storage_key": "img/a.png"})
    aid = r.json()["id"]
    assert client.get(f"/api/v1/workspaces/{ws2}/assets/media/{aid}", headers=h2).status_code == 404


def test_scenes_bulk_replace_and_list(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    mid = _mk_content(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers,
                    json={"name": "ep1", "content_item_id": mid})
    tid = r.json()["id"]

    scenes = [
        {"title": "Hook", "script_segment": "Stop scrolling", "narration": "Stop scrolling",
         "visual_intent": "city night", "start_seconds": 0.0, "end_seconds": 3.0},
        {"title": "Payoff", "script_segment": "Do this", "narration": "Do this",
         "visual_intent": "desk", "start_seconds": 3.0, "end_seconds": 10.0},
    ]
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tid}/scenes",
                    headers=headers, json={"content_item_id": mid, "scenes": scenes})
    assert r.status_code == 200, r.text
    assert [s["index"] for s in r.json()["scenes"]] == [0, 1]

    bad = [{"title": "x", "start_seconds": 5.0, "end_seconds": 5.0}]
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tid}/scenes",
                    headers=headers, json={"scenes": bad})
    assert r.status_code == 422, r.text

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tid}/scenes", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["scenes"]) == 2
    assert r.json()["scenes"][0]["timeline_id"] == tid
