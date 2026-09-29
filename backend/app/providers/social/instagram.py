"""Instagram inbox provider — official Graph API comment endpoints.

Declared capabilities (implemented below):
  READ_COMMENTS   GET    /v21.0/{media-id}/comments     (``after`` cursor paging)
  REPLY_COMMENT   POST   /v21.0/{comment-id}/replies
  DELETE_COMMENT  DELETE /v21.0/{comment-id}

Not declared (no official endpoint wired up here, so never claimed):
READ_MENTIONS, READ_MESSAGES. Instagram image posts are also not published by
``InstagramPublisher`` (see the platform registry), so only Reels publishing
is claimed at the platform level.

``post_remote_id`` (the reel/media id) is required: the Graph comment API is
scoped to a single media object. Needs the ``instagram_manage_comments``
scope; 401/403 surface as :class:`ProviderNotConfigured`.
"""

from __future__ import annotations

from typing import Any

from app.engine.platform_registry import Capability
from app.providers.social.base import (
    CommentItem,
    Page,
    Receipt,
    SocialPlatformProvider,
    _parse_dt,
)


class InstagramProvider(SocialPlatformProvider):
    platform = "instagram"
    capabilities = frozenset({
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
    })
    comments_require_post = True
    GRAPH = "https://graph.facebook.com/v21.0"

    # -- READ_COMMENTS ------------------------------------------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        if not str(post_remote_id or "").strip():
            raise ValueError(
                "instagram: post_remote_id (media id) is required — Graph "
                "comment reads are scoped to a single media object"
            )
        token = self.access_token(account)
        params: dict[str, Any] = {
            "access_token": token,
            "limit": limit,
            "fields": "id,text,timestamp,username,user_id",
        }
        if cursor:
            params["after"] = str(cursor)
        resp = self._check(
            self.http.get(f"{self.GRAPH}/{post_remote_id}/comments", params=params),
            what="comments.list",
        )
        body = resp.json()
        items = [
            self._to_item(raw, str(post_remote_id))
            for raw in (body.get("data") or [])
        ]
        paging = body.get("paging") or {}
        after = ((paging.get("cursors") or {}).get("after")) or ""
        next_cursor = str(after) if (paging.get("next") and after) else None
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _to_item(raw: dict, post_id: str) -> CommentItem:
        return CommentItem(
            remote_id=str(raw.get("id") or ""),
            post_remote_id=post_id,
            text=str(raw.get("text") or ""),
            author_remote_id=str(raw.get("user_id") or ""),
            author_name=str(raw.get("username") or ""),
            created_at=_parse_dt(raw.get("timestamp")),
            parent_remote_id=None,
            thread_id=post_id,
        )

    # -- REPLY_COMMENT ------------------------------------------------------
    def _reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        token = self.access_token(account)
        resp = self._check(
            self.http.post(
                f"{self.GRAPH}/{remote_id}/replies",
                data={"message": text, "access_token": token},
            ),
            what="comments.replies",
        )
        body = resp.json()
        return Receipt(
            remote_reply_id=str(body.get("id") or ""),
            raw=body,
            mock=False,
        )

    # -- DELETE_COMMENT -----------------------------------------------------
    def _delete_comment(self, account: Any, remote_id: str) -> Receipt:
        token = self.access_token(account)
        resp = self._check(
            self.http.delete(
                f"{self.GRAPH}/{remote_id}",
                params={"access_token": token},
            ),
            what="comments.delete",
        )
        try:
            raw = resp.json() if resp.content else {}
        except ValueError:
            raw = {}
        return Receipt(remote_reply_id=remote_id, raw=raw, mock=False)


__all__ = ["InstagramProvider"]
