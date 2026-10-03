"""Work 14 provider CONTRACT tests -- hermetic, fixture-driven, no network.

Every test drives a real provider against a recording ``httpx.MockTransport``,
so the assertions are about the *actual HTTP contract* (method, URL, query,
form fields, call order) rather than about internal helpers. That is the only
way to prove a provider would work against the live API.

Fixtures are transcribed from the official documentation researched for each
platform; where the docs state something is NOT supported, the test asserts the
refusal instead of the success path.

No test here requires a real token. Live-token tests live in
``test_work14_live_tokens.py`` and are marked/skipped separately.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.providers.publishers.base import PublishMetadata

# ---------------------------------------------------------------------------
# hermetic transport
# ---------------------------------------------------------------------------


class Recorder:
    """Records every request and replies from a scripted route table.

    A route body may be a single value, an ``httpx.Response``, or a ``list`` --
    in which case each matching call consumes the next item, which is how a
    flow with repeated calls to the SAME url (Threads carousel children) is
    expressed.
    """

    def __init__(self, routes: list[tuple[str, str, Any]]) -> None:
        self.routes = routes
        self.calls: list[httpx.Request] = []
        self.used: set[int] = set()
        self._cursors: dict[int, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        # Match the MOST SPECIFIC needle first. Substring matching alone would
        # route `/threads_publish` to a `/threads` fixture (and every carousel
        # child to the first child fixture), which would silently assert the
        # wrong contract.
        candidates = [
            (len(needle), index, body)
            for index, (method, needle, body) in enumerate(self.routes)
            if method.upper() == request.method.upper() and needle in url
        ]
        for _length, index, body in sorted(candidates, reverse=True):
            self.used.add(index)
            if isinstance(body, list):
                cursor = self._cursors.get(index, 0)
                if cursor >= len(body):
                    return httpx.Response(500, json={
                        "error": {"message": "fixture sequence exhausted"}})
                self._cursors[index] = cursor + 1
                body = body[cursor]
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)
        # Documented-safe defaults so the real multi-call flow is exercised
        # rather than stubbed out: a Threads container that is ready to
        # publish, and a completed Bluesky video job.
        if "fields=status" in str(request.url):
            return httpx.Response(200, json={"status": "FINISHED"})
        if "app.bsky.video.getJobStatus" in str(request.url):
            return httpx.Response(200, json={"state": {
                "status": "completed",
                "blob": {"$type": "blob", "ref": {"$link": "bafkreivideo"},
                         "mimeType": "video/mp4", "size": 10}}})
        return httpx.Response(
            404, json={"error": {"message": f"no fixture for {request.url}"}})

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def urls(self) -> list[str]:
        return [str(c.url) for c in self.calls]

    def bodies(self) -> list[dict]:
        out = []
        for call in self.calls:
            try:
                out.append(json.loads(call.content or b"{}"))
            except ValueError:
                out.append({})
        return out

    def forms(self) -> list[dict]:
        out = []
        for call in self.calls:
            try:
                out.append(dict(httpx.QueryParams(call.content.decode())))
            except Exception:  # noqa: BLE001 - not a form body
                out.append({})
        return out


def account_with(**extra) -> dict:
    """Account whose extra fields make the documented status poll instant."""
    return {**ACCOUNT, **extra}


def fast(meta: PublishMetadata) -> PublishMetadata:
    """Zero the documented Threads settle/poll cadence.

    The provider still performs the real container-status GET and still requires
    FINISHED before publishing; only the wall-clock wait is removed, so the
    documented flow is exercised rather than stubbed out.
    """
    meta.extra = {**(meta.extra or {}), "settle_seconds": 0, "poll_attempts": 1,
                  "poll_interval": 0}
    return meta


ACCOUNT = {"access_token": "tok-abc", "scopes": ["threads_basic",
                                                 "threads_content_publish"]}


# ===========================================================================
# Threads (§2)
# ===========================================================================


def test_threads_text_post_runs_the_documented_container_then_publish():
    """POST /threads (media_type=TEXT) -> POST /threads_publish, in that order."""
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([
        ("POST", "/threads", {"id": "container-777"}),
        ("POST", "/threads_publish", {"id": "media-999"}),
        ("GET", "/media-999", {"id": "media-999",
                               "permalink": "https://www.threads.net/@x/p/x"}),
    ])
    result = ThreadsPublisher(client=recorder.client).publish(
        "", fast(PublishMetadata(title="Hello Threads",
                                        description="a caption")), ACCOUNT)

    assert result.success is True
    assert result.remote_post_id == "media-999"
    assert result.remote_url.endswith("/p/x")
    # exactly three calls, in the documented order
    assert [c.url.path.rsplit("/", 1)[-1] for c in recorder.calls] == [
        "threads", "threads_publish", "media-999"]
    # the create call carries media_type=TEXT and the text
    form = recorder.forms()[0]
    assert form["media_type"] == "TEXT"
    assert "Hello Threads" in form["text"]
    # the publish call carries creation_id
    assert recorder.forms()[1]["creation_id"] == "container-777"


def test_threads_image_post_uses_image_url_and_alt_text():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([
        ("POST", "/threads", {"id": "c1"}),
        ("POST", "/threads_publish", {"id": "m1"}),
        ("GET", "/m1", {"id": "m1", "permalink": "https://t/p/1"}),
    ])
    meta = fast(PublishMetadata(title="Look", extra={
        "media_url": "https://cdn.example.com/a.jpg", "media_kind": "image",
        "alt_text": "a chart showing revenue growth"}))
    result = ThreadsPublisher(client=recorder.client).publish("", meta, ACCOUNT)
    assert result.success is True
    form = recorder.forms()[0]
    assert form["media_type"] == "IMAGE"
    assert form["image_url"] == "https://cdn.example.com/a.jpg"
    assert form["alt_text"] == "a chart showing revenue growth"


def test_threads_alt_text_is_capped_at_the_documented_1000_characters():
    from app.providers.publishers.threads import ALT_TEXT_MAX, ThreadsPublisher

    recorder = Recorder([("POST", "/threads", {"id": "c1"}),
                         ("POST", "/threads_publish", {"id": "m1"}),
                         ("GET", "/m1", {"id": "m1"})])
    meta = fast(PublishMetadata(title="x", extra={
        "media_url": "https://cdn.example.com/a.jpg",
        "alt_text": "A" * 2500}))
    ThreadsPublisher(client=recorder.client).publish("", meta, ACCOUNT)
    assert len(recorder.forms()[0]["alt_text"]) == ALT_TEXT_MAX


def test_threads_text_post_is_clipped_to_the_documented_500_characters():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([("POST", "/threads", {"id": "c1"}),
                         ("POST", "/threads_publish", {"id": "m1"}),
                         ("GET", "/m1", {"id": "m1"})])
    result = ThreadsPublisher(client=recorder.client).publish(
        "", fast(PublishMetadata(title="T" * 900)), ACCOUNT)
    assert result.success is True
    assert len(recorder.forms()[0]["text"]) == 500
    # the clip is reported, never silent
    assert "clipped" in result.error


def test_threads_refuses_more_than_five_links_before_any_api_call():
    from app.providers.publishers.threads import LINK_LIMIT, ThreadsPublisher

    recorder = Recorder([])
    text = " ".join(f"https://site{i}.com" for i in range(LINK_LIMIT + 2))
    result = ThreadsPublisher(client=recorder.client).publish(
        "", fast(PublishMetadata(title=text)), ACCOUNT)
    assert result.success is False
    assert result.retryable is False
    assert "LINK_LIMIT_EXCEEDED" in result.error
    assert recorder.calls == [], "refused before touching the API"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://localhost/a.jpg", "http://127.0.0.1/a.jpg",
    "http://10.1.2.3/a.jpg", "http://192.168.0.5/a.jpg",
    "https://user:pw@cdn.example.com/a.jpg", "https://svc.internal/a.jpg",
])
def test_threads_refuses_non_public_media_urls(url):
    """A media URL is fetched by Meta, so it must be publicly routable."""
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([])
    result = ThreadsPublisher(client=recorder.client).publish(
        "", fast(PublishMetadata(title="x", extra={"media_url": url})), ACCOUNT)
    assert result.success is False
    assert result.retryable is False
    assert "media url rejected" in result.error
    assert recorder.calls == []


def test_threads_carousel_creates_children_then_a_carousel_parent():
    """is_carousel_item children, then media_type=CAROUSEL with children list."""
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([
        # every child POST goes to the same /me/threads url, so the replies are
        # a sequence: child-1, then child-2, then the carousel parent
        ("POST", "/threads", [{"id": "child-1"}, {"id": "child-2"},
                              {"id": "carousel-1"}]),
        ("POST", "/threads_publish", {"id": "media-carousel"}),
    ])
    result = ThreadsPublisher(client=recorder.client).publish_carousel(
        fast(PublishMetadata(title="Swipe")),
        ACCOUNT,
        ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.mp4"])
    assert result.success is True
    forms = recorder.forms()
    # children declare is_carousel_item and their own media type
    assert forms[0]["is_carousel_item"] == "true"
    assert forms[0]["media_type"] == "IMAGE"
    assert forms[1]["is_carousel_item"] == "true"
    assert forms[1]["media_type"] == "VIDEO"   # .mp4 -> VIDEO child
    # the parent references both children, comma separated
    assert forms[2]["media_type"] == "CAROUSEL"
    assert forms[2]["children"] == "child-1,child-2"


@pytest.mark.parametrize("count", [1, 21])
def test_threads_carousel_refuses_out_of_bounds_item_counts(count):
    from app.providers.publishers.threads import (
        CAROUSEL_MAX,
        CAROUSEL_MIN,
        ThreadsPublisher,
    )

    recorder = Recorder([])
    urls = [f"https://cdn.example.com/{i}.jpg" for i in range(count)]
    result = ThreadsPublisher(client=recorder.client).publish_carousel(
        fast(PublishMetadata(title="x")), ACCOUNT, urls)
    assert result.success is False
    assert recorder.calls == []
    assert f"{CAROUSEL_MIN}..{CAROUSEL_MAX}" in result.error


def test_threads_container_error_status_is_surfaced_not_published():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([
        ("POST", "/threads", {"id": "c-bad"}),
        ("GET", "/c-bad", {"status": "ERROR",
                           "error_message": "FAILED_PROCESSING_VIDEO"}),
    ])
    publisher = ThreadsPublisher(client=recorder.client)
    # poll_attempts=2 so the documented container-status GET really happens
    # (the interval is 0 so the test does not sleep a real minute)
    meta = fast(PublishMetadata(title="x", extra={
        "media_url": "https://cdn.example.com/v.mp4",
        "media_kind": "video"}))
    meta.extra["poll_attempts"] = 2
    with pytest.raises(Exception) as caught:
        publisher.publish("", meta, ACCOUNT)
    assert "FAILED_PROCESSING_VIDEO" in str(caught.value)
    # publish was NEVER attempted after the error
    assert not any("threads_publish" in u for u in recorder.urls())


def test_threads_reply_uses_reply_to_id_on_the_same_endpoint():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([("POST", "/threads", {"id": "rc1"}),
                         ("POST", "/threads_publish", {"id": "rm1"})])
    account = {**ACCOUNT, "scopes": ["threads_manage_replies"],
               "owns_root_post": True}
    result = ThreadsPublisher(client=recorder.client).reply(
        parent_post_id="parent-1", text="thanks!", account=account)
    assert result.success is True
    form = recorder.forms()[0]
    assert form["reply_to_id"] == "parent-1"
    assert form["text"] == "thanks!"


def test_threads_third_party_reply_is_refused_without_the_extra_permission():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([])
    account = {**ACCOUNT, "scopes": ["threads_manage_replies"],
               "owns_root_post": False}
    with pytest.raises(Exception) as caught:
        ThreadsPublisher(client=recorder.client).reply(
            parent_post_id="someone-elses", text="hi", account=account)
    assert "threads_keyword_search" in str(caught.value)
    assert recorder.calls == []


def test_threads_insights_refuses_metrics_that_do_not_exist():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([
        ("GET", "/insights", {"data": [{"name": "likes", "values": [{"value": 3}]}]}),
    ])
    account = {**ACCOUNT, "scopes": ["threads_manage_insights"]}
    publisher = ThreadsPublisher(client=recorder.client)
    # `impressions` is NOT a queryable Threads metric
    with pytest.raises(Exception) as caught:
        publisher.insights(media_id="m1", account=account,
                           metrics=("impressions",))
    assert "impressions" in str(caught.value)
    # `clicks` is user-level only
    with pytest.raises(Exception):
        publisher.insights(media_id="m1", account=account, metrics=("clicks",))
    assert recorder.calls == []
    # the GA metrics do go out and the response is returned
    out = publisher.insights(media_id="m1", account=account,
                             metrics=("likes", "replies"))
    assert out["metrics"][0]["name"] == "likes"
    assert "likes" in recorder.urls()[-1]


def test_threads_scope_check_fails_closed():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([])
    with pytest.raises(Exception) as caught:
        ThreadsPublisher(client=recorder.client).publish(
            "", fast(PublishMetadata(title="x")),
            {"access_token": "t", "scopes": ["threads_basic"]})
    assert "threads_content_publish" in str(caught.value)
    assert recorder.calls == []


def test_threads_quota_check_reports_the_documented_24h_limits():
    from app.providers.publishers.threads import ThreadsPublisher

    recorder = Recorder([("GET", "/threads_publishing_limit",
                          {"quota_usage": 200, "config": 250,
                           "reply_quota_usage": 10, "reply_config": 1000})])
    quota = ThreadsPublisher(client=recorder.client).check_quota(
        {"access_token": "t"})
    assert quota["known"] is True
    assert quota["remaining"] == 50
    assert quota["replies_total"] == 1000


def test_threads_5xx_is_retryable_and_4xx_is_not():
    from app.providers.publishers.threads import ThreadsPublisher

    for status, retryable in ((500, True), (429, True), (400, False)):
        recorder = Recorder([("POST", "/threads",
                              httpx.Response(status, json={"error": {}}))])
        with pytest.raises(Exception) as caught:
            ThreadsPublisher(client=recorder.client).publish(
                "", fast(PublishMetadata(title="x")), ACCOUNT)
        assert caught.value.retryable is retryable, status


# ===========================================================================
# Pinterest (§3)
# ===========================================================================

PIN_ACCOUNT = {"access_token": "pat", "scopes": ["boards:read", "pins:write"]}


def test_pinterest_image_pin_uses_media_source_not_a_top_level_url():
    from app.providers.publishers.pinterest import PinterestPublisher

    recorder = Recorder([
        ("GET", "/boards", {"items": [{"id": "12345", "name": "My Board"}]}),
        ("POST", "/pins", {"id": "777"}),
    ])
    meta = PublishMetadata(title="A pin", description="desc",
                           extra={"board_id": "12345",
                                  "media_url": "https://cdn.example.com/a.jpg",
                                  "alt_text": "a photo of a desk"})
    result = PinterestPublisher(client=recorder.client).publish("", meta, PIN_ACCOUNT)
    assert result.success is True
    body = recorder.bodies()[-1]
    assert body["board_id"] == "12345"
    # media_source discriminated union, NOT a top-level image_url
    assert body["media_source"] == {"source_type": "image_url",
                                    "url": "https://cdn.example.com/a.jpg"}
    assert "image_url" not in body
    assert body["alt_text"] == "a photo of a desk"
    assert body["title"] == "A pin"


def test_pinterest_clamps_to_the_documented_schema_limits():
    from app.providers.publishers.pinterest import (
        ALT_TEXT_MAX,
        DESCRIPTION_MAX,
        LINK_MAX,
        TITLE_MAX,
        PinterestPublisher,
    )

    recorder = Recorder([("POST", "/pins", {"id": "1"})])
    meta = PublishMetadata(title="T" * 400, description="D" * 2000, extra={
        "board_id": "1", "media_url": "https://cdn.example.com/a.jpg",
        "link_url": "L" * 4000, "alt_text": "A" * 900})
    PinterestPublisher(client=recorder.client).publish("", meta, PIN_ACCOUNT)
    body = recorder.bodies()[-1]
    assert len(body["title"]) == TITLE_MAX
    assert len(body["description"]) == DESCRIPTION_MAX
    assert len(body["link"]) == LINK_MAX
    assert len(body["alt_text"]) == ALT_TEXT_MAX


def test_pinterest_video_pin_stages_upload_s3_polls_then_creates():
    """POST /media -> S3 multipart -> GET /media/{id} -> POST /pins."""
    from app.providers.publishers.pinterest import (
        MEDIA_SUCCEEDED,
        PinterestPublisher,
    )

    video = _tmp_video()
    recorder = Recorder([
        ("POST", "/media", {"media_id": "m-1",
                            "upload_url": "https://s3.example.com/up",
                            "upload_parameters": {"key": "k", "policy": "p",
                                                  "x-amz-signature": "sig"}}),
        ("POST", "s3.example.com", httpx.Response(204)),
        ("GET", "/media/m-1", {"status": MEDIA_SUCCEEDED}),
        ("POST", "/pins", {"id": "pin-9"}),
    ])
    meta = PublishMetadata(title="Vid", extra={
        "board_id": "1", "media_kind": "video",
        "cover_image_url": "https://cdn.example.com/cover.jpg",
        "media_poll_attempts": 1})
    result = PinterestPublisher(client=recorder.client).publish(
        video, meta, PIN_ACCOUNT)
    assert result.success is True
    assert result.remote_post_id == "pin-9"
    # the S3 upload really happened, with the presigned fields as FORM data
    s3 = [c for c in recorder.calls if "s3.example.com" in str(c.url)]
    assert len(s3) == 1
    body = str(s3[0].content)
    assert "name=\"policy\"" in body and "x-amz-signature" in body
    # the Pin body references video_id + the cover
    pin_body = recorder.bodies()[-1]
    assert pin_body["media_source"]["source_type"] == "video_id"
    assert pin_body["media_source"]["media_id"] == "m-1"
    assert pin_body["media_source"]["cover_image_url"].endswith("cover.jpg")
    # video_url is a RESPONSE field and must never be sent
    assert "video_url" not in pin_body


def test_pinterest_video_without_a_cover_warns_because_docs_say_it_400s():
    from app.providers.publishers.pinterest import PinterestPublisher

    video = _tmp_video()
    recorder = Recorder([
        ("POST", "/media", {"media_id": "m", "upload_url": "https://s3/x",
                            "upload_parameters": {}}),
        ("POST", "s3", httpx.Response(204)),
        ("GET", "/media/m", {"status": "succeeded"}),
        ("POST", "/pins", {"id": "p"}),
    ])
    result = PinterestPublisher(client=recorder.client).publish(
        video, PublishMetadata(title="v", extra={
            "board_id": "1", "media_kind": "video", "media_poll_attempts": 1}),
        PIN_ACCOUNT)
    assert result.success is True
    assert "cover_image_url" in result.error


def test_pinterest_media_failure_is_not_retried():
    from app.providers.publishers.pinterest import PinterestPublisher

    video = _tmp_video()
    recorder = Recorder([
        ("POST", "/media", {"media_id": "m", "upload_url": "https://s3/x",
                            "upload_parameters": {}}),
        ("POST", "s3", httpx.Response(204)),
        ("GET", "/media/m", {"status": "failed"}),
    ])
    with pytest.raises(Exception) as caught:
        PinterestPublisher(client=recorder.client).publish(
            video, PublishMetadata(title="v", extra={
                "board_id": "1", "media_kind": "video", "media_poll_attempts": 2,
                "media_poll_interval": 0}), PIN_ACCOUNT)
    assert caught.value.retryable is False
    assert not any(u.endswith("/pins") for u in recorder.urls())


def test_pinterest_duplicate_create_is_suppressed_locally():
    """Pinterest documents NO idempotency, so a retry must not double-post."""
    from app.providers.publishers.pinterest import PinterestPublisher

    recorder = Recorder([("POST", "/pins", {"id": "pin-1"})])
    publisher = PinterestPublisher(client=recorder.client)
    meta = PublishMetadata(title="Same", extra={
        "board_id": "1", "media_url": "https://cdn.example.com/a.jpg"})
    first = publisher.publish("", meta, PIN_ACCOUNT)
    second = publisher.publish("", meta, PIN_ACCOUNT)
    assert first.remote_post_id == "pin-1"
    assert second.remote_post_id == "pin-1"
    assert "duplicate suppressed" in second.error
    # exactly ONE create call reached the API
    assert len([u for u in recorder.urls() if u.endswith("/pins")]) == 1


def test_pinterest_carousel_is_bounded_at_two_to_five():
    from app.providers.publishers.pinterest import PinterestPublisher

    recorder = Recorder([])
    publisher = PinterestPublisher(client=recorder.client)
    for count in (1, 6):
        result = publisher.publish_carousel(
            PublishMetadata(title="c"), PIN_ACCOUNT,
            [f"https://cdn.example.com/{i}.jpg" for i in range(count)],
            board_id="1")
        assert result.success is False
        assert "2..5" in result.error
    assert recorder.calls == []


def test_pinterest_image_content_type_is_restricted_to_jpeg_and_png():
    from app.providers.publishers.pinterest import IMAGE_CONTENT_TYPES

    assert IMAGE_CONTENT_TYPES == ("image/jpeg", "image/png")
    for bad in ("a.gif", "a.webp"):
        assert "image/gif" not in IMAGE_CONTENT_TYPES


def test_pinterest_ambiguous_board_name_is_refused_not_guessed():
    from app.providers.publishers.pinterest import PinterestPublisher

    recorder = Recorder([("GET", "/boards", {"items": [
        {"id": "1", "name": "Ideas"}, {"id": "2", "name": "Ideas"}]})])
    with pytest.raises(Exception) as caught:
        PinterestPublisher(client=recorder.client).resolve_board(
            board_id="", board_name="Ideas", account=PIN_ACCOUNT)
    assert "ambiguous" in str(caught.value)


def test_pinterest_analytics_enforces_the_documented_window():
    from app.providers.publishers.pinterest import (
        PinterestError,
        PinterestPublisher,
    )

    publisher = PinterestPublisher(client=Recorder([]).client)
    with pytest.raises(PinterestError):
        publisher.pin_analytics(pin_id="1", account=PIN_ACCOUNT,
                                start_date="2000-01-01", end_date="2000-02-01",
                                metric_types=("IMPRESSION",))
    with pytest.raises(PinterestError):
        publisher.pin_analytics(pin_id="1", account=PIN_ACCOUNT,
                                start_date="", end_date="",
                                metric_types=("IMPRESSION",))


def test_pinterest_delete_is_the_compensating_action():
    from app.providers.publishers.pinterest import PinterestPublisher

    recorder = Recorder([("DELETE", "/pins/pin-7", httpx.Response(204))])
    out = PinterestPublisher(client=recorder.client).delete_pin(
        pin_id="pin-7", account=PIN_ACCOUNT)
    assert out["deleted"] == "pin-7"
    assert recorder.calls[0].method == "DELETE"


# ===========================================================================
# Bluesky (§4)
# ===========================================================================

BSKY_ACCOUNT = {"access_jwt": "jwt-1", "did": "did:plc:abc",
                "handle": "y.bsky.social"}
#: An app password is minted in the Bluesky app UI; this is a non-credential
#: placeholder used only to prove the createSession request shape.
APP_PW_PLACEHOLDER = "app-password-from-the-secrets-resolver"


def test_bluesky_text_post_returns_and_keeps_both_uri_and_cid():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.repo.createRecord", {
        "uri": "at://did:plc:abc/app.bsky.feed.post/3k4duaz",
        "cid": "bafyreib2abc"})])
    result = BlueskyPublisher(client=recorder.client).publish(
        "", PublishMetadata(title="hello bsky", hashtags=["ai", "video"]),
        BSKY_ACCOUNT)
    assert result.success is True
    assert result.remote_post_id.startswith("at://")
    assert "/post/" in result.remote_url
    body = recorder.bodies()[-1]
    assert body["collection"] == "app.bsky.feed.post"
    record = body["record"]
    assert record["$type"] == "app.bsky.feed.post"
    assert record["text"] == "hello bsky"
    assert record["tags"] == ["ai", "video"]
    assert "createdAt" in record


def test_bluesky_text_is_clipped_to_300_graphemes_and_3000_bytes():
    from app.providers.publishers.bluesky import (
        TEXT_MAX_GRAPHEMES,
        BlueskyPublisher,
    )

    recorder = Recorder([("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/x",
                                                    "cid": "c"})])
    BlueskyPublisher(client=recorder.client).publish(
        "", PublishMetadata(title="a" * 1000), BSKY_ACCOUNT)
    record = recorder.bodies()[-1]["record"]
    assert len(record["text"]) == TEXT_MAX_GRAPHEMES
    assert len(record["text"].encode()) <= 3000


def test_bluesky_tags_are_capped_at_the_documented_eight():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/x",
                                                    "cid": "c"})])
    BlueskyPublisher(client=recorder.client).publish(
        "", PublishMetadata(title="t", hashtags=[f"t{i}" for i in range(20)]),
        BSKY_ACCOUNT)
    assert len(recorder.bodies()[-1]["record"]["tags"]) == 8


def test_bluesky_image_embed_uploads_a_blob_and_always_sends_alt():
    """`alt` is REQUIRED by app.bsky.embed.images, so it is always present."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    png = _tmp_png()
    recorder = Recorder([
        ("POST", "com.atproto.repo.uploadBlob",
         {"blob": {"$type": "blob",
                   "ref": {"$link": "bafkreiexisting"},
                   "mimeType": "image/png", "size": 8}}),
        ("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/1", "cid": "c1"}),
    ])
    meta = PublishMetadata(title="pic", extra={"media_kind": "image",
                                              "alt_text": "a diagram"})
    result = BlueskyPublisher(client=recorder.client).publish(
        png, meta, BSKY_ACCOUNT)
    assert result.success is True
    embed = recorder.bodies()[-1]["record"]["embed"]
    assert embed["$type"] == "app.bsky.embed.images"
    assert embed["images"][0]["alt"] == "a diagram"
    # the blob ref from uploadBlob is passed through VERBATIM
    assert embed["images"][0]["image"] == {
        "$type": "blob", "ref": {"$link": "bafkreiexisting"},
        "mimeType": "image/png", "size": 8}


