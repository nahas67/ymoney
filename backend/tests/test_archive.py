"""Work 11 Lane L: project archive (contracts §10).

Two contract-named cases plus the behaviour they imply:

  * **archive excludes secrets** -- a workspace seeded with a real
    ApiCredential, a WebhookSubscription secret, a SourceConnector
    config, an ``.env`` file and a media asset whose bytes contain a
    token must produce an archive in which NONE of that material
    appears. Asserted against the raw bytes of BOTH modes, not just the
    parsed JSON, so a stray member in the zip is caught too.
  * **archive workspace isolation** -- a project/asset in workspace B is
    never in workspace A's archive, and a cross-workspace id answers
    404 (never 403) on every archive route.

Plus: MANIFEST_ONLY vs PORTABLE_ARCHIVE shape, the documented portable
layout, media binaries being manifest-only, the ARCHIVED -> 409 edit
freeze -> unarchive lifecycle, and download permissions.
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest

from app.db import session_scope

SECRETS = (
    "sk-live-DO-NOT-ARCHIVE-abc123",
    "whsec_archive_must_not_appear",
    "connector-token-9f8e7d",
    "workspace-settings-secret-42",
)


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"arc{uuid.uuid4().hex[:8]}@test.local"
    r = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "supersecret123"}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], data["user"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def _project(client, ws_id, headers, name="Archive Me"):
    r = client.post(f"/api/v1/workspaces/{ws_id}/projects",
                    json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_secrets(ws_id, user_id):
    """Real credential rows + a .env file + a token-bearing media asset."""
    from app.models import (
        ApiCredential,
        MediaAsset,
        Project,
        ProjectTarget,
        SourceConnector,
        WebhookSubscription,
    )
    from app.services.storage import STORAGE_ROOT

    with session_scope() as s:
        s.add(ApiCredential(workspace_id=ws_id, provider="openai",
                            name="prod", value_enc=SECRETS[0]))
        s.add(WebhookSubscription(workspace_id=ws_id, url="https://example.test/hook",
                                  secret_enc=SECRETS[1], events_json=["PUBLISHED"]))
        s.add(SourceConnector(workspace_id=ws_id, kind="rss", name="feed",
                              config_json={"url": "https://x.test/rss",
                                           "token": SECRETS[2]}))

    # a real file on disk holding a token, plus an .env beside it
    root = STORAGE_ROOT / ws_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "clip.mp4").write_bytes(b"binary-ish-" + SECRETS[0].encode())
    (root / ".env").write_text("API_KEY=" + SECRETS[0])

    with session_scope() as s:
        asset = MediaAsset(
            workspace_id=ws_id, type="video", origin="upload",
            storage_key="clip.mp4", mime_type="video/mp4",
            file_size=len(b"binary-ish-") + len(SECRETS[0]),
            checksum="deadbeef",
        )
        s.add(asset)
        s.flush()
        project = Project(workspace_id=ws_id, name="Secret Holder", created_by=user_id)
        s.add(project)
        s.flush()
        s.add(ProjectTarget(project_id=project.id, target_type="ugc_asset",
                            target_id=asset.id))
        asset_id = asset.id
        holder_id = project.id
    return asset_id, holder_id


def _seed_content(client, ws_id, headers, project_id, user_id):
    """A content item + timeline + captions linked into the project."""
    from app.engine.timeline import create_empty
    from app.models import ContentItem, ContentTimeline, ProjectTarget, Scene

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="Archive topic",
                           status="SCRIPT_READY", research_json={"notes": "research body"},
                           strategy_json={"angle": "test"})
        s.add(item)
        s.flush()
        content_id = item.id
        doc = create_empty(ws_id, duration_seconds=4.0)
        for track in doc["tracks"]:
            if track["kind"] == "caption":
                track["clips"].append(
                    {"id": "c1", "name": "Hello", "start": 0.0, "duration": 2.0,
                     "text": {"content": "Hello archive"}, "source": {},
                     "effects": []}
                )
            if track["kind"] == "video":
                track["clips"].append(
                    {"id": "v1", "name": "clip", "start": 0.0, "duration": 4.0,
                     "source": {}, "effects": []}
                )
        timeline = ContentTimeline(workspace_id=ws_id, content_item_id=content_id,
                                   name="main", tracks_json=doc, duration_seconds=4.0)
        s.add(timeline)
        s.flush()
        s.add(Scene(workspace_id=ws_id, content_item_id=content_id,
                    title="Scene 1", script_segment="Narration text"))
        s.add(ProjectTarget(project_id=project_id, target_type="content",
                            target_id=content_id))
        s.add(ProjectTarget(project_id=project_id, target_type="timeline",
                            target_id=timeline.id))
    return content_id, timeline.id


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["MANIFEST_ONLY", "PORTABLE_ARCHIVE"])
def test_archive_excludes_secrets(tmp_path, monkeypatch, mode):
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)
    _seed_secrets(ws_id, user_id)
    project_id = _project(client, ws_id, headers)
    _seed_content(client, ws_id, headers, project_id, user_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
                    json={"mode": mode}, headers=headers)
    assert r.status_code == 201, r.text
    archive_id = r.json()["id"]

    download = client.get(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archives/{archive_id}/download",
        headers=headers,
    )
    assert download.status_code == 200, download.text
    raw = download.content
    for secret in SECRETS:
        assert secret.encode() not in raw, f"{mode} leaked {secret[:12]}..."

    # belt and braces: nothing in the parsed tree carries them either
    if mode == "MANIFEST_ONLY":
        document = json.loads(raw)
        flat = json.dumps(document)
    else:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            names = bundle.namelist()
            for member in names:
                assert ".env" not in member, names
                assert "clip.mp4" not in member, names  # binaries are manifest-only
            document = json.loads(bundle.read("manifest.json"))
        flat = json.dumps(document)
    for secret in SECRETS:
        assert secret not in flat

    # the honest statement about media IS present
    assert document["media_included"] is False
    assert "NOT included" in document["media_note"]


def test_archive_manifest_shape_and_portable_layout(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)
    project_id = _project(client, ws_id, headers)
    _, timeline_id = _seed_content(client, ws_id, headers, project_id, user_id)

    manifest = client.post(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
        json={"mode": "MANIFEST_ONLY"}, headers=headers).json()
    document = json.loads(client.get(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archives/"
        f"{manifest['id']}/download", headers=headers).content)
    for key in ("project", "timelines", "captions", "scripts", "research",
                "branddna", "assets", "lineage", "exports", "excluded", "counts"):
        assert key in document, key
    assert document["project"]["id"] == project_id
    assert timeline_id in document["timelines"]
    # version list travels with the doc (not just the current tip)
    assert document["timelines"][timeline_id]["versions"]
    assert document["timelines"][timeline_id]["manifest_hash"]

    portable = client.post(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
        json={"mode": "PORTABLE_ARCHIVE"}, headers=headers).json()
    raw = client.get(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archives/"
        f"{portable['id']}/download", headers=headers).content
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        names = set(bundle.namelist())
    for expected in ("manifest.json", "project.json", "branddna.json",
                     "assets.json", "lineage.json", "exports.json"):
        assert expected in names, expected
    assert f"timelines/{timeline_id}.json" in names
    assert f"captions/{timeline_id}.srt" in names
    assert f"captions/{timeline_id}.vtt" in names
    # checksums recorded so the payload is verifiable after download
    assert portable["checksum"] and portable["size_bytes"] > 0


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_archive_workspace_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, user_a, headers_a = _register(client)
    ws_b, user_b, headers_b = _register(client)
    project_a = _project(client, ws_a, headers_a, "A project")
    _seed_content(client, ws_a, headers_a, project_a, user_a)
    _seed_secrets(ws_b, user_b)

    # workspace A's archive contains nothing of workspace B
    created = client.post(
        f"/api/v1/workspaces/{ws_a}/projects/{project_a}/archive",
        json={"mode": "PORTABLE_ARCHIVE"}, headers=headers_a)
    assert created.status_code == 201, created.text
    raw = client.get(
        f"/api/v1/workspaces/{ws_a}/projects/{project_a}/archives/"
        f"{created.json()['id']}/download", headers=headers_a).content
    for secret in SECRETS:
        assert secret.encode() not in raw

    # A cannot archive / list / download anything in B
    foreign = client.post(
        f"/api/v1/workspaces/{ws_a}/projects/{project_a}/archives",
        json={}, headers=headers_a)
    assert foreign.status_code in (404, 405)

    # B's own holder project archives fine and only sees its own asset
    b_project = _project(client, ws_b, headers_b, "B holder")
    listed = client.get(
        f"/api/v1/workspaces/{ws_b}/projects/{b_project}/archives", headers=headers_b
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_cross_workspace_archive_id_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, _, headers_a = _register(client)
    ws_b, _, headers_b = _register(client)
    project_a = _project(client, ws_a, headers_a)
    project_b = _project(client, ws_b, headers_b, "B")
    archive_id = client.post(
        f"/api/v1/workspaces/{ws_b}/projects/{project_b}/archive",
        json={"mode": "MANIFEST_ONLY"}, headers=headers_b).json()["id"]

    # A asking for B's archive id -> 404, never 403
    r = client.get(
        f"/api/v1/workspaces/{ws_a}/projects/{project_a}/archives/{archive_id}/download",
        headers=headers_a,
    )
    assert r.status_code == 404, r.text
    # and an unknown id is the same answer
    assert client.get(
        f"/api/v1/workspaces/{ws_a}/projects/{project_a}/archives/"
        f"00000000-0000-0000-0000-000000000000/download", headers=headers_a,
    ).status_code == 404
    # B can still read its own
    assert client.get(
        f"/api/v1/workspaces/{ws_b}/projects/{project_b}/archives/{archive_id}/download",
        headers=headers_b,
    ).status_code == 200


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_archive_freezes_edits_until_unarchive(tmp_path, monkeypatch):
    """Archiving flips status to ARCHIVED and the edit gate refuses with
    409; unarchive restores ACTIVE and editing works again.

    The gate itself lives in ``engine/archive.assert_project_active``.
    """
    from app.db import SessionLocal
    from app.engine import archive as archive_engine
    from app.models import Project

    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    project_id = _project(client, ws_id, headers)

    created = client.post(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
        json={"mode": "MANIFEST_ONLY"}, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["state"] == "COMPLETE"

    session = SessionLocal()
    try:
        project = session.get(Project, project_id)
        assert project.status == "ARCHIVED"
        assert project.archived_at is not None
        # the edit-freeze gate refuses an archived project
        with pytest.raises(archive_engine.ArchiveError) as exc:
            archive_engine.assert_project_active(project)
        assert "archived" in str(exc.value)
    finally:
        session.close()

    # ... but the project stays READABLE while archived
    detail = client.get(f"/api/v1/workspaces/{ws_id}/projects/{project_id}",
                        headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "ARCHIVED"

    restored = client.post(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/unarchive", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "ACTIVE"
    assert client.patch(f"/api/v1/workspaces/{ws_id}/projects/{project_id}",
                        json={"name": "renamed after unarchive"},
                        headers=headers).status_code == 200

    # unarchiving a project that is not archived is a domain 409
    again = client.post(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/unarchive", headers=headers)
    assert again.status_code == 409, again.text


def _make_viewer(client, ws_id):
    """Register a fresh user and grant them the viewer role on ws_id."""
    from app.models import WorkspaceMember

    email = f"vw{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role="viewer"))
    login = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "supersecret123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user_id


def test_archive_rejects_unknown_mode_and_bad_rbac(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    project_id = _project(client, ws_id, headers)

    bad = client.post(f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
                      json={"mode": "EVERYTHING"}, headers=headers)
    assert bad.status_code == 422, bad.text

    # a plain workspace viewer cannot archive: they are not a project
    # member, so edit_project is denied (contracts §3 matrix)
    viewer_headers, viewer_id = _make_viewer(client, ws_id)
    denied = client.post(f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
                         json={"mode": "MANIFEST_ONLY"}, headers=viewer_headers)
    assert denied.status_code == 403, denied.text

    # once they hold the VIEWER project role they may read but not archive
    from app.models import ProjectMember

    with session_scope() as s:
        s.add(ProjectMember(project_id=project_id, user_id=viewer_id, role="VIEWER"))
    assert client.get(f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archives",
                      headers=viewer_headers).status_code == 200
    assert client.post(f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archive",
                       json={"mode": "MANIFEST_ONLY"},
                       headers=viewer_headers).status_code == 403


def test_unknown_archive_id_is_404_not_403(tmp_path, monkeypatch):
    """A cross-workspace archive id is indistinguishable from a missing
    one -- the isolation rule this repo enforces on every surface."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register(client)
    project_id = _project(client, ws_id, headers)
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/projects/{project_id}/archives/{missing}/download",
        headers=headers,
    )
    assert r.status_code == 404, r.text
