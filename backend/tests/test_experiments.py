"""Lane B experiments: state machine, honest stats, routes, isolation."""
from __future__ import annotations

import uuid

import pytest

from app.engine.performance.experiments import (
    ExperimentError,
    analyze_experiment,
    assign_arms,
    auto_assign,
    cancel_experiment,
    create_experiment,
    start_experiment,
)
from app.models.content import PostMetric, PublishedPost
from app.models.experiment import Experiment


def _make(db_session, ws_id, **kw):
    params = {"kind": "HOOK", "hypothesis": "q beats direct",
              "control": {"variant_ref": "pv-ctrl"},
              "variants": [{"variant_ref": "pv-var", "descriptor": "question hook"}],
              "primary_metric": "views", "minimum_sample": 60}
    params.update(kw)
    return create_experiment(db_session, ws_id, **params)


def _seed_posts(db_session, ws_id, ref, values, platform="youtube"):
    token = uuid.uuid4().hex[:8]
    for i, val in enumerate(values):
        post = PublishedPost(workspace_id=ws_id, content_item_id="ci-1",
                             video_id=f"v-{token}-{ref}-{i}", platform=platform,
                             platform_variant_id=ref)
        db_session.add(post)
        db_session.flush()
        db_session.add(PostMetric(post_id=post.id, views=int(val)))
    db_session.commit()


# --- engine: validation + state machine ------------------------------------