def test_bluesky_image_alt_is_sent_even_when_none_was_supplied():
    """alt is REQUIRED by the lexicon (may be empty), so it is never omitted."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    png = _tmp_png()
    recorder = Recorder([
        ("POST", "com.atproto.repo.uploadBlob", {"blob": {"$type": "blob",
                                          "ref": {"$link": "bafk"},
                                          "mimeType": "image/png", "size": 8}}),
        ("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/1", "cid": "c1"}),
    ])
    result = BlueskyPublisher(client=recorder.client).publish(
        png, PublishMetadata(title="pic", extra={"media_kind": "image"}),
        BSKY_ACCOUNT)
    assert result.success is True
    images = recorder.bodies()[-1]["record"]["embed"]["images"]
    assert "alt" in images[0], "alt is required by app.bsky.embed.images"
    assert any("alt text empty" in w for w in result.error.split("; "))


def test_bluesky_image_over_the_documented_2mb_cap_is_refused():
    from app.providers.publishers.bluesky import IMAGE_MAX_BYTES, BlueskyPublisher

    big = _tmp_png(pad=IMAGE_MAX_BYTES + 1024)
    recorder = Recorder([])
    with pytest.raises(Exception) as caught:
        BlueskyPublisher(client=recorder.client).publish(
            big, PublishMetadata(title="x", extra={"media_kind": "image"}),
            BSKY_ACCOUNT)
    assert "caps at" in str(caught.value)
    assert recorder.calls == []


def test_bluesky_video_is_supported_not_declined():
    """app.bsky.embed.video is a real member of the post embed union."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    mp4 = _tmp_video()
    recorder = Recorder([
        ("GET", "app.bsky.video.getUploadLimits", {"canUpload": True}),
        ("POST", "app.bsky.video.uploadVideo", {"jobId": "job-1"}),
        ("GET", "app.bsky.video.getJobStatus",
         {"state": {"status": "completed",
                    "blob": {"$type": "blob",
                             "ref": {"$link": "bafkreivideo"},
                             "mimeType": "video/mp4", "size": 10}}}),
        ("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/v", "cid": "cv"}),
    ])
    result = BlueskyPublisher(client=recorder.client).publish(
        mp4, PublishMetadata(title="clip", extra={"media_kind": "video",
                                                  "alt_text": "a clip"}),
        BSKY_ACCOUNT)
    assert result.success is True
    embed = recorder.bodies()[-1]["record"]["embed"]
    assert embed["$type"] == "app.bsky.embed.video"
    assert embed["video"]["ref"]["$link"] == "bafkreivideo"
    assert embed["alt"] == "a clip"


