"""Work 14 §12 -- the read-only distribution API surface.

Exposes the verified profiles, the optimizer result, and the per-platform
capability/capability-state so the UI can render capability badges, the
optimization diff, platform warnings and LIVE/MOCK/HANDOFF/UNAVAILABLE.

Every endpoint is read-only and workspace-scoped. Nothing here can publish.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.api.v1.distribution import distribution_router
from app.main import create_app


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client):
    import uuid

    email = f"w14api{uuid.uuid4().hex[:8]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"}


def _paths() -> set[str]:
    # the router's own prefix is relative; the app mounts it under /api/v1
    return {r.path for r in distribution_router.routes}


def test_router_is_registered_in_the_app():
    """Check the OpenAPI schema: app.routes can hold a router-level entry."""
    app = create_app()
    schema_paths = set(app.openapi()["paths"])
    assert any(p.endswith("/distribution/platforms") for p in schema_paths), (
        f"the distribution router is not mounted: {sorted(schema_paths)[:8]}")
    assert any("/distribution/capabilities" in p for p in schema_paths)
    assert any("/distribution/optimize" in p for p in schema_paths)


@pytest.mark.parametrize("path", [
    "/workspaces/{workspace_id}/distribution/platforms",
    "/workspaces/{workspace_id}/distribution/platforms/{platform}",
    "/workspaces/{workspace_id}/distribution/capabilities",
    "/workspaces/{workspace_id}/distribution/optimize",
])
def test_endpoint_exists_in_the_router(path):
    assert path in _paths(), f"missing route {path}; have {sorted(_paths())}"


def test_platforms_list_reports_every_verified_profile(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws}/distribution/platforms",
                   headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {item["platform"] for item in body["items"]}
    assert {"threads", "pinterest", "bluesky", "snapchat"} <= names
    for item in body["items"]:
        assert "verified_limits" in item
        assert "unverified" in item


def test_platform_detail_exposes_provenance_for_every_limit(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws}/distribution/platforms/threads",
                   headers=headers)
    assert r.status_code == 200, r.text
    profile = r.json()
    assert profile["platform"] == "threads"
    # every constraint field has ONE shape, verified or not
    for field in ("title_max", "alt_text", "link_limit", "rate_limit",
                  "hashtag_limit", "text_limits"):
        entry = profile[field]
        assert set(entry) >= {"value", "source", "verified"}, field
    # a verified limit carries the document that states it
    assert profile["alt_text"]["value"] == 1000
    assert profile["alt_text"]["verified"] is True
    assert profile["alt_text"]["source"].startswith("http")
    # an undocumented one is explicitly NOT a limit
    assert profile["title_max"]["verified"] is False
    assert profile["title_max"]["value"] == "UNKNOWN"
    assert profile["hashtag_limit"]["verified"] is False
    assert profile["required_permissions"]


def test_unknown_platform_detail_404s(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws}/distribution/platforms/myspace",
                   headers=headers)
    assert r.status_code == 404, r.status_code


def test_capabilities_endpoint_shows_badges_and_publish_mode(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws}/distribution/capabilities",
                   headers=headers)
    assert r.status_code == 200, r.text
    items = {i["platform"]: i for i in r.json()["items"]}
    assert items["snapchat"]["publish_mode"] == "USER_HANDOFF"
    assert "USER_HANDOFF" in items["snapchat"]["capabilities"]
    assert "DIRECT_PUBLISH" not in items["snapchat"]["capabilities"]
    assert items["threads"]["publish_mode"] == "DIRECT_PUBLISH"
    assert "CAROUSEL" in items["threads"]["capabilities"]
    assert items["pinterest"]["publish_mode"] == "DIRECT_PUBLISH"
    assert items["bluesky"]["publish_mode"] == "DIRECT_PUBLISH"
    for item in items.values():
        assert "supports_inbox" in item and "supports_analytics" in item


def test_capabilities_require_authentication(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/v1/workspaces/whatever/distribution/capabilities")
    assert r.status_code in (401, 403), r.status_code


def test_optimize_endpoint_returns_the_diff_and_provenance(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    payload = {
        "platform": "pinterest",
        "master": {"title": "T" * 400, "description": "D" * 2000,
                   "hashtags": [f"t{i}" for i in range(12)],
                   "duration_s": 40.0, "aspect": "9:16",
                   "media_kind": "image"},
    }
    r = client.post(f"/api/v1/workspaces/{ws}/distribution/optimize",
                    headers=headers, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    # the official Pinterest title limit is applied
    assert len(body["spec"]["title"]) == 100
    # and the diff explains it
    assert body["changed_fields"]
    for decision in body["decisions"]:
        assert decision["reason"]
        assert decision["source"]
        assert 1 <= decision["priority"] <= 5
    assert body["blocked"] == []


def test_optimize_reports_a_block_for_an_unsupported_media_type(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    payload = {"platform": "pinterest",
               "master": {"title": "t", "description": "d",
                          "media_kind": "text"}}
    r = client.post(f"/api/v1/workspaces/{ws}/distribution/optimize",
                    headers=headers, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"], r.json()["blocked"]


def test_optimize_honours_brand_dna_precedence(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers = _register(client)
    payload = {"platform": "threads",
               "master": {"title": "t", "description": "d",
                          "media_kind": "video"},
               "brand": {"caption_style": {"style": "karaoke"}}}
    r = client.post(f"/api/v1/workspaces/{ws}/distribution/optimize",
                    headers=headers, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["spec"]["caption"]["style"] == "karaoke"


def test_optimize_requires_authentication(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/v1/workspaces/w/distribution/optimize",
                    json={"platform": "threads", "master": {}})
    assert r.status_code in (401, 403), r.status_code


def test_no_distribution_endpoint_can_publish(tmp_path, monkeypatch):
    """The Work 14 API surface is read-only by construction."""
    assert not any("publish" in p for p in _paths()), (
        f"a distribution route looks like a publish route: "
        f"{[p for p in _paths() if 'publish' in p]}")


def test_router_object_is_an_api_router():
    assert isinstance(distribution_router, APIRouter)
