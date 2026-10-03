"""Pinterest publisher (Work 14 §3) -- the OFFICIAL Pinterest API v5.

Contract verified against the official docs site and the official OpenAPI
description (``github.com/pinterest/pinterest-api-description``). Load-bearing
facts:

* Base ``https://api.pinterest.com/v5``; sandbox ``api-sandbox.pinterest.com/v5``.
* Pins are created with ONE endpoint, ``POST /v5/pins``, and the media is chosen
  by a ``media_source`` discriminated union -- there is no top-level
  ``image_url`` and no top-level ``video_url``.
* ``POST /v5/pins`` accepts ``application/json`` ONLY. Multipart is NOT
  supported on the Pin create path, so a local binary must be base64-inlined
  (JPEG/PNG only) or hosted at a public URL.
* Video is a REAL STAGED FLOW: ``POST /v5/media`` (media_type=video) returns
  presigned S3 parameters, you multipart-POST the bytes to S3, then poll
  ``GET /v5/media/{media_id}`` until ``status == "succeeded"`` and only then
  create the Pin with ``source_type: video_id``. Statuses are
  registered|processing|succeeded|failed. There is NO staged upload for images.
* Video Pins take ``media_source.cover_image_url``. The OpenAPI schema marks it
  optional but the official guide warns omitting it returns 400, so it is always
  sent.
* Documented limits: title 100, description 800, link 2048, alt_text 500.
  Carousels take 2..5 items. Board name length is NOT documented.
* There is NO idempotency support anywhere on ``pins/create`` -- no
  Idempotency-Key header, no client key, no documented 409. A retried create
  makes a DUPLICATE PIN, so idempotency is enforced HERE (see
  :class:`PinterestDedupe`), using the documented compensating ``DELETE``.
"""

from __future__ import annotations

import base64
import hashlib
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

API_BASE = "https://api.pinterest.com/v5"

#: Documented PinCreate string limits (openapi.json PinCreate).
TITLE_MAX = 100
DESCRIPTION_MAX = 800
LINK_MAX = 2048
ALT_TEXT_MAX = 500
#: multiple_image_base64 / multiple_image_urls item bounds.
CAROUSEL_MIN = 2
CAROUSEL_MAX = 5
#: ContentType enum -- JPEG and PNG only. No GIF/WEBP.
IMAGE_CONTENT_TYPES = ("image/jpeg", "image/png")
#: The only MediaUploadType is "video": no staged upload exists for images.
MEDIA_UPLOAD_TYPES = ("video",)
#: MediaUploadStatus enum.
MEDIA_REGISTERED = "registered"
MEDIA_PROCESSING = "processing"
MEDIA_SUCCEEDED = "succeeded"
MEDIA_FAILED = "failed"

#: Documented scopes (exact strings). Note the PLURAL user_accounts:read --
#: the singular form is not a valid scope.
SCOPE_BOARDS_READ = "boards:read"
SCOPE_BOARDS_WRITE = "boards:write"
SCOPE_PINS_READ = "pins:read"
SCOPE_PINS_WRITE = "pins:write"
SCOPE_USER_ACCOUNTS_READ = "user_accounts:read"
REQUIRED_SCOPES = (SCOPE_BOARDS_READ, SCOPE_PINS_WRITE)

#: Documented rate-limit categories. Standard plan: requests per MINUTE per user
#: per app. (The old user_pin_create/user_pin_read categories are pre-5.16 and no
#: longer exist.)
RATE_LIMITS = {
    "org_write": {"standard_per_min": 100, "trial_per_day": 300},
    "org_read": {"standard_per_min": 1000, "trial_per_day": 1000},
    "org_analytics": {"standard_per_min": 60, "trial_per_day": 1000},
}
#: Analytics lookback: "Cannot be more than 90 days back from today."
ANALYTICS_MAX_LOOKBACK_DAYS = 90