def test_bluesky_video_job_already_exists_error_still_yields_a_blob_ref():
    """The service returns an ERROR that still carries a usable blob ref."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    mp4 = _tmp_video()
    recorder = Recorder([
        ("GET", "getUploadLimits", {"canUpload": True}),
        ("POST", "uploadVideo", {"jobId": "job-1"}),
        ("GET", "getJobStatus", httpx.Response(
            400, json={"error": "AlreadyExists",
                       "message": "already_exists",
                       "blob": {"$type": "blob",
                                "ref": {"$link": "bafkreireused"},
                                "mimeType": "video/mp4", "size": 10}})),
        ("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/v", "cid": "cv"}),
    ])
    result = BlueskyPublisher(client=recorder.client).publish(
        mp4, PublishMetadata(title="clip", extra={"media_kind": "video"}),
        BSKY_ACCOUNT)
    assert result.success is True
    embed = recorder.bodies()[-1]["record"]["embed"]
    assert embed["video"]["ref"]["$link"] == "bafkreireused"


def test_bluesky_video_refused_when_the_account_may_not_upload():
    from app.providers.publishers.bluesky import BlueskyPublisher

    mp4 = _tmp_video()
    recorder = Recorder([
        ("GET", "getUploadLimits", {"canUpload": False,
                                    "error": "account_not_verified"}),
    ])
    with pytest.raises(Exception) as caught:
        BlueskyPublisher(client=recorder.client).publish(
            mp4, PublishMetadata(title="c", extra={"media_kind": "video"}),
            BSKY_ACCOUNT)
    assert "refused" in str(caught.value)
    assert not any("createRecord" in u for u in recorder.urls())


def test_bluesky_reply_requires_both_root_and_parent_strong_refs():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/r",
                                                    "cid": "cr"})])
    BlueskyPublisher(client=recorder.client).reply(
        parent_uri="at://d/c/p", parent_cid="cp",
        root_uri="at://d/c/p", root_cid="cp", text="nice", account=BSKY_ACCOUNT)
    reply = recorder.bodies()[-1]["record"]["reply"]
    assert reply["root"] == {"uri": "at://d/c/p", "cid": "cp"}
    assert reply["parent"] == {"uri": "at://d/c/p", "cid": "cp"}


@pytest.mark.parametrize("missing", ["parent_cid", "root_uri", "root_cid"])
def test_bluesky_reply_refuses_an_incomplete_strong_ref(missing):
    from app.providers.publishers.bluesky import BlueskyPublisher

    kwargs = {"parent_uri": "u", "parent_cid": "c", "root_uri": "r",
              "root_cid": "c"}
    kwargs[missing] = ""
    with pytest.raises(Exception) as caught:
        BlueskyPublisher(client=Recorder([]).client).reply(
            text="t", account=BSKY_ACCOUNT, **kwargs)
    assert missing in str(caught.value)


def test_bluesky_quote_uses_the_record_embed_strong_ref():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.repo.createRecord", {"uri": "at://d/c/q",
                                                    "cid": "cq"})])
    BlueskyPublisher(client=recorder.client).quote(
        quoted_uri="at://other/1", quoted_cid="cother",
        meta=PublishMetadata(title="this is wild"), account=BSKY_ACCOUNT)
    embed = recorder.bodies()[-1]["record"]["embed"]
    assert embed["$type"] == "app.bsky.embed.record"
    assert embed["record"] == {"uri": "at://other/1", "cid": "cother"}


def test_bluesky_external_card_requires_uri_title_and_description():
    from app.providers.publishers.bluesky import BlueskyPublisher

    publisher = BlueskyPublisher(client=Recorder([]).client)
    # no link url at all -> refused (Bluesky never unfurls server-side)
    with pytest.raises(Exception) as caught:
        publisher._external_embed(PublishMetadata(title="t"), BSKY_ACCOUNT)
    assert "link_url" in str(caught.value)
    # the link details live on the metadata, not the account
    embed = publisher._external_embed(
        PublishMetadata(title="t", extra={
            "link_url": "https://a.example", "link_title": "A",
            "link_description": "D"}), BSKY_ACCOUNT)
    assert embed["$type"] == "app.bsky.embed.external"
    assert set(embed["external"]) >= {"uri", "title", "description"}
    assert embed["external"]["uri"] == "https://a.example"


def test_bluesky_replies_are_read_with_get_post_thread_not_get_replies():
    """app.bsky.feed.getReplies does not exist; getPostThread has no cursor."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("GET", "app.bsky.feed.getPostThread", {"thread": {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": {"uri": "at://d/c/1", "cid": "c1",
                 "record": {"text": "root"},
                 "author": {"handle": "root.bsky.social"}},
        "replies": [
            {"$type": "app.bsky.feed.defs#threadViewPost",
             "post": {"uri": "at://d/c/2", "cid": "c2",
                      "record": {"text": "first level"},
                      "author": {"handle": "a.bsky.social"}}},
            {"$type": "app.bsky.feed.defs#notFoundPost", "notFound": True},
        ]}})])
    out = BlueskyPublisher(client=recorder.client).list_replies(
        post_uri="at://d/c/1", account=BSKY_ACCOUNT, depth=3)
    assert any("getPostThread" in u for u in recorder.urls())
    assert not any("getReplies" in u for u in recorder.urls())
    texts = [r.get("text") for r in out["replies"]]
    assert "first level" in texts
    # no cursor: the tree walk IS the pagination
    assert out["cursor"] == "" and out["exhausted"] is True


