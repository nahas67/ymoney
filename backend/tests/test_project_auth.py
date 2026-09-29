"""Work 11 Lane F: project RBAC (backend/app/services/project_auth.py).

Locks contracts §3 without HTTP, plus the FastAPI dependency through a
TestClient:

  * capability matrix truth table (5 roles x 9 caps, positive + negative)
  * workspace-admin bypass (governance) and the plain-member denial
  * unlinked-target fallback: no raise for collaboration caps AND for
    mutation caps (the route's require_workspace_role floor is the line)
  * linked target + no membership -> 403 "insufficient project role"
  * foreign project / foreign-linked target -> 404 (never 403)
  * last-owner protection (demote + remove) and the OWNER-grant repair path
  * unknown capability / bad args -> ValueError (routes map that to 422)
  * require_project_capability(project_id_param, capability) dependency
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

CAPS = (
    "view_project",
    "edit_project",
    "edit_timeline",
    "comment",
    "request_revision",
    "approve",
    "export",
    "publish",
    "manage_collaborators",
)
ROLES = ("OWNER", "ADMIN", "EDITOR", "REVIEWER", "VIEWER")

# contracts §3 matrix, transposed: role -> capabilities it must be granted
ALLOWED: dict[str, set[str]] = {
    "OWNER": {"view_project", "edit_project", "edit_timeline", "comment",
              "request_revision", "approve", "export", "publish",
              "manage_collaborators"},
    "ADMIN": {"view_project", "edit_project", "edit_timeline", "comment",
              "request_revision", "approve", "export", "publish"},
    "EDITOR": {"view_project", "edit_timeline", "comment", "export"},
    "REVIEWER": {"view_project", "comment", "request_revision", "approve"},
    "VIEWER": {"view_project"},
}


# ---------------------------------------------------------------------------
# seeding (always a fresh session_scope; handler sessions are never re-read
# through an old identity map)
# ---------------------------------------------------------------------------


def _seed(*, ws_role="member", project=True, project_role=None, superuser=False):
    """One workspace + user (+ optional project/membership). Returns ids."""
    from app.db import session_scope
    from app.models import Project, ProjectMember, User, Workspace, WorkspaceMember

    tag = uuid.uuid4().hex[:10]
    with session_scope() as s:
        user = User(
            email=f"pa{tag}@test.local", password_hash="x", is_superuser=superuser
        )
        ws = Workspace(name=f"WS {tag}", slug=f"ws-{tag}", niche="AI money")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=ws_role))
        project_id = member_id = None
        if project:
            row = Project(workspace_id=ws.id, name=f"P {tag}", created_by=user.id)
            s.add(row)
            s.flush()
            project_id = row.id
            if project_role:
                member = ProjectMember(project_id=row.id, user_id=user.id, role=project_role)
                s.add(member)
                s.flush()
                member_id = member.id
        return {
            "user": user.id,
            "ws": ws.id,
            "project": project_id,
            "member": member_id,
        }


def _ctx(ids):
    """Fresh (db, ws, user) triple for one assertion block.

    ``session_scope`` is a generator contextmanager, not a Session factory, so
    it must be entered (see app/db.py). The caller owns the session and closes
    it via the returned context manager.
    """
    from app.db import SessionLocal
    from app.models import User, Workspace

    s = SessionLocal()
    ws = s.get(Workspace, ids["ws"])
    user = s.get(User, ids["user"])
    assert ws is not None and user is not None
    return s, ws, user


def _deny(fn, *args, status=403, detail="insufficient project role", **kwargs):
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == status, (exc.value.status_code, exc.value.detail)
    if detail is not None:
        assert exc.value.detail == detail


# ---------------------------------------------------------------------------
# matrix
# ---------------------------------------------------------------------------


def test_capability_matrix_truth_table():
    """Every role x every capability: allow exactly when the matrix says so."""
    from app.models import ProjectMember
    from app.services.project_auth import CAPABILITIES, assert_capability

    # matrix itself is the contracts §3 table, verbatim
    expected = {cap: {role for role in ROLES if cap in ALLOWED[role]} for cap in CAPS}
    assert expected == CAPABILITIES

    ids = _seed(ws_role="member", project_role="VIEWER")
    s, ws, user = _ctx(ids)
    try:
        member = s.get(ProjectMember, ids["member"])
        assert member is not None
        for role in ROLES:
            # direct role write: this test is about assert_capability, and the
            # service's last-owner rule is covered separately below
            member.role = role
            s.flush()
            for cap in CAPS:
                allowed = cap in ALLOWED[role]
                if allowed:
                    assert_capability(
                        s, ws, user, capability=cap, project_id=ids["project"]
                    )
                else:
                    _deny(
                        assert_capability,
                        s, ws, user, capability=cap, project_id=ids["project"],
                    )
    finally:
        s.close()


def test_workspace_admin_bypass_and_member_denial():
    from app.services.project_auth import assert_capability, can

    admin = _seed(ws_role="admin", project_role=None)
    s, ws, user = _ctx(admin)
    try:
        # governance: no project membership needed at all
        for cap in CAPS:
            assert_capability(s, ws, user, capability=cap, project_id=admin["project"])
            assert can(s, ws, user, capability=cap, project_id=admin["project"]) is True
    finally:
        s.close()

    plain = _seed(ws_role="member", project_role=None)
    s, ws, user = _ctx(plain)
    try:
        # workspace member, not a project member -> every cap denied
        for cap in CAPS:
            _deny(assert_capability, s, ws, user, capability=cap,
                  project_id=plain["project"])
            assert can(s, ws, user, capability=cap, project_id=plain["project"]) is False
    finally:
        s.close()

    viewer = _seed(ws_role="viewer", project_role="VIEWER")
    s, ws, user = _ctx(viewer)
    try:
        # project VIEWER keeps view_project but gains nothing else
        assert_capability(s, ws, user, capability="view_project",
                          project_id=viewer["project"])
        _deny(assert_capability, s, ws, user, capability="comment",
              project_id=viewer["project"])
    finally:
        s.close()


def test_unlinked_target_fallback_never_raises():
    """Contracts §3 fallback: target not linked to a project -> no raise.

    Collaboration caps pass outright for any workspace member (their route
    floor is viewer); mutation caps fall through to the route's own
    require_workspace_role floor, so assert_capability must NOT raise either.
    """
    from app.services.project_auth import assert_capability

    ids = _seed(ws_role="viewer", project=False)
    # A second project in the SAME workspace, with no membership row for this
    # user: the 403 path (a foreign-workspace id would be 404 by design).
    from app.db import session_scope as _ss
    from app.models import Project as _Project

    with _ss() as s2:
        other = _Project(workspace_id=ids["ws"], name="unlinked", created_by=ids["user"])
        s2.add(other)
        s2.flush()
        other_project = other.id

    s, ws, user = _ctx(ids)
    target = {"target_type": "content", "target_id": f"c-{uuid.uuid4().hex[:10]}"}
    try:
        # collaboration caps: allowed for any ws member
        for cap in ("view_project", "comment", "request_revision", "approve"):
            assert assert_capability(s, ws, user, capability=cap, **target) is None
        # mutation caps: fall through (the route floor enforces the rest)
        for cap in ("edit_timeline", "edit_project", "publish", "export",
                    "manage_collaborators"):
            assert assert_capability(s, ws, user, capability=cap, **target) is None
        # ...and an explicit project id in this workspace still gates
        # (no membership -> 403, not the unlinked fallback)
        _deny(assert_capability, s, ws, user, capability="view_project",
              project_id=other_project)
    finally:
        s.close()


def test_linked_target_requires_membership():
    from app.db import session_scope
    from app.models import ProjectTarget
    from app.services.project_auth import assert_capability

    ids = _seed(ws_role="member", project_role=None)
    target = {"target_type": "content", "target_id": f"c-{uuid.uuid4().hex[:10]}"}
    with session_scope() as s:
        s.add(ProjectTarget(project_id=ids["project"], target_type="content",
                            target_id=target["target_id"]))
    s, ws, user = _ctx(ids)
    try:
        # linked, but this user is not a project member -> 403 for everything
        _deny(assert_capability, s, ws, user, capability="view_project", **target)
        _deny(assert_capability, s, ws, user, capability="comment", **target)
    finally:
        s.close()

    from app.services.project_auth import add_member

    with session_scope() as s:
        add_member(s, ids["project"], ids["user"], "VIEWER", by_user=ids["user"])
    s, ws, user = _ctx(ids)
    try:
        assert_capability(s, ws, user, capability="view_project", **target)
        _deny(assert_capability, s, ws, user, capability="comment", **target)
    finally:
        s.close()

    with session_scope() as s:
        add_member(s, ids["project"], ids["user"], "REVIEWER", by_user=ids["user"])
    s, ws, user = _ctx(ids)
    try:
        assert_capability(s, ws, user, capability="comment", **target)
        assert_capability(s, ws, user, capability="approve", **target)
        _deny(assert_capability, s, ws, user, capability="edit_timeline", **target)
    finally:
        s.close()


def test_foreign_project_and_target_are_404():
    """Never leak or allow another workspace's ids (404, not 403)."""
    from app.db import session_scope
    from app.models import ProjectTarget
    from app.services.project_auth import assert_capability

    mine = _seed(ws_role="owner", project=True, project_role="OWNER")
    theirs = _seed(ws_role="owner", project=True, project_role="OWNER")
    with session_scope() as s:
        s.add(ProjectTarget(project_id=theirs["project"], target_type="content",
                            target_id="foreign-target"))

    s, ws, user = _ctx(mine)
    try:
        # explicit foreign project id -> 404 (even for a workspace owner/admin)
        _deny(assert_capability, s, ws, user, capability="view_project",
              project_id=theirs["project"], status=404, detail="project not found")
        # target linked only in another workspace -> 404
        _deny(assert_capability, s, ws, user, capability="comment",
              target_type="content", target_id="foreign-target",
              status=404, detail="target not found")
        # and `can` reads both as False
        from app.services.project_auth import can

        assert can(s, ws, user, capability="view_project",
                   project_id=theirs["project"]) is False
        # own project still resolves
        assert_capability(s, ws, user, capability="manage_collaborators",
                          project_id=mine["project"])
    finally:
        s.close()


