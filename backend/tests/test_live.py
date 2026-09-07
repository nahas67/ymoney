"""Tests for live monitoring endpoints (metrics series + agent graph)."""
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
    email = f"live{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_live_metrics_shape(client):
    headers, ws_id = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/live/metrics", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["series"]) == 60
    first = body["series"][0]
    for k in ("minute", "runs", "failures", "cost_usd", "events"):
        assert k in first
    for k in ("running_agents", "runs_last_hour", "failures_last_hour", "cost_last_hour_usd", "total_cost_usd"):
        assert k in body["now"]


def test_live_metrics_counts_runs(db_session, client):
    from app.models import AgentRun

    headers, ws_id = _register(client)
    db_session.add(AgentRun(workspace_id=ws_id, agent_key="producer", task_type="render",
                            status="SUCCEEDED", duration_ms=1200, cost_usd=0.02))
    db_session.add(AgentRun(workspace_id=ws_id, agent_key="quality", task_type="review",
                            status="FAILED", duration_ms=300, cost_usd=0.0))
    db_session.commit()

    r = client.get(f"/api/v1/workspaces/{ws_id}/live/metrics", headers=headers)
    body = r.json()
    assert body["now"]["runs_last_hour"] >= 2
    assert body["now"]["failures_last_hour"] >= 1
    assert body["now"]["total_cost_usd"] >= 0.02


def test_agent_graph_returns_pipeline(client, db_session):
    from app.models import AgentRun

    headers, ws_id = _register(client)
    db_session.add(AgentRun(workspace_id=ws_id, agent_key="trend_hunter", task_type="fetch",
                            status="SUCCEEDED", duration_ms=800, cost_usd=0.0))
    db_session.commit()

    r = client.get(f"/api/v1/workspaces/{ws_id}/live/agents/graph", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    keys = [n["key"] for n in body["nodes"]]
    assert keys[0] == "trend_hunter"
    assert len(keys) == 12
    node = body["nodes"][0]
    assert node["runs"] >= 1
    assert node["status"] in ("idle", "busy", "error", "disabled")
    assert isinstance(body["recent_runs"], list)


def test_agent_graph_disabled_agent(client, db_session):
    from app.models import AgentConfig

    headers, ws_id = _register(client)
    db_session.add(AgentConfig(workspace_id=ws_id, agent_key="publisher", enabled=False))
    db_session.commit()

    r = client.get(f"/api/v1/workspaces/{ws_id}/live/agents/graph", headers=headers)
    node = next(n for n in r.json()["nodes"] if n["key"] == "publisher")
    assert node["status"] == "disabled"


def test_live_requires_auth(client):
    r = client.get("/api/v1/workspaces/some-ws/live/metrics")
    assert r.status_code in (401, 403, 404)
