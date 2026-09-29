"""Work 11 Lane F: Projects API (backend/app/api/v1/projects.py).

Contracts §4 endpoint surface, using the repo's API-test fixture style
(register -> workspace-scoped bearer -> TestClient), mirroring
tests/test_inbox_api.py + tests/test_knowledge_api.py:

  * create: member floor (viewer 403, no-auth 401), creator becomes OWNER
  * list: ``{"items": [...]}`` (and ``{"items": []}`` on an empty workspace)
  * detail: name/description/status + member_count/target_count + members
    + targets
  * PATCH: edit_project cap (project OWNER/ADMIN 200; non-member of the
    project 403; project VIEWER 403) -- name/description only
  * members: add 201, foreign/unknown user 404, detail visibility, remove
    200, last-owner demote/remove 409, self-leave 200, sole-owner leave 409
  * targets: real content link 201, unknown target 404, foreign-workspace
    target 404, honest idempotent re-link, GET list shape, DELETE 200
  * transfer: OWNER -> 200 with role swap (fresh session), non-member 404,
    wrong-role member 409 (contracts §4: target must be OWNER/ADMIN),
    non-owner 403
  * RBAC summary + cross-workspace isolation (foreign project id -> 404)
  * error hygiene: generic 500 ``{"detail": "internal error"}``, no echo
  * activity: PROJECT_CREATED row in the append-only ``events`` ledger

Seeding always happens through ``session_scope`` BEFORE the request; rows
written by handler sessions are re-read through a FRESH ``session_scope``
(never through a stale identity map -- ``expire_on_commit=False``).
"""
from __future__ import annotations

import uuid

from app.api.v1 import projects as projects_mod

# ---------------------------------------------------------------------------
# fixtures / helpers (mirror tests/test_inbox_api.py)
# ---------------------------------------------------------------------------


