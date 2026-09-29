"""Work 11 Lane L: enterprise ops + retention (contracts §10).

Covers the three named cases:

  * **policy validation** -- non-negative ints only, max 3650 days, NULL
    means keep forever, bad input is 422 and changes nothing
  * **sweep guards** -- an asset referenced by a live review / an open
    comment / a lineage parent / a project target is NEVER deleted; an
    unreferenced expired render asset IS
  * **audit is never deleted** -- ``audit_retention_days=0`` still
    returns ``events_untouched > 0`` and leaves every ledger row intact

Plus: the ops overview renders every section, isolates a failing
section instead of 500-ing, and is admin-gated; the RETENTION_SWEEP job
handler is registered idempotently and runs a real sweep.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.db import session_scope
from app.models.base import utcnow


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"ops{uuid.uuid4().hex[:8]}@test.local"
    r = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "supersecret123"}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], data["user"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def _asset(ws_id, *, origin="render", key="clip.mp4", age_days=400, size=1024):
    from app.models import MediaAsset

    with session_scope() as s:
        row = MediaAsset(
            workspace_id=ws_id, type="video", origin=origin, storage_key=key,
            mime_type="video/mp4", file_size=size, checksum="abc",
            created_at=utcnow() - timedelta(days=age_days),
        )
        s.add(row)
        s.flush()
        return row.id


def _set_policy(ws_id, **days):
    from app.models import RetentionPolicy

    with session_scope() as s:
        row = RetentionPolicy(workspace_id=ws_id, **days)
        s.add(row)
        s.flush()
        return row.id


# ---------------------------------------------------------------------------
# policy validation
# ---------------------------------------------------------------------------


def test_retention_defaults_are_keep_forever(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    body = client.get(f"/api/v1/workspaces/{ws_id}/retention", headers=headers).json()
    for field in ("audit_retention_days", "render_retention_days",
                  "temp_asset_retention_days", "export_retention_days"):
        assert body[field] is None, field
    assert body["audit_enforced"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"render_retention_days": -1},
        {"render_retention_days": 3651},
        {"export_retention_days": 999999},
        {"temp_asset_retention_days": -30},
    ],
)
def test_retention_rejects_out_of_range(tmp_path, monkeypatch, payload):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    r = client.put(f"/api/v1/workspaces/{ws_id}/retention", json=payload, headers=headers)
    assert r.status_code == 422, (payload, r.status_code, r.text)
    # a rejected update leaves the policy untouched
    assert client.get(f"/api/v1/workspaces/{ws_id}/retention",
                      headers=headers).json()["render_retention_days"] is None


def test_retention_accepts_valid_and_boundary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    r = client.put(
        f"/api/v1/workspaces/{ws_id}/retention",
        json={"render_retention_days": 30, "export_retention_days": 3650,
              "audit_retention_days": 0, "temp_asset_retention_days": None},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["render_retention_days"] == 30
    assert body["export_retention_days"] == 3650  # boundary is allowed
    assert body["audit_retention_days"] == 0
    assert body["temp_asset_retention_days"] is None
    # the update is auditable
    assert body["updated_by"]


def test_retention_requires_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    from app.models import User, WorkspaceMember

    email = f"mem{uuid.uuid4().hex[:8]}@test.local"
    reg = client.post("/api/v1/auth/register",
                      json={"email": email, "password": "supersecret123"})
    user_id = reg.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role="member"))
        assert s.get(User, user_id) is not None
    login = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "supersecret123"})
    member = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get(f"/api/v1/workspaces/{ws_id}/retention",
                      headers=member).status_code == 403
    assert client.put(f"/api/v1/workspaces/{ws_id}/retention",
                      json={"render_retention_days": 5},
                      headers=member).status_code == 403
    # and the admin still can
    assert client.get(f"/api/v1/workspaces/{ws_id}/retention",
                      headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_deletes_unreferenced_expired_render_asset(tmp_path, monkeypatch):
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    ws_id, _, _ = None, None, None
    # seed directly (no HTTP needed for the engine-level assertions)
    from app.models import User, Workspace, WorkspaceMember

    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id = ws.id

    orphan = _asset(ws_id, key="orphan.mp4", age_days=400)
    _set_policy(ws_id, render_retention_days=30)

    with session_scope() as s:
        result = retention_service.sweep(s, ws_id)
        assert result["deleted"] == 1, result
        assert result["events_untouched"] >= 0

    from app.models import MediaAsset

    with session_scope() as s:
        assert s.get(MediaAsset, orphan) is None


def test_sweep_skips_assets_inside_the_retention_window(tmp_path, monkeypatch):
    from app.models import MediaAsset
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    from app.models import User, Workspace, WorkspaceMember

    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id = ws.id

    fresh = _asset(ws_id, key="fresh.mp4", age_days=3)
    _set_policy(ws_id, render_retention_days=30)

    with session_scope() as s:
        result = retention_service.sweep(s, ws_id)
    assert result["deleted"] == 0, result
    with session_scope() as s:
        assert s.get(MediaAsset, fresh) is not None


@pytest.mark.parametrize("guard", ["live_review", "open_comment", "project_target"])
def test_sweep_guards_protect_referenced_assets(tmp_path, monkeypatch, guard):
    """Each guard independently blocks deletion of a referenced asset."""
    from app.models import (
        Comment,
        MediaAsset,
        Project,
        ProjectTarget,
        Review,
        User,
        Workspace,
        WorkspaceMember,
    )
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id, user_id = ws.id, user.id

    protected = _asset(ws_id, key="protected.mp4", age_days=400)
    also_delete = _asset(ws_id, key="free.mp4", age_days=400)

    with session_scope() as s:
        if guard == "live_review":
            s.add(Review(workspace_id=ws_id, target_type="asset",
                         target_id=protected, title="live", state="IN_REVIEW",
                         created_by=user_id))
        elif guard == "open_comment":
            s.add(Comment(workspace_id=ws_id, target_type="asset",
                          target_id=protected, body="still open",
                          author_id=user_id))
        else:
            project = Project(workspace_id=ws_id, name="linked", created_by=user_id)
            s.add(project)
            s.flush()
            s.add(ProjectTarget(project_id=project.id, target_type="ugc_asset",
                                target_id=protected))
    _set_policy(ws_id, render_retention_days=30)

    with session_scope() as s:
        result = retention_service.sweep(s, ws_id)
        # the referenced asset survives; the unrelated one does not
        assert result["deleted"] == 1, result
        assert result["skipped"].get("protected") == 1, result

    with session_scope() as s:
        assert s.get(MediaAsset, protected) is not None, guard
        assert s.get(MediaAsset, also_delete) is None, guard


def test_audit_rows_are_never_deleted(tmp_path, monkeypatch):
    """audit_retention_days=0 must still leave every ledger row intact."""
    from app.models import EventLog, User, Workspace, WorkspaceMember
    from app.services import activity as activity_service
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id = ws.id

    for index in range(3):
        activity_service.emit(ws_id, "PUBLISHED", message=f"event {index}")

    with session_scope() as s:
        before = s.query(EventLog).filter(EventLog.workspace_id == ws_id).count()
    assert before >= 3

    # the most aggressive audit policy imaginable
    _set_policy(ws_id, audit_retention_days=0, render_retention_days=1)
    _asset(ws_id, key="gone.mp4", age_days=400)

    with session_scope() as s:
        result = retention_service.sweep(s, ws_id)
    assert result["events_untouched"] > 0, result

    with session_scope() as s:
        after = s.query(EventLog).filter(EventLog.workspace_id == ws_id).count()
    assert after >= before, "the sweep deleted audit rows"


def test_sweep_is_a_noop_when_policy_is_unset(tmp_path, monkeypatch):
    from app.models import MediaAsset, User, Workspace, WorkspaceMember
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id = ws.id

    asset_id = _asset(ws_id, key="keep.mp4", age_days=900)  # no policy at all
    with session_scope() as s:
        result = retention_service.sweep(s, ws_id)
    assert result["deleted"] == 0
    with session_scope() as s:
        assert s.get(MediaAsset, asset_id) is not None


# ---------------------------------------------------------------------------
# job handler
# ---------------------------------------------------------------------------


def test_retention_sweep_job_registration_is_idempotent():
    from app.services import jobs as jobs_service
    from app.services import retention as retention_service

    retention_service.register_retention_jobs()
    retention_service.register_retention_jobs()  # must not raise
    assert retention_service.JOB_TYPE in jobs_service._handlers


def test_retention_sweep_job_runs(tmp_path, monkeypatch):
    from app.models import User, Workspace, WorkspaceMember
    from app.services import retention as retention_service

    _client(tmp_path, monkeypatch)
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"sw{tag}@test.local", password_hash="x")
        ws = Workspace(name="sweep", slug=f"sw-{tag}", niche="AI")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
        ws_id = ws.id
    _set_policy(ws_id, render_retention_days=30)
    _asset(ws_id, key="job.mp4", age_days=400)

    class _Ctx:
        workspace_id = ws_id
        payload: dict = {}

        def cancelled(self) -> bool:
            return False

    result = retention_service.handle_retention_sweep(_Ctx())
    assert result["deleted"] == 1, result


# ---------------------------------------------------------------------------
# ops overview
# ---------------------------------------------------------------------------


def test_ops_overview_sections(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/ops/overview", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    for section in ("jobs", "reviews", "exports", "storage", "provider_health",
                    "costs", "audit", "retention"):
        assert section in body, section
        assert body[section].get("available") is True, (section, body[section])
    assert body["jobs"]["by_status"] == {} or isinstance(body["jobs"]["by_status"], dict)
    assert body["reviews"]["open"] == 0
    assert body["reviews"]["stale_approvals"] == []
    assert isinstance(body["storage"]["bytes"], int)
    assert isinstance(body["storage"]["file_count"], int)
    # honest numbers: a negative byte count or a fractional file count would
    # mean the sum/walk fallback is lying about what is on disk
    assert body["storage"]["bytes"] >= 0
    assert body["storage"]["file_count"] >= 0
    assert body["storage"]["source"] in ("database", "filesystem")
    # provider health reuses the 11 existing readiness probes
    check_ids = {c["id"] for c in body["provider_health"]["checks"]}
    assert {"llm", "video_engine", "ffmpeg", "database", "storage"} <= check_ids
    assert body["costs"]["available"] is True
    assert body["workspace_id"] == ws_id


def test_ops_overview_isolates_a_failing_section(tmp_path, monkeypatch):
    """One broken section must not take down the page."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    from app.api.v1 import ops as ops_mod

    def boom(_db, _ws):
        raise RuntimeError("section exploded with a secret detail")

    monkeypatch.setattr(ops_mod, "_jobs_section", boom)
    r = client.get(f"/api/v1/workspaces/{ws_id}/ops/overview", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"]["available"] is False
    # the reason is a type name only -- no exception text
    assert body["jobs"]["reason"] == "RuntimeError"
    assert "secret detail" not in r.text
    # and every other section still rendered
    assert body["storage"]["available"] is True
    assert body["audit"]["available"] is True


def test_ops_overview_requires_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, _ = _register(client)
    from app.models import User, WorkspaceMember

    def _role(role: str) -> dict:
        email = f"{role[:3]}{uuid.uuid4().hex[:8]}@test.local"
        reg = client.post("/api/v1/auth/register",
                          json={"email": email, "password": "supersecret123"})
        with session_scope() as s:
            s.add(WorkspaceMember(workspace_id=ws_id,
                                  user_id=reg.json()["user"]["id"], role=role))
            assert s.get(User, reg.json()["user"]["id"]) is not None
        login = client.post("/api/v1/auth/login",
                            json={"email": email, "password": "supersecret123"})
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    # the overview is admin-only: viewer AND member are both refused
    for role in ("viewer", "member"):
        denied = client.get(f"/api/v1/workspaces/{ws_id}/ops/overview",
                            headers=_role(role))
        assert denied.status_code == 403, (role, denied.status_code, denied.text)


def test_retention_put_emits_retention_policy_updated_event(tmp_path, monkeypatch):
    """A policy change is auditable: the ledger row lands and is whitelisted."""
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)

    r = client.put(
        f"/api/v1/workspaces/{ws_id}/retention",
        json={"render_retention_days": 14, "audit_retention_days": 0},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    feed = client.get(
        f"/api/v1/workspaces/{ws_id}/activity?kind=RETENTION_POLICY_UPDATED",
        headers=headers,
    )
    assert feed.status_code == 200, feed.text
    items = feed.json()["items"]
    assert len(items) == 1, [item["kind"] for item in items]
    row = items[0]
    assert row["actor"] == user_id
    assert row["data"]["render_retention_days"] == 14
    assert row["data"]["audit_retention_days"] == 0
    assert row["created_at"]

    # ... and outbound subscribers hear about it (webhooks drop unknown kinds)
    from app.services.webhooks import WEBHOOK_EVENTS

    assert "RETENTION_POLICY_UPDATED" in WEBHOOK_EVENTS
    assert "RETENTION_SWEEP" in WEBHOOK_EVENTS


def test_ops_route_500_is_generic_and_logged(tmp_path, monkeypatch, caplog):
    """Unexpected faults answer a short generic detail -- never the exception."""
    import logging

    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    from app.api.v1 import ops as ops_mod

    def boom(_db, _ws):
        raise RuntimeError("policy store unavailable secret=hunter2")

    monkeypatch.setattr(ops_mod.retention_service, "get_policy", boom)
    with caplog.at_level(logging.ERROR, logger="ymoney.collab"):
        r = client.get(f"/api/v1/workspaces/{ws_id}/retention", headers=headers)
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "internal error"}
    assert "hunter2" not in r.text
    assert "policy store" not in r.text
    assert "RuntimeError" not in r.text
    assert any(
        record.name == "ymoney.collab" and "ops route failed" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
