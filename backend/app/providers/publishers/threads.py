"""Threads publisher (Work 14 §2) -- Meta's OFFICIAL Threads API.

Contract verified against official Meta developer documentation
(https://developers.facebook.com/documentation/threads/*). The load-bearing
facts this implementation is built on:

* There is exactly ONE container-create endpoint:
  ``POST /{threads-user-id}/threads`` with a ``media_type`` discriminator.
  The Instagram-style ``/media`` and ``/media_reel`` paths DO NOT EXIST for
  Threads, and neither does a resumable/binary upload: media is referenced by a
  PUBLIC URL that Meta fetches server-side.
* Publishing is a SECOND call: ``POST /{threads-user-id}/threads_publish`` with
  ``creation_id``. Text posts may skip it with ``auto_publish_text=true``
  (text only).
* A container has a real lifecycle. ``GET /{container}?fields=status,
  error_message`` returns EXPIRED | ERROR | FINISHED | IN_PROGRESS | PUBLISHED.
  Meta recommends polling once per minute for at most 5 minutes; the container
  expires after 24 HOURS, so "a few minutes" is the wrong mental model.
* Replies use the SAME two endpoints with ``reply_to_id`` -- there is no
  separate reply endpoint.
* Alt text is ``alt_text``, max 1000 characters, and works on image/video/
  carousel posts ONLY.
* Carousel is officially supported: children are created with
  ``is_carousel_item=true`` (IMAGE or VIDEO each), then a parent container with
  ``media_type=CAROUSEL`` and a comma-separated ``children`` list, 2..20 items.
  The whole carousel counts as ONE post against the 250/24h quota.
* Quotas are per-profile, rolling 24h: 250 posts, 1000 replies, 100 deletes,
  readable from ``GET /{threads-user-id}/threads_publishing_limit``.
* Auth is a SEPARATE integration from Instagram: threads.com/oauth/authorize,
  graph.threads.com/oauth/access_token, and the Threads-specific
  th_exchange_token / th_refresh_token grants. This provider never reuses an
  Instagram token or id.

Nothing here accepts a caller-supplied filter, expression or URL template from
AI or user input: every parameter is chosen from the closed vocabularies below
or passed through URL validation.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from app.providers.publishers.base import (
    BasePublisher,
    PublishMetadata,
    PublishResult,
)

API_BASE = "https://graph.threads.net/v1.0"

#: Container media types Meta actually accepts at create time. AUDIO is
#: readable on posts but is NOT creatable, so it is deliberately absent.
MEDIA_TYPES = ("TEXT", "IMAGE", "VIDEO", "CAROUSEL")

#: Read-back literals (they differ from create-time on purpose: TEXT ->
#: TEXT_POST, CAROUSEL -> CAROUSEL_ALBUM).
READ_MEDIA_TYPES = ("TEXT_POST", "IMAGE", "VIDEO", "CAROUSEL_ALBUM", "AUDIO",
                    "REPOST_FACADE")

#: Documented container statuses.
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_FINISHED = "FINISHED"
STATUS_ERROR = "ERROR"
STATUS_EXPIRED = "EXPIRED"
STATUS_PUBLISHED = "PUBLISHED"

#: Documented container error_message literals (note Meta's own typo in
#: INVALID_ASPEC_RATIO, reproduced so matching on it works).
CONTAINER_ERRORS = (
    "FAILED_DOWNLOADING_VIDEO", "FAILED_PROCESSING_AUDIO",
    "FAILED_PROCESSING_VIDEO", "INVALID_ASPEC_RATIO", "INVALID_BIT_RATE",
    "INVALID_DURATION", "INVALID_FRAME_RATE", "INVALID_AUDIO_CHANNELS",
    "INVALID_AUDIO_CHANNEL_LAYOUT", "UNKNOWN",
)

#: Documented limits, echoed here so the provider can fail fast rather than
#: letting Meta reject a publish minutes later.
TEXT_MAX_CHARS = 500
ALT_TEXT_MAX = 1000
LINK_LIMIT = 5
POSTS_PER_24H = 250
REPLIES_PER_24H = 1000
CAROUSEL_MIN = 2
CAROUSEL_MAX = 20
VIDEO_MAX_SECONDS = 300.0
#: Meta's documented poll cadence: once per minute, at most 5 minutes.
POLL_INTERVAL_SECONDS = 60.0
POLL_MAX_ATTEMPTS = 5
#: Meta recommends ~30s after creation before publishing.
SETTLE_SECONDS = 30.0

#: Required permissions per operation (official literal scope strings).
PERM_PUBLISH = "threads_content_publish"
PERM_BASIC = "threads_basic"
PERM_MANAGE_REPLIES = "threads_manage_replies"
PERM_READ_REPLIES = "threads_read_replies"
PERM_INSIGHTS = "threads_manage_insights"
PERM_DELETE = "threads_delete"
#: A third-party reply needs one of these ON TOP OF threads_manage_replies.
THIRD_PARTY_REPLY_PERMS = ("threads_keyword_search", "threads_manage_mentions")


class ThreadsError(RuntimeError):
    """Threads API failure. ``retryable`` drives the scheduler's backoff."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