def _register(client, email=None):
    """Register a user + personal workspace. Returns (ws_id, headers, user_id).

    Deviation from test_inbox_api._register: the third element is the caller's
    user id -- the member/transfer tests must name explicit user ids.
    """
    email = email or f"pj{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return (
        data["workspace"]["id"],
        {"Authorization": f"Bearer {data['access_token']}"},
        data["user"]["id"],
    )


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _login_headers(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_user(client, ws_id, role):
    """Register a fresh user, grant them `role` on ws_id.

    Returns (headers, user_id).
    """
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"{role.lower()}{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    return _login_headers(client, email), user_id


def _base(ws_id):
    return f"/api/v1/workspaces/{ws_id}/projects"


def _create_project(client, ws_id, headers, name="Launch"):
    r = client.post(_base(ws_id), headers=headers, json={"name": name, "description": "d"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_content(ws_id, topic="collab target"):
    from app.db import session_scope
    from app.models import ContentItem

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic=topic)
        s.add(item)
        s.flush()
        return item.id


def _members(project_id):
    """Fresh-session re-read of project_members: {user_id: role}."""
    from app.db import session_scope
    from app.models import ProjectMember

    with session_scope() as s:
        return {
            row.user_id: row.role
            for row in s.query(ProjectMember).filter(ProjectMember.project_id == project_id)
        }


# ---------------------------------------------------------------------------
# create / list / detail
# ---------------------------------------------------------------------------


def test_create_project_member_floor_and_creator_becomes_owner(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_viewer, _ = _make_user(client, ws, "viewer")
    h_member, member_id = _make_user(client, ws, "member")

    # no auth -> 401
    r = client.post(_base(ws), json={"name": "anon"})
    assert r.status_code == 401, r.text

    # viewer floor -> 403 (contracts §4: member+ creates)
    r = client.post(_base(ws), headers=h_viewer, json={"name": "nope"})
    assert r.status_code == 403, r.text

    # member -> 201, creator becomes project OWNER (fresh session)
    r = client.post(_base(ws), headers=h_member,
                    json={"name": "Alpha", "description": "first project"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Alpha"
    assert body["description"] == "first project"
    assert body["status"] == "ACTIVE"
    assert body["workspace_id"] == ws
    assert body["created_by"] == member_id
    assert _members(body["id"]) == {member_id: "OWNER"}


def test_list_shape_and_empty_workspace(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h, _uid = _register(client)

    # an empty workspace answers the canonical empty shape
    r = client.get(_base(ws), headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}

    _create_project(client, ws, h, "One")
    _create_project(client, ws, h, "Two")

    r = client.get(_base(ws), headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == {"items"}
    assert len(data["items"]) == 2
    for item in data["items"]:
        assert set(item) >= {"id", "workspace_id", "name", "description", "status",
                             "created_by", "created_at", "updated_at"}
        # list rows carry no members/targets payload (detail-only keys)
        assert "members" not in item and "targets" not in item


def test_detail_shape_counts_members_targets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h, uid = _register(client)
    _h_editor, editor_id = _make_user(client, ws, "member")

    pid = _create_project(client, ws, h, "Detailed")
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h,
                    json={"user_id": editor_id, "role": "EDITOR"})
    assert r.status_code == 201, r.text
    content_id = _seed_content(ws)
    r = client.post(f"{_base(ws)}/{pid}/targets", headers=h,
                    json={"target_type": "content", "target_id": content_id})
    assert r.status_code == 201, r.text

    r = client.get(f"{_base(ws)}/{pid}", headers=h)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["name"] == "Detailed"
    assert detail["description"] == "d"
    assert detail["status"] == "ACTIVE"
    assert detail["member_count"] == 2
    assert detail["target_count"] == 1
    assert {m["user_id"]: m["role"] for m in detail["members"]} == {
        uid: "OWNER", editor_id: "EDITOR",
    }
    assert detail["targets"] == [
        {"target_type": "content", "target_id": content_id,
         "created_at": detail["targets"][0]["created_at"]}
    ]
    # every member/target row carries its own timestamp
    assert all(m["created_at"] for m in detail["members"])


# ---------------------------------------------------------------------------
# PATCH (edit_project)
# ---------------------------------------------------------------------------


def test_patch_edit_project_capability(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_owner, owner_id = _make_user(client, ws, "member")
    h_stranger, _ = _make_user(client, ws, "member")

    pid = _create_project(client, ws, h_owner, "Before")

    # project OWNER (a plain workspace member) edits name + description
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_owner,
                     json={"name": "After", "description": "changed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "After"
    assert r.json()["description"] == "changed"

    # a workspace member with NO project membership -> 403 (contracts §3)
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_stranger,
                     json={"name": "Hijack"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "insufficient project role"

    # project VIEWER also lacks edit_project -> 403
    h_viewer, viewer_id = _make_user(client, ws, "member")
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": viewer_id, "role": "VIEWER"})
    assert r.status_code == 201, r.text
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_viewer, json={"name": "Nope"})
    assert r.status_code == 403, r.text

    # blank name is rejected without touching the row (422)
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_owner, json={"name": "   "})
    assert r.status_code == 422, r.text
    r = client.get(f"{_base(ws)}/{pid}", headers=h_owner)
    assert r.json()["name"] == "After"

    # status is NOT patchable here (archive is lane L) -- unknown fields are
    # simply ignored by the pydantic body, so the status stays ACTIVE
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_owner,
                     json={"name": "After2", "status": "ARCHIVED"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE"
    assert owner_id  # creator id retained for readability of the flow

    # contracts §3 governance rule: workspace ADMIN+ bypasses project gating
    # entirely -- this admin holds NO project membership row at all
    h_wsadmin, _wsadmin_id = _make_user(client, ws, "admin")
    r = client.patch(f"{_base(ws)}/{pid}", headers=h_wsadmin,
                     json={"name": "ByWsAdmin"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "ByWsAdmin"


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


def test_members_add_remove_and_last_owner_protection(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_owner, owner_id = _make_user(client, ws, "member")
    h_second, second_id = _make_user(client, ws, "member")

    # a registered user who is NOT a member of this workspace
    outsider_id = _register(client)[2]

    pid = _create_project(client, ws, h_owner, "Team")

    # add a workspace user as EDITOR -> 201
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": second_id, "role": "EDITOR"})
    assert r.status_code == 201, r.text
    assert r.json() == {"user_id": second_id, "role": "EDITOR",
                        "created_at": r.json()["created_at"]}
    assert r.json()["created_at"]

    # ...and it is visible in the detail payload
    r = client.get(f"{_base(ws)}/{pid}", headers=h_owner)
    assert r.json()["member_count"] == 2
    assert second_id in {m["user_id"] for m in r.json()["members"]}

    # a user who is NOT a workspace member -> 404 (never 409/403)
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": outsider_id, "role": "EDITOR"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "user not found"

    # a project EDITOR cannot manage collaborators (cap is OWNER-only)
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_second,
                    json={"user_id": outsider_id, "role": "VIEWER"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "insufficient project role"

    # remove a member -> 200 (owner removes the EDITOR)
    r = client.delete(f"{_base(ws)}/{pid}/members/{second_id}", headers=h_owner)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "user_id": second_id}
    assert _members(pid) == {owner_id: "OWNER"}

    # removing a non-member -> 404
    r = client.delete(f"{_base(ws)}/{pid}/members/{second_id}", headers=h_owner)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "member not found"

    # re-add the second user so the self-leave paths can be exercised
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": second_id, "role": "EDITOR"})
    assert r.status_code == 201, r.text

    # self-leave by a NON-owner member -> 200 (no cap required)
    r = client.delete(f"{_base(ws)}/{pid}/members/{second_id}", headers=h_second)
    assert r.status_code == 200, r.text
    assert _members(pid) == {owner_id: "OWNER"}

    # demote the final OWNER -> 409
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": owner_id, "role": "ADMIN"})
    assert r.status_code == 409, r.text
    assert "final owner" in r.json()["detail"]
    assert _members(pid) == {owner_id: "OWNER"}

    # remove the final OWNER (self-leave) -> 409
    r = client.delete(f"{_base(ws)}/{pid}/members/{owner_id}", headers=h_owner)
    assert r.status_code == 409, r.text
    assert "final owner" in r.json()["detail"]
    assert _members(pid) == {owner_id: "OWNER"}

    # repair path: granting OWNER to a second member unblocks the first
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": second_id, "role": "OWNER"})
    assert r.status_code == 201, r.text
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": owner_id, "role": "ADMIN"})
    assert r.status_code == 201, r.text
    assert _members(pid) == {owner_id: "ADMIN", second_id: "OWNER"}

    # contracts §4/§9: member mutations are audited in the activity ledger
    from app.db import session_scope
    from app.models import EventLog

    with session_scope() as s:
        kinds = [
            row.kind
            for row in s.query(EventLog).filter(
                EventLog.workspace_id == ws,
                EventLog.kind.in_(("PROJECT_MEMBER_ADDED", "PROJECT_MEMBER_REMOVED")),
            )
        ]
    assert "PROJECT_MEMBER_ADDED" in kinds, kinds
    assert "PROJECT_MEMBER_REMOVED" in kinds, kinds


def test_ws_admin_repairs_sole_owner_project(tmp_path, monkeypatch):
    """Contracts §3: ws admin+ bypasses project gating, so admin can repair a
    stuck sole-owner project (grant OWNER to somebody else) without holding
    any project role herself."""
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_owner, owner_id = _make_user(client, ws, "member")
    h_wsadmin, wsadmin_id = _make_user(client, ws, "admin")

    pid = _create_project(client, ws, h_owner, "Stuck")
    assert _members(pid) == {owner_id: "OWNER"}

    # the ws admin holds no project membership, yet manage_collaborators passes
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_wsadmin,
                    json={"user_id": wsadmin_id, "role": "OWNER"})
    assert r.status_code == 201, r.text
    assert _members(pid) == {owner_id: "OWNER", wsadmin_id: "OWNER"}

    # two owners now: the original can be demoted (repair complete)
    r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                    json={"user_id": owner_id, "role": "ADMIN"})
    assert r.status_code == 201, r.text
    assert _members(pid) == {owner_id: "ADMIN", wsadmin_id: "OWNER"}


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------


def test_targets_crud_and_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)
    h_stranger, _ = _make_user(client, ws_a, "member")

    pid = _create_project(client, ws_a, h_a, "Targets")
    content_id = _seed_content(ws_a)
    foreign_content_id = _seed_content(ws_b, "foreign target")

    # link a real content item -> 201
    r = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_a,
                    json={"target_type": "content", "target_id": content_id})
    assert r.status_code == 201, r.text
    assert r.json()["target_type"] == "content"
    assert r.json()["target_id"] == content_id
    assert r.json()["created_at"]

    # duplicate link is honestly idempotent: 201 with the SAME edge
    r2 = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_a,
                     json={"target_type": "content", "target_id": content_id})
    assert r2.status_code == 201, r2.text
    assert r2.json()["target_id"] == content_id

    # list shape
    r = client.get(f"{_base(ws_a)}/{pid}/targets", headers=h_a)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [
        {"target_type": "content", "target_id": content_id,
         "created_at": r.json()["items"][0]["created_at"]}
    ]}

    # unknown target id -> 404
    r = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_a,
                    json={"target_type": "content", "target_id": uuid.uuid4().hex})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "target not found"

    # foreign-workspace target -> 404 (no id leaking across workspaces)
    r = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_a,
                    json={"target_type": "content", "target_id": foreign_content_id})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "target not found"

    # unknown target_type is rejected by the body enum -> 422
    r = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_a,
                    json={"target_type": "nonsense", "target_id": content_id})
    assert r.status_code == 422, r.text

    # edit_project cap: a workspace member with no project membership -> 403
    r = client.post(f"{_base(ws_a)}/{pid}/targets", headers=h_stranger,
                    json={"target_type": "content", "target_id": content_id})
    assert r.status_code == 403, r.text

    # unlink -> 200, then the edge is gone and a re-delete is 404
    r = client.delete(f"{_base(ws_a)}/{pid}/targets/content/{content_id}", headers=h_a)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "target_type": "content", "target_id": content_id}
    r = client.get(f"{_base(ws_a)}/{pid}/targets", headers=h_a)
    assert r.json() == {"items": []}
    r = client.delete(f"{_base(ws_a)}/{pid}/targets/content/{content_id}", headers=h_a)
    assert r.status_code == 404, r.text

    # the second workspace's project is untouched by all of the above
    pid_b = _create_project(client, ws_b, h_b, "Other WS")
    r = client.get(f"{_base(ws_b)}/{pid_b}/targets", headers=h_b)
    assert r.json() == {"items": []}


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------


def test_transfer_ownership_lifecycle(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_owner, owner_id = _make_user(client, ws, "member")
    h_admin, admin_id = _make_user(client, ws, "member")
    h_editor, editor_id = _make_user(client, ws, "member")
    h_plain, plain_id = _make_user(client, ws, "member")  # not a project member
    outsider_id = _register(client)[2]                    # not a workspace member

    pid = _create_project(client, ws, h_owner, "Transfer")
    for uid, role in ((admin_id, "ADMIN"), (editor_id, "EDITOR")):
        r = client.post(f"{_base(ws)}/{pid}/members", headers=h_owner,
                        json={"user_id": uid, "role": role})
        assert r.status_code == 201, r.text

    # a project EDITOR (workspace member, no ws-admin) -> 403
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_editor,
                    json={"to_user_id": admin_id})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "insufficient project role"

    # transfer target must be a project member -> 404 (missing membership)
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_owner,
                    json={"to_user_id": plain_id})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "member not found"

    # ...and must not even be a workspace member first -> 404
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_owner,
                    json={"to_user_id": outsider_id})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "user not found"

    # contracts §4: target must hold OWNER or ADMIN -- an EDITOR conflicts
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_owner,
                    json={"to_user_id": editor_id})
    assert r.status_code == 409, r.text
    assert "OWNER or ADMIN" in r.json()["detail"]

    # happy path: OWNER transfers to the project ADMIN -> 200
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_owner,
                    json={"to_user_id": admin_id})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == pid

    # roles swapped, read from a FRESH session
    assert _members(pid) == {owner_id: "ADMIN", admin_id: "OWNER",
                             editor_id: "EDITOR"}

    # the demoted old owner no longer holds manage_collaborators -> 403
    r = client.post(f"{_base(ws)}/{pid}/transfer", headers=h_owner,
                    json={"to_user_id": editor_id})
    assert r.status_code == 403, r.text

    # contracts §4/§9: the transfer is audited exactly once
    from app.db import session_scope
    from app.models import EventLog

    with session_scope() as s:
        rows = (
            s.query(EventLog)
            .filter(EventLog.workspace_id == ws, EventLog.kind == "PROJECT_TRANSFERRED")
            .all()
        )
    assert len(rows) == 1, [r.kind for r in rows]
    assert rows[0].source == "collab"
    assert rows[0].data_json["project_id"] == pid
    assert rows[0].data_json["to_user_id"] == admin_id
    assert rows[0].data_json["actor"] == owner_id


