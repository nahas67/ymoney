"""Social platform provider abstraction (Work 09, Lane A).

One interface for inbox reads (comments/mentions) and writes (replies,
deletes) across platforms, with **explicit capability declarations**: a
provider only declares what it actually implements against an official
platform API. Anything else raises an honest error instead of pretending.

Contract notes for callers (Lanes B/C/D):
  * ``account`` is a ``SocialAccount`` ORM row (or a plain dict carrying the
    already-resolved fields for tests). Providers decrypt OAuth tokens
    INTERNALLY through the app secrets service — callers never see or pass
    raw tokens.
  * ``post_remote_id`` is an optional keyword on the read methods. It is
    required by platforms whose comment API is scoped to a single post
    (Facebook/Instagram/LinkedIn/X) and optional on YouTube (channel-wide
    when omitted).
  * Pagination cursors are opaque provider strings (``Page.next_cursor``);
    pass them back verbatim. ``None`` means exhausted.
  * HTTP goes through ``httpx``; inject ``client=``/``transport=`` in tests.

Official endpoints implemented by the concrete providers are documented in
each module docstring. No browser automation, ever.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.engine.platform_registry import Capability

#: Hard cap accepted per call; providers never ask for more.
MAX_PAGE_SIZE = 100


class ProviderNotConfigured(Exception):
    """Missing/expired credentials, scopes, or an unavailable API product.

    This is the honest "we cannot do this for real right now" error — never
    fall back to simulated data on this exception.
    """

    def __init__(self, message: str, *, platform: str = "") -> None:
        super().__init__(message)
        self.platform = platform


class ProviderCapabilityError(Exception):
    """The requested capability is not declared/implemented for this platform."""

    def __init__(self, platform: str, capability: Capability | str) -> None:
        cap = capability.value if isinstance(capability, Capability) else str(capability)
        super().__init__(
            f"{platform}: capability {cap} is not implemented — check "
            "app.engine.platform_registry before calling"
        )
        self.platform = platform
        self.capability = cap


class ProviderRateLimited(Exception):
    """HTTP 429 / platform rate limit. ``retry_after`` is seconds when known."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        platform: str = "",
    ) -> None:
        super().__init__(message)
        self.platform = platform
        self.retry_after = retry_after


@dataclass(frozen=True)
class CommentItem:
    remote_id: str
    post_remote_id: str
    text: str
    author_remote_id: str = ""
    author_name: str = ""
    created_at: datetime | None = None
    parent_remote_id: str | None = None  # None = top-level comment
    thread_id: str = ""  # provider thread/topic id (fallback: post_remote_id)


@dataclass(frozen=True)
class Page:
    items: list[CommentItem]
    next_cursor: str | None


@dataclass(frozen=True)
class Receipt:
    remote_reply_id: str
    raw: dict
    mock: bool = False


