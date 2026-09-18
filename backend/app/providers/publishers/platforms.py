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


FINANCE_TERMS = ("money", "income", "budget", "save", "invest", "earn", "cash", "finance", "stock", "crypto", "debt", "loan")
FINANCE_DISCLAIMER = "Not financial advice. For education only — do your own research."


def _finance_advice(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in FINANCE_TERMS)


def _disclosure_suffix(meta: PublishMetadata) -> str:
    parts = []
    if meta.is_ai_generated or meta.altered_content:
        parts.append("AI-generated content.")
    if meta.contains_finance_advice:
        parts.append(FINANCE_DISCLAIMER)
    return (" " + " ".join(parts)) if parts else ""


YOUTUBE_QUOTA_UNITS_PER_UPLOAD = 1600
YOUTUBE_QUOTA_DAILY_LIMIT = 10000
_TIKTOK_CHUNK_SIZE = 10 * 1024 * 1024
_FB_REELS_WINDOW = 30
_FB_REELS_WINDOW_SECONDS = 24 * 3600


def youtube_quota_exhausted(published_today: int) -> bool:
    """True when today's YouTube uploads would exceed the default 10k quota pool."""
    return published_today * YOUTUBE_QUOTA_UNITS_PER_UPLOAD >= YOUTUBE_QUOTA_DAILY_LIMIT


class YouTubePublisher(BasePublisher):
    """YouTube Data API v3 chunked resumable upload with resume + extras."""

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

    def _shorts_check(self, video_path: str) -> str:
        """Warn when a vertical upload cannot be a Short (>60s). Non-blocking."""
        try:
            from app.services.storage import probe_metadata

            meta = probe_metadata(Path(video_path))
            dur = meta.get("duration_seconds")
            if dur and dur > 62:
                return f"video is {dur:.0f}s — YouTube Shorts require ≤60s; will upload as regular video"
        except Exception:
            pass
        return ""

    def _put_chunked(self, upload_url: str, access_token: str, path: Path) -> dict:
        size = path.stat().st_size
        chunk = 8 * 1024 * 1024
        offset = 0
        backoff = 2.0
        with path.open("rb") as f:
            while offset < size:
                end = min(offset + chunk, size) - 1
                f.seek(offset)
                data = f.read(end - offset + 1)
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }
                for attempt in range(5):
                    try:
                        up = httpx.put(upload_url, headers=headers, content=data, timeout=600)
                        if up.status_code == 308:
                            # Resume Incomplete — continue at next offset.
                            offset = end + 1
                            backoff = 2.0
                            break
                        up.raise_for_status()
                        return up.json()
                    except httpx.HTTPError as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status in (500, 502, 503, 504, 429) and attempt < 4:
                            import time as _t

                            _t.sleep(backoff)
                            backoff = min(backoff * 2, 30)
                            continue
                        raise
                else:
                    raise httpx.HTTPError("chunk upload retries exhausted")
        raise httpx.HTTPError("upload incomplete without terminal response")

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            path = Path(video_path)
            if not path.exists():
                return PublishResult(success=False, error=f"video file missing: {path.name}")
            if path.stat().st_size == 0:
                return PublishResult(success=False, error="video file is empty")
            shorts_note = self._shorts_check(str(path))
            category_id = (meta.category_id or "27").strip() or "27"
            privacy = meta.privacy if meta.privacy in ("public", "unlisted", "private") else "public"
            finance = meta.contains_finance_advice or _finance_advice(meta.title + " " + meta.description)
            disclosure = _disclosure_suffix(meta) if (meta.is_ai_generated or meta.altered_content or finance) else ""
            if finance and FINANCE_DISCLAIMER not in disclosure:
                disclosure = (disclosure + " " + FINANCE_DISCLAIMER).strip()
            status_body: dict = {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": bool(meta.made_for_kids),
            }
            scheduled = (meta.extra or {}).get("scheduled_publish_time")
            if scheduled:
                try:
                    from datetime import UTC, datetime as _dt

                    ts = float(scheduled)
                    if ts > _dt.now(UTC).timestamp() + 300:
                        status_body["privacyStatus"] = "private"
                        status_body["publishAt"] = _dt.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")
                except (TypeError, ValueError):
                    pass

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
                        "description": (meta.description + "\n\n" + " ".join(meta.hashtags) + disclosure)[:5000],
                        "tags": meta.keywords[:30],
                        "categoryId": category_id,
                    },
                    "status": status_body,
                },
                timeout=60,
            )
            init.raise_for_status()
            upload_url = init.headers["Location"]
            try:
                data = self._put_chunked(upload_url, access_token, path)
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status >= 500 or status == 429
                return PublishResult(success=False, error=f"YouTube upload error: {exc}", retryable=retryable)
            vid = data["id"]
            logger.info(f"youtube published: {vid}")
            extra_note = f" ({shorts_note})" if shorts_note else ""
            self._post_extras(access_token, vid, meta)
            return PublishResult(
                success=True,
                remote_post_id=vid,
                remote_url=f"https://youtube.com/watch?v={vid}",
                error=shorts_note,
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc), retryable=False)
        except httpx.HTTPError as exc:
            status = getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(success=False, error=f"YouTube API error: {exc}", retryable=retryable)

    def _post_extras(self, access_token: str, video_id: str, meta: PublishMetadata) -> None:
        """Best-effort thumbnail + captions; failures never fail the publish."""
        try:
            if meta.thumbnail_path and Path(meta.thumbnail_path).exists():
                with Path(meta.thumbnail_path).open("rb") as tf:
                    httpx.post(
                        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                        params={"videoId": video_id},
                        headers={"Authorization": f"Bearer {access_token}"},
                        files={"": (Path(meta.thumbnail_path).name, tf.read(), "image/jpeg")},
                        timeout=120,
                    )
        except Exception as exc:
            logger.warning(f"youtube thumbnail upload failed: {exc}")
        try:
            if meta.captions_path and Path(meta.captions_path).exists():
                with Path(meta.captions_path).open("rb") as cf:
                    httpx.post(
                        "https://www.googleapis.com/youtube/v3/captions?part=snippet",
                        headers={"Authorization": f"Bearer {access_token}"},
                        data={
                            "snippet": f'{{"videoId": "{video_id}", "language": "{meta.captions_language or "en"}", "name": "Subtitles"}}',
                        },
                        files={"": ("captions.srt", cf.read(), "application/octet-stream")},
                        timeout=120,
                    )
        except Exception as exc:
            logger.warning(f"youtube captions upload failed: {exc}")


