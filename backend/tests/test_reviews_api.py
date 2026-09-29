"""Work 11 Lane R: version-bound reviews (contracts §5, §13).

Locks the review contract end-to-end over HTTP:

  * exact-version binding (bound_version + bound_manifest_hash captured at
    creation; unversioned targets bind NULL)
  * approve re-verifies the binding -> 409 + stale, state stays IN_REVIEW
  * staleness on edit: GET flips stale, and an APPROVED review reports
    approval_valid=false while keeping its APPROVED history
  * state machine legality (DRAFT->IN_REVIEW->..., CHANGES_REQUESTED
    re-request rebinds, terminal states) + illegal transitions -> 409
  * RBAC matrix (viewer POST 403 / member 201 / project VIEWER 403 /
    project REVIEWER 201 / viewer GET 200) + capabilities badges
  * workspace isolation (foreign review id -> 404, never 403)
  * REQUEST_CHANGES auto-opens a revision request
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app + TestClient rooted in a temp dir (mirrors test_timeline_diff)."""
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


def _add_user(client, ws_id, role):
    """Register a fresh user, grant `role` on ws_id, return (headers, user_id)."""
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"u{uuid.uuid4().hex[:8]}@test.local"
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


def _make_timeline(client, ws_id, headers, *, clip_id="c1", duration=5.0):
    """Create a timeline with one video clip; return its id + current version."""
    r = client.post(f"/api/v1/workspaces/{ws_id}/timelines",
                    headers=headers,
                    json={"name": "main", "fps": 30.0, "duration_seconds": 10.0,
                          "aspect": "9:16", "tracks": None})
    assert r.status_code == 200, r.text
    timeline = r.json()
    _save_timeline(client, ws_id, headers, timeline["id"],
                   base_version=timeline["version"], clip_id=clip_id, duration=duration)
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{timeline['id']}", headers=headers)
    return timeline["id"], r.json()["version"]


def _tracks(clip_id: str, duration: float) -> list:
    return [{
        "id": "t_video", "kind": "video", "name": "Video",
        "clips": [{"id": clip_id, "name": clip_id, "start": 0.0, "duration": duration,
                   "source": {"asset_id": "a1"}, "effects": [], "source_start": 0.0,
                   "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                   "transform": {}, "text": {}, "transition_in": "cut",
                   "transition_out": "cut"}],
    }]


def _save_timeline(client, ws_id, headers, timeline_id, *, base_version,
                   clip_id="c1", duration=5.0):
    r = client.put(f"/api/v1/workspaces/{ws_id}/timelines/{timeline_id}",
                   headers=headers,
                   json={"base_version": base_version,
                         "tracks": _tracks(clip_id, duration)})
    assert r.status_code == 200, r.text
    return r.json()["version"]


def _manifest_hash(client, ws_id, headers, timeline_id):
    """The timeline's current render-manifest hash (the manifest route's own field)."""
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{timeline_id}/manifest",
                   headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["manifest_hash"]


def _create_review(client, ws_id, headers, timeline_id, **extra):
    payload = {"target_type": "timeline_version", "target_id": timeline_id,
               "title": "Cut v1"}
    payload.update(extra)
    r = client.post(f"/api/v1/workspaces/{ws_id}/reviews", headers=headers, json=payload)
    return r


# ---------------------------------------------------------------------------
# exact-version binding
# ---------------------------------------------------------------------------


def test_exact_version_binding(client, tmp_path, monkeypatch):
    """Creation captures bound_version + bound_manifest_hash of the current tip."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)
    expected_hash = _manifest_hash(client, ws, h, timeline_id)

    r = _create_review(client, ws, h, timeline_id)
    assert r.status_code == 201, r.text
    review = r.json()
    assert review["state"] == "DRAFT"
    assert review["bound_version"] == str(version)
    assert review["bound_manifest_hash"] == expected_hash
    assert review["stale"] is False
    assert review["approval_valid"] is False  # not approved yet


def test_unversioned_target_binds_null(client, tmp_path, monkeypatch):
    """A non-versioned target (campaign) binds NULL version/hash."""
    ws, h, _ = _register(client)
    r = _create_review(client, ws, h, "some-campaign-id", target_type="campaign")
    assert r.status_code == 201, r.text
    review = r.json()
    assert review["bound_version"] is None
    assert review["bound_manifest_hash"] is None
    assert review["stale"] is False


# ---------------------------------------------------------------------------
# approve re-verifies the binding
# ---------------------------------------------------------------------------


def test_approve_on_moved_target_returns_409_and_stays_in_review(client, tmp_path, monkeypatch):
    """Edit the target after submitting, then APPROVE -> 409 + stale, IN_REVIEW."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)

    review = _create_review(client, ws, h, timeline_id).json()
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "IN_REVIEW"

    # the target moves: a different clip id -> a different manifest hash
    _save_timeline(client, ws, h, timeline_id, base_version=version,
                   clip_id="c2", duration=6.0)

    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "review target changed since review was requested" in detail["error"]
    assert detail["stale"] is True

    # state stays IN_REVIEW (not approved) and is now flagged stale
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}", headers=h)
    body = r.json()
    assert body["state"] == "IN_REVIEW"
    assert body["stale"] is True


