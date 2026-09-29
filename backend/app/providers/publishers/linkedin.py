"""LinkedIn publisher — official Assets register-upload + ``/rest/posts`` API.

Publishing modes (all real, no relay, no browser automation):
  * **text**  — ``video_path`` empty/blank → ``POST /rest/posts`` without
    ``content`` (PUBLISH_TEXT).
  * **image** — ``POST /assets?action=registerUpload`` (``feedshare-image``
    recipe) → ``PUT`` the bytes to the returned ``uploadUrl`` → posts with
    ``content.media`` (PUBLISH_IMAGE).
  * **video** — same flow with the ``feedshare-video`` recipe
    (PUBLISH_VIDEO / PUBLISH_SHORT; LinkedIn accepts ≤15 min / 5 GB).

Honest failures: a missing token or owner id returns
``PublishResult(success=False, error="account missing credentials: ...")`` —
never a simulated publish. HTTP errors return ``retryable=True`` for
429/5xx/connection problems.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.providers.publishers.base import (
    BasePublisher,
    PublishMetadata,
    PublishResult,
)
from app.providers.publishers.platforms import _disclosure_suffix, _require

#: LinkedIn post/commentary hard limit (matches the campaign profile).
MAX_COMMENTARY = 3000
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class LinkedInPublisher(BasePublisher):
    platform = "linkedin"
    API = "https://api.linkedin.com"
    #: Documented LinkedIn API version; bump when LinkedIn deprecates it.
    VERSION = "202609"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=60)
        return self._client

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _owner(account: dict) -> str:
        raw = str(
            account.get("owner_urn")
            or account.get("urn")
            or account.get("external_id")
            or ""
        ).strip()
        if not raw:
            raise PermissionError(
                "account missing credentials: external_id (LinkedIn person/"
                "organization id) — reconnect the account"
            )
        if raw.startswith("urn:li:"):
            return raw
        if raw.startswith(("person:", "organization:")):
            return f"urn:li:{raw}"
        return f"urn:li:person:{raw}"

    @staticmethod
    def _commentary(meta: PublishMetadata) -> str:
        parts = [meta.title, meta.description, " ".join(meta.hashtags)]
        text = " ".join(p for p in parts if p).strip()
        text = (text + _disclosure_suffix(meta)).strip()
        return text[:MAX_COMMENTARY]

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": self.VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def _register_upload(self, token: str, owner: str, kind: str) -> tuple[str, str]:
        """(asset URN, uploadUrl) for a feedshare image/video recipe."""
        resp = self.http.post(
            f"{self.API}/assets?action=registerUpload",
            headers=self._headers(token),
            json={
                "registerUploadRequest": {
                    "owner": owner,
                    "recipes": [f"urn:li:digitalmediaRecipe:feedshare-{kind}"],
                    "serviceRelationships": [
                        {
                            "identifier": "urn:li:userGeneratedProducts",
                            "relationship": "OWNER",
                        }
                    ],
                }
            },
        )
        resp.raise_for_status()
        values = resp.json().get("values") or {}
        asset = str(values.get("asset") or "")
        upload_url = str(values.get("uploadUrl") or "")
        if not asset or not upload_url:
            raise httpx.HTTPStatusError(
                f"linkedin: registerUpload returned no asset/uploadUrl: "
                f"{resp.text[:200]}",
                request=resp.request,
                response=resp,
            )
        return asset, upload_url

    def _upload(self, upload_url: str, path: Path) -> None:
        mime = "video/mp4" if path.suffix.lower() not in IMAGE_EXT else "image/*"
        put = self.http.put(
            upload_url,
            content=path.read_bytes(),
            headers={"Content-Type": mime},
        )
        put.raise_for_status()

    def _create_post(
        self, token: str, owner: str, commentary: str, asset: str = ""
    ) -> PublishResult:
        body: dict = {
            "author": owner,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "TARGETED_FEED_DISTRIBUTION",
                "defaultDistribution": "SELF",
                "targetableEntities": {},
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if asset:
            body["content"] = {"media": {"id": asset}}
        resp = self.http.post(
            f"{self.API}/rest/posts",
            headers=self._headers(token),
            json=body,
        )
        resp.raise_for_status()
        post_id = str(
            resp.headers.get("x-restli-id") or (resp.json() or {}).get("id") or ""
        )
        return PublishResult(
            success=True,
            remote_post_id=post_id,
            remote_url=f"https://www.linkedin.com/feed/update/{post_id}" if post_id else "",
        )

    # -- BasePublisher ------------------------------------------------------
    def publish(
        self, video_path: str, meta: PublishMetadata, account: dict
    ) -> PublishResult:
        try:
            token = _require(account, "access_token")
            owner = self._owner(account)
            commentary = self._commentary(meta)
            if not commentary:
                return PublishResult(
                    success=False, error="empty post text — nothing to publish"
                )
            raw_path = str(video_path or "").strip()
            if not raw_path:
                # PUBLISH_TEXT: a media-less feed post is an explicit request.
                return self._create_post(token, owner, commentary)
            path = Path(raw_path)
            if not path.exists():
                return PublishResult(success=False, error="video file missing")
            if path.stat().st_size == 0:
                return PublishResult(success=False, error="video file is empty")
            kind = "image" if path.suffix.lower() in IMAGE_EXT else "video"
            asset, upload_url = self._register_upload(token, owner, kind)
            self._upload(upload_url, path)
            return self._create_post(token, owner, commentary, asset=asset)
        except PermissionError as exc:
            return PublishResult(success=False, error=str(exc))
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status >= 500 or status == 429
            return PublishResult(
                success=False,
                error=f"LinkedIn API error: {exc}",
                retryable=retryable,
            )


__all__ = ["IMAGE_EXT", "MAX_COMMENTARY", "LinkedInPublisher"]
