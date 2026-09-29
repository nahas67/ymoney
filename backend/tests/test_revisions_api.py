"""Work 11 Lane R: revision request lifecycle (contracts §7, §13).

Locks the revision contract end-to-end over HTTP:

  * revision request lifecycle (OPEN -> ADDRESSED | DISMISSED, reopen)
  * **NO auto-transition**: editing a timeline does NOT flip OPEN -> ADDRESSED
  * kind enum + non-empty description validation -> 422
  * explicit set_state is the only state mover; illegal moves -> 409
  * workspace isolation (foreign revision -> 404)
  * ADDRESSED requires the reviewer-side cap; the matrix is asserted
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
    email = f"rv{uuid.uuid4().hex[:8]}@test.local"
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

    email = f"r{uuid.uuid4().hex[:8]}@test.local"
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
    return r.json()["id"], r.json()["version"]


def _tracks(clip_id, duration=5.0):
    return [{
        "id": "t_video", "kind": "video", "name": "Video",
        "clips": [{"id": clip_id, "name": clip_id, "start": 0.0, "duration": duration,
                   "source": {"asset_id": "a1"}, "effects": [], "source_start": 0.0,
                   "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                   "transform": {}, "text": {}, "transition_in": "cut",
                   "transition_out": "cut"}],
    }]


def _create_revision(client, ws_id, headers, *, items=None, **extra):
    if items is None:
        items = [{"kind": "trim", "description": "tighten the intro"}]
    body = {"items": items,
            "target_type": "timeline_version", "target_id": "tl-1"}
    body.update(extra)
    return client.post(f"/api/v1/workspaces/{ws_id}/revisions", headers=headers, json=body)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_revision_lifecycle_open_addressed_reopen(client):
    ws, h, _ = _register(client)
    r = _create_revision(client, ws, h)
    assert r.status_code == 201, r.text
    rev = r.json()["items"][0]
    assert rev["state"] == "OPEN"
    assert rev["items"][0]["kind"] == "trim"

    # OPEN -> ADDRESSED
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=h, json={"state": "ADDRESSED"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "ADDRESSED"
    assert body["resolved_at"] != ""
    assert body["resolved_by"] is not None

    # reopen -> OPEN (resolution cleared)
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=h, json={"state": "OPEN"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "OPEN"
    assert r.json()["resolved_at"] == ""


def test_revision_dismissed_and_illegal_transitions(client):
    ws, h, _ = _register(client)
    rev = _create_revision(client, ws, h).json()["items"][0]
    # OPEN -> DISMISSED
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=h, json={"state": "DISMISSED"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "DISMISSED"
    # DISMISSED -> ADDRESSED is illegal (must reopen first) -> 409
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=h, json={"state": "ADDRESSED"})
    assert r.status_code == 409, r.text
    assert "illegal revision transition" in r.json()["detail"]


# ---------------------------------------------------------------------------
# NO auto-transition (the contract's honesty rule)
# ---------------------------------------------------------------------------


def test_editing_timeline_does_not_auto_address(client):
    """Editing the target must NOT flip OPEN -> ADDRESSED."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)
    rev = _create_revision(client, ws, h, target_id=timeline_id).json()["items"][0]
    assert rev["state"] == "OPEN"

    # edit the timeline (a real content change)
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{timeline_id}", headers=h,
                   json={"base_version": version, "tracks": _tracks("c2", 6.0)})
    assert r.status_code == 200, r.text

    # the revision is STILL open -- "the file changed" is not "the ask was met"
    r = client.get(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "OPEN"
    assert r.json()["resolved_at"] == ""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_revision_item_validation(client):
    ws, h, _ = _register(client)
    # unknown kind -> 422
    r = _create_revision(client, ws, h, items=[{"kind": "teleport", "description": "x"}])
    assert r.status_code == 422, r.text
    # empty description -> 422
    r = _create_revision(client, ws, h, items=[{"kind": "trim", "description": "   "}])
    assert r.status_code == 422, r.text
    # empty item list -> 422
    r = _create_revision(client, ws, h, items=[])
    assert r.status_code == 422, r.text
    # valid multi-item
    r = _create_revision(client, ws, h, items=[
        {"kind": "trim", "description": "trim the tail"},
        {"kind": "caption", "description": "fix the typo", "anchor": {"t_start": 2.0}},
    ])
    assert r.status_code == 201, r.text
    assert len(r.json()["items"][0]["items"]) == 2


# ---------------------------------------------------------------------------
# list + isolation
# ---------------------------------------------------------------------------


def test_revision_list_filters(client):
    ws, h, _ = _register(client)
    _create_revision(client, ws, h, target_id="tl-1")
    _create_revision(client, ws, h, target_id="tl-2")
    r = client.get(f"/api/v1/workspaces/{ws}/revisions", headers=h,
                   params={"target_id": "tl-1"})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
    r = client.get(f"/api/v1/workspaces/{ws}/revisions", headers=h,
                   params={"state": "OPEN"})
    assert len(r.json()["items"]) == 2
    r = client.get(f"/api/v1/workspaces/{ws}/revisions", headers=h,
                   params={"state": "ADDRESSED"})
    assert r.json()["items"] == []


def test_revision_workspace_isolation_404(client):
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)
    rev = _create_revision(client, ws_a, h_a).json()["items"][0]
    r = client.get(f"/api/v1/workspaces/{ws_b}/revisions/{rev['id']}", headers=h_b)
    assert r.status_code == 404, r.text
    r = client.post(f"/api/v1/workspaces/{ws_b}/revisions/{rev['id']}/state",
                    headers=h_b, json={"state": "ADDRESSED"})
    assert r.status_code == 404, r.text
    r = client.get(f"/api/v1/workspaces/{ws_b}/revisions", headers=h_b)
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# matrix: ADDRESSED needs the reviewer-side cap
# ---------------------------------------------------------------------------