# ---------------------------------------------------------------------------
# staleness on edit
# ---------------------------------------------------------------------------


def test_stale_after_edit_approval_valid_false(client, tmp_path, monkeypatch):
    """An APPROVED review keeps its state but reports approval_valid=false."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)

    review = _create_review(client, ws, h, timeline_id).json()
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["state"] == "APPROVED"
    assert approved["approval_valid"] is True

    # edit the timeline -> tip hash moves
    _save_timeline(client, ws, h, timeline_id, base_version=version,
                   clip_id="c9", duration=7.0)

    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}", headers=h)
    body = r.json()
    assert body["state"] == "APPROVED"       # history preserved
    assert body["stale"] is True             # tip moved
    assert body["approval_valid"] is False   # but the approval no longer counts
    assert body["stale_detected_at"] != ""


def test_list_also_refreshes_staleness(client, tmp_path, monkeypatch):
    """Staleness is refreshed lazily on LIST as well as GET."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)

    _save_timeline(client, ws, h, timeline_id, base_version=version, clip_id="cx", duration=8.0)

    r = client.get(f"/api/v1/workspaces/{ws}/reviews", headers=h)
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["id"] == review["id"])
    assert item["stale"] is True


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


def test_state_machine_illegal_transitions_409(client, tmp_path, monkeypatch):
    ws, h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()

    # DRAFT cannot be approved directly
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 409, r.text
    assert "illegal review transition" in r.json()["detail"]

    # submit, approve -> terminal; approving again is illegal
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 409, r.text


def test_changes_requested_then_rerequest_rebinds(client, tmp_path, monkeypatch):
    """CHANGES_REQUESTED -> IN_REVIEW re-request rebinds to the new version."""
    ws, h, _ = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()
    bound_before = review["bound_version"]

    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "REQUEST_CHANGES", "body": "trim it"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CHANGES_REQUESTED"

    # the target moves
    new_version = _save_timeline(client, ws, h, timeline_id, base_version=version,
                                 clip_id="c3", duration=4.0)

    # re-request rebinds to the new tip and clears stale
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "IN_REVIEW"
    assert body["bound_version"] == str(new_version)
    assert body["bound_version"] != bound_before
    assert body["stale"] is False


def test_cancel_is_terminal(client, tmp_path, monkeypatch):
    ws, h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()
    # DRAFT -> CANCELLED is NOT a contracts-§5 edge (illegal -> 409)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/cancel", headers=h)
    assert r.status_code == 409, r.text
    assert "illegal review transition" in r.json()["detail"]

    # IN_REVIEW -> CANCELLED is legal and terminal
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CANCELLED"
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    assert r.status_code == 409, r.text
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# REQUEST_CHANGES auto-opens a revision
# ---------------------------------------------------------------------------


def test_request_changes_auto_creates_revision(client, tmp_path, monkeypatch):
    ws, h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions", headers=h,
                    json={"decision": "REQUEST_CHANGES", "body": "tighten the cut"})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/revisions", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "OPEN"
    assert items[0]["review_id"] == review["id"]


# ---------------------------------------------------------------------------
# RBAC matrix (contracts §5)
# ---------------------------------------------------------------------------


def test_review_rbac_matrix(client, tmp_path, monkeypatch):
    ws, member_h, member_id = _register(client)
    viewer_h, _ = _add_user(client, ws, "viewer")
    timeline_id, _ = _make_timeline(client, ws, member_h)

    # viewer POST -> 403; member POST -> 201
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=viewer_h,
                    json={"target_type": "timeline_version", "target_id": timeline_id})
    assert r.status_code == 403, r.text
    r = _create_review(client, ws, member_h, timeline_id)
    assert r.status_code == 201, r.text

    # viewer GET -> 200
    r = client.get(f"/api/v1/workspaces/{ws}/reviews", headers=viewer_h)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