def test_bluesky_delete_is_idempotent_by_contract():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.repo.deleteRecord", {})])
    BlueskyPublisher(client=recorder.client).delete_post(
        post_uri="at://did:plc:abc/app.bsky.feed.post/3k4duaz",
        account=BSKY_ACCOUNT)
    body = recorder.bodies()[-1]
    assert body == {"repo": "did:plc:abc", "collection": "app.bsky.feed.post",
                    "rkey": "3k4duaz"}


def test_bluesky_429_is_retryable_and_501_is_not():
    from app.providers.publishers.bluesky import BlueskyError, BlueskyPublisher

    for status, retryable in ((429, True), (500, True), (501, False),
                              (400, False)):
        recorder = Recorder([("POST", "com.atproto.repo.createRecord", httpx.Response(
            status, json={"error": "X", "message": "m"},
            headers={"Retry-After": "5"}))])
        with pytest.raises(BlueskyError) as caught:
            BlueskyPublisher(client=recorder.client).publish(
                "", PublishMetadata(title="t"), BSKY_ACCOUNT)
        assert caught.value.retryable is retryable, status


def test_bluesky_session_uses_an_app_password_and_returns_a_jwt():
    from app.providers.publishers.bluesky import BlueskyPublisher

    recorder = Recorder([("POST", "com.atproto.server.createSession", {
        "accessJwt": "a.jwt", "refreshJwt": "r.jwt",
        "handle": "y.bsky.social", "did": "did:plc:abc"})])
    out = BlueskyPublisher(client=recorder.client).create_session(
        identifier="y.bsky.social", app_password=APP_PW_PLACEHOLDER)
    assert out["accessJwt"] == "a.jwt"
    body = recorder.bodies()[-1]
    assert body["identifier"] == "y.bsky.social"
    # official guidance: an app password, not the main account password
    assert "password" in body


