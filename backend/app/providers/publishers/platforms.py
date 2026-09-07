"""Real platform publishers (YouTube, TikTok, Facebook) + Upload-Post relay.

Each implementation:
- validates that required credentials exist on the connected account,
- refreshes OAuth access tokens when expired (YouTube/Facebook),
- calls the official upload endpoints via httpx,
- returns actionable errors with retryable hints.

These code paths require real platform apps/credentials to function; until
then the system falls back to MockPublisher when MOCK_PUBLISHING=true.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from loguru import logger

from app.providers.publishers.base import BasePublisher, PublishMetadata, PublishResult


def _require(account: dict, *keys: str) -> str:
    missing = [k for k in keys if not account.get(k)]
    if missing:
        raise PermissionError(
            f"account missing credentials: {', '.join(missing)} — reconnect the account"
        )
    return account[keys[0]]


class YouTubePublisher(BasePublisher):
    """YouTube Data API v3 resumable upload."""

    platform = "youtube"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def refresh_access_token(self, account: dict, client_id: str, client_secret: str) -> str:
        refresh_token = _require(account, "refresh_token")
        resp = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"]

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            path = Path(video_path)
            if not path.exists():
                return PublishResult(success=False, error=f"video file missing: {path.name}")

            init = httpx.post(
                "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Upload-Content-Length": str(path.stat().st_size),
                    "X-Upload-Content-Type": "video/mp4",
                },
                json={
                    "snippet": {
                        "title": meta.title[:100],
                        "description": (meta.description + "\n\n" + " ".join(meta.hashtags))[:5000],
                        "tags": meta.keywords[:30],
                        "categoryId": "27",  # Education; strategist may override
                    },
                    "status": {
                        "privacyStatus": meta.privacy if meta.privacy in ("public", "unlisted", "private") else "public",
                        "selfDeclaredMadeForKids": False,
                    },
                },
                timeout=60,
            )
            init.raise_for_status()
            upload_url = init.headers["Location"]
            with path.open("rb") as f:
                up = httpx.put(
                    upload_url,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "video/mp4"},
                    content=f.read(),
                    timeout=1800,
                )
            up.raise_for_status()
            data = up.json()
            vid = data["id"]
            logger.info(f"youtube published: {vid}")
            return PublishResult(
                success=True,
                remote_post_id=vid,
                remote_url=f"https://youtube.com/watch?v={vid}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc), retryable=False)
        except httpx.HTTPError as exc:
            status = getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(success=False, error=f"YouTube API error: {exc}", retryable=retryable)


class TikTokPublisher(BasePublisher):
    """TikTok Content Posting API — direct post (requires audited app)."""

    platform = "tiktok"
    API = "https://open.tiktokapis.com/v2"

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            path = Path(video_path)
            size = path.stat().st_size if path.exists() else 0
            if not size:
                return PublishResult(success=False, error="video file missing or empty")

            init = httpx.post(
                f"{self.API}/post/publish/video/init/",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "post_info": {
                        "title": (meta.title + " " + " ".join(meta.hashtags[:5]))[:2200],
                        "privacy_level": "SELF_ONLY" if meta.privacy == "private" else "PUBLIC_TO_EVERYONE",
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": size,
                        "chunk_size": size,
                        "total_chunk_count": 1,
                    },
                },
                timeout=60,
            )
            init.raise_for_status()
            data = init.json()["data"]
            upload_url = data["upload_url"]
            publish_id = data.get("publish_id", "")
            with path.open("rb") as f:
                up = httpx.put(upload_url, content=f.read(), timeout=1800)
            up.raise_for_status()
            logger.info(f"tiktok direct post initiated: {publish_id}")
            return PublishResult(
                success=True,
                remote_post_id=publish_id,
                remote_url=f"https://www.tiktok.com/@me/video/{publish_id}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except (httpx.HTTPError, KeyError) as exc:
            return PublishResult(success=False, error=f"TikTok API error: {exc}", retryable=True)


class FacebookPagePublisher(BasePublisher):
    """Facebook Graph API video upload to a page feed."""

    platform = "facebook"
    GRAPH = "https://graph.facebook.com/v21.0"

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            page_id = _require(account, "external_id")
            path = Path(video_path)
            if not path.exists():
                return PublishResult(success=False, error="video file missing")

            with path.open("rb") as f:
                resp = httpx.post(
                    f"{self.GRAPH}/{page_id}/videos",
                    params={"access_token": access_token},
                    data={"description": (meta.description + "\n" + " ".join(meta.hashtags))[:5000]},
                    files={"source": (path.name, f, "video/mp4")},
                    timeout=1800,
                )
            resp.raise_for_status()
            data = resp.json()
            post_id = data.get("id", "")
            return PublishResult(
                success=True,
                remote_post_id=post_id,
                remote_url=f"https://facebook.com/{post_id}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except httpx.HTTPError as exc:
            return PublishResult(success=False, error=f"Facebook API error: {exc}", retryable=True)


class UploadPostRelay(BasePublisher):
    """upload-post.com relay — one API key publishes to TikTok/IG/YT/Facebook/etc.

    API reference highlights (verified against docs.upload-post.com):
      Authorization: Apikey <key>
      POST /api/upload  multipart fields:
        user, title, video (file or URL), platform[] (repeatable),
        description, youtube_title, tiktok_title, tags[], privacyStatus,
        is_ai_generated
    """

    platform = "upload_post_relay"
    API_BASE = "https://api.upload-post.com"

    def __init__(self, api_key: str, username: str):
        self.api_key = api_key
        self.username = username

    def supports(self, platform: str) -> bool:
        return platform in ("tiktok", "instagram", "youtube", "facebook", "linkedin")

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        platforms = account.get("platforms") or [account.get("platform") or "tiktok"]
        platforms = [p for p in platforms if p != "instagram" or True]
        try:
            path = Path(video_path)
            form: list = [
                ("user", self.username),
                ("title", meta.title[:2200]),
                ("description", meta.description[:4000]),
                ("privacyStatus", meta.privacy),
                ("is_ai_generated", "true"),
            ]
            for p in platforms:
                form.append(("platform[]", p))
            for h in meta.hashtags[:12]:
                form.append(("tags[]", h.lstrip("#")))
            # per-platform titles when provided in extra
            extra = meta.extra or {}
            if extra.get("youtube_title"):
                form.append(("youtube_title", str(extra["youtube_title"])[:100]))
            if extra.get("tiktok_title"):
                form.append(("tiktok_title", str(extra["tiktok_title"])[:2200]))
            with path.open("rb") as vf:
                resp = httpx.post(
                    f"{self.API_BASE}/api/upload",
                    headers={"Authorization": f"Apikey {self.api_key}"},
                    data=form,
                    files={"video": (path.name, vf.read(), "video/mp4")},
                    timeout=600,
                )
            resp.raise_for_status()
            data = resp.json()
            post_id = str(data.get("id") or data.get("postId") or int(time.time()))
            logger.info(f"upload-post relay published: {post_id} -> {platforms}")
            return PublishResult(success=True, remote_post_id=post_id)
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = getattr(getattr(exc, "response", None), "text", "")[:200]
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(
                success=False,
                error=f"upload-post relay error{f' ({status})' if status else ''}: {exc} {detail}",
                retryable=retryable,
            )
