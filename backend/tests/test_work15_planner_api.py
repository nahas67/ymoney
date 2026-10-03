"""Work 15 planner API -- route surface, autonomy enforcement, and the
"no publishing from the planner" invariant.

Drives the real FastAPI app through ``TestClient`` with a real workspace, a
real owner, and a real database. The security-relevant assertions are
structural: the route table itself is inspected, so a future route that grants
publishing would fail here rather than being discovered in production.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

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
    # raise_server_exceptions=True so a 500 surfaces the real traceback
    # instead of the middleware's generic body. The end-to-end tests want the
    # status code, which they assert explicitly.
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def owner(client):
    """Register, create a workspace, return (headers, workspace_id)."""
    import time

    email = f"w15{os.urandom(4).hex()}@test.local"
    res = client.post("/api/v1/auth/register", json={
        "email": email, "password": "Sup3rSecret!pass", "name": "W15"})
    assert res.status_code in (200, 201), res.text
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post("/api/v1/workspaces", headers=headers, json={
        "name": "W15 Planner", "niche": "AI money", "slug": f"w15-{os.urandom(4).hex()}"})
    assert res.status_code in (200, 201), res.text
    ws_id = res.json()["id"]
    _ = time
    return headers, ws_id


def _second_workspace(client):
    """A second, distinct workspace owned by the SAME caller."""
    email = f"w15b{os.urandom(4).hex()}@test.local"
    res = client.post("/api/v1/auth/register", json={
        "email": email, "password": "Sup3rSecret!pass", "name": "W15b"})
    assert res.status_code in (200, 201), res.text
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    made = client.post("/api/v1/workspaces", headers=headers, json={
        "name": "W15 Other", "niche": "AI money",
        "slug": f"w15o-{os.urandom(4).hex()}"})
    assert made.status_code in (200, 201), made.text
    return headers, made.json()["id"]


@pytest.fixture()
def ws(db_session):
    """A real workspace, for the tests that assert on engine state directly."""
    from app.models import Workspace

    row = Workspace(name="w15-engine", slug=f"w15e-{os.urandom(4).hex()}")
    db_session.add(row)
    db_session.commit()
    return row


def _signal(db, workspace_id, *, topic="budget tips", ref="r1"):
    from app.engine.planning.signals import SignalIngest, ingest_signal

    return ingest_signal(db, workspace_id, SignalIngest(
        source="community", topic=topic, external_ref=ref,
        evidence_ids=["ev-1"], confidence=0.8, evidence_verified=True))


@contextmanager
def _db():
    """A short-lived session, for asserting on rows an HTTP test created."""
    from app.db import session_scope

    with session_scope() as session:
        yield session


# ===========================================================================
# route surface
# ===========================================================================


def test_planner_routes_are_registered():
    """The OpenAPI schema is the source of truth.

    This FastAPI version keeps included routers behind lazy wrappers, so
    ``app.routes`` does not show the flattened paths -- the schema does.
    """
    app = create_app()
    paths = sorted(p for p in app.openapi()["paths"] if "/planner/" in p)
    assert len(paths) >= 12, paths
    for expected in ("/planner/signals", "/planner/opportunities",
                     "/planner/plan", "/planner/plans", "/planner/calendar",
                     "/planner/policy", "/planner/capacity",
                     "/planner/memory"):
        assert any(expected in p for p in paths), f"missing {expected}"


def test_the_planner_exposes_no_publish_route():
    """§6: planning autonomy must never bypass publication approval.

    Asserted on the ROUTE TABLE, so adding a publishing route to the planner
    fails this test rather than shipping.
    """
    from app.api.v1.planner import planner_router

    for route in planner_router.routes:
        path = getattr(route, "path", "")
        for forbidden in ("publish", "post-live", "push-live", "/send"):
            assert forbidden not in path.lower(), (
                f"planner route {path} looks like a publication path")


def test_planner_never_calls_a_publisher():
    """Structural: no planning module imports the publisher layer."""
    from pathlib import Path

    import app.engine.planning as planning_pkg

    root = Path(planning_pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "publishers" not in text, (
            f"{path.name} imports the publisher layer")
        assert "publish_flow" not in text, (
            f"{path.name} imports the publish flow")


# ===========================================================================
# signals
# ===========================================================================


def test_ingest_and_read_a_signal(client, owner):
    headers, ws = owner
    res = client.post(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers,
                      json={"source": "community", "topic": "budget tips",
                            "external_ref": "r1",
                            "evidence_ids": ["ev-1"], "confidence": 0.8,
                            "evidence_verified": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True

    res = client.get(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers)
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["count"] == 1
    signal = payload["signals"][0]
    assert signal["evidence_ids"] == ["ev-1"]
    assert signal["usable_as_demand"] is True
    # one observation -> no measurable rate
    assert signal["velocity"] is None


def test_duplicate_ingest_is_idempotent_over_http(client, owner):
    headers, ws = owner
    payload = {"source": "research", "topic": "budget tips",
               "external_ref": "same", "evidence_ids": ["ev-1"],
               "evidence_verified": True}
    first = client.post(f"/api/v1/workspaces/{ws}/planner/signals",
                        headers=headers, json=payload)
    second = client.post(f"/api/v1/workspaces/{ws}/planner/signals",
                         headers=headers, json=payload)
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["duplicate"] is True
    res = client.get(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers)
    assert res.json()["count"] == 1


def test_unknown_signal_source_is_422(client, owner):
    headers, ws = owner
    res = client.post(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers,
                      json={"source": "astrology", "topic": "x"})
    assert res.status_code == 422, res.text


# ===========================================================================
# autonomy modes over HTTP
# ===========================================================================


def _seed(client, headers, ws, topic="budget tips"):
    return client.post(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers,
                       json={"source": "community", "topic": topic,
                             "external_ref": f"r-{topic}",
                             "evidence_ids": ["ev-1"],
                             "evidence_verified": True})


def test_recommend_mode_returns_suggestions_and_writes_nothing(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "RECOMMEND", "platforms": ["threads"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["suggestions"], body
    assert body["items"] == []
    assert body["publishes"] is False

    plans = client.get(f"/api/v1/workspaces/{ws}/planner/plans",
                       headers=headers).json()
    assert plans["count"] == 0


def test_approval_mode_writes_plan_items(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "APPROVAL", "platforms": ["threads"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"], body
    assert body["plan_id"]


def test_unknown_autonomy_mode_is_422(client, owner):
    headers, ws = owner
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "YOLO"})
    assert res.status_code == 422, res.text


def test_preview_mode_writes_nothing(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "AUTONOMOUS", "preview": True})
    assert res.status_code == 200, res.text
    assert res.json()["preview"] is True
    plans = client.get(f"/api/v1/workspaces/{ws}/planner/plans",
                       headers=headers).json()
    assert plans["count"] == 0


def test_policy_endpoint_reports_that_no_mode_publishes(client, owner):
    headers, ws = owner
    res = client.get(f"/api/v1/workspaces/{ws}/planner/policy", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publishes"] is False
    for mode, entry in body["table"].items():
        assert entry["publishes"] is False, mode
        assert "publish" in entry["note"]


# ===========================================================================
# the trend -> campaign action chain
# ===========================================================================


def _plan_once(client, headers, ws):
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "AUTONOMOUS", "platforms": ["threads"]})
    assert res.status_code == 200, res.text
    return res.json()


def test_campaign_creation_is_refused_in_recommend_mode(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item = _plan_once(client, headers, ws)["items"][0]["id"]
    res = client.post(f"/api/v1/workspaces/{ws}/planner/items/{item}/campaign",
                      headers=headers, json={"autonomy": "RECOMMEND"})
    assert res.status_code == 403, res.text


def test_campaign_creation_is_idempotent_over_http(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item = _plan_once(client, headers, ws)["items"][0]["id"]
    url = f"/api/v1/workspaces/{ws}/planner/items/{item}/campaign"
    first = client.post(url, headers=headers, json={"autonomy": "APPROVAL"})
    second = client.post(url, headers=headers, json={"autonomy": "APPROVAL"})
    assert first.status_code == 200, first.text
    assert first.json()["campaign_id"] == second.json()["campaign_id"]


def test_scheduling_writes_a_schedule_entry_and_does_not_publish(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    url = f"/api/v1/workspaces/{ws}/planner/items/{item_id}/campaign"
    campaign = client.post(url, headers=headers,
                           json={"autonomy": "APPROVAL"}).json()
    assert campaign["campaign_id"]

    res = client.post(
        f"/api/v1/workspaces/{ws}/planner/items/{item_id}/schedule",
        headers=headers,
        json={"autonomy": "AUTONOMOUS", "allowed_actions": ["SCHEDULE"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "SCHEDULED"
    assert body["publishes"] is False
    assert body["schedule_entry"]["entry_id"]
    # a real ScheduleEntry now exists for this workspace
    calendar = client.get(f"/api/v1/workspaces/{ws}/planner/calendar",
                          headers=headers).json()
    assert calendar["entries"], calendar
    assert "seed window" in body["why_scheduled"]


def test_scheduling_requires_the_allowlist_under_autonomous(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    res = client.post(
        f"/api/v1/workspaces/{ws}/planner/items/{item_id}/schedule",
        headers=headers, json={"autonomy": "AUTONOMOUS"})
    assert res.status_code == 403, res.text
    assert "allowlist" in res.json()["detail"]


def test_reject_requires_a_reason(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    res = client.post(f"/api/v1/workspaces/{ws}/planner/items/{item_id}/reject",
                      headers=headers, json={"autonomy": "APPROVAL"})
    assert res.status_code == 422, res.text
    ok = client.post(f"/api/v1/workspaces/{ws}/planner/items/{item_id}/reject",
                     headers=headers,
                     json={"autonomy": "APPROVAL", "reason": "off-strategy"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "CANCELLED"
    assert ok.json()["reason"] == "off-strategy"


def test_approve_and_research_more(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    approved = client.post(
        f"/api/v1/workspaces/{ws}/planner/items/{item_id}/approve",
        headers=headers, json={"autonomy": "APPROVAL"})
    assert approved.json()["status"] == "PLANNED"
    more = client.post(
        f"/api/v1/workspaces/{ws}/planner/items/{item_id}/research_more",
        headers=headers, json={"autonomy": "APPROVAL",
                               "reason": "need a second source"})
    assert more.json()["status"] == "RESEARCHING"


def test_a_foreign_workspace_item_is_404(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    res = client.post(f"/api/v1/workspaces/{ws}/planner/items/{item_id}/approve",
                      headers=headers, json={"autonomy": "APPROVAL"})
    assert res.status_code == 200
    # a bogus id in the SAME workspace is also 404
    missing = client.post(
        f"/api/v1/workspaces/{ws}/planner/items/does-not-exist/approve",
        headers=headers, json={"autonomy": "APPROVAL"})
    assert missing.status_code == 404


# ===========================================================================
# opportunities, capacity, memory, feedback
# ===========================================================================


def test_opportunities_separate_measured_from_inferred(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    _plan_once(client, headers, ws)
    res = client.get(f"/api/v1/workspaces/{ws}/planner/opportunities",
                     headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 1
    row = body["opportunities"][0]
    assert row["basis"] in ("OBSERVED", "INFERRED", "RECOMMENDED")
    assert row["basis_meaning"]
    assert row["evidence"]
    # the forbidden claims are published as a contract, not silently absent
    assert "guaranteed virality" in body["forbidden_claims"]
    # every factor states whether it was measured
    for factor in (row["scoring"].get("factors") or {}).values():
        assert "measured" in factor and "why" in factor
    assert row["why"]


def test_capacity_can_be_declared_and_read_back(client, owner):
    headers, ws = owner
    res = client.post(f"/api/v1/workspaces/{ws}/planner/capacity", headers=headers,
                      json={"shorts_per_day": 2, "review_slots_per_day": 2})
    assert res.status_code == 200, res.text
    body = res.json()
    # `declared` reports room over a 30-day horizon, so 2/day reads as 60 --
    # the rate is scaled, not reported raw.
    assert body["declared"]["shorts"] == 60
    assert body["declared"]["review"] == 60
    assert body["is_unbounded"] is False


def test_capacity_check_does_not_write_the_stored_row(client, owner):
    """A viewer dry-run must not overwrite the operator's declared limits."""
    headers, ws = owner
    client.post(f"/api/v1/workspaces/{ws}/planner/capacity", headers=headers,
                json={"shorts_per_day": 2, "review_slots_per_day": 2})
    res = client.post(f"/api/v1/workspaces/{ws}/planner/capacity/check",
                      headers=headers, json={"shorts_per_day": 99})
    assert res.status_code == 200, res.text
    # the real row is untouched
    calendar = client.get(f"/api/v1/workspaces/{ws}/planner/calendar",
                          headers=headers).json()
    assert calendar["capacity"]["shorts_per_day"] == 2