def _public_media_url(url: str) -> str:
    """Validate a media URL before handing it to Meta to fetch.

    Meta cURLs ``image_url``/``video_url`` from its own servers, so an
    arbitrary or internal URL is both a security problem (it turns our
    publisher into a request proxy for whatever the caller typed) and a
    guaranteed publish failure. Only public http(s) is accepted.

    The host is VALIDATED, not fetched: literal private/loopback/link-local
    addresses and ``*.internal``/``*.local`` names are refused outright. A
    hostname that merely resolves to a private address cannot be caught
    without a DNS lookup here, so callers should prefer an allowlisted asset
    host; that residual case is noted rather than pretended away.
    """
    import ipaddress
    from urllib.parse import urlparse

    raw = str(url or "").strip()
    if not raw:
        raise ThreadsError("media url is required: Threads fetches media by URL")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ThreadsError(f"media url must be http(s), got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        # userinfo in a media URL is a credential-leak pattern, never legitimate
        raise ThreadsError("media url must not embed credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ThreadsError("media url has no host")
    if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise ThreadsError(f"media url host {host!r} is not publicly reachable")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ThreadsError(
            f"media url host {host!r} is a non-public address "
            f"({address}); only globally routable media URLs are accepted")
    return raw


def _clip_text(text: str, max_chars: int = TEXT_MAX_CHARS) -> tuple[str, bool]:
    """Return (text, was_clipped). Clipping is never silent -- the caller
    reports it, because a user who typed 500 characters deserves to know."""
    clean = str(text or "")
    if len(clean) <= max_chars:
        return clean, False
    return clean[:max_chars], True


def _count_links(text: str) -> int:
    import re

    return len(re.findall(r"https?://\S+", str(text or "")))


class ThreadsPublisher(BasePublisher):
    """Official Threads API publisher."""

    platform = "threads"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._own_client = False

    # -- plumbing ---------------------------------------------------------

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=30.0)

    def _own(self) -> httpx.Client:
        """The client to use. ``self._own_client`` marks one we must close.

        An INJECTED client is owned by the caller: closing it here would break
        a shared, pooled client mid-publish ("Cannot reopen a client instance,
        once it has been closed"). Only a client this provider created itself is
        closed, and only on the success path.
        """
        if self._client is not None:
            self._own_client = False
            return self._client
        self._own_client = True
        return httpx.Client(timeout=30.0)

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        client = self._own()
        try:
            yield client
        finally:
            if self._own_client:
                client.close()

    @staticmethod
    def _require(account: dict, *keys: str) -> str:
        missing = [k for k in keys if not (account or {}).get(k)]
        if missing:
            raise ThreadsError(
                f"Threads account is missing {missing}: connect the account "
                f"and complete Threads OAuth (a separate integration from "
                f"Instagram) before publishing")
        return ""

    def _params(self, account: dict, data: dict) -> dict:
        token = str((account or {}).get("access_token") or "")
        if not token:
            raise ThreadsError("Threads account has no access_token")
        return {**data, "access_token": token}

    def _user_id(self, account: dict) -> str:
        # "me" is used in official examples; a stored id is preferred when the
        # OAuth exchange returned one.
        return str((account or {}).get("threads_user_id") or "me")

    # -- capability guards ------------------------------------------------

    @staticmethod
    def check_scopes(account: dict, *, need: str) -> None:
        """Fail closed when the granted scopes do not cover ``need``.

        A Threads token is app-scoped, so the granted set is knowable and
        checkable. Guessing here is how an app ends up 403ing in production.
        """
        granted = {str(s) for s in ((account or {}).get("scopes") or [])}
        if not granted:
            return  # token introspection not available; let the API decide
        if need not in granted:
            raise ThreadsError(
                f"Threads token lacks {need!r}; granted={sorted(granted)}")

    def check_quota(self, account: dict) -> dict:
        """Read the documented publishing quota before publishing.

        GET /{threads-user-id}/threads_publishing_limit is the documented way to
        see remaining posts/replies. A failure here is NOT fatal: the limit
        endpoint is a convenience, and refusing to publish because we could not
        read a counter would be worse than attempting and handling the error.
        """
        try:
            with self._session() as http:
                response = http.get(
                    f"{API_BASE}/{self._user_id(account)}/threads_publishing_limit",
                    params=self._params(account, {
                        "fields": "quota_usage,config,reply_quota_usage,reply_config"}),
                )
            if response.status_code >= 400:
                return {"known": False, "reason": f"HTTP {response.status_code}"}
            body = response.json() or {}
            usage = body.get("quota_usage") or 0
            config = body.get("config") or POSTS_PER_24H
            return {"known": True, "used": int(usage), "total": int(config),
                    "remaining": max(0, int(config) - int(usage)),
                    "replies_used": int(body.get("reply_quota_usage") or 0),
                    "replies_total": int(body.get("reply_config") or REPLIES_PER_24H)}
        except Exception as exc:  # noqa: BLE001 - advisory only
            return {"known": False, "reason": str(exc)[:200]}

    # -- the documented container -> publish -> verify flow ---------------

    def _create_container(self, account: dict, *, media_type: str,
                          data: dict) -> str:
        payload = self._params(account, {"media_type": media_type, **data})
        with self._session() as http:
            response = http.post(
                f"{API_BASE}/{self._user_id(account)}/threads", data=payload)
        body = _json(response)
        container_id = str(body.get("id") or "").strip()
        if not container_id:
            raise ThreadsError(
                f"container creation returned no id: {body}", retryable=True)
        return container_id

    def _await_ready(self, account: dict, container_id: str, *,
                     settle: float = SETTLE_SECONDS,
                     interval: float = POLL_INTERVAL_SECONDS,
                     attempts: int = POLL_MAX_ATTEMPTS) -> dict:
        """Poll the documented status endpoint until FINISHED.

        Uses Meta's documented cadence (once per minute, at most 5 minutes).
        With ``settle=0``/``attempts=1`` the poll is skipped entirely, which is
        what the hermetic contract tests do -- a test must not sleep 30s.
        """
        if attempts <= 1:
            return {"status": STATUS_FINISHED, "polled": False}
        if settle > 0:
            time.sleep(settle)
        last: dict[str, Any] = {"status": STATUS_IN_PROGRESS, "polled": True}
        for attempt in range(attempts):
            with self._session() as http:
                response = http.get(
                    f"{API_BASE}/{container_id}",
                    params=self._params(account, {"fields": "status,error_message"}))
            body = _json(response)
            status = str(body.get("status") or "").upper()
            last = {"status": status, "error_message": body.get("error_message"),
                    "attempt": attempt + 1, "polled": True}
            if status in (STATUS_FINISHED, STATUS_PUBLISHED):
                return last
            if status in (STATUS_ERROR, STATUS_EXPIRED):
                raise ThreadsError(
                    f"container {container_id} became {status}: "
                    f"{body.get('error_message')}", retryable=False)
            if attempt < attempts - 1 and interval > 0:
                time.sleep(interval)
        raise ThreadsError(
            f"container {container_id} still {last['status']} after "
            f"{attempts} polls", retryable=True)

    def _publish_container(self, account: dict, container_id: str) -> str:
        with self._session() as http:
            response = http.post(
                f"{API_BASE}/{self._user_id(account)}/threads_publish",
                data=self._params(account, {"creation_id": container_id}))
        body = _json(response)
        media_id = str(body.get("id") or "").strip()
        if not media_id:
            raise ThreadsError(
                f"threads_publish returned no id: {body}", retryable=True)
        return media_id

    def _verify(self, account: dict, media_id: str) -> dict:
        """Read the published post back: id + permalink + alt_text."""
        with self._session() as http:
            response = http.get(
                f"{API_BASE}/{media_id}",
                params=self._params(account, {"fields": "id,permalink,alt_text"}))
        return _json(response)

    # -- public API -------------------------------------------------------

    def publish(self, video_path: str, meta: PublishMetadata,
                account: dict) -> PublishResult:
        """Publish a text / image / video post through the official API."""
        self._require(account, "access_token")
        self.check_scopes(account, need=PERM_PUBLISH)

        text, clipped = _clip_text(
            f"{meta.title}\n\n{meta.description}".strip() if meta.description
            else meta.title)
        links = _count_links(text)
        if links > LINK_LIMIT:
            return PublishResult(
                success=False, retryable=False,
                error=(f"post has {links} links; Threads rejects more than "
                       f"{LINK_LIMIT} at container creation "
                       f"(THREADS_API__LINK_LIMIT_EXCEEDED)"))

        media_url = str((meta.extra or {}).get("media_url") or "").strip()
        if media_url:
            media_type = "IMAGE"
            if str((meta.extra or {}).get("media_kind") or "image").lower() == "video":
                media_type = "VIDEO"
        elif video_path:
            # A local path cannot be published: Threads has no binary upload, it
            # fetches a public URL. Say so instead of silently posting text.
            return PublishResult(
                success=False, retryable=False,
                error=("Threads requires a publicly reachable media URL; got a "
                       "local file path. Upload the asset and pass "
                       "meta.extra['media_url']."))
        else:
            media_type = "TEXT"

        data: dict[str, Any] = {"text": text}
        alt = str((meta.extra or {}).get("alt_text") or "").strip()
        if media_url:
            # A media URL comes from user input and is fetched by Meta, so an
            # unsafe host is a normal, reportable refusal rather than an
            # exception: the caller gets a non-retryable failure it can show.
            try:
                safe_url = _public_media_url(media_url)
            except ThreadsError as exc:
                return PublishResult(success=False, retryable=False,
                                     error=f"media url rejected: {exc}")
            data["video_url" if media_type == "VIDEO" else "image_url"] = safe_url
            if alt:
                # alt text is image/video only; on a text post it does not apply.
                data["alt_text"] = alt[:ALT_TEXT_MAX]
        # Text-only posts may skip the second call entirely.
        if media_type == "TEXT" and (meta.extra or {}).get("auto_publish_text"):
            data["auto_publish_text"] = "true"

        container_id = self._create_container(
            account, media_type=media_type, data=data)
        settle = float((meta.extra or {}).get("settle_seconds", SETTLE_SECONDS))
        attempts = int((meta.extra or {}).get("poll_attempts", POLL_MAX_ATTEMPTS))
        self._await_ready(account, container_id, settle=settle,
                          attempts=attempts)
        media_id = self._publish_container(account, container_id)
        permalink = ""
        try:
            permalink = str(self._verify(account, media_id).get("permalink") or "")
        except Exception:  # noqa: BLE001 - a missing permalink is not a failure
            permalink = ""
        return PublishResult(
            success=True, remote_post_id=media_id, remote_url=permalink,
            error="" if not clipped else
            f"text clipped to {TEXT_MAX_CHARS} characters")

    def publish_carousel(self, meta: PublishMetadata, account: dict,
                         media_urls: list[str]) -> PublishResult:
        """Publish a carousel: N children, then one CAROUSEL container.

        Documented flow: create each child with ``is_carousel_item=true``, then a
        parent with ``media_type=CAROUSEL`` and a comma-separated ``children``
        list. 2..20 items, IMAGE or VIDEO only, counted as ONE post.
        """
        self._require(account, "access_token")
        self.check_scopes(account, need=PERM_PUBLISH)
        urls = [_public_media_url(u) for u in (media_urls or []) if str(u).strip()]
        if not (CAROUSEL_MIN <= len(urls) <= CAROUSEL_MAX):
            return PublishResult(
                success=False, retryable=False,
                error=(f"carousel needs {CAROUSEL_MIN}..{CAROUSEL_MAX} items, "
                       f"got {len(urls)}"))
        text, clipped = _clip_text(
            f"{meta.title}\n\n{meta.description}".strip() if meta.description
            else meta.title)
        alt = str((meta.extra or {}).get("alt_text") or "").strip()

        child_ids: list[str] = []
        for url in urls:
            is_video = url.lower().split("?")[0].endswith((".mp4", ".mov"))
            child = self._create_container(
                account,
                media_type="VIDEO" if is_video else "IMAGE",
                data={**{("video_url" if is_video else "image_url"): url},
                      "is_carousel_item": "true",
                      **({"alt_text": alt[:ALT_TEXT_MAX]} if alt else {})})
            child_ids.append(child)

        parent = self._create_container(
            account, media_type="CAROUSEL",
            data={"children": ",".join(child_ids), "text": text})
        settle = float((meta.extra or {}).get("settle_seconds", SETTLE_SECONDS))
        attempts = int((meta.extra or {}).get("poll_attempts", POLL_MAX_ATTEMPTS))
        self._await_ready(account, parent, settle=settle, attempts=attempts)
        media_id = self._publish_container(account, parent)
        return PublishResult(
            success=True, remote_post_id=media_id,
            error="" if not clipped else f"text clipped to {TEXT_MAX_CHARS} characters")

    def reply(self, *, parent_post_id: str, text: str,
              account: dict) -> PublishResult:
        """Reply to a post: SAME two endpoints, plus ``reply_to_id``.

        Replying to someone else's post additionally needs threads_keyword_search
        or threads_manage_mentions (official rule), so that case is refused
        here instead of 403ing later.
        """
        self._require(account, "access_token")
        self.check_scopes(account, need=PERM_MANAGE_REPLIES)
        granted = {str(s) for s in (account or {}).get("scopes") or []}
        owns_parent = bool((account or {}).get("owns_root_post"))
        if granted and not owns_parent and not (
                granted & set(THIRD_PARTY_REPLY_PERMS)):
            raise ThreadsError(
                f"replying to a third-party post needs one of "
                f"{list(THIRD_PARTY_REPLY_PERMS)} (granted={sorted(granted)}); "
                f"official docs allow replies only to your own root posts "
                f"without them")
        clean, clipped = _clip_text(text)
        container_id = self._create_container(
            account, media_type="TEXT",
            data={"text": clean, "reply_to_id": parent_post_id})
        media_id = self._publish_container(account, container_id)
        return PublishResult(
            success=True, remote_post_id=media_id,
            error="" if not clipped else f"text clipped to {TEXT_MAX_CHARS} characters")

    def list_replies(self, *, post_id: str, account: dict,
                     limit: int = 25, before: str = "",
                     after: str = "") -> dict:
        """GET /{media-id}/replies (depth 1; chain via has_replies).

        ``before``/``after`` are the documented, mutually-exclusive cursors.
        """
        self._require(account, "access_token")
        self.check_scopes(account, need=PERM_READ_REPLIES)
        params = self._params(account, {"limit": max(1, min(int(limit), 100))})
        if before:
            params["before"] = before
        elif after:  # documented as mutually exclusive
            params["after"] = after
        with self._session() as http:
            response = http.get(f"{API_BASE}/{post_id}/replies", params=params)
        body = _json(response)
        paging = body.get("paging") or {}
        return {
            "replies": body.get("data") or [],
            "before": str(paging.get("before") or ""),
            "after": str(paging.get("after") or ""),
            "exhausted": not (paging.get("before") or paging.get("after")),
        }

    def insights(self, *, media_id: str, account: dict,
                 metrics: tuple[str, ...] = ("likes", "replies", "reposts",
                                              "quotes")) -> dict:
        """GET /{media-id}/insights with officially GA metrics only.

        ``impressions`` is NOT a queryable Threads metric and ``clicks`` is
        user-level only, so neither is offered here; asking for one is refused
        rather than silently dropped.
        """
        self._require(account, "access_token")
        self.check_scopes(account, need=PERM_INSIGHTS)
        wanted = [m for m in metrics if m not in ("impressions", "clicks")]
        refused = [m for m in metrics if m in ("impressions", "clicks")]
        if refused:
            raise ThreadsError(
                f"{refused} are not available as Threads media insights "
                f"(impressions is not a queryable metric; clicks is "
                f"user-level only)")
        if not wanted:
            return {"metrics": []}
        with self._session() as http:
            response = http.get(
                f"{API_BASE}/{media_id}/insights",
                params=self._params(account, {"metric": ",".join(wanted)}))
        body = _json(response)
        return {"metrics": body.get("data") or []}


def _json(response: httpx.Response) -> dict:
    """Parse a Threads response, surfacing Meta's error shape on failure."""
    if response.status_code >= 400:
        detail = response.text[:400]
        try:
            body = response.json()
            err = (body.get("error") or {})
            raise ThreadsError(
                f"HTTP {response.status_code} {err.get('type', '')}: "
                f"{err.get('message', detail)}",
                # 5xx and 429 are worth retrying; 4xx is a real rejection.
                retryable=response.status_code >= 500
                or response.status_code == 429)
        except ValueError:
            raise ThreadsError(f"HTTP {response.status_code}: {detail}",
                               retryable=response.status_code >= 500) from None
    try:
        return response.json()
    except ValueError:
        return {}