def test_project_scoped_rbac_viewer_403_reviewer_201(client, tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import ProjectMember

    ws, admin_h, admin_id = _register(client)
    # creator of the project is the workspace owner -> OWNER of the project
    r = client.post(f"/api/v1/workspaces/{ws}/projects", headers=admin_h,
                    json={"name": "P", "description": ""})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    pv_h, pv_id = _add_user(client, ws, "member")
    pr_h, pr_id = _add_user(client, ws, "member")
    with session_scope() as s:
        s.add(ProjectMember(project_id=project_id, user_id=pv_id, role="VIEWER"))
        s.add(ProjectMember(project_id=project_id, user_id=pr_id, role="REVIEWER"))
    s = None  # noqa: F841

    timeline_id, _ = _make_timeline(client, ws, admin_h)
    payload = {"target_type": "timeline_version", "target_id": timeline_id,
               "title": "scoped", "project_id": project_id}

    # project VIEWER -> 403
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=pv_h, json=payload)
    assert r.status_code == 403, r.text
    # project REVIEWER -> 201
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=pr_h, json=payload)
    assert r.status_code == 201, r.text


def test_capabilities_badges_on_detail(client, tmp_path, monkeypatch):
    ws, h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, h)
    review = _create_review(client, ws, h, timeline_id).json()
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}", headers=h)
    assert r.status_code == 200, r.text
    caps = r.json()["capabilities"]
    for key in ("can_approve", "can_request_changes", "can_cancel", "can_assign"):
        assert key in caps, caps
        assert isinstance(caps[key], bool)
    # the workspace owner (creator) holds every badge on an unlinked target
    assert caps == {"can_approve": True, "can_request_changes": True,
                    "can_cancel": True, "can_assign": True}


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_review_workspace_isolation_404(client, tmp_path, monkeypatch):
    ws_a, h_a, _ = _register(client)
    ws_b, h_b, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws_a, h_a)
    review = _create_review(client, ws_a, h_a, timeline_id).json()

    # a review from workspace A is invisible (404, not 403) in workspace B
    r = client.get(f"/api/v1/workspaces/{ws_b}/reviews/{review['id']}", headers=h_b)
    assert r.status_code == 404, r.text
    r = client.post(f"/api/v1/workspaces/{ws_b}/reviews/{review['id']}/submit", headers=h_b)
    assert r.status_code == 404, r.text
    # and it does not leak into workspace B's list
    r = client.get(f"/api/v1/workspaces/{ws_b}/reviews", headers=h_b)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------