def test_create_validation(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    with pytest.raises(ExperimentError):
        _make(db_session, ws_id, kind="NOPE")
    with pytest.raises(ExperimentError):
        _make(db_session, ws_id, primary_metric="vibes")
    with pytest.raises(ExperimentError):
        _make(db_session, ws_id, variants=[])
    with pytest.raises(ExperimentError):
        _make(db_session, ws_id, control={"variant_ref": ""})
    with pytest.raises(ExperimentError):
        _make(db_session, ws_id,
              variants=[{"variant_ref": "pv-ctrl", "descriptor": "dup"}])
    row = _make(db_session, ws_id)
    assert row.status == "DRAFT"
    assert row.kind == "HOOK"


def test_start_cancel_transitions(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id)
    start_experiment(db_session, row)
    assert row.status == "RUNNING" and row.started_at is not None
    with pytest.raises(ExperimentError):
        start_experiment(db_session, row)
    cancel_experiment(db_session, row)
    assert row.status == "CANCELLED" and row.ended_at is not None
    with pytest.raises(ExperimentError):
        cancel_experiment(db_session, row)


def test_assign_helpers():
    assert auto_assign([], 2) == {}
    assert auto_assign(["a", "b", "c"], 1) == {"a": "control", "b": "variant_0",
                                              "c": "control"}
    exp = Experiment(control_json={"variant_ref": "c"},
                     variants_json=[{"variant_ref": "v", "descriptor": "d"}])
    assert assign_arms(exp) == {"c": "control", "v": "variant_0"}


# --- engine: analysis -------------------------------------------------------

def test_analyze_winner(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id, minimum_sample=60)
    start_experiment(db_session, row)
    _seed_posts(db_session, ws_id, "pv-ctrl", [100 + (i % 5) for i in range(30)])
    _seed_posts(db_session, ws_id, "pv-var", [160 + (i % 5) for i in range(30)])

    analyze_experiment(db_session, row)

    assert row.status == "COMPLETED"
    assert row.result_json["winner"] == "pv-var"
    assert row.result_json["control"]["n"] == 30
    assert row.result_json["arms"][0]["significant"] is True
    assert row.confidence == "95% CI excludes zero"


def test_analyze_insufficient(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id, minimum_sample=60)
    start_experiment(db_session, row)
    _seed_posts(db_session, ws_id, "pv-ctrl", [10, 12])
    _seed_posts(db_session, ws_id, "pv-var", [50, 55])

    analyze_experiment(db_session, row)

    assert row.status == "INSUFFICIENT_DATA"
    assert "minimum_sample" in row.result_json["reason"]
    assert row.confidence == "n/a"


def test_analyze_inconclusive_small_n(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id, minimum_sample=10)
    start_experiment(db_session, row)
    _seed_posts(db_session, ws_id, "pv-ctrl", [100 + i for i in range(5)])
    _seed_posts(db_session, ws_id, "pv-var", [200 + i for i in range(5)])

    analyze_experiment(db_session, row)

    assert row.status == "INCONCLUSIVE"
    assert "30" in row.result_json["reason"]
    assert row.confidence == "n/a"


def test_analyze_inconclusive_no_lift(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id, minimum_sample=60)
    start_experiment(db_session, row)
    vals = [100 + (i % 5) for i in range(30)]
    _seed_posts(db_session, ws_id, "pv-ctrl", vals)
    _seed_posts(db_session, ws_id, "pv-var", list(vals))

    analyze_experiment(db_session, row)

    assert row.status == "INCONCLUSIVE"
    assert row.result_json["winner"] is None
    assert "zero" in row.result_json["reason"]


def test_analyze_requires_running(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    row = _make(db_session, ws_id)
    with pytest.raises(ExperimentError):
        analyze_experiment(db_session, row)


# --- routes -----------------------------------------------------------------

def _register(client, email=None):
    email = email or f"ex{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _payload(**kw):
    body = {"kind": "TITLE", "hypothesis": "numbers win",
            "control": {"variant_ref": "pv-ctrl"},
            "variants": [{"variant_ref": "pv-var", "descriptor": "numbered"}],
            "primary_metric": "views", "minimum_sample": 4}
    body.update(kw)
    return body


def test_routes_crud_start_cancel(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/experiments"

    r = client.post(base, headers=headers, json=_payload(kind="NOPE"))
    assert r.status_code == 422, r.text

    r = client.post(base, headers=headers, json=_payload())
    assert r.status_code == 200, r.text
    exp_id = r.json()["id"]
    assert r.json()["status"] == "DRAFT"

    r = client.get(base, headers=headers)
    assert r.status_code == 200 and r.json()["total"] == 1

    r = client.get(f"{base}/{exp_id}", headers=headers)
    assert r.status_code == 200 and r.json()["kind"] == "TITLE"

    r = client.post(f"{base}/{exp_id}/analyze", headers=headers)
    assert r.status_code == 422  # analyze from DRAFT is rejected

    r = client.post(f"{base}/{exp_id}/start", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "RUNNING"

    r = client.post(f"{base}/{exp_id}/start", headers=headers)
    assert r.status_code == 422

    r = client.post(f"{base}/{exp_id}/cancel", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"


def test_route_analyze_transitions(tmp_path, monkeypatch):
    from app.db import session_scope

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/experiments"

    r = client.post(base, headers=headers, json=_payload(minimum_sample=60))
    exp_id = r.json()["id"]
    client.post(f"{base}/{exp_id}/start", headers=headers)

    with session_scope() as db:
        token = uuid.uuid4().hex[:8]
        for i in range(3):
            for ref, val in (("pv-ctrl", 10), ("pv-var", 90)):
                post = PublishedPost(workspace_id=ws_id, content_item_id="ci",
                                     video_id=f"v-{token}-{ref}-{i}", platform="youtube",
                                     platform_variant_id=ref)
                db.add(post)
                db.flush()
                db.add(PostMetric(post_id=post.id, views=val))

    r = client.post(f"{base}/{exp_id}/analyze", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "INSUFFICIENT_DATA"
    assert r.json()["confidence"] == "n/a"


def test_routes_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)
    base_a = f"/api/v1/workspaces/{ws_a}/experiments"
    base_b = f"/api/v1/workspaces/{ws_b}/experiments"

    r = client.post(base_a, headers=headers_a, json=_payload())
    exp_id = r.json()["id"]

    r = client.get(f"{base_b}/{exp_id}", headers=headers_b)
    assert r.status_code == 404
    r = client.get(base_b, headers=headers_b)
    assert r.status_code == 200 and r.json()["total"] == 0
    r = client.post(f"{base_b}/{exp_id}/start", headers=headers_b)
    assert r.status_code == 404
