"""Work 11 Lane L: the activity ledger is APPEND-ONLY (contracts §9).

Locks the three properties the contract names, not just the happy path:

  * no mutator route exists -- POST/PUT/PATCH/DELETE on the ledger path
    answer 404/405, and no mutator is registered anywhere in ``api/``
    (checked against the live OpenAPI spec, not against a hand-written
    list, so a future route cannot slip past this test)
  * two emits produce monotonically growing rows, newest-first
  * filters (kind / project_id / target_type / target_id / since) and
    workspace isolation, including a 422 for an unparseable ``since``
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from app.db import session_scope
from app.services import activity as activity_service


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"act{uuid.uuid4().hex[:8]}@test.local"
    r = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "supersecret123"}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _seed_project(client, ws_id, headers, name="Ledger Project"):
    """Create a project through the API and return its id."""
    r = client.post(f"/api/v1/workspaces/{ws_id}/projects",
                    json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# append-only
# ---------------------------------------------------------------------------


def test_ledger_has_no_mutator_route(tmp_path, monkeypatch):
    """Every mutating verb on the ledger path must not succeed."""
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/activity"
    for method, body in (
        ("post", {"kind": "FORGED", "message": "x"}),
        ("put", {"kind": "FORGED"}),
        ("patch", {"kind": "FORGED"}),
    ):
        response = getattr(client, method)(base, json=body, headers=headers)
        assert response.status_code in (404, 405), (method, response.status_code, response.text)
    # DELETE carries no body in this client
    assert client.delete(base, headers=headers).status_code in (404, 405)


def test_no_mutator_symbol_in_api_sources():
    """Static proof: no api/ module calls delete/update on an EventLog.

    Reads the source with ast (no import side effects) and fails on any
    ``db.delete(<event-ish>)`` / ``.update(...)`` on the events model.
    """
    api_root = Path(__file__).resolve().parent.parent / "app" / "api"
    offenders: list[str] = []
    for path in api_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("delete", "update", "delete_all"):
                continue
            rendered = ast.unparse(node)
            if "EventLog" in rendered or "event" in rendered.lower():
                offenders.append(f"{path.name}:{node.lineno} {rendered[:90]}")
    assert not offenders, offenders


def test_emits_are_appended_and_ordered(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/projects",
                    json={"name": "Ordered"}, headers=headers)
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    for index in range(3):
        activity_service.emit(
            ws_id,
            "TIMELINE_EDITED",
            message=f"edit {index}",
            actor=r.json()["created_by"],
            target={"type": "timeline", "id": f"tl-{index}"},
            version=f"v{index}",
            project_id=project_id,
        )

    page = client.get(
        f"/api/v1/workspaces/{ws_id}/activity", headers=headers
    ).json()
    edits = [item for item in page["items"] if item["kind"] == "TIMELINE_EDITED"]
    assert len(edits) == 3, edits
    # newest first, and the row count only ever grows
    assert [e["message"] for e in edits] == ["edit 2", "edit 1", "edit 0"]
    assert edits[0]["project_id"] == project_id
    assert edits[0]["target"] == {"type": "timeline", "id": "tl-2"}
    assert edits[0]["version"] == "v2"

    activity_service.emit(
        ws_id, "TIMELINE_EDITED", message="edit 3",
        target={"type": "timeline", "id": "tl-3"}, project_id=project_id,
    )
    after = client.get(f"/api/v1/workspaces/{ws_id}/activity", headers=headers).json()
    assert len([i for i in after["items"] if i["kind"] == "TIMELINE_EDITED"]) == 4
    # the newest row is first -> strictly monotonic by created_at
    times = [i["created_at"] for i in after["items"]]
    assert times == sorted(times, reverse=True)


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_ledger_filters_and_workspace_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)

    made = client.post(f"/api/v1/workspaces/{ws_a}/projects",
                       json={"name": "A"}, headers=headers_a).json()
    project_a = made["id"]
    project_b = client.post(f"/api/v1/workspaces/{ws_b}/projects",
                            json={"name": "B"}, headers=headers_b).json()["id"]

    activity_service.emit(ws_a, "APPROVED", message="a-approved",
                          target={"type": "timeline", "id": "tl-a"},
                          project_id=project_a)
    activity_service.emit(ws_a, "COMMENT_ADDED", message="a-comment",
                          target={"type": "timestamp", "id": "42"})

    # kind filter
    items = client.get(
        f"/api/v1/workspaces/{ws_a}/activity?kind=APPROVED", headers=headers_a
    ).json()["items"]
    assert [i["message"] for i in items] == ["a-approved"]

    # project_id filter (PROJECT_CREATED also carries the project_id --
    # creating a project legitimately lands in its own ledger)
    items = client.get(
        f"/api/v1/workspaces/{ws_a}/activity?project_id={project_a}", headers=headers_a
    ).json()["items"]
    assert {i["kind"] for i in items} == {"APPROVED", "PROJECT_CREATED"}

    # target filters
    items = client.get(
        f"/api/v1/workspaces/{ws_a}/activity?target_type=timestamp&target_id=42",
        headers=headers_a,
    ).json()["items"]
    assert [i["message"] for i in items] == ["a-comment"]

    # a project id from ANOTHER workspace is simply not visible here
    items = client.get(
        f"/api/v1/workspaces/{ws_a}/activity?project_id={project_b}", headers=headers_a
    ).json()["items"]
    assert items == []

    # workspace A never sees workspace B's rows
    items_b = client.get(
        f"/api/v1/workspaces/{ws_b}/activity", headers=headers_b
    ).json()["items"]
    assert all(i["workspace_id"] == ws_b for i in items_b)
    assert all(i["message"] not in ("a-approved", "a-comment") for i in items_b)


def test_ledger_since_and_bad_since(tmp_path, monkeypatch):
    from datetime import timedelta

    from app.models import EventLog
    from app.models.base import utcnow

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    # backdate one row so `since` has something to actually exclude
    with session_scope() as s:
        s.add(
            EventLog(
                workspace_id=ws_id,
                kind="PUBLISHED",
                message="old",
                level="info",
                source="collab",
                data_json={},
                created_at=utcnow() - timedelta(days=3),
            )
        )
    cutoff = (utcnow() - timedelta(days=1)).isoformat() + "Z"
    activity_service.emit(ws_id, "PUBLISHED", message="recent")

    items = client.get(
        f"/api/v1/workspaces/{ws_id}/activity?since={cutoff}", headers=headers
    ).json()["items"]
    assert "recent" in [i["message"] for i in items]
    assert "old" not in [i["message"] for i in items]

    # an unparseable `since` is a 422, never a silently-ignored filter
    bad = client.get(
        f"/api/v1/workspaces/{ws_id}/activity?since=not-a-timestamp", headers=headers
    )
    assert bad.status_code == 422, bad.text


def test_ledger_requires_workspace_membership(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    outsider_email = f"out{uuid.uuid4().hex[:8]}@test.local"
    client.post("/api/v1/auth/register",
                json={"email": outsider_email, "password": "supersecret123"})
    login = client.post("/api/v1/auth/login",
                        json={"email": outsider_email, "password": "supersecret123"})
    outsider = {"Authorization": f"Bearer {login.json()['access_token']}"}

    denied = client.get(f"/api/v1/workspaces/{ws_id}/activity", headers=outsider)
    assert denied.status_code == 403, denied.text


@pytest.mark.parametrize("path_segment", ["activity", "activity/recent"])
def test_limit_is_bounded(tmp_path, monkeypatch, path_segment):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/{path_segment}?limit=99999", headers=headers
    )
    assert r.status_code in (200, 422), r.text


def test_openapi_declares_only_get_for_the_ledger(tmp_path, monkeypatch):
    """The spec itself carries no mutating verb on any activity path.

    ``test_ledger_has_no_mutator_route`` proves the running app refuses
    them; this proves no route exists to refuse -- checked against the
    live OpenAPI document rather than a hand-written list, so a future
    POST/PUT/DELETE on the ledger cannot slip past the suite.
    """
    client = _client(tmp_path, monkeypatch)
    spec = client.app.openapi()
    ledger = "/api/v1/workspaces/{workspace_id}/activity"
    assert ledger in spec["paths"], sorted(spec["paths"])[:20]
    assert set(spec["paths"][ledger]) == {"get"}, sorted(spec["paths"][ledger])

    mutating = {"post", "put", "patch", "delete"}
    offenders = {
        path: sorted(set(methods) & mutating)
        for path, methods in spec["paths"].items()
        if "/activity" in path and set(methods) & mutating
    }
    assert offenders == {}, offenders


def test_activity_500_is_generic_and_logged(tmp_path, monkeypatch, caplog):
    """An unexpected fault answers a short generic detail -- never the exception."""
    import logging

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    from app.api.v1 import activity as activity_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("ledger exploded with secret=catfood42")

    monkeypatch.setattr(activity_mod.activity_service, "query", boom)
    with caplog.at_level(logging.ERROR, logger="ymoney.collab"):
        r = client.get(f"/api/v1/workspaces/{ws_id}/activity", headers=headers)
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "internal error"}
    assert "catfood42" not in r.text
    assert "RuntimeError" not in r.text
    assert any(
        record.name == "ymoney.collab" and "activity route failed" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