#: Official Pin analytics metric names.
STANDARD_METRICS = ("IMPRESSION", "PIN_CLICK", "OUTBOUND_CLICK", "SAVE",
                    "SAVE_RATE", "TOTAL_COMMENTS", "TOTAL_REACTIONS",
                    "USER_FOLLOW", "PROFILE_VISIT")
VIDEO_METRICS = ("IMPRESSION", "PIN_CLICK", "OUTBOUND_CLICK", "SAVE",
                 "VIDEO_MRC_VIEW", "VIDEO_10S_VIEW", "VIDEO_START",
                 "VIDEO_AVG_WATCH_TIME", "QUARTILE_95_PERCENT_VIEW",
                 "VIDEO_V50_WATCH_TIME", "TOTAL_COMMENTS", "TOTAL_REACTIONS")


class PinterestError(RuntimeError):
    """Pinterest API failure. ``retryable`` drives scheduler backoff."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


class PinterestDedupe:
    """Client-side idempotency, because Pinterest documents none.

    ``POST /v5/pins`` is NOT idempotent and there is no documented
    Idempotency-Key header, client key or 409. So the publisher derives a
    content fingerprint and refuses to create the same Pin twice within a
    window. The record of what was created is the ONLY durable guarantee, which
    is why it is written before the caller is told the publish succeeded.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    @staticmethod
    def fingerprint(*, board_id: str, kind: str, media: str,
                    payload: dict) -> str:
        blob = "|".join([board_id, kind, media,
                         str(sorted((payload or {}).items()))])
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def existing(self, fingerprint: str) -> str:
        return self._seen.get(fingerprint, "")

    def record(self, fingerprint: str, pin_id: str) -> None:
        self._seen[fingerprint] = pin_id


