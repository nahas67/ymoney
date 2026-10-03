"""Bluesky publisher (Work 14 §4) -- the OFFICIAL AT Protocol.

Contract verified against the machine-readable lexicon JSON in
``github.com/bluesky-social/atproto`` and the Bluesky Protocol Services docs at
``bsky.network/docs``. The load-bearing facts:

* A post is a RECORD, not a procedure: ``app.bsky.feed.post`` written with
  ``com.atproto.repo.createRecord``. There is no ``app.bsky.feed.createPost``.
* ``createRecord`` returns ``uri`` AND ``cid``; BOTH are persisted. Every
  reference to a record (reply, quote, repost) is a ``strongRef`` requiring
  both, so a fabricated CID would corrupt the thread.
* Text: ``maxLength`` 3000 bytes AND ``maxGraphemes`` 300. ``tags`` <= 8.
* Media is a two-step blob flow: ``com.atproto.repo.uploadBlob`` returns a blob
  ref ``{"$type":"blob","ref":{"$link":<cid>},"mimeType":...,"size":...}`` that
  is passed into the record VERBATIM.
* ``app.bsky.embed.images#image`` REQUIRES ``alt`` (it may be empty). This is
  the most commonly missed contract, so it is enforced here.
* VIDEO IS SUPPORTED -- ``app.bsky.embed.video`` is a member of the
  ``app.bsky.feed.post`` embed union, uploaded via ``app.bsky.video.uploadVideo``
  on ``video.bsky.app``. It is emphatically NOT "not available".
* ``app.bsky.feed.getReplies`` DOES NOT EXIST. Replies are read with
  ``getPostThread(depth=0..1000)``, which has NO cursor, so the reply tree is
  walked client-side.
* ``app.bsky.embed.external`` REQUIRES ``uri`` + ``title`` + ``description``.
  There is NO server-side unfurl: the client must fetch and embed the card.

Auth: an app password + ``com.atproto.server.createSession``. Client-credentials
is NOT supported -- the atproto OAuth spec only allows the authorization_code
grant.
"""

from __future__ import annotations

import json
import mimetypes
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from app.providers.publishers.base import (
    BasePublisher,
    PublishMetadata,
    PublishResult,
)

#: Where XRPC calls go. This is the ACCOUNT'S PDS, not public.api.bsky.app --
#: the AppView endpoints do not support authentication.
DEFAULT_PDS = "https://bsky.social"
VIDEO_SERVICE = "https://video.bsky.app"

COLLECTION = "app.bsky.feed.post"

#: Lexicon limits (lexicons/app/bsky/feed/post.json).
TEXT_MAX_BYTES = 3000
TEXT_MAX_GRAPHEMES = 300
TAGS_MAX = 8
TAG_MAX_BYTES = 640
#: app.bsky.embed.images: maxLength 4, image/*, maxSize 2_000_000.
IMAGES_MAX = 4
IMAGE_MAX_BYTES = 2_000_000
#: app.bsky.embed.external#external.thumb maxSize 1_000_000.
EXTERNAL_THUMB_MAX_BYTES = 1_000_000
#: app.bsky.embed.video: video/mp4, maxSize 300_000_000.
VIDEO_MAX_BYTES = 300_000_000
#: Documented PDS blob ceiling (bsky.network/docs/rate-limits): 52_428_800.
PDS_BLOB_MAX_BYTES = 52_428_800
#: Documented rate budget: 5_000 points/hour, create costs 3 points.
WRITE_POINTS_PER_HOUR = 5000
CREATE_POINTS = 3
MAX_CREATES_PER_HOUR = WRITE_POINTS_PER_HOUR // CREATE_POINTS
#: Documented session limits: 30 per 5 minutes, 300 per day.
SESSION_PER_5MIN = 30
SESSION_PER_DAY = 300

#: alt is required on images. The lexicon sets NO character cap -- the 1000/2000
#: figures in circulation are a client UI constant, not an API contract, so
#: there is deliberately no ALT_MAX here.
ALT_REQUIRED = True


