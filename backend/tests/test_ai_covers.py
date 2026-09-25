"""Generative AI covers: generate via image provider, serve, pick as poster."""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"ai{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def test_ai_covers_generate_serve_pick(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app
    from app.models import ContentItem, Video, VideoVariant

    # Force mock image provider (no network, deterministic PNGs)
    from app.providers import images as img_mod

    monkeypatch.setattr(img_mod, "get_image_provider", lambda: img_mod.MockImageProvider())
    monkeypatch.setattr(
        img_mod,
        "image_provider_status",
        lambda: {"provider": "mock", "healthy": True, "is_mock": True, "error": ""},
    )

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    from app.db import session_scope

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="money habits that compound")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id, label="v1", script="x" * 20, hook="Stop doing this with money?", selected=True)
        s.add(variant)
        s.flush()
        video = Video(variant_id=variant.id, workspace_id=ws_id, engine="ffmpeg_avatar", status="READY", file_path="")
        s.add(video)
        s.flush()
        vid = video.id

    # generate (no prompt -> derives from topic+hook)
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/ai-covers", headers=headers, json={"count": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "mock"
    assert "money habits" in body["prompt"]
    assert len(body["covers"]) == 2
    assert all({"index", "path", "url"} <= set(c) for c in body["covers"])

    # serve candidate 0
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/ai-covers/0/file", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"

    # pick candidate 1 as poster (no video file needed for AI picks)
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail", headers=headers, json={"ai_cover_index": 1})
    assert r.status_code == 200, r.text
    assert r.json()["thumbnail_path"].endswith(f"ai-cover-{vid}-1.png".replace("-", "-")[-40:]) or "ai-cover-" in r.json()["thumbnail_path"]

    # poster serves picked AI cover
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail", headers=headers)
    assert r.status_code == 200, r.text

    # unknown AI index 404s
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail", headers=headers, json={"ai_cover_index": 9})
    assert r.status_code == 404

    # bad size rejected
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/ai-covers", headers=headers, json={"size": "nope"})
    assert r.status_code == 422

    # cross-workspace isolation: second user cannot use first video
    _, _, headers2 = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/ai-covers", headers=headers2, json={"count": 1})
    assert r.status_code in (403, 404)


def test_ai_covers_fails_closed_when_provider_down(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app
    from app.providers import images as img_mod

    monkeypatch.setattr(
        img_mod, "image_provider_status", lambda: {"provider": "pexels", "healthy": False, "is_mock": False, "error": "no key"}
    )

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    from app.db import session_scope
    from app.models import ContentItem, Video, VideoVariant

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="t")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id, label="v1", script="x" * 20, selected=True)
        s.add(variant)
        s.flush()
        video = Video(variant_id=variant.id, workspace_id=ws_id, engine="ffmpeg_avatar", status="READY", file_path="")
        s.add(video)
        s.flush()
        vid = video.id

    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/ai-covers", headers=headers, json={"prompt": "test"})
    assert r.status_code == 503
    assert "pexels" in r.text.lower() or "unhealthy" in r.text.lower() or "provider" in r.text.lower()