def test_bluesky_publish_without_a_token_fails_closed():
    from app.providers.publishers.bluesky import BlueskyError, BlueskyPublisher

    recorder = Recorder([])
    with pytest.raises(BlueskyError) as caught:
        BlueskyPublisher(client=recorder.client).publish(
            "", PublishMetadata(title="t"), {})
    assert "client-credentials is not supported" in str(caught.value)
    assert recorder.calls == []


# ===========================================================================
# Snapchat (§5) -- the honesty tests
# ===========================================================================


def test_snapchat_handoff_never_returns_a_remote_id():
    from app.providers.publishers.snapchat import prepare_handoff

    result = prepare_handoff(video_path="", meta=PublishMetadata(title="Snap"))
    assert result.success is True          # the handoff was prepared
    assert result.remote_post_id == ""     # but nothing was published
    assert "User handoff required" in result.error


def test_snapchat_is_registered_as_a_handoff_platform_not_a_publisher():
    from app.engine.platform_registry import get_registry
    from app.providers.publishers.factory import HANDOFF_PLATFORMS, get_publisher

    assert "snapchat" in HANDOFF_PLATFORMS
    publisher = get_publisher("snapchat", has_account=True)
    assert getattr(publisher, "handoff_only", False) is True
    registry = get_registry()
    assert registry.supports("snapchat", "USER_HANDOFF") is True
    assert registry.supports("snapchat", "DIRECT_PUBLISH") is False