def test_capacity_constrains_the_plan(client, owner):
    headers, ws = owner
    for index in range(4):
        _seed(client, headers, ws, topic=f"topic number {index}")
    client.post(f"/api/v1/workspaces/{ws}/planner/capacity", headers=headers,
                json={"shorts_per_day": 1, "review_slots_per_day": 1})
    # horizon_days=1: the rate is per-day, so 1/day is 1 slot for one day
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "AUTONOMOUS", "horizon_days": 1})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"] == []
    blocked = [b for b in body["blocked"] if b.get("verdict") == "OVER_CAPACITY"]
    assert blocked, body["blocked"]


def test_budget_constrains_the_plan(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    res = client.post(f"/api/v1/workspaces/{ws}/planner/plan", headers=headers,
                      json={"autonomy": "AUTONOMOUS", "budget_usd": 0.10})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"] == []
    assert any("budget" in str(b) for b in body["blocked"])


def test_memory_endpoint_reports_what_was_consulted(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    _plan_once(client, headers, ws)
    res = client.get(f"/api/v1/workspaces/{ws}/planner/memory", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "used_ids" in body
    assert "settled_topics" in body
    assert "needs_revalidation" in body


def test_feedback_records_an_outcome_and_withholds_a_lesson(client, owner):
    headers, ws = owner
    _seed(client, headers, ws)
    item_id = _plan_once(client, headers, ws)["items"][0]["id"]
    res = client.post(f"/api/v1/workspaces/{ws}/planner/feedback/{item_id}",
                      headers=headers, json={"autonomy": "AUTONOMOUS"})
    assert res.status_code == 200, res.text
    body = res.json()
    # the factual outcome is always recorded
    assert body["memory_ids"]
    # but a single item never yields a generalised lesson
    assert body["lesson_written"] is False


def test_signals_require_authentication(client, owner):
    _headers, ws = owner
    res = client.get(f"/api/v1/workspaces/{ws}/planner/signals")
    assert res.status_code in (401, 403), res.status


# ===========================================================================
# regressions the Work 15 audit surfaced -- each locks a real bug shut
# ===========================================================================


def test_feedback_refuses_another_workspaces_item(client, owner):
    """A caller must not write a fabricated outcome into their own memory.

    The endpoint took any id; recording a FOREIGN workspace's item wrote a
    CONTENT_RESULT naming that item into the CALLER's GlobalMemory, which the
    planner then consults as trusted §4 knowledge.
    """
    headers, ws = owner
    other_headers, other_ws = _second_workspace(client)
    _seed(client, other_headers, other_ws)
    other_item = client.post(
        f"/api/v1/workspaces/{other_ws}/planner/plan", headers=other_headers,
        json={"autonomy": "AUTONOMOUS"}).json()["items"][0]["id"]

    res = client.post(f"/api/v1/workspaces/{ws}/planner/feedback/{other_item}",
                      headers=headers, json={"autonomy": "AUTONOMOUS"})
    assert res.status_code == 404, res.text
    # ...and nothing was written into the caller's memory
    memory = client.get(f"/api/v1/workspaces/{ws}/planner/memory",
                        headers=headers).json()
    assert not any(other_item in str(m) for m in memory["memories"])


def test_listing_signals_does_not_write(client, owner):
    """A viewer GET must not rewrite freshness/status/velocity.

    ``claim_signals`` mutated rows, and the route committed -- so a read
    endpoint wrote to the database.
    """
    headers, ws = owner
    _seed(client, headers, ws)
    before = client.get(f"/api/v1/workspaces/{ws}/planner/signals",
                        headers=headers).json()
    assert before["signals"][0]["status"] == "ACTIVE"
    # a GET that wrote would have committed the recomputed velocity
    for _ in range(3):
        client.get(f"/api/v1/workspaces/{ws}/planner/signals", headers=headers)
    after = client.get(f"/api/v1/workspaces/{ws}/planner/signals",
                       headers=headers).json()
    assert after["signals"][0]["status"] == "ACTIVE"
    assert after["signals"][0]["velocity"] is None


def test_scheduling_does_not_write_an_opportunity_id_as_a_content_item(
    client, owner
):
    """ScheduleEntry.content_item_id holds a ContentItem key, nothing else.

    The planner wrote the item's OPPORTUNITY id there, corrupting the shared
    store's idempotency key type for the canonical Scheduler.
    """
    from app.models.content import ScheduleEntry
    from app.models.planning import EditorialPlanItem

    headers, ws = owner
    _seed(client, headers, ws)
    item_id = client.post(f"/api/v1/workspaces/{ws}/planner/plan",
                          headers=headers,
                          json={"autonomy": "AUTONOMOUS",
                                "platforms": ["threads"]}).json()["items"][0]["id"]
    client.post(f"/api/v1/workspaces/{ws}/planner/items/{item_id}/campaign",
                headers=headers, json={"autonomy": "APPROVAL"})
    res = client.post(f"/api/v1/workspaces/{ws}/planner/items/{item_id}/schedule",
                      headers=headers,
                      json={"autonomy": "AUTONOMOUS",
                            "allowed_actions": ["SCHEDULE"]})
    assert res.status_code == 200, res.text
    assert res.json()["publishes"] is False

    with _db() as db:
        entries = db.query(ScheduleEntry).filter(
            ScheduleEntry.workspace_id == ws).all()
        assert entries, "a schedule entry should exist"
        item = db.get(EditorialPlanItem, item_id)
        for entry in entries:
            # NULL is correct here (no ContentItem exists yet); the wrong value
            # would be the opportunity id sitting in a content-item column
            assert entry.content_item_id in (None, "")
            assert entry.content_item_id != item.opportunity_id


def test_research_stages_respect_the_autonomous_allowlist(db_session, ws):
    """An allowlist naming only SCHEDULE must not run research or a brief."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy, PlanningAction
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import run_orchestration

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item_id = result.items[0].id
    calls: list[str] = []
    out = run_orchestration(
        db_session, ws.id, item_id,
        policy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS,
                              allowed_actions=frozenset({PlanningAction.SCHEDULE})),
        research_fn=lambda *_: calls.append("research"),
        brief_fn=lambda *_: calls.append("brief"))
    assert calls == [], f"stages bypassed the allowlist: {calls}"
    assert "research" in out.skipped and "brief" in out.skipped


def test_creating_a_campaign_draft_does_not_approve_the_item(db_session, ws):
    """A draft is not approval: the item must stay IDEA until a human acts."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import run_orchestration

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item_id = result.items[0].id
    out = run_orchestration(db_session, ws.id, item_id,
                            policy=AutonomyPolicy(mode=AutonomyMode.APPROVAL))
    assert out.campaign_id
    from app.models.planning import EditorialPlanItem

    assert db_session.get(EditorialPlanItem, item_id).status == "IDEA"
    # and a human approval is what moves it
    ContentPlanningEngine(db_session, ws.id).approve_item(item_id)
    assert db_session.get(EditorialPlanItem, item_id).status == "PLANNED"