def test_assign_reviewer_and_validation(client, tmp_path, monkeypatch):
    ws, h, _ = _register(client)
    _, other_id = _add_user(client, ws, "member")
    timeline_id, _ = _make_timeline(client, ws, h)

    # reviewer list on create
    r = _create_review(client, ws, h, timeline_id, reviewers=[other_id])
    assert r.status_code == 201, r.text
    review = r.json()
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}", headers=h)
    assert [a["user_id"] for a in r.json()["assignments"]] == [other_id]

    # assign a non-member -> 404
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/assignments",
                    headers=h, json={"user_id": "no-such-user"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# RBAC deep matrix: no-membership floor, decision caps, linked targets
# ---------------------------------------------------------------------------


def _make_project(client, ws, headers, name="P"):
    r = client.post(f"/api/v1/workspaces/{ws}/projects", headers=headers,
                    json={"name": name, "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add_project_member(client, ws, headers, project_id, user_id, role):
    r = client.post(f"/api/v1/workspaces/{ws}/projects/{project_id}/members",
                    headers=headers, json={"user_id": user_id, "role": role})
    assert r.status_code == 201, r.text
    return r


def test_post_review_without_membership_403(client, tmp_path, monkeypatch):
    """No membership row at all (not even viewer) -> 403 on POST and GET."""
    ws, h, _ = _register(client)
    timeline_id, _ = _make_timeline(client, ws, h)
    _, foreign_h, _ = _register(client)  # different workspace, no membership here
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=foreign_h,
                    json={"target_type": "timeline_version", "target_id": timeline_id})
    assert r.status_code == 403, r.text
    r = client.get(f"/api/v1/workspaces/{ws}/reviews", headers=foreign_h)
    assert r.status_code == 403, r.text


def test_decision_caps_project_matrix(client, tmp_path, monkeypatch):
    """APPROVE needs the `approve` cap, REQUEST_CHANGES `request_revision`
    (contracts §3/§5): project VIEWER neither; project EDITOR neither
    (§3 gives EDITOR neither of those caps); project REVIEWER both."""
    ws, owner_h, _ = _register(client)
    project_id = _make_project(client, ws, owner_h)
    v_h, v_id = _add_user(client, ws, "member")
    r_h, r_id = _add_user(client, ws, "member")
    e_h, e_id = _add_user(client, ws, "member")
    _add_project_member(client, ws, owner_h, project_id, v_id, "VIEWER")
    _add_project_member(client, ws, owner_h, project_id, r_id, "REVIEWER")
    _add_project_member(client, ws, owner_h, project_id, e_id, "EDITOR")

    timeline_id, _ = _make_timeline(client, ws, owner_h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=owner_h,
                    json={"target_type": "timeline_version", "target_id": timeline_id,
                          "title": "scoped", "project_id": project_id})
    assert r.status_code == 201, r.text
    review_id = r.json()["id"]
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review_id}/submit", headers=owner_h)

    def decide(headers, decision):
        return client.post(f"/api/v1/workspaces/{ws}/reviews/{review_id}/decisions",
                           headers=headers, json={"decision": decision})

    # project VIEWER: neither cap -> 403
    assert decide(v_h, "APPROVE").status_code == 403
    assert decide(v_h, "REQUEST_CHANGES").status_code == 403
    # project EDITOR: can edit, but has NEITHER approve nor request_revision
    assert decide(e_h, "APPROVE").status_code == 403
    assert decide(e_h, "REQUEST_CHANGES").status_code == 403
    # project REVIEWER: request_revision -> 200 (+ auto revision request)
    r = decide(r_h, "REQUEST_CHANGES")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CHANGES_REQUESTED"
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review_id}/revisions",
                   headers=owner_h)
    assert len(r.json()["items"]) == 1
    # re-request, then REVIEWER approve -> 200
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review_id}/submit", headers=owner_h)
    r = decide(r_h, "APPROVE")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "APPROVED"


def test_linked_target_membership_missing_403_admin_bypass(client, tmp_path, monkeypatch):
    """Target linked to a project (target-type lookup path):
    missing project membership -> 403; project VIEWER -> 403 / REVIEWER -> 201;
    workspace admin bypasses even without a project role (contracts §3)."""
    from app.db import session_scope
    from app.models import ProjectTarget

    ws, owner_h, _ = _register(client)
    project_id = _make_project(client, ws, owner_h)
    timeline_id, _ = _make_timeline(client, ws, owner_h)
    # review targets use type "timeline_version" (the projects API link
    # vocabulary only covers content/campaign/timeline/localization/ugc_asset),
    # so the link row -- what project_auth actually looks up -- is inserted here.
    with session_scope() as s:
        s.add(ProjectTarget(project_id=project_id, target_type="timeline_version",
                            target_id=timeline_id))

    m_h, _ = _add_user(client, ws, "member")   # ws member, NOT a project member
    admin_h, _ = _add_user(client, ws, "admin")
    r_h, r_id = _add_user(client, ws, "member")
    v_h, v_id = _add_user(client, ws, "member")
    _add_project_member(client, ws, owner_h, project_id, r_id, "REVIEWER")
    _add_project_member(client, ws, owner_h, project_id, v_id, "VIEWER")

    payload = {"target_type": "timeline_version", "target_id": timeline_id,
               "title": "linked"}

    # membership missing on a LINKED target -> 403 (never 201)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=m_h, json=payload)
    assert r.status_code == 403, r.text
    # project VIEWER -> 403, project REVIEWER -> 201 (target-linkage path)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=v_h, json=payload)
    assert r.status_code == 403, r.text
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=r_h, json=payload)
    assert r.status_code == 201, r.text

    # workspace admin (governance bypass, no project role) may create + approve
    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=admin_h, json=payload)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/submit", headers=admin_h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/decisions",
                    headers=m_h, json={"decision": "APPROVE"})
    assert r.status_code == 403, r.text    # still missing membership
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/decisions",
                    headers=admin_h, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "APPROVED"


