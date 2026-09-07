"""Sprint 1 #5: cost-estimate chip backend — POST /content/estimate-cost.

The estimate must be honest (marked is_estimate), engine-aware but never
fail on an unreachable engine, and scale with video_count.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"est{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_estimate_shape(client):
    headers, ws_id = _register(client)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/content/estimate-cost",
        headers=headers,
        json={"topic": "ai money", "video_count": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["video_count"] == 2
    assert body["is_estimate"] is True
    assert body["per_video_usd"] >= 0
    assert abs(body["total_usd"] - body["per_video_usd"] * 2) < 0.0001
    assert "engine" in body


def test_estimate_empty_body_ok(client):
    headers, ws_id = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/estimate-cost", headers=headers, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["video_count"] == 1


def test_estimate_rejects_bad_count(client):
    headers, ws_id = _register(client)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/content/estimate-cost",
        headers=headers,
        json={"video_count": 99},
    )
    assert r.status_code == 422


def test_estimate_requires_auth(client):
    r = client.post("/api/v1/workspaces/some-ws/content/estimate-cost", json={})
    assert r.status_code in (401, 403, 404)
