"""Stateless token-signed public media links.

Instagram's Graph API (and any PULL-style publisher) needs a publicly
reachable https URL for the video file. Self-hosted YMONEY has no CDN, so this
service mints short-lived single-file bearer tokens (JWT, 48h) that resolve
through the public /api/v1/public/media endpoint — no S3 required when
YMONEY_PUBLIC_BASE_URL points at a reachable host.

Safety: tokens are unguessable (HS256 over the server secret), scoped to one
stored path, expire quickly, and resolution still goes through the storage
boundary (managed_path), so a token can never escape its workspace directory.
"""

from __future__ import annotations

from datetime import timedelta

import jwt

from app.core.config import settings
from app.models.base import utcnow


def _base_url() -> str:
    return (getattr(settings, "public_base_url", "") or "").rstrip("/")


def public_base_reachable() -> bool:
    """True when the configured public base URL could work for Meta/TikTok.

    Localhost URLs are never reachable by platform servers, so publishers
    must fail closed instead of handing Meta a localhost link.
    """
    base = _base_url().lower()
    if not base.startswith(("http://", "https://")):
        return False
    host = base.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    return host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def create_media_token(workspace_id: str, stored_path: str, expires_hours: float = 48) -> str:
    if not workspace_id or not stored_path:
        raise ValueError("workspace and stored path are required")
    payload = {
        "scope": "public-media",
        "ws": workspace_id,
        "path": stored_path,
        "exp": utcnow() + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_media_token(token: str) -> tuple[str, str]:
    """Returns (workspace_id, stored_path) or raises ValueError."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ValueError(f"invalid media token: {exc}") from exc
    if payload.get("scope") != "public-media":
        raise ValueError("wrong token scope")
    ws, path = payload.get("ws", ""), payload.get("path", "")
    if not ws or not path:
        raise ValueError("media token missing workspace/path")
    return ws, path


def public_media_url(workspace_id: str, stored_path: str) -> str:
    """Full public URL for a stored file, or '' when not publicly servable."""
    if not public_base_reachable():
        return ""
    token = create_media_token(workspace_id, stored_path)
    return f"{_base_url()}/api/v1/public/media/{token}"
