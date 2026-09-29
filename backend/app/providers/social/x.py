"""X (Twitter) inbox provider — official X API v2 endpoints.

Declared capabilities (implemented below):
  READ_COMMENTS   GET  /2/tweets/{id}/replies     (``pagination_token`` paging)
  READ_MENTIONS   GET  /2/users/{id}/mentions     (``/2/users/me`` fallback)
  REPLY_COMMENT   POST /2/tweets                  (``reply.in_reply_to_tweet_id``)
  DELETE_COMMENT  DELETE /2/tweets/{id}

Not declared (no official endpoint wired up here, so never claimed):
READ_MESSAGES — X has no DM ingestion wired up in YMONEY.

``post_remote_id`` (the tweet id) is required for comment reads: the replies
timeline is scoped to a single tweet. Auth: the decrypted OAuth user access
token as a Bearer header (needs ``tweet.read``/``users.read``/
``like.write``/``tweet.write``/``offline.access`` as applicable); 401/403
surface as :class:`ProviderNotConfigured`, 429 (including the
``x-rate-limit-reset`` header) as :class:`ProviderRateLimited`.

Note: the replies/mentions endpoints enforce API minimum page sizes (10 and 5
respectively), so ``limit`` is clamped up to the platform minimum before the
request — a page may therefore return more rows than the caller asked for,
but never fewer than the API allows and never past the cursor.
"""

from __future__ import annotations

from typing import Any

from app.engine.platform_registry import Capability
from app.providers.social.base import (
    CommentItem,
    Page,
    ProviderNotConfigured,
    Receipt,
    SocialPlatformProvider,
    _parse_dt,
)

#: GET /2/tweets/:id/replies accepts max_results in [10, 100].
MIN_REPLIES_PAGE = 10
#: GET /2/users/:id/mentions accepts max_results in [5, 100].
MIN_MENTIONS_PAGE = 5
MAX_PAGE = 100


class XProvider(SocialPlatformProvider):
    platform = "x"
    capabilities = frozenset({
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.READ_MENTIONS,
        Capability.DELETE_COMMENT,
    })
    comments_require_post = True
    API = "https://api.x.com/2"

    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _authors(payload: dict) -> dict:
        """author_id -> "Name @username" from ``includes.users``."""
        out: dict[str, str] = {}
        for user in ((payload.get("includes") or {}).get("users") or []):
            uid = str(user.get("id") or "")
            if not uid:
                continue
            name = str(user.get("name") or "")
            handle = str(user.get("username") or "")
            out[uid] = f"{name} @{handle}".strip() if handle else name
        return out

    def _timeline_items(
        self, payload: dict, *, post_id: str, reply_parent: bool
    ) -> list[CommentItem]:
        authors = self._authors(payload)
        items: list[CommentItem] = []
        for raw in (payload.get("data") or []):
            if not isinstance(raw, dict):
                continue
            author_id = str(raw.get("author_id") or "")
            items.append(
                CommentItem(
                    remote_id=str(raw.get("id") or ""),
                    post_remote_id=post_id,
                    text=str(raw.get("text") or ""),
                    author_remote_id=author_id,
                    author_name=authors.get(author_id, ""),
                    created_at=_parse_dt(raw.get("created_at")),
                    parent_remote_id=post_id if reply_parent else None,
                    thread_id=post_id,
                )
            )
        return items

    def _page_token(self, payload: dict) -> str | None:
        token = ((payload.get("meta") or {}).get("next_token")) or None
        return str(token) if token else None

    # -- READ_COMMENTS ------------------------------------------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        if not str(post_remote_id or "").strip():
            raise ValueError(
                "x: post_remote_id (tweet id) is required — the replies "
                "timeline is scoped to a single tweet"
            )
        token = self.access_token(account)
        params: dict[str, Any] = {
            "max_results": max(MIN_REPLIES_PAGE, min(limit, MAX_PAGE)),
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        if cursor:
            params["pagination_token"] = str(cursor)
        resp = self._check(
            self.http.get(
                f"{self.API}/tweets/{post_remote_id}/replies",
                params=params,
                headers=self._headers(token),
            ),
            what="tweets.replies",
        )
        payload = resp.json()
        return Page(
            items=self._timeline_items(
                payload, post_id=str(post_remote_id), reply_parent=True
            ),
            next_cursor=self._page_token(payload),
        )

    # -- READ_MENTIONS ------------------------------------------------------
    def _list_mentions(
        self, account: Any, *, cursor: str | None, limit: int
    ) -> Page:
        token = self.access_token(account)
        user_id = self._user_id(account, token)
        params: dict[str, Any] = {
            "max_results": max(MIN_MENTIONS_PAGE, min(limit, MAX_PAGE)),
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        if cursor:
            params["pagination_token"] = str(cursor)
        resp = self._check(
            self.http.get(
                f"{self.API}/users/{user_id}/mentions",
                params=params,
                headers=self._headers(token),
            ),
            what="users.mentions",
        )
        payload = resp.json()
        # a mention IS the post it lives on; parent stays None (top-level)
        return Page(
            items=self._timeline_items(
                payload, post_id="", reply_parent=False
            ),
            next_cursor=self._page_token(payload),
        )

    def _user_id(self, account: Any, token: str) -> str:
        """Stored external_id / meta user id, else GET /2/users/me."""
        user_id = self.external_id(account) or str(
            self.account_meta(account).get("user_id") or ""
        )
        if user_id:
            return user_id
        resp = self._check(
            self.http.get(f"{self.API}/users/me", headers=self._headers(token)),
            what="users.me",
        )
        user_id = str((resp.json().get("data") or {}).get("id") or "")
        if not user_id:
            raise ProviderNotConfigured(
                "x: token has no user id (missing users.read scope) — "
                "reconnect the account",
                platform=self.platform,
            )
        return user_id

    # -- REPLY_COMMENT ------------------------------------------------------
    def _reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        token = self.access_token(account)
        resp = self._check(
            self.http.post(
                f"{self.API}/tweets",
                headers=self._headers(token),
                json={"text": text, "reply": {"in_reply_to_tweet_id": remote_id}},
            ),
            what="tweets.create",
        )
        body = resp.json()
        return Receipt(
            remote_reply_id=str((body.get("data") or {}).get("id") or ""),
            raw=body,
            mock=False,
        )

    # -- DELETE_COMMENT -----------------------------------------------------
    def _delete_comment(self, account: Any, remote_id: str) -> Receipt:
        token = self.access_token(account)
        resp = self._check(
            self.http.delete(
                f"{self.API}/tweets/{remote_id}",
                headers=self._headers(token),
            ),
            what="tweets.delete",
        )
        try:
            raw = resp.json() if resp.content else {}
        except ValueError:
            raw = {}
        return Receipt(remote_reply_id=remote_id, raw=raw, mock=False)


__all__ = ["MAX_PAGE", "MIN_MENTIONS_PAGE", "MIN_REPLIES_PAGE", "XProvider"]
