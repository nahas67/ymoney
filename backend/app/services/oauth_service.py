"""OAuth 2.0 connect flows for publishing platforms.

Currently implemented: YouTube (Google) authorization-code flow.
- start(): builds Google's authorization URL (state = signed short-lived JWT)
- handle_callback(): verifies state, exchanges code for tokens, stores the
  account with AES-GCM-encrypted refresh material.

Client credentials resolve through provider_settings (DB -> env).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import jwt
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db import session_scope
from app.models.base import utcnow
from app.models import SocialAccount
from app.services import provider_settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPES = "https://www.googleapis.com/auth/youtube.upload"


class OAuthError(Exception):
    pass


def _redirect_uri(workspace_id: str) -> str:
    base = getattr(settings, "public_base_url", "") or f"http://127.0.0.1:{settings.port}"
    return f"{base.rstrip('/')}/api/v1/workspaces/{workspace_id}/publishing/oauth/youtube/callback"


def youtube_start(workspace_id: str) -> dict:
    client_id, client_secret = provider_settings.google_oauth_client(workspace_id=workspace_id)
    if not client_id or not client_secret:
        raise OAuthError(
            "Google OAuth client not configured — add the client ID and secret under "
            "Settings → Connections first"
        )
    state = jwt.encode(
        {"ws": workspace_id, "exp": utcnow() + timedelta(minutes=15)},
        settings.secret_key,
        algorithm="HS256",
    )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(workspace_id),
        "response_type": "code",
        "scope": YOUTUBE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    from urllib.parse import urlencode

    return {"authorize_url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}


def youtube_callback(workspace_id: str, code: str, state: str) -> dict:
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise OAuthError(f"invalid state: {exc}") from exc
    if payload.get("ws") != workspace_id:
        raise OAuthError("state/workspace mismatch")

    client_id, client_secret = provider_settings.google_oauth_client(workspace_id=workspace_id)
    if not client_id or not client_secret:
        raise OAuthError("Google OAuth client not configured")

    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _redirect_uri(workspace_id),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthError(f"token exchange failed: {resp.text[:300]}")
    tok = resp.json()
    if not tok.get("refresh_token"):
        raise OAuthError(
            "Google did not return a refresh token — revoke the app's access at "
            "myaccount.google.com/permissions and reconnect with prompt=consent"
        )

    # channel identity (channels.list requires the same token)
    ch_title, ch_id = "", ""
    try:
        ch_resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            timeout=20,
        )
        items = ch_resp.json().get("items", [])
        if items:
            ch_id = items[0].get("id", "")
            ch_title = items[0].get("snippet", {}).get("title", "")
    except httpx.HTTPError:
        pass

    expires_at = utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)))
    with session_scope() as s:
        existing = s.scalar(
            select(SocialAccount).where(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.platform == "youtube",
                SocialAccount.external_id == ch_id,
            )
        ) if ch_id else None
        if existing is None:
            existing = s.scalar(
                select(SocialAccount).where(
                    SocialAccount.workspace_id == workspace_id,
                    SocialAccount.platform == "youtube",
                )
            )
        if existing:
            acc = existing
        else:
            acc = SocialAccount(workspace_id=workspace_id, platform="youtube")
            s.add(acc)
        acc.external_id = ch_id
        acc.display_name = ch_title or "YouTube channel"
        acc.access_token_enc = encrypt_secret(tok["access_token"])
        acc.refresh_token_enc = encrypt_secret(tok["refresh_token"])
        acc.token_expires_at = expires_at
        acc.status = "connected"
        s.flush()
        acc_id = acc.id

    from app.services.events import record_event

    record_event(
        workspace_id,
        "account.connected",
        f"YouTube connected: {acc.display_name} (real uploads enabled)",
        level="success",
        source="publishing",
    )
    return {"id": acc_id, "platform": "youtube", "display_name": acc.display_name}


def get_decrypted_account(account_row) -> dict:
    """Decrypt an ORM SocialAccount into a publish-ready dict."""
    return {
        "platform": account_row.platform,
        "external_id": account_row.external_id,
        "display_name": account_row.display_name,
        "access_token": decrypt_secret(account_row.access_token_enc or ""),
        "refresh_token": decrypt_secret(account_row.refresh_token_enc or ""),
    }
