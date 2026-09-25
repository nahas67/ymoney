"""Tests for the publishing-path validation endpoint (no real network)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"pub{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_publisher_status_flags_real_path_only_when_configured(db_session):
    """Mock mode with no relay/account must NOT report ready."""
    from app.api.v1.misc import _publisher_status

    status = _publisher_status()
    for name, info in status.items():
        if info["mode"] == "mock":
            assert info["ready"] is False, f"{name} claims ready in mock mode"
            assert "not configured" in info["detail"]


def test_publisher_status_counts_connected_accounts(db_session, workspace_with_user):
    from app.api.v1.misc import _publisher_status
    from app.models import SocialAccount

    db_session.add(
        SocialAccount(
            workspace_id=workspace_with_user["workspace"],
            platform="youtube",
            display_name="test",
            access_token_enc="enc",
        )
    )
    db_session.commit()

    status = _publisher_status()
    yt = status["youtube"]
    assert yt["ready"] is True
    assert yt["mode"] == "real"
    assert "connected account" in yt["detail"]


def test_test_publishing_relay_validation(client, workspace_with_user):
    """Relay key+username set → live validation call; 200 marks valid."""
    from app.services.provider_settings import set_credential

    set_credential("upload_post.api_key", "test-key")
    set_credential("upload_post.username", "testuser")
    try:
        with patch("httpx.get", return_value=type("R", (), {
            "status_code": 200,
            "text": "ok",
            "json": staticmethod(lambda: {"success": True, "plan": "Default", "email": "e@x.y"}),
        })()):
            headers, ws_id = _register(client)
            r = client.post(
                f"/api/v1/workspaces/{ws_id}/connections/test-publishing",
                headers=headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["relay_configured"] is True
        assert body["relay_valid"] is True
        assert body["ok"] is True
        assert body["relay_plan"] == "Default"
    finally:
        set_credential("upload_post.api_key", None)
        set_credential("upload_post.username", None)


def test_test_publishing_no_path_reports_simulated(client, workspace_with_user):
    from app.services.provider_settings import set_credential

    set_credential("upload_post.api_key", None)
    set_credential("upload_post.username", None)
    headers, ws_id = _register(client)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/connections/test-publishing",
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["relay_configured"] is False
    assert "No real publishing path" in body["detail"]


def test_credential_write_collapses_duplicates_newest_wins(db_session):
    """Writes keep one row per (provider, scope); reads return the newest."""
    from app.models import ApiCredential
    from app.services.provider_settings import get_credential, set_credential

    key = "google.client_id"
    set_credential(key, "first-value", workspace_id=None)
    set_credential(key, "second-value", workspace_id=None)
    try:
        val, source = get_credential(key, workspace_id=None)
        assert val == "second-value"
        assert source == "db"
        count = db_session.query(ApiCredential).filter(
            ApiCredential.provider == key,
            ApiCredential.workspace_id.is_(None),
        ).count()
        assert count == 1
    finally:
        set_credential(key, None, workspace_id=None)


def test_credential_unique_indexes_enforce_one_row_per_scope(db_session, workspace_with_user):
    """Migration 0008 indexes exist and reject a duplicate row at the DB."""
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db import session_scope
    from app.models import ApiCredential
    from app.services.provider_settings import set_credential

    names = {
        r[0]
        for r in db_session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'uq_api_credentials_%'"
            )
        ).fetchall()
    }
    assert "uq_api_credentials_provider_ws" in names
    assert "uq_api_credentials_provider_global" in names

    ws = workspace_with_user["workspace"]
    key = "upload_post.api_key"
    set_credential(key, "keep-me", workspace_id=ws)
    try:
        with pytest.raises(IntegrityError), session_scope() as s:
            s.add(
                ApiCredential(
                    workspace_id=ws, provider=key, name="duplicate", value_enc="x"
                )
            )
    finally:
        set_credential(key, None, workspace_id=ws)


def test_connection_credentials_are_workspace_isolated(client):
    """A key saved in workspace A must not be visible in workspace B."""
    from app.services.provider_settings import set_credential

    headers_a, ws_a = _register(client)
    headers_b, ws_b = _register(client)
    try:
        saved = client.put(
            f"/api/v1/workspaces/{ws_a}/connections",
            headers=headers_a,
            json={"key": "google.client_id", "value": "workspace-a-client"},
        )
        assert saved.status_code == 200, saved.text

        listed = client.get(
            f"/api/v1/workspaces/{ws_b}/connections", headers=headers_b
        )
        assert listed.status_code == 200, listed.text
        row = next(i for i in listed.json()["items"] if i["key"] == "google.client_id")
        assert row["configured"] is False
        assert row["source"] == "none"
        assert row["masked"] is None
    finally:
        set_credential("google.client_id", None, workspace_id=ws_a)