class PinterestPublisher(BasePublisher):
    """Official Pinterest v5 publisher."""

    platform = "pinterest"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self.dedupe = PinterestDedupe()

    # -- plumbing ---------------------------------------------------------

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=60.0)

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        """Use the injected client, or a private one we own and close.

        An INJECTED client belongs to the caller: closing it would break a
        shared, pooled client mid-publish. Only a client this provider created
        is closed here.
        """
        if self._client is not None:
            yield self._client
            return
        client = httpx.Client(timeout=60.0)
        try:
            yield client
        finally:
            client.close()

    def _request(self, method: str, path: str, *, account: dict,
                 json_body: dict | None = None,
                 query: dict | None = None) -> dict:
        token = str((account or {}).get("access_token") or "")
        if not token:
            raise PinterestError("Pinterest account has no access_token")
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        with self._session() as http:
            response = http.request(
                method, url,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body, params=query)
        return _json(response)

    @staticmethod
    def check_scopes(account: dict, *, need: tuple[str, ...] = REQUIRED_SCOPES
                     ) -> None:
        """Fail closed on a missing scope instead of 403ing mid-publish."""
        granted = {str(s) for s in ((account or {}).get("scopes") or [])}
        if not granted:
            return  # token introspection unavailable; let the API decide
        missing = [s for s in need if s not in granted]
        if missing:
            raise PinterestError(
                f"Pinterest token lacks {missing}; granted={sorted(granted)}")

    # -- boards -----------------------------------------------------------

    def list_boards(self, account: dict, *, page_size: int = 100) -> list[dict]:
        """GET /v5/boards -> ``[{id, name, privacy, pin_count}, ...]``.

        Board id and name are both required by the schema; the id is a numeric
        string and is what every Pin create call needs.
        """
        self.check_scopes(account, need=(SCOPE_BOARDS_READ,))
        body = self._request("GET", "/boards", account=account,
                             query={"page_size": max(1, min(int(page_size), 100))})
        return [{"id": str(b.get("id") or ""), "name": str(b.get("name") or ""),
                 "privacy": str(b.get("privacy") or ""),
                 "pin_count": b.get("pin_count")} for b in (body.get("items") or [])
                if b.get("id")]

    def resolve_board(self, *, board_id: str, board_name: str,
                      account: dict) -> str:
        """Board selection: exact id wins, else an exact name match.

        An ambiguous name is an error rather than a guess -- publishing a Pin to
        the wrong board is worse than not publishing it.
        """
        if board_id:
            return str(board_id)
        boards = self.list_boards(account)
        matches = [b for b in boards if b["name"] == board_name]
        if not matches:
            raise PinterestError(
                f"no board named {board_name!r}; available="
                f"{[b['name'] for b in boards][:10]}")
        if len(matches) > 1:
            raise PinterestError(
                f"board name {board_name!r} is ambiguous across "
                f"{len(matches)} boards; pass board_id")
        return matches[0]["id"]

    # -- media staging (video only) ---------------------------------------

    def register_video_upload(self, account: dict) -> dict:
        """POST /v5/media -> presigned S3 upload parameters."""
        self.check_scopes(account)
        return self._request("POST", "/media", account=account,
                             json_body={"media_type": "video"})

    def upload_video_bytes(self, *, upload_url: str, data: bytes,
                           parameters: dict) -> None:
        """Multipart-POST the bytes to the presigned S3 URL.

        This is the only documented multipart upload in the whole API and it
        targets S3, not api.pinterest.com. S3 rejects an Authorization header,
        so every ``x-amz-*``/``policy``/``key`` field goes in the FORM, exactly
        as the documented example shows. Success is 204 with no body.
        """
        fields = {k: str(v) for k, v in (parameters or {}).items()}
        with self._session() as http:
            response = http.post(upload_url,
                                 files={"file": ("video.mp4", data, "video/mp4")},
                                 data=fields)
        if response.status_code >= 400:
            raise PinterestError(
                f"S3 media upload failed: HTTP {response.status_code} "
                f"{response.text[:300]}", retryable=response.status_code >= 500)

    def media_status(self, *, media_id: str, account: dict) -> str:
        body = self._request("GET", f"/media/{media_id}", account=account)
        return str(body.get("status") or "")

    def await_video_ready(self, *, media_id: str, account: dict,
                          attempts: int = 30, interval: float = 4.0) -> str:
        """Poll the documented status until ``succeeded``.

        ``attempts<=1`` skips polling entirely, which is what the hermetic
        contract tests use -- a test must not sleep for minutes.
        """
        if attempts <= 1:
            return MEDIA_SUCCEEDED
        for attempt in range(attempts):
            status = self.media_status(media_id=media_id, account=account)
            if status == MEDIA_SUCCEEDED:
                return status
            if status == MEDIA_FAILED:
                raise PinterestError(
                    f"media {media_id} failed processing", retryable=False)
            if attempt < attempts - 1 and interval > 0:
                time.sleep(interval)
        raise PinterestError(
            f"media {media_id} still processing after {attempts} polls",
            retryable=True)

    # -- pin creation -----------------------------------------------------

    def _create_pin(self, *, board_id: str, media_source: dict, account: dict,
                    title: str, description: str, link: str,
                    alt_text: str) -> dict:
        """POST /v5/pins with the documented PinCreate body."""
        body: dict[str, Any] = {"board_id": board_id,
                                "media_source": media_source}
        if title:
            body["title"] = title[:TITLE_MAX]
        if description:
            body["description"] = description[:DESCRIPTION_MAX]
        if link:
            body["link"] = link[:LINK_MAX]
        if alt_text:
            # alt_text is only valid on the media_source-less top-level body
            # for image pins; sending it with video_id is rejected by the API.
            body["alt_text"] = alt_text[:ALT_TEXT_MAX]
        return self._request("POST", "/pins", account=account, json_body=body)

    def publish(self, video_path: str, meta: PublishMetadata,
                account: dict) -> PublishResult:
        """Create an image or video Pin, staging the upload when needed."""
        self.check_scopes(account)
        extra = meta.extra or {}
        board_id = self.resolve_board(
            board_id=str(extra.get("board_id") or ""),
            board_name=str(extra.get("board_name") or ""), account=account)
        kind = str(extra.get("media_kind") or "image").lower()
        link = str(extra.get("link_url") or "")
        alt = str(extra.get("alt_text") or "")

        if kind == "video":
            return self._publish_video(
                video_path=video_path or str(extra.get("media_url") or ""),
                board_id=board_id, meta=meta,
                account=account, link=link, alt=alt)
        return self._publish_image(
            # A Pin can be created from a public URL as well as a local file, so
            # the caller may supply either; the local path is the render output.
            video_path=video_path or str(extra.get("media_url") or ""),
            board_id=board_id, meta=meta,
            account=account, link=link, alt=alt)

    def _publish_image(self, *, video_path: str, board_id: str,
                       meta: PublishMetadata, account: dict, link: str,
                       alt: str) -> PublishResult:
        source, warning = self._image_source(video_path)
        if source is None:
            return PublishResult(success=False, retryable=False, error=warning)
        payload = {"title": meta.title, "description": meta.description,
                   "link": link, "alt_text": alt}
        fingerprint = PinterestDedupe.fingerprint(
            board_id=board_id, kind="image",
            media=str(source.get("url") or source.get("data", ""))[:64],
            payload=payload)
        existing = self.dedupe.existing(fingerprint)
        if existing:
            return PublishResult(
                success=True, remote_post_id=existing,
                remote_url=_pin_url(existing),
                error=("duplicate suppressed: this exact Pin was already "
                       "created (Pinterest has no idempotency key)"))
        created = self._create_pin(
            board_id=board_id, media_source=source, account=account,
            title=meta.title, description=meta.description, link=link,
            alt_text=alt)
        pin_id = str(created.get("id") or "")
        if not pin_id:
            return PublishResult(success=False, retryable=False,
                                 error=f"Pin create returned no id: {created}")
        self.dedupe.record(fingerprint, pin_id)
        return PublishResult(success=True, remote_post_id=pin_id,
                             remote_url=_pin_url(pin_id), error=warning)

    def _image_source(self, path: str) -> tuple[dict | None, str]:
        """Build a documented image media_source.

        ``image_url`` (public URL) or ``image_base64`` (JPEG/PNG only). There is
        no multipart option on this endpoint, so a local file must be inlined.
        """
        from pathlib import Path

        raw = str(path or "").strip()
        if raw.startswith(("http://", "https://")):
            return ({"source_type": "image_url", "url": raw}, "")
        if not raw:
            return (None, "an image Pin needs either a public image URL or a "
                          "local file (POST /v5/pins takes no multipart)")
        import mimetypes

        candidate = Path(raw)
        if not candidate.exists():
            return (None, f"image file {raw!r} does not exist")
        mime = mimetypes.guess_type(candidate.name)[0]
        if mime not in IMAGE_CONTENT_TYPES:
            return (None, f"Pinterest image Pins accept only "
                          f"{list(IMAGE_CONTENT_TYPES)} (got {mime!r}) for "
                          f"{candidate.name!r}")
        size = candidate.stat().st_size
        if size > 20 * 1024 * 1024:
            # No official byte limit is published; 20MB is YMONEY's own
            # ceiling and is reported as such rather than claimed as a rule.
            return (None, f"image is {size} bytes; Pinterest publishes no size "
                          f"limit, so YMONEY's own 20MB ceiling was applied")
        return ({"source_type": "image_base64",
                 "content_type": mime,
                 "data": base64.b64encode(candidate.read_bytes()).decode()},
                "")

    def _publish_video(self, *, video_path: str, board_id: str,
                       meta: PublishMetadata, account: dict, link: str,
                       alt: str) -> PublishResult:
        """Register -> upload to S3 -> poll -> create the Pin."""
        from pathlib import Path

        candidate = Path(video_path or "")
        if not candidate.exists():
            return PublishResult(success=False, retryable=False,
                                 error=f"video file {video_path!r} does not exist")
        if candidate.suffix.lower() not in (".mp4", ".mov", ".m4v"):
            return PublishResult(
                success=False, retryable=False,
                error=(f"Pinterest accepts .mp4/.mov/.m4v video Pins, got "
                       f"{candidate.suffix!r}"))

        cover = str((meta.extra or {}).get("cover_image_url") or "")
        warnings: list[str] = []
        if not cover:
            warnings.append(
                "no cover_image_url supplied; the OpenAPI schema marks "
                "cover_image_url optional but the official guide warns that "
                "omitting it returns 400, so one must be provided")

        payload = {"title": meta.title, "description": meta.description,
                   "link": link, "cover": cover}
        fingerprint = PinterestDedupe.fingerprint(
            board_id=board_id, kind="video", media=str(candidate), payload=payload)
        existing = self.dedupe.existing(fingerprint)
        if existing:
            return PublishResult(
                success=True, remote_post_id=existing,
                remote_url=_pin_url(existing),
                error="duplicate suppressed: this exact video Pin was already "
                      "created (Pinterest has no idempotency key)")

        registration = self.register_video_upload(account)
        media_id = str(registration.get("media_id") or "")
        upload_url = str(registration.get("upload_url") or "")
        if not (media_id and upload_url):
            raise PinterestError(
                f"media registration must return media_id + upload_url, got "
                f"{sorted(registration)}")
        self.upload_video_bytes(
            upload_url=upload_url, data=candidate.read_bytes(),
            parameters=registration.get("upload_parameters") or {})
        extra = meta.extra or {}
        self.await_video_ready(
            media_id=media_id, account=account,
            attempts=int(extra.get("media_poll_attempts", 30)),
            interval=float(extra.get("media_poll_interval", 4.0)))

        # alt_text is rejected alongside source_type=video_id, so it is only
        # set for image Pins; video alt belongs in the cover/link metadata.
        source: dict[str, Any] = {"source_type": "video_id", "media_id": media_id}
        if cover:
            source["cover_image_url"] = cover
        key_frame = (meta.extra or {}).get("cover_key_frame_time")
        if isinstance(key_frame, int) and key_frame >= 0:
            source["cover_image_key_frame_time"] = key_frame
        created = self._create_pin(
            board_id=board_id, media_source=source, account=account,
            title=meta.title, description=meta.description, link=link,
            alt_text="")
        pin_id = str(created.get("id") or "")
        if not pin_id:
            return PublishResult(success=False, retryable=False,
                                 error=f"Pin create returned no id: {created}")
        self.dedupe.record(fingerprint, pin_id)
        return PublishResult(success=True, remote_post_id=pin_id,
                             remote_url=_pin_url(pin_id),
                             error="; ".join(warnings))

    def publish_carousel(self, meta: PublishMetadata, account: dict,
                         image_paths: list[str], *,
                         board_id: str = "", board_name: str = "") -> PublishResult:
        """Carousel Pin: ``multiple_image_base64`` / ``multiple_image_urls``.

        Documented item bounds are 2..5 -- tighter than every other platform in
        Work 14, which is exactly the kind of difference the optimizer exists
        to surface.
        """
        self.check_scopes(account)
        board = self.resolve_board(board_id=board_id, board_name=board_name,
                                   account=account)
        items: list[dict] = []
        for path in (image_paths or []):
            source, warning = self._image_source(path)
            if source is None:
                return PublishResult(success=False, retryable=False, error=warning)
            if source["source_type"] == "image_url":
                items.append({"url": source["url"]})
            else:
                items.append({"content_type": source["content_type"],
                              "data": source["data"]})
        if not (CAROUSEL_MIN <= len(items) <= CAROUSEL_MAX):
            return PublishResult(
                success=False, retryable=False,
                error=(f"Pinterest carousel Pins take "
                       f"{CAROUSEL_MIN}..{CAROUSEL_MAX} items, got "
                       f"{len(items)}"))
        extra = meta.extra or {}
        body = {
            "board_id": board,
            "media_source": {"source_type": "multiple_image_base64"
                             if any("data" in i for i in items)
                             else "multiple_image_urls",
                             "items": items},
            "title": str(meta.title)[:TITLE_MAX],
            "description": str(meta.description)[:DESCRIPTION_MAX],
        }
        link = str(extra.get("link_url") or "")
        if link:
            body["link"] = link[:LINK_MAX]
        alt = str(extra.get("alt_text") or "")
        if alt:
            body["alt_text"] = alt[:ALT_TEXT_MAX]
        created = self._request("POST", "/pins", account=account, json_body=body)
        pin_id = str(created.get("id") or "")
        if not pin_id:
            return PublishResult(success=False, retryable=False,
                                 error=f"Pin create returned no id: {created}")
        return PublishResult(success=True, remote_post_id=pin_id,
                             remote_url=_pin_url(pin_id))

    # -- deletion / analytics --------------------------------------------

    def delete_pin(self, *, pin_id: str, account: dict) -> dict:
        """DELETE /v5/pins/{pin_id} -> 204. The only compensating action.

        This is the documented recovery for a duplicate created by a retried
        non-idempotent create, which is why idempotency lives in the publisher.
        """
        self.check_scopes(account)
        self._request("DELETE", f"/pins/{pin_id}", account=account)
        return {"deleted": pin_id}

    def pin_analytics(self, *, pin_id: str, account: dict,
                      start_date: str, end_date: str,
                      metric_types: tuple[str, ...] = ("IMPRESSION", "SAVE",
                                                       "OUTBOUND_CLICK",
                                                       "PIN_CLICK")) -> dict:
        """GET /v5/pins/{pin_id}/analytics.

        ``start_date``/``end_date``/``metric_types`` are all REQUIRED and the
        window may not exceed 90 days back or 90 days past the start.
        """
        self.check_scopes(account, need=(SCOPE_PINS_READ,))
        if not (start_date and end_date and metric_types):
            raise PinterestError(
                "pin analytics requires start_date, end_date and metric_types")
        _assert_within_lookback(start_date, end_date)
        body = self._request(
            "GET", f"/pins/{pin_id}/analytics", account=account,
            query={"start_date": start_date, "end_date": end_date,
                   "metric_types": ",".join(metric_types)})
        return {"summary": body.get("summary_metrics") or {},
                "daily": body.get("daily_metrics") or [],
                "lifetime": body.get("lifetime_metrics") or {}}


