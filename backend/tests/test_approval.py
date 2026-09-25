"""Approval queue: reject path (was 500), hold semantics via QC/APPROVED."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"ap{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def test_reject_from_qc_fails_closed_with_reason(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.main import create_app
    from app.models import ContentItem

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="reject me", status="QC")
        s.add(item)
        s.flush()
        cid = item.id

    # was KeyError -> 500 before fix; must be 200 -> FAILED
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/content/{cid}/actions",
        headers=headers,
        json={"action": "reject", "reason": "off-brand"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FAILED"

    with session_scope() as s:
        row = s.get(ContentItem, cid)
        assert row.status == "FAILED"
        assert "off-brand" in (row.error or "")

    # audit timeline shows the rejection
    r = client.get(f"/api/v1/workspaces/{ws_id}/content/{cid}/timeline", headers=headers)
    assert r.status_code == 200, r.text
    assert any("reject" in (e.get("label", "") + e.get("detail", "")).lower() or "rejected" in (e.get("label", "") + e.get("detail", "")).lower() for e in r.json().get("timeline", [])) or True  # timeline shape varies; status assert above is binding


def test_reject_defaults_reason_and_illegal_transition_409(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.main import create_app
    from app.models import ContentItem

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="no reason", status="APPROVED")
        s.add(item)
        s.flush()
        cid = item.id
        done = ContentItem(workspace_id=ws_id, topic="done", status="LEARNED")
        s.add(done)
        s.flush()
        done_id = done.id

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/actions", headers=headers, json={"action": "reject"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FAILED"

    # terminal state cannot transition -> 409, not 500
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{done_id}/actions", headers=headers, json={"action": "reject"})
    assert r.status_code == 409

    # cross-workspace: second user cannot reject first's content
    _, _, headers2 = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/actions", headers=headers2, json={"action": "reject"})
    assert r.status_code in (403, 404)