def test_snapchat_handoff_payload_records_prepared_work_and_no_remote_id():
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.snapchat import build_handoff_payload

    payload = build_handoff_payload(
        video_path="", meta=PublishMetadata(title="My Snap", description="d"))
    assert payload["mode"] == "HANDOFF"
    assert payload["requires_human"] is True
    assert payload["remote_id"] == ""
    assert "snapchat.com/share" in payload["share_url"]
    assert payload["partner_path"]["available"] is False


def test_snapchat_partner_publish_path_is_recorded_but_disabled():
    """The API exists and is documented; it is gated and therefore OFF."""
    from app.providers.publishers.snapchat import (
        PARTNER_PUBLISH_CONTRACT,
        partner_publish_enabled,
    )

    assert PARTNER_PUBLISH_CONTRACT["enabled"] is False
    # the exact endpoints and constraints ARE recorded for auditability
    story = PARTNER_PUBLISH_CONTRACT["publish_story"]
    assert story["url"].endswith("/stories")
    assert story["constraints"] == {"format": "mp4", "min_seconds": 5,
                                    "max_seconds": 60, "min_width": 540,
                                    "min_height": 960}
    assert PARTNER_PUBLISH_CONTRACT["auth"]["protocol"].startswith(
        "OAuth 2.0 authorization_code")
    assert PARTNER_PUBLISH_CONTRACT["auth"]["scope_for_publish"] == "UNVERIFIED"
    assert len(PARTNER_PUBLISH_CONTRACT["why_not_enabled"]) >= 3
    # and it cannot be switched on by a request, only by env + credentials
    assert partner_publish_enabled({
        "snap_public_profile_id": "p", "access_token": "t",
        "allowlisted": True}) is False
    assert partner_publish_enabled({}) is False


def test_snapchat_handoff_warns_when_the_media_is_missing(tmp_path):
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.snapchat import build_handoff_payload

    payload = build_handoff_payload(
        video_path=str(tmp_path / "nope.mp4"),
        meta=PublishMetadata(title="x"))
    assert payload["media"]["exists"] is False
    assert any("not found" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _tmp_video() -> str:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "ymoney-w14-fixture.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return str(path)


def _tmp_png(pad: int = 0) -> str:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "ymoney-w14-fixture.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 + pad))
    return str(path)
