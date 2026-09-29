"""Facebook inbox provider — official Graph API comment endpoints.

Declared capabilities (implemented below):
  READ_COMMENTS   GET    /v21.0/{post-id}/comments      (``after`` cursor paging)
  REPLY_COMMENT   POST   /v21.0/{comment-id}/replies
  DELETE_COMMENT  DELETE /v21.0/{comment-id}

Not declared (no official endpoint wired up here, so never claimed):
READ_MENTIONS, READ_MESSAGES — the Graph API exposes neither for a Page
token in this repo.

``post_remote_id`` (the reel/post id) is required: Facebook's comment API is
scoped to a single post. OAuth tokens are decrypted from the encrypted
``SocialAccount`` row internally; 401/403 surface as
:class:`ProviderNotConfigured` and 429 as :class:`ProviderRateLimited`.
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


def _graph_dt(value: Any) -> Any:
    """Graph timestamps are ``2026-01-01T00:00:00+0000`` — normalize first."""
    if isinstance(value, str) and len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    return _parse_dt(value)


class FacebookProvider(SocialPlatformProvider):
    platform = "facebook"
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
                "facebook: post_remote_id is required — Graph comment reads are "
                "scoped to a single post"
            )
        token = self.access_token(account)
        params: dict[str, Any] = {
            "access_token": token,
            "limit": limit,
            "fields": "id,message,from,created_time,parent",
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
        author = raw.get("from") or {}
        parent = (raw.get("parent") or {}).get("id") or None
        return CommentItem(
            remote_id=str(raw.get("id") or ""),
            post_remote_id=post_id,
            text=str(raw.get("message") or ""),
            author_remote_id=str(author.get("id") or ""),
            author_name=str(author.get("name") or ""),
            created_at=_graph_dt(raw.get("created_time")),
            parent_remote_id=parent,
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


__all__ = ["FacebookProvider"]
