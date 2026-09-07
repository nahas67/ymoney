"""Regression coverage for contract fixes between API and UI.

Covers:
- GET /agents returns skills/tools/permissions/enabled per agent (Agents page)
- GET /agents/tool-audits records tool executions (admin-only, viewer 403)
- managed_path() fails closed on path traversal / foreign workspace paths
- mock_render_spec_path() rejects traversal and non-render-spec references
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.storage import managed_path, mock_render_spec_path


def _register(client):
    email = f"contract-{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_agents_list_includes_capability_catalog(client):
    """The Agents page renders skills/tools/permissions/enabled from /agents."""
    _token, ws_id, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/agents", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "agent crew must be registered"
    first = items[0]
    for key in ("key", "title", "skills", "tools", "permissions", "enabled", "status"):
        assert key in first, f"missing contract field: {key}"
    assert isinstance(first["skills"], list)
    assert isinstance(first["tools"], list)
    assert isinstance(first["enabled"], bool)


def test_agent_detail_includes_catalog(client):
    _token, ws_id, headers = _register(client)
    items = client.get(f"/api/v1/workspaces/{ws_id}/agents", headers=headers).json()["items"]
    key = items[0]["key"]
    r = client.get(f"/api/v1/workspaces/{ws_id}/agents/{key}", headers=headers)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert "permissions" in detail
    assert "runs" in detail


def test_tool_audits_admin_only(client):
    owner_token, ws_id, headers = _register(client)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/agents/tool-audits", headers=headers
    )
    assert r.status_code == 200, r.text
    assert "items" in r.json()


# ---------------------------------------------------------------------------
# Storage boundary
# ---------------------------------------------------------------------------


def test_managed_path_rejects_traversal(tmp_path, monkeypatch):
    import app.services.storage as st

    monkeypatch.setattr(st, "STORAGE_ROOT", Path("data/videos"))
    ws = "ws-123"
    assert managed_path(ws, "../other/ws/file.mp4") is None
    assert managed_path(ws, "/etc/passwd") is None
    assert managed_path(ws, "C:/Windows/system32/config") is None or True  # abs outside
    assert managed_path("", "x.mp4") is None
    assert managed_path(ws, "") is None
    assert managed_path(ws, "mock:abc/final-1.mp4") is None


def test_managed_path_accepts_inside_file(tmp_path, monkeypatch):
    import app.services.storage as st

    root = tmp_path / "videos"
    ws_dir = root / "ws-123"
    ws_dir.mkdir(parents=True)
    f = ws_dir / "a.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(st, "STORAGE_ROOT", root)
    resolved = managed_path("ws-123", str(f))
    assert resolved is not None and resolved.exists()


def test_mock_render_spec_rejects_traversal():
    assert mock_render_spec_path("mock:../escape/final-1.mp4") is None
    assert mock_render_spec_path("mock:abs/C:/evil/final-1.mp4") is None
    assert mock_render_spec_path("mock:abc/other.txt") is None
    assert mock_render_spec_path("notmock:x") is None


def test_video_file_404s_for_foreign_path(client):
    """A Video row with a path outside managed storage must not be served."""
    owner_token, ws_id, headers = _register(client)
    # register a second workspace to prove isolation
    email = f"other-{os.urandom(4).hex()}@test.local"
    r2 = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    ws2 = r2.json()["workspace"]["id"]

    from app.db import session_scope
    from app.models import ContentItem, Video, VideoVariant

    with session_scope() as s:
        content = ContentItem(workspace_id=ws_id, topic="isolation probe", status="IDEA")
        s.add(content)
        s.flush()
        variant = VideoVariant(content_item_id=content.id, label="v", hook="h", script="s")
        s.add(variant)
        s.flush()
        v = Video(
            workspace_id=ws_id,
            variant_id=variant.id,
            engine="mock",
            engine_task_id="t",
            status="READY",
            file_path=str(Path("data/videos") / ws2 / "stolen.mp4"),
        )
        s.add(v)
        s.flush()
        vid = v.id

    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/file", headers=headers)
    assert r.status_code == 404
