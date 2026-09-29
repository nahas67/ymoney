"""Work 11 Lane R: anchored comments (contracts §6, §13).

Locks the comment contract end-to-end over HTTP:

  * timestamp / time_range anchoring + validation (t_start required, t_end
    required and > t_start, non-negative) -> 422 on bad anchors
  * comments NEVER mutate content: tracks_json bytes unchanged after add/resolve
  * thread replies (same target, flat under the root) + resolve/reopen on ROOT
    comments only
  * mentions must be workspace members (else 422)
  * workspace isolation (a foreign target/comment never leaks)
  * version_ref is captured for timeline comments (context only)
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client):
    email = f"cm{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return (data["workspace"]["id"],
            {"Authorization": f"Bearer {data['access_token']}"},
            data["user"]["id"])


def _add_user(client, ws_id, role="member"):
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"m{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    r = client.post("/api/v1/auth/login",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, user_id


def _make_timeline(client, ws_id, headers):
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=headers,
                    json={"name": "main", "fps": 30.0, "duration_seconds": 10.0,
                          "aspect": "9:16", "tracks": None})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tracks_json(client, ws_id, headers, timeline_id):
    """Raw tracks_json of the tip (the exact bytes comments must not disturb)."""
    from app.db import session_scope
    from app.engine.timeline import tip_version

    with session_scope() as s:
        tip = tip_version(s, timeline_id)
        assert tip is not None
        return dict(tip.tracks_json or {})


def _as_bytes(doc) -> bytes:
    """Canonical serialization of a tracks doc (byte-level comparison)."""
    import json

    return json.dumps(doc, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _comment(client, ws_id, headers, **payload):
    body = {"target_type": "timeline", "target_id": "tl-1", "body": "hello"}
    body.update(payload)
    return client.post(f"/api/v1/workspaces/{ws_id}/comments", headers=headers, json=body)


# ---------------------------------------------------------------------------
# anchoring + validation
# ---------------------------------------------------------------------------


def test_timestamp_anchor_requires_t_start(client):
    ws, h, _ = _register(client)
    r = _comment(client, ws, h, target_type="timestamp", anchor={})
    assert r.status_code == 422, r.text
    assert "t_start" in r.json()["detail"]


def test_time_range_anchor_requires_t_end_and_ordering(client):
    ws, h, _ = _register(client)
    # missing t_end
    r = _comment(client, ws, h, target_type="time_range", anchor={"t_start": 1.0})
    assert r.status_code == 422, r.text
    # t_end <= t_start
    r = _comment(client, ws, h, target_type="time_range",
                 anchor={"t_start": 5.0, "t_end": 5.0})
    assert r.status_code == 422, r.text
    # negative t_start
    r = _comment(client, ws, h, target_type="timestamp", anchor={"t_start": -1.0})
    assert r.status_code == 422, r.text
    # valid range -> 201
    r = _comment(client, ws, h, target_type="time_range",
                 anchor={"t_start": 1.0, "t_end": 4.5}, body="trim here")
    assert r.status_code == 201, r.text
    assert r.json()["anchor"]["t_start"] == 1.0
    assert r.json()["anchor"]["t_end"] == 4.5


# ---------------------------------------------------------------------------
# comments never mutate content
# ---------------------------------------------------------------------------


def test_comments_never_mutate_tracks_json(client):
    ws, h, _ = _register(client)
    timeline_id = _make_timeline(client, ws, h)
    before = _tracks_json(client, ws, h, timeline_id)
    before_bytes = _as_bytes(before)

    r = _comment(client, ws, h, target_id=timeline_id, body="note on the cut")
    assert r.status_code == 201, r.text
    comment_id = r.json()["id"]

    # resolve it
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{comment_id}/resolve", headers=h)
    assert r.status_code == 200, r.text

    after = _tracks_json(client, ws, h, timeline_id)
    assert _as_bytes(after) == before_bytes  # byte-identical content


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


def test_thread_reply_same_target_and_flat_depth(client):
    ws, h, _ = _register(client)
    root = _comment(client, ws, h, target_id="tl-1", body="root").json()
    reply1 = _comment(client, ws, h, target_id="tl-1", body="reply", parent_id=root["id"]).json()
    # a reply to a reply is re-parented onto the root (flat thread)
    reply2 = _comment(client, ws, h, target_id="tl-1", body="reply2",
                      parent_id=reply1["id"]).json()
    assert reply1["parent_id"] == root["id"]
    assert reply2["parent_id"] == root["id"]


def test_reply_to_different_target_422(client):
    ws, h, _ = _register(client)
    root = _comment(client, ws, h, target_id="tl-1", body="root").json()
    r = _comment(client, ws, h, target_id="tl-2", body="elsewhere", parent_id=root["id"])
    assert r.status_code == 422, r.text


def test_resolve_only_root_comments(client):
    ws, h, _ = _register(client)
    root = _comment(client, ws, h, target_id="tl-1", body="root").json()
    reply = _comment(client, ws, h, target_id="tl-1", body="reply",
                     parent_id=root["id"]).json()
    # resolving a reply -> 422 (root comments only)
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{reply['id']}/resolve", headers=h)
    assert r.status_code == 422, r.text
    # resolving the root works
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{root['id']}/resolve", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["resolved_at"] != ""
    # reopen
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{root['id']}/reopen", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["resolved_at"] == ""


def test_resolved_hidden_unless_requested(client):
    ws, h, _ = _register(client)
    root = _comment(client, ws, h, target_id="tl-1", body="root").json()
    client.post(f"/api/v1/workspaces/{ws}/comments/{root['id']}/resolve", headers=h)
    r = client.get(f"/api/v1/workspaces/{ws}/comments",
                   headers=h, params={"target_type": "timeline", "target_id": "tl-1"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    r = client.get(f"/api/v1/workspaces/{ws}/comments", headers=h,
                   params={"target_type": "timeline", "target_id": "tl-1",
                           "include_resolved": True})
    assert len(r.json()["items"]) == 1


# ---------------------------------------------------------------------------
# mentions
# ---------------------------------------------------------------------------


def test_mentions_must_be_workspace_members(client):
    ws, h, _ = _register(client)
    _, other_id = _add_user(client, ws)
    # a real member mention -> ok
    r = _comment(client, ws, h, target_id="tl-1", body="ping", mentions=[other_id])
    assert r.status_code == 201, r.text
    assert r.json()["mentions"] == [other_id]
    # a non-member (including a foreign workspace user) -> 422
    ws_b, _, foreign_id = _register(client)
    assert foreign_id
    r = _comment(client, ws, h, target_id="tl-1", body="ping", mentions=[foreign_id])
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_comment_workspace_isolation(client):
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)
    root = _comment(client, ws_a, h_a, target_id="tl-1", body="private").json()

    # the comment is not visible in workspace B
    r = client.get(f"/api/v1/workspaces/{ws_b}/comments", headers=h_b,
                   params={"target_type": "timeline", "target_id": "tl-1"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    # resolving a foreign comment id -> 404, never 403
    r = client.post(f"/api/v1/workspaces/{ws_b}/comments/{root['id']}/resolve", headers=h_b)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# version_ref (context only)
# ---------------------------------------------------------------------------


def test_timeline_comment_captures_version_ref(client):
    ws, h, _ = _register(client)
    timeline_id = _make_timeline(client, ws, h)
    r = _comment(client, ws, h, target_id=timeline_id, body="versioned note")
    assert r.status_code == 201, r.text
    body = r.json()
    # version_ref is the timeline version at creation (a string), context only
    assert body["version_ref"] is not None
    assert isinstance(body["version_ref"], str)


# ---------------------------------------------------------------------------
# foreign target -> 404 (a target that exists in ANOTHER workspace)
# ---------------------------------------------------------------------------


def test_foreign_timeline_target_404(client):
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)
    timeline_id = _make_timeline(client, ws_a, h_a)

    # POST/GET on a foreign target -> 404 (never 403, never a partial list)
    r = _comment(client, ws_b, h_b, target_id=timeline_id, body="cross-ws")
    assert r.status_code == 404, r.text
    r = client.get(f"/api/v1/workspaces/{ws_b}/comments", headers=h_b,
                   params={"target_type": "timeline", "target_id": timeline_id})
    assert r.status_code == 404, r.text

    # the owning workspace still reads and writes normally
    r = _comment(client, ws_a, h_a, target_id=timeline_id, body="mine")
    assert r.status_code == 201, r.text
    r = client.get(f"/api/v1/workspaces/{ws_a}/comments", headers=h_a,
                   params={"target_type": "timeline", "target_id": timeline_id})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


# ---------------------------------------------------------------------------
# resolve/reopen cap matrix (actual rule: author OR `comment` cap)
# ---------------------------------------------------------------------------


def test_resolve_reopen_cap_matrix(client):
    ws, owner_h, _ = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws}/projects", headers=owner_h,
                    json={"name": "C", "description": ""})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    e_h, e_id = _add_user(client, ws, "member")
    r_h, r_id = _add_user(client, ws, "member")
    v_h, v_id = _add_user(client, ws, "member")
    for uid, role in ((e_id, "EDITOR"), (r_id, "REVIEWER"), (v_id, "VIEWER")):
        rr = client.post(f"/api/v1/workspaces/{ws}/projects/{project_id}/members",
                         headers=owner_h, json={"user_id": uid, "role": role})
        assert rr.status_code == 201, rr.text

    # comment authored by the project EDITOR (holds the comment cap)
    r = _comment(client, ws, e_h, target_id="tl-9", body="needs a fix",
                 project_id=project_id)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # demote the author to VIEWER -> the author branch still allows resolve
    rr = client.post(f"/api/v1/workspaces/{ws}/projects/{project_id}/members",
                     headers=owner_h, json={"user_id": e_id, "role": "VIEWER"})
    assert rr.status_code == 201, rr.text
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{cid}/resolve", headers=e_h)
    assert r.status_code == 200, r.text      # author bypass (no cap needed)

    # project VIEWER (not the author) -> 403: no comment cap
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{cid}/resolve", headers=v_h)
    assert r.status_code == 403, r.text
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{cid}/reopen", headers=v_h)
    assert r.status_code == 403, r.text

    # project REVIEWER (not the author) -> allowed: holds the comment cap
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{cid}/reopen", headers=r_h)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/workspaces/{ws}/comments/{cid}/resolve", headers=r_h)
    assert r.status_code == 200, r.text