class BlueskyError(RuntimeError):
    """AT Protocol failure. ``retryable`` drives scheduler backoff."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


def _grapheme_len(text: str) -> int:
    """Length in grapheme clusters.

    ``len()`` counts code points, so a single emoji with a ZWJ sequence counts
    as several and would falsely trip the 300-grapheme cap. ``regex`` provides
    \\X when available; the code-point fallback only ever over-counts, which
    fails safe (clips early) rather than publishing an over-long post.
    """
    try:
        import regex  # type: ignore

        return len(regex.findall(r"\X", text))
    except Exception:  # noqa: BLE001 - optional dependency
        return len(text)


def clip_text(text: str) -> tuple[str, bool]:
    """Clip to BOTH documented caps. Returns (text, was_clipped)."""
    clean = str(text or "")
    clipped = False
    if len(clean.encode("utf-8")) > TEXT_MAX_BYTES:
        clean = clean.encode("utf-8")[:TEXT_MAX_BYTES].decode("utf-8", "ignore")
        clipped = True
    if _grapheme_len(clean) > TEXT_MAX_GRAPHEMES:
        try:
            import regex  # type: ignore

            clean = regex.sub(r"\X", "", clean, count=(
                _grapheme_len(clean) - TEXT_MAX_GRAPHEMES))
        except Exception:  # noqa: BLE001
            clean = clean[:TEXT_MAX_GRAPHEMES]
        clipped = True
    return clean.strip(), clipped


class BlueskyPublisher(BasePublisher):
    """Official AT Protocol publisher."""

    platform = "bluesky"

    def __init__(self, client: httpx.Client | None = None,
                 pds: str = DEFAULT_PDS) -> None:
        self._client = client
        self.pds = pds.rstrip("/")

    # -- plumbing ---------------------------------------------------------

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=60.0)

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        """Use the injected client, or a private one we own and close.

        An INJECTED client belongs to the caller: closing it would break a
        shared, pooled client mid-publish ("Cannot reopen a client instance,
        once it has been closed"). Only a client this provider created is
        closed here.
        """
        if self._client is not None:
            yield self._client
            return
        client = httpx.Client(timeout=60.0)
        try:
            yield client
        finally:
            client.close()

    def _xrpc(self, method: str, nsid: str, *, account: dict,
              json_body: dict | None = None, raw: bytes | None = None,
              query: dict | None = None) -> dict:
        """One XRPC call against the account's PDS."""
        token = str((account or {}).get("access_jwt") or
                    (account or {}).get("access_token") or "")
        if not token:
            raise BlueskyError(
                "Bluesky account has no access token; connect with an app "
                "password (createSession) -- client-credentials is not "
                "supported by the AT Protocol")
        url = f"{self.pds}/xrpc/{nsid}"
        headers = {"Authorization": f"Bearer {token}"}
        kwargs: dict[str, Any] = {"headers": headers}
        if raw is not None:
            kwargs["content"] = raw
            headers["Content-Type"] = (mimetypes.guess_type("x.bin")[0]
                                       or "application/octet-stream")
        elif json_body is not None:
            kwargs["json"] = json_body
        if query:
            kwargs["params"] = query
        with self._session() as http:
            response = http.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise _xrpc_error(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- session ----------------------------------------------------------

    def create_session(self, *, identifier: str, app_password: str,
                       client: httpx.Client | None = None) -> dict:
        """``com.atproto.server.createSession`` with an APP PASSWORD.

        Official guidance is explicit: use an app password, not the account's
        main password, because an app password is revocable and does not grant
        account-management rights.

        Uses the injected client when one was given (and the ``client=``
        override otherwise), so a test or a pooled caller is never bypassed.
        """
        if not identifier or not app_password:
            raise BlueskyError("identifier and app_password are required")
        if self._client is not None:
            http = self._client
        elif client is not None:
            http = client
        else:
            http = httpx.Client(timeout=30.0)
        owned = http is not self._client and client is None
        try:
            response = http.post(
                f"{self.pds}/xrpc/com.atproto.server.createSession",
                json={"identifier": identifier, "password": app_password})
        finally:
            if owned:
                http.close()
        if response.status_code >= 400:
            raise _xrpc_error(response)
        body = response.json() or {}
        if not body.get("accessJwt"):
            raise BlueskyError("createSession returned no accessJwt")
        return body

    def refresh_session(self, *, account: dict) -> dict:
        """``refreshSession``. Access JWTs live only minutes, so this is
        proactive, never reactive -- a publish must not fail on a stale token."""
        return self._xrpc("POST", "com.atproto.server.refreshSession",
                          account=account)

    def get_session(self, *, account: dict) -> dict:
        """``getSession`` -- the documented way to check whether the account's
        email is verified, which Bluesky requires before video upload."""
        return self._xrpc("GET", "com.atproto.server.getSession", account=account)

    # -- blobs ------------------------------------------------------------

    def upload_blob(self, *, path: str, account: dict,
                    max_bytes: int = IMAGE_MAX_BYTES) -> dict:
        """``com.atproto.repo.uploadBlob``; returns the blob ref VERBATIM.

        The returned object is embedded into the record as-is. Rebuilding it
        locally would break the CID/mimeType/size agreement the PDS expects.
        """
        data = Path(path).read_bytes()
        if len(data) > max_bytes:
            raise BlueskyError(
                f"blob is {len(data)} bytes; this embed caps at {max_bytes}")
        if len(data) > PDS_BLOB_MAX_BYTES:
            raise BlueskyError(
                f"blob is {len(data)} bytes; the PDS caps single uploads at "
                f"{PDS_BLOB_MAX_BYTES} (bsky.network/docs/rate-limits)")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        headers_token = str((account or {}).get("access_jwt") or "")
        if not headers_token:
            raise BlueskyError("Bluesky account has no access_jwt")
        with self._session() as http:
            response = http.post(
                f"{self.pds}/xrpc/com.atproto.repo.uploadBlob",
                headers={"Authorization": f"Bearer {headers_token}",
                         "Content-Type": mime},
                content=data)
        if response.status_code >= 400:
            raise _xrpc_error(response)
        blob = (response.json() or {}).get("blob")
        if not isinstance(blob, dict) or not (blob.get("ref") or {}).get("$link"):
            raise BlueskyError("uploadBlob returned no usable blob ref")
        return blob

    # -- records ----------------------------------------------------------

    def _create_record(self, *, record: dict, account: dict,
                       rkey: str = "") -> dict:
        # The token is checked first: "no credential" is a more accurate and
        # more actionable error than a downstream missing-did complaint.
        if not str((account or {}).get("access_jwt") or
                   (account or {}).get("access_token") or ""):
            raise BlueskyError(
                "Bluesky account has no access token; connect with an app "
                "password (createSession) -- client-credentials is not "
                "supported by the AT Protocol")
        body: dict[str, Any] = {
            "repo": str((account or {}).get("did") or
                        (account or {}).get("handle") or ""),
            "collection": COLLECTION,
            "record": record,
        }
        if not body["repo"]:
            raise BlueskyError(
                "Bluesky account has no did/handle; the repo is required by "
                "com.atproto.repo.createRecord")
        if rkey:
            body["rkey"] = rkey
        out = self._xrpc("POST", "com.atproto.repo.createRecord",
                         account=account, json_body=body)
        # uri AND cid are both required by every strongRef, so both are kept.
        if not (out.get("uri") and out.get("cid")):
            raise BlueskyError(
                f"createRecord must return uri and cid, got {list(out)}")
        return {"uri": str(out["uri"]), "cid": str(out["cid"])}

    def _now(self) -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _text_record(self, meta: PublishMetadata, *,
                     embed: dict | None = None,
                     reply: dict | None = None) -> dict:
        text, _ = clip_text(
            f"{meta.title}\n\n{meta.description}".strip() if meta.description
            else meta.title)
        tags = [str(t).lstrip("#") for t in (meta.hashtags or [])][:TAGS_MAX]
        record: dict[str, Any] = {"$type": COLLECTION, "text": text,
                                  "createdAt": self._now()}
        if tags:
            record["tags"] = [t[:TAG_MAX_BYTES] for t in tags]
        if embed:
            record["embed"] = embed
        if reply:
            record["reply"] = reply
        return record

    # -- public API -------------------------------------------------------

    def publish(self, video_path: str, meta: PublishMetadata,
                account: dict) -> PublishResult:
        """Publish a post. VIDEO IS SUPPORTED here, not declined."""
        embed: dict | None = None
        warnings: list[str] = []
        kind = str((meta.extra or {}).get("media_kind") or "text").lower()
        alt = str((meta.extra or {}).get("alt_text") or "").strip()

        if kind in ("image", "images") and video_path:
            paths = [video_path] if isinstance(video_path, str) else list(video_path)
            if len(paths) > IMAGES_MAX:
                return PublishResult(
                    success=False, retryable=False,
                    error=(f"app.bsky.embed.images takes at most {IMAGES_MAX} "
                           f"images, got {len(paths)}"))
            images = []
            for item in paths:
                blob = self.upload_blob(path=item, account=account)
                if ALT_REQUIRED and not alt:
                    # `alt` is REQUIRED by the lexicon (may be an empty
                    # string). Sending an image without it is a hard reject.
                    warnings.append("alt text empty: the lexicon requires the "
                                    "alt field, so an empty string was sent")
                images.append({"alt": alt, "image": blob})
            embed = {"$type": "app.bsky.embed.images", "images": images}
        elif kind == "video" and video_path:
            embed = self._video_embed(path=video_path, account=account, alt=alt)
        elif kind == "link":
            embed = self._external_embed(meta, account)

        out = self._create_record(record=self._text_record(
            meta, embed=embed), account=account)
        return PublishResult(
            success=True, remote_post_id=out["uri"], remote_url=permalink(out["uri"]),
            error="; ".join(warnings))

    def _video_embed(self, *, path: str, account: dict, alt: str) -> dict:
        """``app.bsky.embed.video`` via the video service.

        Official flow: get a service token -> ``app.bsky.video.uploadVideo`` on
        video.bsky.app -> poll ``getJobStatus`` until a blob ref appears -> embed
        it. The simple ``uploadBlob`` path is NOT used for video because the
        PDS caps single uploads at 50 MB, below the video lexicon ceiling.
        """
        size = Path(path).stat().st_size
        if size > VIDEO_MAX_BYTES:
            raise BlueskyError(
                f"video is {size} bytes; app.bsky.embed.video caps at "
                f"{VIDEO_MAX_BYTES}")
        limits = self._video_limits(account)
        if limits.get("canUpload") is False:
            raise BlueskyError(
                f"video upload refused by the service: {limits.get('error')}")
        with self._session() as http:
            response = http.post(
                f"{VIDEO_SERVICE}/xrpc/app.bsky.video.uploadVideo",
                headers={"Authorization": f"Bearer {str((account or {}).get('access_jwt') or '')}",
                         "Content-Type": "video/mp4"},
                content=Path(path).read_bytes())
        if response.status_code >= 400:
            raise _xrpc_error(response)
        body = response.json() or {}
        job = self._await_video_job(account, str(body.get("jobId") or ""))
        embed: dict[str, Any] = {"$type": "app.bsky.embed.video", "video": job}
        if alt:
            embed["alt"] = alt
        return embed

    def _video_limits(self, account: dict) -> dict:
        """``app.bsky.video.getUploadLimits``.

        The numeric daily caps are NOT published in official docs, so they are
        read at runtime rather than hardcoded. A failure is advisory.
        """
        try:
            token = str((account or {}).get("access_jwt") or "")
            with self._session() as http:
                response = http.get(
                    f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getUploadLimits",
                    headers={"Authorization": f"Bearer {token}"})
            if response.status_code < 400:
                return response.json() or {}
            return {"error": f"HTTP {response.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}

    def _await_video_job(self, account: dict, job_id: str, *,
                         attempts: int = 10,
                         interval: float = 5.0) -> dict:
        """Poll ``getJobStatus`` until a blob ref appears.

        Official gotcha this handles: when the video was already processed the
        service returns an ``already_exists`` ERROR that STILL carries a valid
        blob ref. The PDS therefore answers with a 4xx whose body is useful --
        so the blob is read off the error body too, instead of the error being
        raised and the already-uploaded video wasted.
        """
        if not job_id:
            raise BlueskyError("uploadVideo returned no jobId")
        for attempt in range(max(1, attempts)):
            try:
                body = self._xrpc("GET", "app.bsky.video.getJobStatus",
                                  account=account, query={"jobId": job_id})
            except BlueskyError as exc:
                # An error that carries a blob ref means the job is DONE.
                blob = _blob_in(str(exc))
                if blob:
                    return blob
                raise
            state = (body.get("state") or body.get("jobStatus") or {})
            blob = (state.get("blob") if isinstance(state, dict) else None) or body.get("blob")
            if isinstance(blob, dict) and (blob.get("ref") or {}).get("$link"):
                return blob
            if attempt < attempts - 1 and interval > 0:
                time.sleep(interval)
        raise BlueskyError(
            f"video job {job_id} produced no blob after {attempts} polls",
            retryable=True)

    def _external_embed(self, meta: PublishMetadata, account: dict) -> dict:
        """``app.bsky.embed.external``.

        ``uri`` + ``title`` + ``description`` are ALL mandatory. There is no
        server-side unfurl, so the card must be embedded by the client; the
        thumb blob (optional, <=1MB) is uploaded from meta.extra when supplied.
        """
        extra = meta.extra or {}
        uri = str(extra.get("link_url") or "").strip()
        if not uri:
            raise BlueskyError(
                "a link embed needs meta.extra['link_url']; Bluesky does not "
                "unfurl links server-side")
        external: dict[str, Any] = {
            "uri": uri,
            "title": str(extra.get("link_title") or meta.title or uri)[:200],
            "description": str(extra.get("link_description")
                               or meta.description or "")[:1000],
        }
        thumb_path = str(extra.get("link_thumb_path") or "").strip()
        if thumb_path:
            external["thumb"] = self.upload_blob(
                path=thumb_path, account=account,
                max_bytes=EXTERNAL_THUMB_MAX_BYTES)
        return {"$type": "app.bsky.embed.external", "external": external}

    def reply(self, *, parent_uri: str, parent_cid: str, root_uri: str,
              root_cid: str, text: str, account: dict) -> PublishResult:
        """Reply to a post.

        ``replyRef`` requires BOTH ``root`` and ``parent``, and each strongRef
        requires ``uri`` AND ``cid``. A reply to the first level has identical
        root and parent; a deeper reply must carry the real thread root.
        """
        for name, value in (("parent_uri", parent_uri), ("parent_cid", parent_cid),
                            ("root_uri", root_uri), ("root_cid", root_cid)):
            if not value:
                raise BlueskyError(f"{name} is required: strongRefs need uri AND cid")
        record = self._text_record(
            PublishMetadata(title=text), reply={
                "root": {"uri": root_uri, "cid": root_cid},
                "parent": {"uri": parent_uri, "cid": parent_cid},
            })
        out = self._create_record(record=record, account=account)
        return PublishResult(success=True, remote_post_id=out["uri"],
                             remote_url=permalink(out["uri"]))

    def quote(self, *, quoted_uri: str, quoted_cid: str,
              meta: PublishMetadata, account: dict) -> PublishResult:
        """Quote/reference a post via ``app.bsky.embed.record`` (a strongRef)."""
        if not (quoted_uri and quoted_cid):
            raise BlueskyError("a quote needs both quoted_uri and quoted_cid")
        record = self._text_record(meta, embed={
            "$type": "app.bsky.embed.record",
            "record": {"uri": quoted_uri, "cid": quoted_cid}})
        out = self._create_record(record=record, account=account)
        return PublishResult(success=True, remote_post_id=out["uri"],
                             remote_url=permalink(out["uri"]))

    def list_replies(self, *, post_uri: str, account: dict,
                     depth: int = 6) -> dict:
        """Read replies with ``getPostThread``.

        ``getReplies`` does not exist. ``getPostThread`` returns a NESTED tree
        with no cursor, so volume is controlled by ``depth`` (0..1000) and the
        tree is flattened here. Pagination therefore has no next-cursor: the
        walk is the pagination.
        """
        body = self._xrpc("GET", "app.bsky.feed.getPostThread", account=account,
                          query={"uri": post_uri,
                                 "depth": max(0, min(int(depth), 1000))})
        collected: list[dict] = []

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            if node.get("$type") == "app.bsky.feed.defs#notFoundPost":
                return
            if node.get("$type") == "app.bsky.feed.defs#blockedPost":
                collected.append({"blocked": True})
                return
            post = (node.get("post") or {})
            if post:
                author = post.get("author") or {}
                collected.append({
                    "uri": post.get("uri"), "cid": post.get("cid"),
                    "text": post.get("record", {}).get("text", ""),
                    "created_at": post.get("record", {}).get("createdAt"),
                    "author_handle": author.get("handle"),
                    "author_did": author.get("did"),
                })
            for child in (node.get("replies") or []):
                walk(child)

        walk(body.get("thread"))
        return {"replies": collected, "cursor": "",
                "exhausted": True,
                "note": ("getPostThread has no cursor; the reply tree is walked "
                         "client-side and volume is bounded by depth")}

    def search(self, *, query: str, account: dict, limit: int = 25,
               cursor: str = "") -> dict:
        """``app.bsky.feed.searchPosts`` (V1 -- V2 network support is unverified)."""
        params: dict[str, Any] = {"q": query, "limit": max(1, min(int(limit), 100))}
        if cursor:
            params["cursor"] = cursor
        body = self._xrpc("GET", "app.bsky.feed.searchPosts", account=account,
                          query=params)
        next_cursor = str(body.get("cursor") or "")
        return {"posts": body.get("posts") or [], "cursor": next_cursor,
                "exhausted": not next_cursor}

    def delete_post(self, *, post_uri: str, account: dict) -> dict:
        """``com.atproto.repo.deleteRecord`` with (repo, collection, rkey).

        The official description is "Delete a repository record, or ensure it
        doesn't exist", i.e. it is IDEMPOTENT by design -- so a retried delete
        succeeds instead of erroring.
        """
        repo, _, rkey = str(post_uri).partition("/app.bsky.feed.post/")
        # The at-uri form is at://<did>/<nsid>/<rkey>; `repo` must be the bare
        # DID/handle, so the scheme is stripped.
        if repo.startswith("at://"):
            repo = repo[len("at://"):]
        if not (repo and rkey):
            raise BlueskyError(
                f"cannot parse an at-uri into repo/rkey: {post_uri!r}")
        return self._xrpc("POST", "com.atproto.repo.deleteRecord",
                          account=account,
                          json_body={"repo": repo, "collection": COLLECTION,
                                     "rkey": rkey})


def _blob_in(message: str) -> dict | None:
    """Recover a blob ref from an error MESSAGE, if the body carried one.

    ``app.bsky.video.getJobStatus`` answers a duplicate job with an
    ``already_exists`` error whose body still contains the BlobRef. The XRPC
    error string embeds that body, so the ref is parsed back out of it rather
    than losing an already-uploaded video.
    """
    marker = '"$type"'
    if marker not in message:
        return None
    start = message.rfind("{", 0, message.index(marker))
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(message)):
        char = message[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(message[start:index + 1])
                except ValueError:
                    return None
                if (candidate.get("ref") or {}).get("$link"):
                    return candidate
                return None
    return None


def permalink(at_uri: str) -> str:
    """Human-facing URL for an ``at://`` URI (best effort, never required)."""
    raw = str(at_uri or "")
    if not raw.startswith("at://"):
        return raw
    rest = raw[len("at://"):]
    did, _, tail = rest.partition("/")
    rkey = tail.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def _xrpc_error(response: httpx.Response) -> BlueskyError:
    """Translate an XRPC error, mapping the documented status codes.

    atproto.xrpc specifies 429 (back off, honour Retry-After) and 501 (do not
    retry); 5xx is worth retrying, other 4xx are real rejections.
    """
    status = response.status_code
    try:
        body = response.json() or {}
        message = str(body.get("message") or body.get("error") or response.text)
        error = str(body.get("error") or "")
    except ValueError:
        body, message, error = {}, response.text[:300], ""
    if status == 429:
        retry_after = response.headers.get("Retry-After", "")
        return BlueskyError(
            f"rate limited (429) {error} {message}; Retry-After={retry_after!r} "
            f"-- back off and retry", retryable=True)
    if status == 501:
        return BlueskyError(
            f"not implemented by this PDS (501): {message}", retryable=False)
    if status == 401:
        return BlueskyError(
            f"unauthorized (401) {error}: {message}; the accessJwt expires "
            f"within minutes -- refresh the session", retryable=True)
    # Some documented error responses (notably a duplicate video job) still
    # carry a usable BlobRef in the body. Keep the raw body in the message so
    # the caller can recover it instead of discarding completed work.
    suffix = ""
    if "$type" in (body_text := (response.text or "")) and "blob" in body_text:
        suffix = f" body={body_text[:400]}"
    return BlueskyError(
        f"XRPC HTTP {status} {error}: {message}{suffix}".strip(),
        retryable=status >= 500)


def _dumps(value: Any) -> str:  # pragma: no cover - debugging aid
    return json.dumps(value, indent=2, sort_keys=True, default=str)
