"""X (Twitter) publisher — official ``POST /2/tweets`` + v1.1 media upload.

Publishing modes (all real, no relay, no browser automation):
  * **text**  — ``video_path`` empty/blank → ``POST /2/tweets`` with text only
    (PUBLISH_TEXT).
  * **image** — v1.1 simple upload (``upload.twitter.com/1.1/media/upload.json``)
    → tweet with ``media.media_ids`` (PUBLISH_IMAGE).
  * **video** — v1.1 chunked upload (INIT → APPEND → FINALIZE, then bounded
    ``COMMAND=STATUS`` polling until the media is ``succeeded``) → tweet with
    ``media.media_ids`` (PUBLISH_VIDEO / PUBLISH_SHORT; in-feed video is
    capped at 140 s on standard access — enforced by the campaign profile).

Honest failures: a missing token returns
``PublishResult(success=False, error="account missing credentials: ...")`` —
never a simulated publish. HTTP errors return ``retryable=True`` for
429/5xx/connection problems; media-processing failures fail closed.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from app.providers.publishers.base import (
    BasePublisher,
    PublisherError,
    PublishMetadata,
    PublishResult,
)
from app.providers.publishers.platforms import _disclosure_suffix, _require

#: X post hard limit (matches the campaign profile description_max).
MAX_TWEET_CHARS = 280
#: v1.1 chunked-upload segment size (5 MB is the API max; stay under it).
CHUNK_SIZE = 4 * 1024 * 1024
#: Cap on media-processing polls (each poll waits ``check_after_secs``).
MEDIA_POLL_DEADLINE_S = 300

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class XPublisher(BasePublisher):
    platform = "x"
    API = "https://api.x.com/2"
    UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=60)
        return self._client

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _tweet_text(meta: PublishMetadata) -> str:
        parts = [meta.title, meta.description, " ".join(meta.hashtags)]
        text = " ".join(p for p in parts if p).strip()
        text = (text + _disclosure_suffix(meta)).strip()
        if len(text) > MAX_TWEET_CHARS:
            text = text[: MAX_TWEET_CHARS - 3].rstrip() + "..."
        return text

    def _upload_image(self, token: str, path: Path) -> str:
        resp = self.http.post(
            self.UPLOAD,
            headers=self._headers(token),
            files={"media": (path.name, path.read_bytes(), IMAGE_MIME.get(path.suffix.lower(), "image/*"))},
        )
        resp.raise_for_status()
        media_id = str(resp.json().get("media_id_string") or "")
        if not media_id:
            raise PublisherError("X media upload returned no media_id_string")
        return media_id

    def _upload_video(self, token: str, path: Path) -> str:
        headers = self._headers(token)
        size = path.stat().st_size
        init = self.http.post(
            self.UPLOAD,
            headers=headers,
            data={
                "command": "INIT",
                "media_type": "video/mp4",
                "total_bytes": str(size),
                "media_category": "tweet_video",
            },
        )
        init.raise_for_status()
        media_id = str(init.json().get("media_id_string") or "")
        if not media_id:
            raise PublisherError("X INIT returned no media_id_string")
        with path.open("rb") as handle:
            index = 0
            while True:
                segment = handle.read(CHUNK_SIZE)
                if not segment:
                    break
                part = self.http.post(
                    self.UPLOAD,
                    headers=headers,
                    data={
                        "command": "APPEND",
                        "media_id": media_id,
                        "segment_index": str(index),
                    },
                    files={"media": (path.name, segment, "video/mp4")},
                )
                part.raise_for_status()
                index += 1
        finalize = self.http.post(
            self.UPLOAD,
            headers=headers,
            data={"command": "FINALIZE", "media_id": media_id},
        )
        finalize.raise_for_status()
        try:
            body = finalize.json() if finalize.content else {}
        except ValueError:
            body = {}
        self._await_media(token, media_id, body)
        return media_id

    def _await_media(self, token: str, media_id: str, finalize_body: dict) -> None:
        """Poll COMMAND=STATUS until processing succeeds (or fail closed)."""
        headers = self._headers(token)
        info = (finalize_body.get("processing_info") or {})
        deadline = time.time() + MEDIA_POLL_DEADLINE_S
        while info.get("state") in ("pending", "in_progress"):
            if time.time() >= deadline:
                raise PublisherError(
                    f"X media processing timed out after {MEDIA_POLL_DEADLINE_S}s "
                    f"(media_id {media_id})"
                )
            wait = float(info.get("check_after_secs") or 10)
            time.sleep(min(wait, 10))
            status = self.http.get(
                self.UPLOAD,
                headers=headers,
                params={"command": "STATUS", "media_id": media_id},
            )
            status.raise_for_status()
            try:
                info = (status.json() or {}).get("processing_info") or {}
            except ValueError:
                info = {}
        if info.get("state") == "failed":
            raise PublisherError(
                f"X media processing failed: {info.get('error') or 'unknown error'}"
            )

    def _post_tweet(self, token: str, text: str, media_ids: list[str]) -> PublishResult:
        body: dict = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        resp = self.http.post(
            f"{self.API}/tweets",
            headers=self._headers(token),
            json=body,
        )
        resp.raise_for_status()
        tweet_id = str((resp.json() or {}).get("data", {}).get("id") or "")
        return PublishResult(
            success=True,
            remote_post_id=tweet_id,
            remote_url=f"https://x.com/i/status/{tweet_id}" if tweet_id else "",
        )

    # -- BasePublisher ------------------------------------------------------
    def publish(
        self, video_path: str, meta: PublishMetadata, account: dict
    ) -> PublishResult:
        try:
            token = _require(account, "access_token")
            text = self._tweet_text(meta)
            if not text:
                return PublishResult(
                    success=False, error="empty tweet text — nothing to publish"
                )
            raw_path = str(video_path or "").strip()
            if not raw_path:
                # PUBLISH_TEXT: a media-less tweet is an explicit request.
                return self._post_tweet(token, text, [])
            path = Path(raw_path)
            if not path.exists():
                return PublishResult(success=False, error="video file missing")
            if path.stat().st_size == 0:
                return PublishResult(success=False, error="video file is empty")
            if path.suffix.lower() in IMAGE_EXT:
                media_ids = [self._upload_image(token, path)]
            else:
                media_ids = [self._upload_video(token, path)]
            return self._post_tweet(token, text, media_ids)
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except PublisherError as exc:
            return PublishResult(success=False, error=str(exc), retryable=False)
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(
                success=False,
                error=f"X API error: {exc}",
                retryable=retryable,
            )


__all__ = [
    "CHUNK_SIZE",
    "IMAGE_EXT",
    "MAX_TWEET_CHARS",
    "MEDIA_POLL_DEADLINE_S",
    "XPublisher",
]
