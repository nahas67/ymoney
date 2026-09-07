"""API integration tests via FastAPI TestClient."""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(autouse=True)
def _bypass_gate(monkeypatch):
    import app.services.readiness as rd

    monkeypatch.setattr(rd, "run_readiness", lambda force_refresh=True: {
        "status": "ready", "checked_at": "", "stale_after_hours": 24,
        "checks": [], "blocking_failures": [], "message": "test"})


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client):
    import os

    email = f"api{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_register_requires_strong_password(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "weak@x.io", "password": "short"},
    )
    assert r.status_code == 422


def test_auth_me_requires_token(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code in (401, 403)


def test_workspace_isolation(client):
    tok1, ws1, h1 = _register(client)
    tok2, ws2, h2 = _register(client)
    # user 2 cannot see user 1's workspace detail
    r = client.get(f"/api/v1/workspaces/{ws1}", headers=h2)
    assert r.status_code == 403


def test_full_api_cycle_with_mocks(client):
    token, ws_id, headers = _register(client)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/autopilot/start",
        json={"mode": "SINGLE_CYCLE", "config": {"measure_delay_minutes": 0.02},
              "override_readiness": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # readiness endpoint exists and returns real probe results
    rd = client.get("/api/v1/system/readiness").json()
    assert rd["status"] in ("ready", "blocked")

    deadline = time.time() + 120
    state = None
    while time.time() < deadline:
        st = client.get(f"/api/v1/workspaces/{ws_id}/autopilot/status", headers=headers).json()
        state = st["state"]
        if st["cycles_completed"] >= 1 and state in ("STOPPED",):
            break
        time.sleep(1)
    assert state == "STOPPED"

    content = client.get(f"/api/v1/workspaces/{ws_id}/content", headers=headers).json()
    assert content["total"] >= 1

    opps = client.get(f"/api/v1/workspaces/{ws_id}/opportunities", headers=headers).json()
    assert opps["total"] >= 1
    top = opps["items"][0]
    assert top["components"], "scores must be explainable"

    posts = client.get(f"/api/v1/workspaces/{ws_id}/publishing/posts", headers=headers).json()
    assert isinstance(posts.get("items"), list)

    costs = client.get(f"/api/v1/workspaces/{ws_id}/costs", headers=headers).json()
    assert costs["daily_budget_usd"] > 0


def test_activity_recent_endpoint(client):
    token, ws_id, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/activity/recent", headers=headers)
    assert r.status_code == 200