def test_last_owner_protection():
    from app.db import session_scope
    from app.services.project_auth import add_member, remove_member

    owner = _seed(ws_role="member", project_role="OWNER")
    second = _seed(ws_role="member", project=False)["user"]

    # cannot demote the sole OWNER
    with session_scope() as s, pytest.raises(ValueError, match="final owner"):
        add_member(s, owner["project"], owner["user"], "ADMIN", by_user=owner["user"])
    # cannot remove the sole OWNER either
    with session_scope() as s, pytest.raises(ValueError, match="final owner"):
        remove_member(s, owner["project"], owner["user"], by_user=owner["user"])
    with session_scope() as s:
        # repair path: granting OWNER is never blocked...
        add_member(s, owner["project"], second, "OWNER", by_user=owner["user"])
    with session_scope() as s:
        from app.models import ProjectMember

        roles = {
            row.user_id: row.role
            for row in s.query(ProjectMember)
            .filter(ProjectMember.project_id == owner["project"])
        }
        assert roles[second] == "OWNER"
        # ...and only then can the first owner be demoted/removed
        add_member(s, owner["project"], owner["user"], "ADMIN", by_user=second)
    with session_scope() as s:
        remove_member(s, owner["project"], owner["user"], by_user=second)
    with session_scope() as s:
        from app.models import ProjectMember

        assert (
            s.query(ProjectMember)
            .filter(ProjectMember.project_id == owner["project"],
                    ProjectMember.user_id == owner["user"])
            .count()
            == 0
        )


