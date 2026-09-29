"""YouTube inbox provider — official Data API v3 comment endpoints.

Declared capabilities (implemented below):
  READ_COMMENTS  GET  /youtube/v3/commentThreads
                 (``allThreadsRelatedToChannelId`` for channel-wide reads,
                  ``videoId`` when ``post_remote_id`` is supplied)
  REPLY_COMMENT  POST /youtube/v3/comments?part=snippet (``parentId``)
  DELETE_COMMENT DELETE /youtube/v3/comments?id=...

Not declared (no official endpoint wired up here): READ_MENTIONS,
READ_MESSAGES — YouTube exposes neither for a channel, so we never claim it.

OAuth: tokens come from the encrypted ``SocialAccount`` row; when the access
token is expired and Google client credentials are configured, the provider
refreshes it via POST https://oauth2.googleapis.com/token (the refreshed
token is used for the call, not persisted — reconnecting persists grants).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.security import decrypt_secret
from app.engine.platform_registry import Capability
from app.providers.social.base import (
    CommentItem,
    Page,
    ProviderNotConfigured,
    Receipt,
    SocialPlatformProvider,
    _parse_dt,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"


class YouTubeProvider(SocialPlatformProvider):
    platform = "youtube"
    capabilities = frozenset({
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
    })
    API = "https://www.googleapis.com/youtube/v3"

    # -- credentials --------------------------------------------------------
    def _access_token(self, account: Any) -> str:
        token = self.access_token(account)
        if not self._expired(account):
            return token
        return self._refresh(account) or token

    def _expired(self, account: Any) -> bool:
        if isinstance(account, dict):
            return False
        expires = getattr(account, "token_expires_at", None)
        if not expires:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    def _refresh(self, account: Any) -> str:
        """Refresh an expired Google token; '' when refresh material is absent."""
        if isinstance(account, dict):
            refresh_token = str(account.get("refresh_token") or "")
        else:
            refresh_enc = str(getattr(account, "refresh_token_enc", "") or "")
            refresh_token = decrypt_secret(refresh_enc) if refresh_enc else ""
        if not refresh_token:
            return ""
        from app.services.provider_settings import google_oauth_client

        client_id, client_secret = google_oauth_client(
            workspace_id=self.workspace_id(account)
        )
        if not client_id or not client_secret:
            return ""
        resp = self.http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise ProviderNotConfigured(
                f"youtube: token refresh failed ({resp.status_code}) — "
                "reconnect the Google account",
                platform=self.platform,
            )
        return str(resp.json().get("access_token") or "")

    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # -- READ_COMMENTS ------------------------------------------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        token = self._access_token(account)
        params: dict[str, Any] = {
            "part": "snippet,replies",
            "maxResults": limit,
            "order": "date",
            "textFormat": "plainText",
        }
        if post_remote_id:
            params["videoId"] = post_remote_id
        else:
            params["allThreadsRelatedToChannelId"] = self._resolve_channel(account, token)
        if cursor:
            params["pageToken"] = cursor
        resp = self._check(
            self.http.get(
                f"{self.API}/commentThreads",
                params=params,
                headers=self._headers(token),
            ),
            what="commentThreads.list",
        )
        data = resp.json()
        items: list[CommentItem] = []
        for thread in data.get("items") or []:
            thread_id = str(thread.get("id") or "")
            top = (thread.get("snippet") or {}).get("topLevelComment") or {}
            top_id = str(top.get("id") or "")
            top_snippet = top.get("snippet") or {}
            video_id = str(
                (thread.get("snippet") or {}).get("videoId")
                or top_snippet.get("videoId")
                or post_remote_id
                or ""
            )
            if top_id:
                items.append(
                    self._to_item(top_id, top_snippet, video_id, parent=None,
                                  thread_id=thread_id)
                )
            # Replies ride along with their thread; the page limit bounds the
            # number of threads fetched, and every reply in those threads is
            # returned so moderation never misses a nested comment.
            for reply in ((thread.get("replies") or {}).get("comments") or []):
                snippet = reply.get("snippet") or {}
                items.append(
                    self._to_item(
                        str(reply.get("id") or ""),
                        snippet,
                        video_id,
                        parent=str(snippet.get("parentId") or top_id or ""),
                        thread_id=thread_id,
                    )
                )
        next_cursor = data.get("nextPageToken") or None
        return Page(items=items, next_cursor=str(next_cursor) if next_cursor else None)

    @staticmethod
    def _to_item(
        remote_id: str,
        snippet: dict,
        post_id: str,
        *,
        parent: str | None,
        thread_id: str,
    ) -> CommentItem:
        author = snippet.get("authorChannelId") or {}
        return CommentItem(
            remote_id=remote_id,
            post_remote_id=post_id,
            text=str(snippet.get("textDisplay") or snippet.get("textOriginal") or ""),
            author_remote_id=str(author.get("value") or ""),
            author_name=str(snippet.get("authorDisplayName") or ""),
            created_at=_parse_dt(snippet.get("publishedAt")),
            parent_remote_id=parent,
            thread_id=thread_id or post_id,
        )

    def _resolve_channel(self, account: Any, token: str) -> str:
        """Channel id from the stored external_id, else channels.list?mine=true."""
        channel = self.external_id(account) or str(
            self.account_meta(account).get("channel_id") or ""
        )
        if channel:
            return channel
        resp = self._check(
            self.http.get(
                f"{self.API}/channels",
                params={"part": "id", "mine": "true"},
                headers=self._headers(token),
            ),
            what="channels.list",
        )
        items = resp.json().get("items") or []
        if not items:
            raise ProviderNotConfigured(
                "youtube: token has no channel (missing youtube.readonly "
                "scope) — reconnect the account",
                platform=self.platform,
            )
        return str(items[0].get("id") or "")

    # -- REPLY_COMMENT ------------------------------------------------------
    def _reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        token = self._access_token(account)
        resp = self._check(
            self.http.post(
                f"{self.API}/comments",
                params={"part": "snippet"},
                headers=self._headers(token),
                json={"snippet": {"parentId": remote_id, "textOriginal": text}},
            ),
            what="comments.insert",
        )
        body = resp.json()
        return Receipt(remote_reply_id=str(body.get("id") or ""), raw=body, mock=False)

    # -- DELETE_COMMENT -----------------------------------------------------
    def _delete_comment(self, account: Any, remote_id: str) -> Receipt:
        token = self._access_token(account)
        resp = self._check(
            self.http.delete(
                f"{self.API}/comments",
                params={"id": remote_id},
                headers=self._headers(token),
            ),
            what="comments.delete",
        )
        try:
            raw = resp.json() if resp.content else {}
        except ValueError:
            raw = {}
        return Receipt(remote_reply_id=remote_id, raw=raw, mock=False)


__all__ = ["YouTubeProvider"]
