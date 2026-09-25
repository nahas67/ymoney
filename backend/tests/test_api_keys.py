"""Workspace API keys: mint once-only plaintext, key auth, revoke, isolation."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"key{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def test_api_key_lifecycle(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    # mint requires auth
    r = client.post(f"/api/v1/workspaces/{ws_id}/api-keys", json={"name": "ci"})
    assert r.status_code in (401, 403)

    # invalid role rejected
    r = client.post(f"/api/v1/workspaces/{ws_id}/api-keys", headers=headers, json={"role": "superuser"})
    assert r.status_code == 422

    # mint
    r = client.post(f"/api/v1/workspaces/{ws_id}/api-keys", headers=headers, json={"name": "ci", "role": "member"})
    assert r.status_code == 200, r.text
    body = r.json()
    raw = body["api_key"]
    assert raw.startswith("ym_")
    assert body["prefix"] == raw[:12]
    key_id = body["id"]

    # list hides secrets
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["prefix"] == raw[:12]
    assert "api_key" not in items[0] and "key_hash" not in items[0]

    # key auth via Bearer
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys/me", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_id"] == ws_id

    # key auth via X-API-Key
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys/me", headers={"X-API-Key": raw})
    assert r.status_code == 200, r.text

    # last_used_at recorded
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys", headers=headers)
    assert r.json()["items"][0]["last_used_at"] is not None

    # bad key denied
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys/me", headers={"Authorization": "Bearer ym_boguskey123"})
    assert r.status_code == 401

    # JWT is not a key
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys/me", headers=headers)
    assert r.status_code == 401

    # cross-workspace denied
    _, ws2, _ = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws2}/api-keys/me", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403

    # revoke kills the key
    r = client.post(f"/api/v1/workspaces/{ws_id}/api-keys/{key_id}/revoke", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/api-keys/me", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401

    # revoke unknown 404s
    r = client.post(f"/api/v1/workspaces/{ws_id}/api-keys/nonexistent/revoke", headers=headers)
    assert r.status_code == 404
