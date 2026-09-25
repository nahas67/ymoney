"""White-label brand kit tests: resolve defaults, logo upload/serve, UI wiring data."""
from __future__ import annotations

import io


def _register(client):
    import os

    email = f"brand{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_brand_defaults_and_invalid_accent_fallback():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    r = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["brand"] == {"app_name": "", "accent": "#22c55e", "logo_path": ""}

    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"app_name": "Acme Shorts", "accent": "not-a-color"}}})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers)
    brand = r.json()["brand"]
    assert brand["app_name"] == "Acme Shorts"
    assert brand["accent"] == "#22c55e"  # invalid falls back, never breaks chrome

    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"accent": "#f59e0b"}}})
    assert r.status_code == 200
    assert client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers).json()["brand"]["accent"] == "#f59e0b"


def test_logo_upload_serve_and_validation():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("logo.png", io.BytesIO(png), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["logo_path"].endswith("logo.png")

    brand = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers).json()["brand"]
    assert brand["logo_path"].endswith("logo.png")

    token = headers["Authorization"].split(" ", 1)[1]
    r = client.get(f"/api/v1/workspaces/{ws_id}/brand/logo/file?token={token}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"

    r = client.get(f"/api/v1/workspaces/{ws_id}/brand/logo/file?token=bad")
    assert r.status_code == 401

    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400

    big = b"0" * (2 * 1024 * 1024 + 1)
    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("big.png", io.BytesIO(big), "image/png")})
    assert r.status_code == 413


def test_settings_rejects_raw_safety_bypass():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    # safety subtree must go through the validated endpoint, not the merge
    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"safety": {"daily_budget_usd": -5}}})
    assert r.status_code == 422, r.text

    # legit keys still merge fine
    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"app_name": "Ok"}}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["brand"]["app_name"] == "Ok"
