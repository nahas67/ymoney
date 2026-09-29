"""LinkedIn inbox provider — official Community Management API.

Declared capabilities (implemented below):
  READ_COMMENTS   GET  /rest/socialActions/{activity-urn}/comments
                  (``q=threadedReplies``, ``startingAt`` offset cursor;
                   nested replies are flattened under their top-level comment)
  REPLY_COMMENT   POST /rest/socialActions/{activity-urn}/comments
                  (``parentComment`` = the comment URN; the activity URN is
                   parsed from ``urn:li:comment:(activity:...,comment:...)``)

Not declared (no official endpoint wired up here, so never claimed):
READ_MENTIONS, DELETE_COMMENT, READ_MESSAGES.

``post_remote_id`` is required (the activity/share URN of the post whose
comments are read). Auth: decrypted OAuth token + ``LinkedIn-Version`` +
``X-Restli-Protocol-Version`` headers; 401/403 surface as
:class:`ProviderNotConfigured`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.engine.platform_registry import Capability
from app.providers.social.base import (
    CommentItem,
    Page,
    Receipt,
    SocialPlatformProvider,
)

#: Comment URN carries the parent activity: urn:li:comment:(activity:...,comment:...)
_COMMENT_URN = re.compile(r"urn:li:comment:\(\s*activity:([^,)\s]+)")


class LinkedInProvider(SocialPlatformProvider):
    platform = "linkedin"
    capabilities = frozenset({
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
    })
    comments_require_post = True
    API = "https://api.linkedin.com"
    #: Documented LinkedIn API version; bump when LinkedIn deprecates it.
    VERSION = "202609"

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": self.VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    @staticmethod
    def _activity_urn(post_remote_id: str) -> str:
        """Normalize the caller's post id to an activity URN."""
        raw = str(post_remote_id or "").strip()
        if raw.startswith("urn:"):
            return raw
        return f"urn:li:activity:{raw}"

    @staticmethod
    def _activity_from_comment(comment_urn: str) -> str:
        """Derive the parent activity URN from a comment URN (for replies)."""
        match = _COMMENT_URN.search(str(comment_urn or ""))
        if not match:
            raise ValueError(
                "linkedin: cannot derive the parent activity URN from comment "
                f"id {comment_urn!r} — expected urn:li:comment:(activity:...,"
                "comment:...)"
            )
        inner = match.group(1)
        return inner if inner.startswith("urn:") else f"urn:li:activity:{inner}"

    # -- READ_COMMENTS ------------------------------------------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        if not str(post_remote_id or "").strip():
            raise ValueError(
                "linkedin: post_remote_id (activity URN) is required — the "
                "socialActions comment API is scoped to a single post"
            )
        token = self.access_token(account)
        activity = self._activity_urn(post_remote_id)
        params: dict[str, Any] = {"q": "threadedReplies", "count": limit}
        if cursor:
            try:
                params["startingAt"] = int(str(cursor))
            except ValueError as exc:
                raise ValueError(f"linkedin: bad cursor {cursor!r}") from exc
        resp = self._check(
            self.http.get(
                f"{self.API}/rest/socialActions/{quote(activity, safe='')}/comments",
                params=params,
                headers=self._headers(token),
            ),
            what="socialActions.comments.list",
        )
        body = resp.json()
        elements = [e for e in (body.get("elements") or []) if isinstance(e, dict)]
        items: list[CommentItem] = []
        for element in elements:
            items.append(self._to_item(element, activity, parent=None))
            for reply in (element.get("elements") or []):
                if isinstance(reply, dict):
                    items.append(
                        self._to_item(
                            reply, activity, parent=str(element.get("id") or "")
                        )
                    )
        paging = body.get("paging") or {}
        try:
            start = int(paging.get("start") or 0)
            total = int(paging.get("total") or 0)
        except (TypeError, ValueError):
            start, total = 0, 0
        consumed = start + len(elements)
        next_cursor = str(consumed) if (elements and total and consumed < total) else None
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _to_item(element: dict, activity: str, *, parent: str | None) -> CommentItem:
        created_ms = element.get("createdAt")
        created_at = None
        if isinstance(created_ms, (int, float)) and created_ms > 0:
            created_at = datetime.fromtimestamp(int(created_ms) / 1000.0, tz=UTC)
        linked_parent = str(element.get("parentComment") or "") or None
        return CommentItem(
            remote_id=str(element.get("id") or ""),
            post_remote_id=activity,
            text=str((element.get("message") or {}).get("text") or ""),
            author_remote_id=str(element.get("author") or ""),
            author_name="",  # the comments API returns person URNs, not names
            created_at=created_at,
            parent_remote_id=parent or linked_parent,
            thread_id=activity,
        )

    # -- REPLY_COMMENT ------------------------------------------------------
    def _reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        activity = self._activity_from_comment(remote_id)
        token = self.access_token(account)
        resp = self._check(
            self.http.post(
                f"{self.API}/rest/socialActions/{quote(activity, safe='')}/comments",
                headers=self._headers(token),
                json={"parentComment": remote_id, "message": {"text": text}},
            ),
            what="socialActions.comments.create",
        )
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        reply_id = str(body.get("id") or resp.headers.get("x-restli-id") or "")
        return Receipt(remote_reply_id=reply_id, raw=body, mock=False)


__all__ = ["LinkedInProvider"]