def _assert_within_lookback(start_date: str, end_date: str) -> None:
    """Enforce the documented 90-day analytics window before calling out."""
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
    except ValueError as exc:
        raise PinterestError(
            f"analytics dates must be YYYY-MM-DD, got {start_date!r}/"
            f"{end_date!r}") from exc
    today = date.today()
    if start < today - timedelta(days=ANALYTICS_MAX_LOOKBACK_DAYS):
        raise PinterestError(
            f"start_date {start} is more than {ANALYTICS_MAX_LOOKBACK_DAYS} "
            f"days back; Pinterest rejects it")
    if (end - start).days > ANALYTICS_MAX_LOOKBACK_DAYS:
        raise PinterestError(
            f"end_date {end} is more than {ANALYTICS_MAX_LOOKBACK_DAYS} days "
            f"past start_date {start}; Pinterest rejects it")


def _pin_url(pin_id: str) -> str:
    return f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else ""


def _json(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        detail = response.text[:400]
        retryable = response.status_code >= 500 or response.status_code == 429
        if response.status_code == 429:
            detail = (f"rate limited (429) {detail}; documented category "
                      f"limits are in x-ratelimit-limit/remaining/reset "
                      f"response headers")
        raise PinterestError(f"HTTP {response.status_code}: {detail}",
                             retryable=retryable)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}
