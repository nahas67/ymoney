"""Regression tests for local authentication."""

import os

from fastapi.testclient import TestClient

from app.main import create_app


def test_register_and_login_return_workspace():
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        email = f"auth-{os.urandom(4).hex()}@test.local"
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "supersecret123"},
        )
        assert registered.status_code == 200, registered.text
        logged_in = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "supersecret123"},
        )
        assert logged_in.status_code == 200, logged_in.text
        payload = logged_in.json()
        assert payload["access_token"]
        assert payload["workspace"]["id"]


def test_refresh_token_is_single_use():
    """Rotation must atomically consume a refresh token: replay is rejected."""
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        email = f"refresh-{os.urandom(4).hex()}@test.local"
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "supersecret123"},
        )
        assert registered.status_code == 200, registered.text
        original = registered.json()["refresh_token"]

        # First rotation succeeds and returns a fresh token pair.
        first = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original}
        )
        assert first.status_code == 200, first.text
        assert first.json()["refresh_token"] != original

        # Replaying the consumed token is rejected (single-use claim).
        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original}
        )
        assert replay.status_code == 401, replay.text

        # The rotated token is the live one and still works once.
        second = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": first.json()["refresh_token"]},
        )
        assert second.status_code == 200, second.text
        assert second.json()["access_token"]


def test_invalid_login_is_not_an_internal_error():
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "missing@test.local", "password": "wrong"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid credentials"


def test_register_rolls_back_user_and_workspace_on_late_failure(monkeypatch):
    """A failure after user/workspace flush must leave no partial account."""
    from app.api.v1 import auth as auth_api
    from app.db import session_scope
    from app.models import User, Workspace

    def fail_token_issue(*_args, **_kwargs):
        raise RuntimeError("token service unavailable")

    monkeypatch.setattr(auth_api, "issue_tokens", fail_token_issue)
    email = f"atomic-{os.urandom(4).hex()}@test.local"
    display_name = email.split("@", 1)[0]

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "supersecret123"},
        )

    assert response.status_code == 500
    with session_scope() as s:
        assert s.query(User).filter(User.email == email).first() is None
        assert s.query(Workspace).filter(
            Workspace.name == f"{display_name}'s workspace"
        ).first() is None