def test_bad_capability_and_bad_args_are_value_error():
    from app.services.project_auth import assert_capability

    ids = _seed(ws_role="admin", project_role="OWNER")
    s, ws, user = _ctx(ids)
    try:
        with pytest.raises(ValueError, match="unknown capability"):
            assert_capability(s, ws, user, capability="teleport",
                              project_id=ids["project"])
        with pytest.raises(ValueError, match="target_type and target_id"):
            assert_capability(s, ws, user, capability="comment", target_type="content")
        with pytest.raises(ValueError, match="project_id or target"):
            assert_capability(s, ws, user, capability="comment")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# FastAPI dependency (contracts §3 thin wrapper)
# ---------------------------------------------------------------------------


def _dep_client(user_id: str):
    """A minimal app exposing require_project_capability over a probe route."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.models import User
    from app.services.auth_service import get_current_user
    from app.services.project_auth import require_project_capability

    with session_scope() as s:
        user = s.get(User, user_id)
        assert user is not None
        user_obj = user  # detached-ish: expire_on_commit=False keeps attributes

    app = FastAPI()
    from fastapi import APIRouter

    router = APIRouter(prefix="/workspaces/{workspace_id}/projects", tags=["probe"])

    @router.get("/{project_id}/probe")
    def probe(
        project_id: str,
        project=Depends(require_project_capability("project_id", "view_project")),
    ):
        return {"project_id": project.id, "ok": True}

    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user_obj
    return TestClient(app, raise_server_exceptions=False)


def test_require_project_capability_dependency():
    owner = _seed(ws_role="member", project_role="OWNER")
    plain = _seed(ws_role="member", project_role=None)
    admin = _seed(ws_role="admin", project_role=None)

    # `plain` and `admin` must be members of the PATH workspace to exercise the
    # project-role gate at all; otherwise the workspace floor answers first
    # ("not a workspace member"), which is a different test.
    from app.db import session_scope as _ss
    from app.models import WorkspaceMember as _WM

    with _ss() as s2:
        s2.add(_WM(workspace_id=owner["ws"], user_id=plain["user"], role="member"))
        # admin must be admin IN THE PATH WORKSPACE, else the governance
        # bypass under test never engages.
        s2.add(_WM(workspace_id=owner["ws"], user_id=admin["user"], role="admin"))
        s2.flush()

    path = f"/workspaces/{owner['ws']}/projects/{owner['project']}/probe"

    r = _dep_client(owner["user"]).get(path)
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": owner["project"], "ok": True}

    r = _dep_client(plain["user"]).get(path)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "insufficient project role"

    # workspace admin bypass rides through the same dependency
    r = _dep_client(admin["user"]).get(path)
    assert r.status_code == 200, r.text

    # foreign project id on my own workspace -> 404 (and no id leaking)
    theirs = _seed(ws_role="owner", project_role="OWNER")
    r = _dep_client(owner["user"]).get(
        f"/workspaces/{owner['ws']}/projects/{theirs['project']}/probe"
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "project not found"

    # a non-member of the path workspace never gets past the floor
    r = _dep_client(plain["user"]).get(
        f"/workspaces/{admin['ws']}/projects/{admin['project']}/probe"
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "not a workspace member"
