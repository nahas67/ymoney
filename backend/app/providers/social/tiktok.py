"""TikTok inbox provider — official Business API comment endpoints.

Declared capabilities (implemented below):
  READ_COMMENTS   POST /v2/video/comment/list/     (``cursor``/``has_more`` paging,
                                                     top-level comments only)
  DELETE_COMMENT  POST /v2/video/comment/delete/

Not declared (no official endpoint wired up here, so never claimed):
  REPLY_COMMENT  — ``/v2/video/comment/publish/`` requires the parent
                   ``video_id`` together with the comment id, and the
                   cross-lane reply contract is ``reply_to_comment(account,
                   remote_id, text)`` with no room for a video id (comment ids
                   are not derivable back to a video). Claimed only once that
                   can be satisfied for real.
  READ_MENTIONS, READ_MESSAGES — no official endpoint wired up here.

``post_remote_id`` (the video id) is required: the Business comment API is
scoped to a single video. Auth: the decrypted access token is sent both as an
``access_token`` query parameter and an ``Authorization: Bearer`` header (both
are accepted by the API); needs a business/creator account with the comment
scopes. A non-zero ``error.code`` in a 200 body is mapped to an honest error,
never silently ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.engine.platform_registry import Capability
from app.providers.social.base import (
    CommentItem,
    Page,
    ProviderNotConfigured,
    Receipt,
    SocialPlatformProvider,
)

#: API maximum comments per request.
MAX_COMMENTS_PER_PAGE = 20


class TikTokProvider(SocialPlatformProvider):
    platform = "tiktok"
    capabilities = frozenset({
        Capability.READ_COMMENTS,
        Capability.DELETE_COMMENT,
    })
    comments_require_post = True
    API = "https://open.tiktokapis.com/v2"

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _payload(self, resp: httpx.Response, what: str) -> dict:
        """Full JSON body; maps HTTP and TikTok-level errors honestly."""
        self._check(resp, what=what)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        error = body.get("error") or {}
        code = error.get("code") or 0
        if code:
            message = str(error.get("message") or error.get("description") or "")
            lowered = f"{code} {message}".lower()
            if any(k in lowered for k in ("token", "scope", "permission", "access")):
                raise ProviderNotConfigured(
                    f"tiktok: {what} rejected (code {code}) — reconnect the "
                    f"account with the comment scopes: {message}",
                    platform=self.platform,
                )
            raise httpx.HTTPStatusError(
                f"tiktok: {what} failed (code {code}): {message}",
                request=resp.request,
                response=resp,
            )
        return body

    # -- READ_COMMENTS ------------------------------------------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        if not str(post_remote_id or "").strip():
            raise ValueError(
                "tiktok: post_remote_id (video_id) is required — the Business "
                "comment API is scoped to a single video"
            )
        token = self.access_token(account)
        body: dict[str, Any] = {
            "video_id": str(post_remote_id),
            "count": min(limit, MAX_COMMENTS_PER_PAGE),
        }
        if cursor:
            body["cursor"] = str(cursor)
        resp = self.http.post(
            f"{self.API}/video/comment/list/",
            params={
                "fields": "text,comment_id,create_time,user",
                "access_token": token,
            },
            json=body,
            headers=self._headers(token),
        )
        payload = self._payload(resp, "video.comment.list")
        data = payload.get("data") or {}
        items = [
            self._to_item(raw, str(post_remote_id))
            for raw in (data.get("comments") or [])
            if isinstance(raw, dict)
        ]
        next_value = data.get("cursor")
        next_cursor = (
            str(next_value)
            if data.get("has_more") and next_value not in (None, "")
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _to_item(raw: dict, post_id: str) -> CommentItem:
        user = raw.get("user") or {}
        created = raw.get("create_time")
        created_at = None
        if isinstance(created, (int, float)) and created > 0:
            created_at = datetime.fromtimestamp(int(created), tz=UTC)
        return CommentItem(
            remote_id=str(raw.get("comment_id") or ""),
            post_remote_id=post_id,
            text=str(raw.get("text") or ""),
            author_remote_id=str(user.get("open_id") or ""),
            author_name=str(user.get("display_name") or user.get("nickname") or ""),
            created_at=created_at,
            parent_remote_id=None,
            thread_id=post_id,
        )

    # -- DELETE_COMMENT -----------------------------------------------------
    def _delete_comment(self, account: Any, remote_id: str) -> Receipt:
        token = self.access_token(account)
        resp = self.http.post(
            f"{self.API}/video/comment/delete/",
            params={"access_token": token},
            json={"comment_id": str(remote_id)},
            headers=self._headers(token),
        )
        payload = self._payload(resp, "video.comment.delete")
        data = payload.get("data") or {}
        return Receipt(
            remote_reply_id=str(data.get("comment_id") or remote_id),
            raw=payload,
            mock=False,
        )


__all__ = ["MAX_COMMENTS_PER_PAGE", "TikTokProvider"]