def test_assignment_caps_and_duplicate_assign(client, tmp_path, monkeypatch):
    """POST /assignments needs `request_revision`; duplicates append (201, two
    rows) -- lock the ACTUAL behavior: neither idempotent nor 409."""
    ws, owner_h, _ = _register(client)
    project_id = _make_project(client, ws, owner_h)
    v_h, v_id = _add_user(client, ws, "member")
    r_h, r_id = _add_user(client, ws, "member")
    _add_project_member(client, ws, owner_h, project_id, v_id, "VIEWER")
    _add_project_member(client, ws, owner_h, project_id, r_id, "REVIEWER")

    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=owner_h,
                    json={"target_type": "campaign", "target_id": "camp-1",
                          "title": "assign", "project_id": project_id})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/assignments",
                    headers=v_h, json={"user_id": v_id})
    assert r.status_code == 403, r.text    # VIEWER lacks request_revision
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/assignments",
                    headers=r_h, json={"user_id": v_id})
    assert r.status_code == 201, r.text    # REVIEWER holds it
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{rid}/assignments",
                    headers=r_h, json={"user_id": v_id})
    assert r.status_code == 201, r.text    # duplicate -> append-only, not 409
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{rid}", headers=owner_h)
    assert sum(1 for a in r.json()["assignments"] if a["user_id"] == v_id) == 2


def test_capabilities_reflect_project_roles(client, tmp_path, monkeypatch):
    """FE badge flags flip with the project role (detail `capabilities`)."""
    ws, owner_h, _ = _register(client)
    project_id = _make_project(client, ws, owner_h)
    v_h, v_id = _add_user(client, ws, "member")
    r_h, r_id = _add_user(client, ws, "member")
    _add_project_member(client, ws, owner_h, project_id, v_id, "VIEWER")
    _add_project_member(client, ws, owner_h, project_id, r_id, "REVIEWER")

    r = client.post(f"/api/v1/workspaces/{ws}/reviews", headers=owner_h,
                    json={"target_type": "campaign", "target_id": "camp-2",
                          "title": "caps", "project_id": project_id})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    def caps(headers):
        r = client.get(f"/api/v1/workspaces/{ws}/reviews/{rid}", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["capabilities"]

    assert caps(owner_h) == {"can_approve": True, "can_request_changes": True,
                             "can_cancel": True, "can_assign": True}
    assert caps(v_h) == {"can_approve": False, "can_request_changes": False,
                         "can_cancel": False, "can_assign": False}
    r_caps = caps(r_h)
    assert r_caps["can_approve"] is True
    assert r_caps["can_request_changes"] is True
    assert r_caps["can_assign"] is True
    assert r_caps["can_cancel"] is False    # not the creator, not ws admin


def test_transition_events_and_decision_history(client, tmp_path, monkeypatch):
    """Transitions emit §9 kinds with actor/target/version/project_id in
    data_json; decision rows are append-only with their OWN binding."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EventLog

    ws, h, uid = _register(client)
    timeline_id, version = _make_timeline(client, ws, h)

    review = _create_review(client, ws, h, timeline_id).json()
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review['id']}/decisions",
                    headers=h, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text

    review2 = _create_review(client, ws, h, timeline_id).json()
    client.post(f"/api/v1/workspaces/{ws}/reviews/{review2['id']}/submit", headers=h)
    r = client.post(f"/api/v1/workspaces/{ws}/reviews/{review2['id']}/decisions",
                    headers=h, json={"decision": "REQUEST_CHANGES", "body": "tighten"})
    assert r.status_code == 200, r.text

    with session_scope() as s:
        rows = s.scalars(select(EventLog).where(EventLog.workspace_id == ws)).all()
        kinds = [row.kind for row in rows]
        for kind in ("REVIEW_REQUESTED", "APPROVED", "CHANGES_REQUESTED"):
            assert kind in kinds, kinds
        ev = next(row for row in rows if row.kind == "APPROVED")
        data = dict(ev.data_json or {})
        assert data.get("actor") == uid
        assert data.get("target") == {"type": "timeline_version", "id": timeline_id}
        assert str(data.get("version")) == str(version)
        assert "project_id" in data        # rides in data_json (None when unscoped)

    # append-only decision history: the APPROVE froze the binding it saw
    r = client.get(f"/api/v1/workspaces/{ws}/reviews/{review['id']}", headers=h)
    assert r.status_code == 200, r.text
    decisions = r.json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "APPROVE"
    assert decisions[0]["bound_version"] == str(version)
    assert decisions[0]["bound_manifest_hash"]
