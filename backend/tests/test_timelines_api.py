"""Timeline HTTP routes: editor load/save, versions, manifest, OTIO, from-video."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"tl{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def test_create_get_update_timeline(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers,
                    json={"name": "main", "duration_seconds": 10.0})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert r.json()["version"] == 1

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{tid}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["tracks"]) == 8  # one per track kind

    # save: add a voice clip via full-tracks replace
    doc = r.json()
    voice = next(t for t in doc["tracks"] if t["kind"] == "voice")
    voice["clips"].append({"id": "n1", "name": "narr", "start": 0.0,
                           "duration": 10.0, "source": {}, "effects": []})
    r = client.put(f"/api/v1/workspaces/{ws_id}/timelines/{tid}", headers=headers,
                   json={"tracks": doc["tracks"], "duration_seconds": 10.0})
    assert r.status_code == 200, r.text
    assert r.json()["tracks"][5]["clips"][0]["id"] == "n1"

    # invalid tracks rejected, not persisted
    bad = [dict(t) for t in doc["tracks"]]
    bad[0]["clips"] = [{"id": "a", "name": "a", "start": 0.0, "duration": 5.0, "source": {}, "effects": []},
                       {"id": "b", "name": "b", "start": 2.0, "duration": 5.0, "source": {}, "effects": []}]
    r = client.put(f"/api/v1/workspaces/{ws_id}/timelines/{tid}", headers=headers,
                   json={"tracks": bad})
    assert r.status_code == 422, r.text


def test_versions_manifest_otio(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers, json={})
    tid = r.json()["id"]

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/{tid}/versions",
                    headers=headers, json={"label": "hook swap"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["parent_timeline_id"] == tid

    v2 = r.json()["id"]
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{v2}/manifest", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["manifest_hash"]) == 32

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{v2}/otio", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["OTIO_SCHEMA"] == "Timeline.1"


def test_from_video_and_list(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines/from-video", headers=headers,
                    json={"video_id": "v1", "aspect": "9:16"})
    assert r.status_code == 200, r.text
    assert r.json()["duration_seconds"] == 5.0  # fallback, keeps editor alive

    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_cross_workspace_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws1, h1 = _register(client)
    ws2, h2 = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws1}/timelines", headers=h1, json={})
    tid = r.json()["id"]

    for method, url, kwargs in [
        ("get", f"/api/v1/workspaces/{ws2}/timelines/{tid}", {}),
        ("put", f"/api/v1/workspaces/{ws2}/timelines/{tid}", {"json": {"name": "x"}}),
        ("get", f"/api/v1/workspaces/{ws2}/timelines/{tid}/manifest", {}),
    ]:
        r = getattr(client, method)(url, headers=h2, **kwargs)
        assert r.status_code == 404, (method, url, r.text)

    r = client.get(f"/api/v1/workspaces/{ws2}/timelines", headers=h2)
    assert r.json()["total"] == 0