def test_addressed_requires_reviewer_cap(client):
    """A workspace viewer cannot mark a revision ADDRESSED (403)."""
    ws, h, _ = _register(client)
    viewer_h, _ = _add_user(client, ws, "viewer")
    rev = _create_revision(client, ws, h).json()["items"][0]
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=viewer_h, json={"state": "ADDRESSED"})
    assert r.status_code == 403, r.text


def test_dismissed_allowed_for_creator(client):
    """The request's creator may DISMISS their own revision."""
    ws, h, _ = _register(client)
    rev = _create_revision(client, ws, h).json()["items"][0]
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=h, json={"state": "DISMISSED"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "DISMISSED"


# ---------------------------------------------------------------------------
# floors/caps matrix -- unlinked targets (contracts §3 fallback + §7)
# ---------------------------------------------------------------------------


def test_revision_unlinked_floors_actual(client):
    """Unlinked target: create falls to the route floor (viewer); DISMISSED by
    a non-creator resolves `edit_timeline` through the unlinked fallback to the
    same floor; ADDRESSED keeps the explicit member floor. Locks ACTUAL."""
    ws, h, _ = _register(client)
    viewer_h, _ = _add_user(client, ws, "viewer")
    member_h, _ = _add_user(client, ws, "member")

    # ws viewer may CREATE on an unlinked target (floor viewer + §3 fallback)
    r = _create_revision(client, ws, viewer_h, target_id="tl-unlinked")
    assert r.status_code == 201, r.text
    rev = r.json()["items"][0]

    # a non-creator member may DISMISS it (unlinked edit_timeline fallback)
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=member_h, json={"state": "DISMISSED"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "DISMISSED"

    # ADDRESSED still needs the member floor: ws viewer -> 403
    r = client.post(f"/api/v1/workspaces/{ws}/revisions/{rev['id']}/state",
                    headers=viewer_h, json={"state": "ADDRESSED"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# floors/caps matrix -- LINKED targets (contracts §7 exact matrix)
# ---------------------------------------------------------------------------


def test_revision_linked_target_matrix(client):
    """Linked target: create + ADDRESSED need `request_revision`; DISMISSED
    needs creator or `edit_timeline` (EDITOR+). Missing project membership or
    project VIEWER -> 403; REVIEWER/EDITOR -> allowed."""
    ws, owner_h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, owner_h)

    r = client.post(f"/api/v1/workspaces/{ws}/projects", headers=owner_h,
                    json={"name": "R", "description": ""})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]
    r = client.post(f"/api/v1/workspaces/{ws}/projects/{project_id}/targets",
                    headers=owner_h,
                    json={"target_type": "timeline", "target_id": timeline_id})
    assert r.status_code == 201, r.text

    m_h, m_id = _add_user(client, ws, "member")   # ws member, no project role
    v_h, v_id = _add_user(client, ws, "member")
    r_h, r_id = _add_user(client, ws, "member")
    e_h, e_id = _add_user(client, ws, "member")
    for uid, role in ((v_id, "VIEWER"), (r_id, "REVIEWER"), (e_id, "EDITOR")):
        rr = client.post(f"/api/v1/workspaces/{ws}/projects/{project_id}/members",
                         headers=owner_h, json={"user_id": uid, "role": role})
        assert rr.status_code == 201, rr.text

    def new_rev(headers):
        return _create_revision(client, ws, headers,
                                target_type="timeline", target_id=timeline_id)

    def state(headers, rev_id, st):
        return client.post(f"/api/v1/workspaces/{ws}/revisions/{rev_id}/state",
                           headers=headers, json={"state": st})

    # create cap: missing membership -> 403, VIEWER -> 403, REVIEWER -> 201
    assert new_rev(m_h).status_code == 403
    assert new_rev(v_h).status_code == 403
    r = new_rev(r_h)
    assert r.status_code == 201, r.text
    rev_r = r.json()["items"][0]

    rev = new_rev(owner_h)
    assert rev.status_code == 201, r.text
    rev = rev.json()["items"][0]

    # ADDRESSED needs `request_revision`: missing membership / VIEWER -> 403
    assert state(m_h, rev["id"], "ADDRESSED").status_code == 403
    assert state(v_h, rev["id"], "ADDRESSED").status_code == 403
    # DISMISSED needs creator or `edit_timeline`: missing membership / VIEWER -> 403
    assert state(m_h, rev["id"], "DISMISSED").status_code == 403
    assert state(v_h, rev["id"], "DISMISSED").status_code == 403
    # project EDITOR may DISMISS someone else's revision
    r = state(e_h, rev["id"], "DISMISSED")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "DISMISSED"
    # project REVIEWER may mark ADDRESSED (reviewer side)
    r = state(r_h, rev_r["id"], "ADDRESSED")
    assert r.status_code == 200, r.text
    assert r.json()["resolved_by"] == r_id
    assert r.json()["resolved_at"] != ""
