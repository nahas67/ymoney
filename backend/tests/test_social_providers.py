"""Work 09 Lane A — social provider contract (fixture transports, no network).

Covers the ``app.providers.social`` contract Lanes B/C rely on:
``get_provider`` resolution, pagination + verbatim cursor resume,
``reply_to_comment`` receipts, capability gating, credential failures, and
error mapping (401/403 → ProviderNotConfigured, 429 → ProviderRateLimited).

Every HTTP call goes through an injected ``httpx.MockTransport``; a
"no network" transport asserts loudly if anything tries to leave the process.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.providers.social import SOCIAL_PLATFORMS, get_provider
from app.providers.social.base import (
    ProviderCapabilityError,
    ProviderNotConfigured,
    ProviderRateLimited,
)

ACCOUNT = {
    "workspace_id": "ws-1",
    "external_id": "ext-1",
    "access_token": "tok-123",
    "meta": {"user_id": "u-1"},
}


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _no_network() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.method} {request.url}")

    return _mock(handler)


# ---------------------------------------------------------------------------
# provider resolution
# ---------------------------------------------------------------------------

def test_get_provider_resolves_every_account_platform():
    registry_platforms = set()
    for platform in SOCIAL_PLATFORMS:
        provider = get_provider(platform, transport=_no_network())
        assert provider.platform == platform
        registry_platforms.add(platform)
    assert registry_platforms == {"facebook", "instagram", "linkedin", "tiktok", "x", "youtube"}
    # fresh instance per call (callers own their client lifecycle)
    assert get_provider("youtube") is not get_provider("youtube")


def test_get_provider_unknown_platform_raises_key_error():
    with pytest.raises(KeyError):
        get_provider("myspace")
    with pytest.raises(KeyError):
        get_provider("")


# ---------------------------------------------------------------------------
# credentials fail closed (no simulated inbox)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", SOCIAL_PLATFORMS)
def test_provider_not_configured_without_credentials(platform):
    provider = get_provider(platform, transport=_no_network())
    with pytest.raises(ProviderNotConfigured) as exc:
        provider.list_comments({}, post_remote_id="post-1")
    assert platform in str(exc.value)


def test_auth_error_maps_to_provider_not_configured():
    provider = get_provider(
        "instagram", transport=_mock(lambda request: httpx.Response(403, text="no scope"))
    )
    with pytest.raises(ProviderNotConfigured) as exc:
        provider.list_comments(ACCOUNT, post_remote_id="M1")
    assert "instagram" in str(exc.value)


def test_rate_limit_maps_to_provider_rate_limited_with_retry_after():
    provider = get_provider(
        "youtube",
        transport=_mock(
            lambda request: httpx.Response(429, headers={"retry-after": "7"}, text="slow down")
        ),
    )
    with pytest.raises(ProviderRateLimited) as exc:
        provider.list_comments(ACCOUNT, post_remote_id="v1")
    assert exc.value.retry_after == 7.0
    assert exc.value.platform == "youtube"


# ---------------------------------------------------------------------------
# capability gating (honest: nothing undeclared is callable)
# ---------------------------------------------------------------------------

def test_capability_errors_for_undeclared_operations():
    facebook = get_provider("facebook", transport=_no_network())
    with pytest.raises(ProviderCapabilityError) as exc:
        facebook.list_mentions(ACCOUNT)
    assert "facebook" in str(exc.value) and "READ_MENTIONS" in str(exc.value)

    linkedin = get_provider("linkedin", transport=_no_network())
    with pytest.raises(ProviderCapabilityError):
        linkedin.delete_comment(ACCOUNT, "urn:li:comment:(activity:1,comment:2)")
    with pytest.raises(ProviderCapabilityError):
        linkedin.list_mentions(ACCOUNT)

    tiktok = get_provider("tiktok", transport=_no_network())
    with pytest.raises(ProviderCapabilityError):
        tiktok.reply_to_comment(ACCOUNT, "c1", "hi")

    youtube = get_provider("youtube", transport=_no_network())
    with pytest.raises(ProviderCapabilityError):
        youtube.list_mentions(ACCOUNT)


def test_no_platform_pretends_to_read_messages():
    # no DM ingestion exists anywhere — no provider exposes list_messages
    for platform in SOCIAL_PLATFORMS:
        assert not hasattr(get_provider(platform), "list_messages")


# ---------------------------------------------------------------------------
# post-scoped reads require the post id (FB/IG/TikTok/LinkedIn/X)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "platform,post_id",
    [
        ("facebook", "P1"),
        ("instagram", "M1"),
        ("tiktok", "V1"),
        ("linkedin", "urn:li:activity:1"),
        ("x", "T1"),
    ],
)
def test_post_scoped_reads_require_post_remote_id(platform, post_id):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    provider = get_provider(platform, transport=_mock(handler))
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT)  # no post id -> honest input error
    assert calls == []

    # with the post id supplied the call reaches the platform API
    page = provider.list_comments(ACCOUNT, post_remote_id=post_id)
    assert page.items == [] and page.next_cursor is None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# pagination + cursor resume
# ---------------------------------------------------------------------------

def test_facebook_list_comments_pagination_and_cursor_resume():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.params.get("after") == "CUR-1":
            return httpx.Response(
                200,
                json={"data": [{"id": "c3", "message": "third"}], "paging": {}},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c1",
                        "message": "first",
                        "from": {"id": "u1", "name": "Alice"},
                        "created_time": "2026-09-01T10:00:00+0000",
                    },
                    {
                        "id": "c2",
                        "message": "second",
                        "from": {"id": "u2", "name": "Bob"},
                        "created_time": "2026-09-01T11:00:00+0000",
                        "parent": {"id": "c1"},
                    },
                ],
                "paging": {
                    "cursors": {"before": "B0", "after": "CUR-1"},
                    "next": "https://graph.facebook.com/v21.0/P1/comments?after=CUR-1",
                },
            },
        )

    provider = get_provider("facebook", transport=_mock(handler))
    page1 = provider.list_comments(ACCOUNT, limit=2, post_remote_id="P1")
    assert [i.remote_id for i in page1.items] == ["c1", "c2"]
    assert page1.next_cursor == "CUR-1"

    first, second = page1.items
    assert first.author_name == "Alice" and first.author_remote_id == "u1"
    assert first.post_remote_id == "P1" and first.thread_id == "P1"
    assert first.created_at is not None and first.created_at.tzinfo is not None
    assert second.parent_remote_id == "c1"

    page2 = provider.list_comments(
        ACCOUNT, cursor=page1.next_cursor, limit=2, post_remote_id="P1"
    )
    assert [i.remote_id for i in page2.items] == ["c3"]
    assert page2.next_cursor is None

    # cursor came back verbatim on the wire; page size was honoured
    assert calls[0].url.params.get("after") is None
    assert calls[1].url.params.get("after") == "CUR-1"
    assert calls[1].url.path == "/v21.0/P1/comments"
    assert calls[0].url.params.get("limit") == "2"
    assert calls[0].url.params.get("access_token") == ACCOUNT["access_token"]


def test_tiktok_list_comments_cursor_resume():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if body.get("cursor") == "20":
            return httpx.Response(
                200,
                json={
                    "error": {"code": 0, "message": "OK"},
                    "data": {
                        "comments": [
                            {"comment_id": "c3", "text": "third", "create_time": 1757000000}
                        ],
                        "cursor": "40",
                        "has_more": 0,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "error": {"code": 0, "message": "OK"},
                "data": {
                    "comments": [
                        {"comment_id": "c1", "text": "first", "create_time": 1756900000,
                         "user": {"open_id": "ou-1", "display_name": "Ada"}},
                        {"comment_id": "c2", "text": "second"},
                    ],
                    "cursor": "20",
                    "has_more": 1,
                },
            },
        )

    provider = get_provider("tiktok", transport=_mock(handler))
    page1 = provider.list_comments(ACCOUNT, limit=20, post_remote_id="V1")
    assert [i.remote_id for i in page1.items] == ["c1", "c2"]
    assert page1.next_cursor == "20"
    assert page1.items[0].author_remote_id == "ou-1"
    assert page1.items[0].created_at is not None

    page2 = provider.list_comments(
        ACCOUNT, cursor=page1.next_cursor, limit=20, post_remote_id="V1"
    )
    assert [i.remote_id for i in page2.items] == ["c3"]
    assert page2.next_cursor is None

    assert calls[1].url.path == "/v2/video/comment/list/"
    assert json.loads(calls[1].content)["cursor"] == "20"
    assert calls[1].headers["Authorization"].startswith("Bearer ")


# ---------------------------------------------------------------------------
# replies produce receipts
# ---------------------------------------------------------------------------

def test_facebook_reply_returns_receipt():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "reply-9"})

    provider = get_provider("facebook", transport=_mock(handler))
    receipt = provider.reply_to_comment(ACCOUNT, "c1", "Thanks!")
    assert receipt.remote_reply_id == "reply-9"
    assert receipt.mock is False
    assert receipt.raw == {"id": "reply-9"}
    assert calls[0].url.path == "/v21.0/c1/replies"
    form = parse_qs(calls[0].content.decode())
    assert form["message"] == ["Thanks!"]


def test_tiktok_delete_returns_receipt():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"error": {"code": 0}, "data": {"comment_id": "c9"}},
        )

    provider = get_provider("tiktok", transport=_mock(handler))
    receipt = provider.delete_comment(ACCOUNT, "c9")
    assert receipt.remote_reply_id == "c9"
    assert receipt.mock is False
    assert calls[0].url.path == "/v2/video/comment/delete/"


def test_reply_rejects_blank_text_and_ids():
    provider = get_provider("youtube", transport=_no_network())
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, "c1", "   ")
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, "", "hi")


def test_tiktok_auth_error_in_200_body_maps_honestly():
    provider = get_provider(
        "tiktok",
        transport=_mock(
            lambda request: httpx.Response(
                200,
                json={
                    "error": {"code": 10202, "message": "invalid access_token"},
                    "data": {},
                },
            )
        ),
    )
    with pytest.raises(ProviderNotConfigured):
        provider.list_comments(ACCOUNT, post_remote_id="V1")


# ---------------------------------------------------------------------------
# page limits
# ---------------------------------------------------------------------------

def test_page_limit_is_validated_and_clamped():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [], "paging": {}})

    provider = get_provider("facebook", transport=_mock(handler))
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT, limit=0, post_remote_id="P1")
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT, limit=-3, post_remote_id="P1")
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT, limit=2.5, post_remote_id="P1")  # type: ignore[arg-type]

    provider.list_comments(ACCOUNT, limit=500, post_remote_id="P1")
    assert calls[0].url.params.get("limit") == "100"  # MAX_PAGE_SIZE
