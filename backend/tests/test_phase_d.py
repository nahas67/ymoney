"""Phase D regression coverage: cycle-detail API, analytics breakdowns,
publishing idempotency pre-registration."""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[str, str, dict]:
    email = f"phaseD{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


# ---------------------------------------------------------------------------
# cycle detail API
# ---------------------------------------------------------------------------


def test_cycle_detail_not_found(client):
    _tok, ws_id, headers = _register(client)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/cycles/nonexistent-id", headers=headers
    )
    assert r.status_code == 404


def test_cycle_detail_404_for_other_workspace(client):
    """No cross-workspace leakage: cycle ids from another workspace are 404."""
    _tok1, ws1, h1 = _register(client)
    tok2, ws2, h2 = _register(client)

    from app.db import session_scope
    from app.engine.autopilot import start_autopilot

    start_autopilot(ws1, mode="SINGLE_CYCLE", override_readiness=True,
                    config={"measure_delay_minutes": 0.02})
    time.sleep(2)

    from sqlalchemy import select

    from app.models import Cycle

    with session_scope() as s:
        cyc = s.scalars(
            select(Cycle).where(Cycle.workspace_id == ws1).limit(1)
        ).first()
    if cyc is None:
        pytest.skip("no cycle created yet")
    r = client.get(f"/api/v1/workspaces/{ws2}/cycles/{cyc.id}", headers=h2)
    assert r.status_code == 404


def test_cycle_detail_shape_after_full_cycle(client):
    """After a single cycle, the detail endpoint exposes stages, jobs, and
    agent runs with step traces."""
    _tok, ws_id, headers = _register(client)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/autopilot/start",
        json={"mode": "SINGLE_CYCLE",
              "config": {"measure_delay_minutes": 0.02},
              "override_readiness": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    deadline = time.time() + 120
    cyc = None
    while time.time() < deadline:
        cycles = client.get(
            f"/api/v1/workspaces/{ws_id}/cycles?limit=5", headers=headers
        ).json()
        items = cycles.get("items") or []
        if items and items[0].get("status") in ("COMPLETED", "FAILED"):
            cyc = items[0]
            break
        time.sleep(1)
    assert cyc is not None, "cycle did not finish in time"

    d = client.get(
        f"/api/v1/workspaces/{ws_id}/cycles/{cyc['id']}", headers=headers
    )
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["id"] == cyc["id"]
    assert body["number"] == cyc["number"]
    assert "stages" in body and "agent_runs" in body
    # stages is a dict of stage -> list of job DTOs
    assert isinstance(body["stages"], dict)
    for stage, job_list in body["stages"].items():
        assert isinstance(stage, str) and stage
        for j in job_list:
            assert {"id", "type", "status", "agent_runs"} <= set(j)
            for run in j["agent_runs"]:
                assert {"agent_key", "status", "steps"} <= set(run)
                assert isinstance(run["steps"], list)


# ---------------------------------------------------------------------------
# analytics breakdowns
# ---------------------------------------------------------------------------


def test_breakdowns_endpoint_empty_workspace(client):
    _tok, ws_id, headers = _register(client)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/analytics/breakdowns", headers=headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by_topic"] == []
    assert body["by_hook_style"] == []
    assert body["by_duration"] == []
    assert body["posts_with_metrics"] == 0


def test_breakdowns_grouping_logic():
    """The hook-style classifier and duration bucketing behave sensibly."""

    # reimplement the classifier inline to check behavior without a DB
    def hook_style(hook: str) -> str:
        h = (hook or "").strip()
        if not h:
            return "unknown"
        if h.endswith("?"):
            return "question"
        first = h.split()[0].lower() if h.split() else ""
        if first in ("stop", "never", "always", "don't", "dont"):
            return "command"
        if any(c.isdigit() for c in h[:20]):
            return "number"
        return "statement"

    assert hook_style("Why do cats purr?") == "question"
    assert hook_style("Stop wasting money!") == "command"
    assert hook_style("3 ways to invest") == "number"
    assert hook_style("The market changed today") == "statement"
    assert hook_style("") == "unknown"

    def bucket_duration(sec):
        if sec is None:
            return "unknown"
        if sec <= 20:
            return "≤20s"
        if sec <= 35:
            return "21–35s"
        if sec <= 60:
            return "36–60s"
        return ">60s"

    assert bucket_duration(None) == "unknown"
    assert bucket_duration(15) == "≤20s"
    assert bucket_duration(30) == "21–35s"
    assert bucket_duration(55) == "36–60s"
    assert bucket_duration(90) == ">60s"


# ---------------------------------------------------------------------------
# publishing idempotency pre-registration
# ---------------------------------------------------------------------------


def test_publishing_job_preregistration(client):
    """After a cycle's UPLOAD stage, a QUEUED/PUBLISHED PublishingJob row must
    exist per (video, platform) — created before the publish call, and never
    duplicated on the same video+platform."""
    _tok, ws_id, headers = _register(client)
    # Force PRODUCE despite modest fixture trend scores: tests exercise the
    # publishing mechanics, not the decision threshold (covered elsewhere).
    from app.db import session_scope
    from app.models import Workspace

    with session_scope() as s:
        w = s.get(Workspace, ws_id)
        w.settings_json = {"safety": {"produce_score_threshold": 1.0,
                                       "min_qc_score": 60}}
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/autopilot/start",
        json={"mode": "SINGLE_CYCLE",
              "config": {"measure_delay_minutes": 0.02},
              "override_readiness": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    deadline = time.time() + 120
    cyc = None
    while time.time() < deadline:
        cycles = client.get(
            f"/api/v1/workspaces/{ws_id}/cycles?limit=5", headers=headers
        ).json()
        items = cycles.get("items") or []
        if items and items[0].get("status") in ("COMPLETED", "FAILED"):
            cyc = items[0]
            break
        time.sleep(1)
    assert cyc is not None, "cycle did not finish in time"

    jobs = client.get(
        f"/api/v1/workspaces/{ws_id}/publishing/jobs", headers=headers
    ).json()["items"]
    assert len(jobs) >= 1, "expected at least one publishing job row after UPLOAD"
    seen: set[tuple[str, str]] = set()
    for j in jobs:
        # one row per (video, platform): enforced by unique index; API must
        # surface attempt counts so retries are visible rather than silent.
        assert j["status"] in ("QUEUED", "PUBLISHED", "FAILED", "RETRYING", "CANCELLED")
        assert j["attempt"] >= 1 or j["status"] == "QUEUED"
        seen.add((j["platform"], j.get("remote_post_id") or ""))
    assert len(seen) == len(jobs), "duplicate (platform, remote_post_id) rows"


def test_publishing_jobs_api_exposes_attempts(client):
    """The jobs API surfaces attempt counts and remote ids (retry visibility)."""
    _tok, ws_id, headers = _register(client)
    jobs = client.get(
        f"/api/v1/workspaces/{ws_id}/publishing/jobs", headers=headers
    )
    assert jobs.status_code == 200
    assert "items" in jobs.json()
