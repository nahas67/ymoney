"""`GET /auth/me` integration — the capability contract the UI depends on.

The unit tests in ``test_work16_5_2_capabilities.py`` prove the derivation. This
file proves the ENDPOINT delivers it, because a correct module wired to nothing
is the exact failure this work order exists to close: the UI would keep
fail-opening and nobody would notice why.

What is asserted here is the wire shape, since that is the contract the frontend
actually reads:

* per-workspace ``role`` and ``capabilities`` (not a single union only);
* an omitted capability list still serialises as ``[]``, because the client's
  fail-open rule distinguishes "none" from "field absent";
* the legacy keys survive, or the existing Editor/Exports/Reviews callers break.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.capabilities import ALL_CAPABILITIES


@pytest.fixture(autouse=True)
def _bypass_gate(monkeypatch):
    import app.services.readiness as rd

    monkeypatch.setattr(
        rd,
        "run_readiness",
        lambda force_refresh=True: {
            "status": "ready",
            "checked_at": "",
            "stale_after_hours": 24,
            "checks": [],
            "blocking_failures": [],
            "message": "test",
        },
    )


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"cap{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data, data["workspace"]["id"]


class TestMeEndpoint:
    def test_reports_role_and_capabilities_for_the_callers_workspace(self, client):
        data, ws_id = _register(client)
        r = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # The registering user is the workspace owner.
        mine = [w for w in body["workspaces"] if w["id"] == ws_id]
        assert mine, body
        entry = mine[0]
        assert entry["role"] == "owner"
        assert entry["capabilities"] == list(ALL_CAPABILITIES)

    def test_legacy_keys_are_preserved(self, client):
        # Editor.tsx / Exports.tsx / Reviews.tsx read id and is_superuser.
        data, _ = _register(client)
        body = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        ).json()
        for key in ("id", "email", "display_name", "is_superuser", "workspaces"):
            assert key in body, f"{key} disappeared from /auth/me"
        assert body["is_superuser"] is False

    def test_capability_list_is_always_present_even_when_empty(self, client):
        # The client fails open only when the FIELD is absent. If a role has no
        # capabilities the key must still exist and be [].
        data, _ = _register(client)
        body = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        ).json()
        for ws in body["workspaces"]:
            assert "capabilities" in ws
            assert isinstance(ws["capabilities"], list)

    def test_union_list_is_present_for_cross_workspace_callers(self, client):
        data, _ = _register(client)
        body = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        ).json()
        assert isinstance(body["capabilities"], list)
        assert set(body["capabilities"]) == set(ALL_CAPABILITIES)

    def test_never_exposes_credentials_or_tokens(self, client):
        # The identity payload is read by every screen; it must not become a
        # place secrets leak from.
        data, _ = _register(client)
        raw = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        ).text
        for leak in ("password", "access_token", "refresh_token", "secret", "api_key"):
            assert leak not in raw.lower(), f"/auth/me leaked {leak!r}"

    def test_still_requires_a_token(self, client):
        r = client.get("/api/v1/auth/me")
        assert r.status_code in (401, 403)