def _parse_dt(value: Any) -> datetime | None:
    """ISO-8601 (with trailing Z) → aware datetime; None on anything else."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SocialPlatformProvider(ABC):
    """Base class: capability gating, credentials, HTTP + error mapping."""

    platform: str = "base"
    capabilities: frozenset[Capability] = frozenset()
    #: True when READ_COMMENTS is scoped to a single post (endpoints that
    #: require a parent id, e.g. Graph/Business/tweet-replies APIs). The inbox
    #: sync engine then iterates the account's published posts instead of
    #: attempting a channel-wide read that the API does not offer.
    comments_require_post: bool = False

    def __init__(
        self,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if client is not None:
            self._client: httpx.Client | None = client
        else:
            self._client = httpx.Client(timeout=30, transport=transport) if transport else None

    # -- HTTP ---------------------------------------------------------------
    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> SocialPlatformProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- capability gating --------------------------------------------------
    def supports(self, cap: Capability | str) -> bool:
        if isinstance(cap, Capability):
            return cap in self.capabilities
        try:
            return Capability(str(cap)) in self.capabilities
        except ValueError as exc:
            raise ValueError(f"unknown capability {cap!r}") from exc

    def _require(self, cap: Capability) -> None:
        if cap not in self.capabilities:
            raise ProviderCapabilityError(self.platform, cap)

    # -- credentials (tokens are decrypted here and never returned) ---------
    def access_token(self, account: Any) -> str:
        """Decrypted OAuth access token; ProviderNotConfigured when absent."""
        if isinstance(account, dict):
            token = str(account.get("access_token") or "")
        else:
            from app.core.security import decrypt_secret

            token = decrypt_secret(getattr(account, "access_token_enc", "") or "")
        if not token:
            raise ProviderNotConfigured(
                f"{self.platform}: no access token — connect the account "
                "(Settings → Publishing) before using the inbox",
                platform=self.platform,
            )
        return token

    def workspace_id(self, account: Any) -> str:
        if isinstance(account, dict):
            return str(account.get("workspace_id") or "")
        return str(getattr(account, "workspace_id", "") or "")

    def external_id(self, account: Any) -> str:
        if isinstance(account, dict):
            return str(account.get("external_id") or "")
        return str(getattr(account, "external_id", "") or "")

    def account_meta(self, account: Any) -> dict:
        if isinstance(account, dict):
            return dict(account.get("meta_json") or account.get("meta") or {})
        return dict(getattr(account, "meta_json", None) or {})

    # -- response mapping ---------------------------------------------------
    def _check(self, resp: httpx.Response, *, what: str) -> httpx.Response:
        """Map platform errors to honest exceptions (429/401/403 first)."""
        if resp.status_code == 429:
            raise ProviderRateLimited(
                f"{self.platform}: rate limited during {what} "
                f"({resp.text[:200]})",
                retry_after=self._retry_after(resp),
                platform=self.platform,
            )
        if resp.status_code in (401, 403):
            raise ProviderNotConfigured(
                f"{self.platform}: {what} rejected ({resp.status_code}) — "
                f"token revoked, scopes missing, or API product not enabled: "
                f"{resp.text[:200]}",
                platform=self.platform,
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise type(exc)(
                f"{self.platform}: {what} failed ({resp.status_code}): "
                f"{resp.text[:300]}",
                request=exc.request,
                response=exc.response,
            ) from None
        return resp

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        """Seconds until the limit resets, from standard rate-limit headers."""
        header = resp.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        reset = resp.headers.get("x-rate-limit-reset")
        if reset:
            try:
                from datetime import UTC

                delta = float(reset) - datetime.now(UTC).timestamp()
                return max(delta, 0.0)
            except ValueError:
                pass
        return None

    @staticmethod
    def _page_limit(limit: int) -> int:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        return min(limit, MAX_PAGE_SIZE)

    # -- public API ---------------------------------------------------------
    def list_comments(
        self,
        account: Any,
        *,
        cursor: str | None = None,
        limit: int = 50,
        post_remote_id: str = "",
    ) -> Page:
        """List comments. ``post_remote_id`` is required by post-scoped APIs."""
        self._require(Capability.READ_COMMENTS)
        return self._list_comments(
            account, cursor=cursor, limit=self._page_limit(limit),
            post_remote_id=post_remote_id,
        )

    def list_mentions(
        self,
        account: Any,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page:
        self._require(Capability.READ_MENTIONS)
        return self._list_mentions(account, cursor=cursor, limit=self._page_limit(limit))

    def reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        self._require(Capability.REPLY_COMMENT)
        if not str(text or "").strip():
            raise ValueError("reply text must be non-empty")
        if not str(remote_id or "").strip():
            raise ValueError("remote_id must be non-empty")
        return self._reply_to_comment(account, remote_id, text)

    def delete_comment(self, account: Any, remote_id: str) -> Receipt:
        self._require(Capability.DELETE_COMMENT)
        if not str(remote_id or "").strip():
            raise ValueError("remote_id must be non-empty")
        return self._delete_comment(account, remote_id)

    # -- subclass hooks (override only what is declared) --------------------
    def _list_comments(
        self, account: Any, *, cursor: str | None, limit: int, post_remote_id: str
    ) -> Page:
        raise ProviderCapabilityError(self.platform, Capability.READ_COMMENTS)

    def _list_mentions(self, account: Any, *, cursor: str | None, limit: int) -> Page:
        raise ProviderCapabilityError(self.platform, Capability.READ_MENTIONS)

    def _reply_to_comment(self, account: Any, remote_id: str, text: str) -> Receipt:
        raise ProviderCapabilityError(self.platform, Capability.REPLY_COMMENT)

    def _delete_comment(self, account: Any, remote_id: str) -> Receipt:
        raise ProviderCapabilityError(self.platform, Capability.DELETE_COMMENT)


__all__ = [
    "MAX_PAGE_SIZE",
    "CommentItem",
    "Page",
    "ProviderCapabilityError",
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "Receipt",
    "SocialPlatformProvider",
    "_parse_dt",
]