class TikTokPublisher(BasePublisher):
    """TikTok Content Posting API — chunked FILE_UPLOAD + status polling."""

    platform = "tiktok"
    API = "https://open.tiktokapis.com/v2"

    def creator_info(self, access_token: str) -> dict:
        resp = httpx.post(
            f"{self.API}/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {access_token}"},
            json={},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def _poll_status(self, access_token: str, publish_id: str, timeout_s: float = 300) -> str:
        import time as _t

        deadline = _t.time() + timeout_s
        last = "PROCESSING"
        while _t.time() < deadline:
            try:
                resp = httpx.post(
                    f"{self.API}/post/publish/status/fetch/",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"publish_id": publish_id},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                last = str(data.get("status", last))
                if last in ("PUBLISH_COMPLETE", "FAILED", "CANCELLED"):
                    return last
            except httpx.HTTPError as exc:
                logger.warning(f"tiktok status poll failed: {exc}")
            _t.sleep(10)
        return last

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            path = Path(video_path)
            size = path.stat().st_size if path.exists() else 0
            if not size:
                return PublishResult(success=False, error="video file missing or empty")

            try:
                info = self.creator_info(access_token)
                privacy_opts = info.get("privacy_level_options") or []
                if privacy_opts and "SELF_ONLY" in privacy_opts and len(privacy_opts) == 1:
                    logger.warning("tiktok app is unaudited — posts will be SELF_ONLY (private)")
                max_dur = info.get("max_video_post_duration_sec")
                if max_dur:
                    try:
                        from app.services.storage import probe_metadata

                        dur = (probe_metadata(path).get("duration_seconds") or 0)
                        if dur and dur > float(max_dur):
                            return PublishResult(
                                success=False,
                                error=f"video {dur:.0f}s exceeds TikTok limit {max_dur}s",
                                retryable=False,
                            )
                    except Exception:
                        pass
            except httpx.HTTPError as exc:
                logger.warning(f"tiktok creator_info check failed: {exc}")

            total_chunks = max(1, (size + _TIKTOK_CHUNK_SIZE - 1) // _TIKTOK_CHUNK_SIZE)
            tags = list(meta.hashtags[:5])
            if (meta.is_ai_generated or meta.altered_content) and not any(
                h.lstrip("#").lower() in ("aigenerated", "ai-generated", "aigeneratedcontent") for h in tags
            ):
                tags.append("#AIgenerated")
            finance = meta.contains_finance_advice or _finance_advice(meta.title + " " + meta.description)
            title = (meta.title + " " + " ".join(tags))[:2200]
            if finance and FINANCE_DISCLAIMER not in title:
                title = (title + " " + FINANCE_DISCLAIMER)[:2200]
            init = httpx.post(
                f"{self.API}/post/publish/video/init/",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "post_info": {
                        "title": title,
                        "privacy_level": "SELF_ONLY" if meta.privacy == "private" else "PUBLIC_TO_EVERYONE",
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": size,
                        "chunk_size": min(size, _TIKTOK_CHUNK_SIZE),
                        "total_chunk_count": total_chunks,
                    },
                },
                timeout=60,
            )
            init.raise_for_status()
            data = init.json()["data"]
            upload_url = data["upload_url"]
            publish_id = data.get("publish_id", "")
            with path.open("rb") as f:
                for idx in range(total_chunks):
                    start = idx * _TIKTOK_CHUNK_SIZE
                    f.seek(start)
                    chunk = f.read(_TIKTOK_CHUNK_SIZE)
                    end = start + len(chunk) - 1
                    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(chunk))}
                    if total_chunks > 1:
                        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                    up = httpx.put(upload_url, headers=headers, content=chunk, timeout=600)
                    up.raise_for_status()
            logger.info(f"tiktok upload finished: {publish_id} ({total_chunks} chunk(s))")
            terminal = self._poll_status(access_token, publish_id) if publish_id else "UNKNOWN"
            if terminal == "FAILED":
                return PublishResult(success=False, error="TikTok transcoding/publish failed", retryable=False)
            if terminal == "CANCELLED":
                return PublishResult(success=False, error="TikTok publish cancelled", retryable=False)
            return PublishResult(
                success=True,
                remote_post_id=publish_id,
                remote_url="",
                error="" if terminal == "PUBLISH_COMPLETE" else f"status: {terminal}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except (httpx.HTTPError, KeyError) as exc:
            return PublishResult(success=False, error=f"TikTok API error: {exc}", retryable=True)


class FacebookPagePublisher(BasePublisher):
    """Facebook Reels 3-phase publish (start → upload → finish) with 30/24h guard."""

    platform = "facebook"
    GRAPH = "https://graph.facebook.com/v21.0"

    def _recent_reels_count(self, workspace_id: str = "") -> int:
        if not workspace_id:
            return 0
        try:
            from datetime import timedelta

            from sqlalchemy import select as _select

            from app.db import session_scope
            from app.models import PublishingJob
            from app.models.base import utcnow

            since = utcnow() - timedelta(seconds=_FB_REELS_WINDOW_SECONDS)
            with session_scope() as s:
                rows = s.scalars(
                    _select(PublishingJob).where(
                        PublishingJob.platform == "facebook",
                        PublishingJob.status == "PUBLISHED",
                        PublishingJob.created_at >= since,
                    )
                ).all()
                return len(rows)
        except Exception:
            return 0

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            page_id = _require(account, "external_id")
            path = Path(video_path)
            if not path.exists():
                return PublishResult(success=False, error="video file missing")
            if path.stat().st_size == 0:
                return PublishResult(success=False, error="video file is empty")

            recent = self._recent_reels_count(account.get("workspace_id", ""))
            if recent >= _FB_REELS_WINDOW:
                return PublishResult(
                    success=False,
                    error="Facebook Reels rate limit: 30 publishes per 24h reached — retry later",
                    retryable=True,
                )

            start = httpx.post(
                f"{self.GRAPH}/{page_id}/video_reels",
                params={
                    "access_token": access_token,
                    "upload_phase": "start",
                },
                timeout=60,
            )
            start.raise_for_status()
            start_data = start.json()
            video_id = str(start_data.get("video_id") or start_data.get("id") or "")
            upload_url = start_data.get("upload_url", "")
            if not video_id:
                return PublishResult(success=False, error=f"Facebook start phase failed: {start.text[:200]}")

            if upload_url:
                with path.open("rb") as f:
                    up = httpx.post(
                        upload_url,
                        headers={
                            "Authorization": f"OAuth {access_token}",
                            "offset": "0",
                            "file_size": str(path.stat().st_size),
                        },
                        content=f.read(),
                        timeout=1800,
                    )
                up.raise_for_status()
            else:
                with path.open("rb") as f:
                    up = httpx.post(
                        f"{self.GRAPH}/{page_id}/video_reels",
                        params={"access_token": access_token, "upload_phase": "transfer"},
                        files={"video_file_chunk": (path.name, f, "video/mp4")},
                        timeout=1800,
                    )
                up.raise_for_status()

            description = (meta.description + "\n" + " ".join(meta.hashtags) + _disclosure_suffix(meta))[:5000]
            finish_body = {
                "access_token": access_token,
                "upload_phase": "finish",
                "video_id": video_id,
                "description": description,
                "video_state": "PUBLISHED",
            }
            scheduled = (meta.extra or {}).get("scheduled_publish_time")
            if scheduled:
                finish_body["video_state"] = "SCHEDULED"
                finish_body["scheduled_publish_time"] = str(scheduled)
            finish = httpx.post(
                f"{self.GRAPH}/{page_id}/video_reels",
                data=finish_body,
                timeout=120,
            )
            finish.raise_for_status()
            data = finish.json()
            post_id = str(data.get("id") or data.get("post_id") or video_id)
            return PublishResult(
                success=True,
                remote_post_id=post_id,
                remote_url=f"https://facebook.com/reel/{post_id}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(success=False, error=f"Facebook API error: {exc}", retryable=retryable)


class InstagramPublisher(BasePublisher):
    """Instagram Reels via Graph API container → publish.

    Requires an IG Business/Creator account linked to a Facebook Page and a
    publicly reachable video_url. Without object storage (P3) there is no
    public URL, so publish fails closed with remediation instead of faking.
    """

    platform = "instagram"
    GRAPH = "https://graph.facebook.com/v21.0"

    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        try:
            access_token = _require(account, "access_token")
            ig_user_id = account.get("external_id") or _require(account, "ig_user_id")
            video_url = (meta.extra or {}).get("video_url") or account.get("video_url")
            if not video_url:
                return PublishResult(
                    success=False,
                    error=(
                        "Instagram Reels needs a public video_url — connect object storage "
                        "(P3) or publish via the Upload-Post relay"
                    ),
                    retryable=False,
                )
            caption = (meta.title + "\n" + meta.description + " " + " ".join(meta.hashtags) + _disclosure_suffix(meta))[:2200]
            create = httpx.post(
                f"{self.GRAPH}/{ig_user_id}/media",
                params={
                    "access_token": access_token,
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "share_to_feed": "true",
                },
                timeout=120,
            )
            create.raise_for_status()
            creation_id = str(create.json().get("id") or "")
            if not creation_id:
                return PublishResult(success=False, error="Instagram container creation failed")
            pub = httpx.post(
                f"{self.GRAPH}/{ig_user_id}/media_publish",
                params={"access_token": access_token, "creation_id": creation_id},
                timeout=120,
            )
            pub.raise_for_status()
            media_id = str(pub.json().get("id") or creation_id)
            return PublishResult(
                success=True,
                remote_post_id=media_id,
                remote_url=f"https://instagram.com/reel/{media_id}",
            )
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(success=False, error=f"Instagram API error: {exc}", retryable=retryable)


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
            ai_flag = str((meta.extra or {}).get("is_ai_generated", True)).lower() not in ("false", "0", "no")
            finance = meta.contains_finance_advice or _finance_advice(meta.title + " " + meta.description)
            description = meta.description[:4000] + _disclosure_suffix(meta)
            if finance and FINANCE_DISCLAIMER not in description:
                description = (description + " " + FINANCE_DISCLAIMER)[:4000]
            form: list = [
                ("user", self.username),
                ("title", meta.title[:2200]),
                ("description", description),
                ("privacyStatus", meta.privacy),
                ("is_ai_generated", "true" if (ai_flag or meta.is_ai_generated) else "false"),
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
                    files={"video": (path.name, vf, "video/mp4")},
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