# ---------------------------------------------------------------------------
# RBAC summary + workspace isolation
# ---------------------------------------------------------------------------


def test_rbac_matrix_summary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, _h, _uid = _register(client)
    h_viewer, _ = _make_user(client, ws, "viewer")
    h_member, _ = _make_user(client, ws, "member")

    # no auth anywhere on the surface -> 401
    assert client.get(_base(ws)).status_code == 401
    assert client.post(_base(ws), json={"name": "x"}).status_code == 401

    # viewer: reads the list, never creates
    r = client.get(_base(ws), headers=h_viewer)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}
    assert client.post(_base(ws), headers=h_viewer,
                       json={"name": "no"}).status_code == 403

    # member: creates
    r = client.post(_base(ws), headers=h_member, json={"name": "yes"})
    assert r.status_code == 201, r.text
    assert _members(r.json()["id"]) == {r.json()["created_by"]: "OWNER"}


def test_cross_workspace_isolation_foreign_project_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)

    pid_a = _create_project(client, ws_a, h_a, "Secret A")

    # workspace B's own member asking for A's project id -> 404, never 403
    r = client.get(f"{_base(ws_b)}/{pid_a}", headers=h_b)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "project not found"

    # mutations carry the same isolation
    r = client.patch(f"{_base(ws_b)}/{pid_a}", headers=h_b, json={"name": "stolen"})
    assert r.status_code == 404, r.text
    r = client.delete(f"{_base(ws_b)}/{pid_a}/targets/content/whatever", headers=h_b)
    assert r.status_code == 404, r.text

    # B's list never contains A's project
    r = client.get(_base(ws_b), headers=h_b)
    assert r.json() == {"items": []}

    # a stranger on A's mount never gets past the workspace floor -> 403
    r = client.get(f"{_base(ws_a)}/{pid_a}", headers=h_b)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "not a workspace member"


# ---------------------------------------------------------------------------
# error hygiene + activity ledger
# ---------------------------------------------------------------------------


def test_error_hygiene_generic_500(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h, _uid = _register(client)
    marker = "SECRETBOOM-9f3a"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(f"underlying collab service exploded: {marker}")

    # record_event is what create_project calls right after its commit
    monkeypatch.setattr(projects_mod, "record_event", _boom)

    r = client.post(_base(ws), headers=h, json={"name": "boom"})
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "internal error"}
    assert marker not in r.text
    assert "exploded" not in r.text
    assert "Traceback" not in r.text


def test_create_emits_project_created_event(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h, uid = _register(client)

    pid = _create_project(client, ws, h, "Audited")

    # fresh session: the append-only events ledger holds the row
    from app.db import session_scope
    from app.models import EventLog

    with session_scope() as s:
        rows = (
            s.query(EventLog)
            .filter(EventLog.workspace_id == ws, EventLog.kind == "PROJECT_CREATED")
            .all()
        )
        assert len(rows) == 1, [r.kind for r in rows]
        row = rows[0]
        assert row.source == "collab"
        assert row.level == "info"
        assert row.message
        assert row.data_json["actor"] == uid
        assert row.data_json["project_id"] == pid
