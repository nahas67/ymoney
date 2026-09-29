"""Work 09 Lane A — X provider contract tests (injected transport).

Contract coverage: replies timeline pagination (``meta.next_token`` resume),
mentions (stored user id + ``/2/users/me`` fallback), reply/delete receipts,
capability gating, credential failures. Live smoke at the bottom is
``-m live`` AND credential gated — it never runs in the default suite and
never runs without real creds.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from app.engine.platform_registry import Capability
from app.providers.social import get_provider
from app.providers.social.base import (
    ProviderNotConfigured,
    ProviderRateLimited,
)
from app.providers.social.x import XProvider

ACCOUNT = {"workspace_id": "ws-1", "external_id": "900001", "access_token": "x-bearer"}
TWEET = "t-100"


def _tweet(
    tweet_id: str,
    text: str,
    *,
    author_id: str = "u-7",
    created_at: str = "2026-09-20T12:00:00.000Z",
) -> dict:
    return {
        "id": tweet_id,
        "text": text,
        "author_id": author_id,
        "created_at": created_at,
    }


def _users() -> dict:
    return {
        "includes": {
            "users": [
                {"id": "u-7", "name": "Ada Lovelace", "username": "ada"},
                {"id": "u-8", "name": "Grace Hopper", "username": "grace"},
            ]
        }
    }


def _provider(handler) -> XProvider:
    return get_provider("x", transport=httpx.MockTransport(handler))


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call: {request.method} {request.url}")


# ---------------------------------------------------------------------------
# READ_COMMENTS (replies timeline)
# ---------------------------------------------------------------------------

def test_list_comments_pagination_and_cursor_resume():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.params.get("pagination_token") == "tok-1":
            payload = {"data": [_tweet("t-102", "third")], "meta": {"result_count": 1}}
            return httpx.Response(200, json=payload)
        payload = {
            "data": [_tweet("t-100", "first"), _tweet("t-101", "second", author_id="u-8")],
            "meta": {"result_count": 2, "next_token": "tok-1"},
            **_users(),
        }
        return httpx.Response(200, json=payload)

    provider = _provider(handler)
    page1 = provider.list_comments(ACCOUNT, limit=50, post_remote_id=TWEET)
    assert [i.remote_id for i in page1.items] == ["t-100", "t-101"]
    assert page1.next_cursor == "tok-1"

    page2 = provider.list_comments(
        ACCOUNT, cursor=page1.next_cursor, limit=50, post_remote_id=TWEET
    )
    assert [i.remote_id for i in page2.items] == ["t-102"]
    assert page2.next_cursor is None

    # wire shape: v2 replies timeline, cursor verbatim, expansion hydrated
    assert calls[0].url.path == "/2/tweets/" + TWEET + "/replies"
    assert calls[0].url.params.get("pagination_token") is None
    assert calls[1].url.params.get("pagination_token") == "tok-1"
    assert calls[0].url.params.get("tweet.fields") == "created_at,author_id"
    assert calls[0].headers["Authorization"] == "Bearer x-bearer"

    first, second = page1.items
    assert first.author_name == "Ada Lovelace @ada"
    assert first.author_remote_id == "u-7"
    assert first.parent_remote_id == TWEET  # a reply lives under the tweet
    assert first.post_remote_id == TWEET
    assert first.created_at is not None and first.created_at.tzinfo is not None
    assert second.author_name == "Grace Hopper @grace"


def test_list_comments_enforces_api_minimum_page_size():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    provider = _provider(handler)
    provider.list_comments(ACCOUNT, limit=1, post_remote_id=TWEET)
    assert calls[0].url.params.get("max_results") == "10"  # API minimum


def test_list_comments_requires_post_remote_id():
    provider = _provider(_no_network)
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT)


# ---------------------------------------------------------------------------
# READ_MENTIONS
# ---------------------------------------------------------------------------

def test_list_mentions_uses_stored_user_id():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = {
            "data": [_tweet("m-1", "@ymoney nice thread", author_id="u-8")],
            "meta": {"result_count": 1},
            **_users(),
        }
        return httpx.Response(200, json=payload)

    provider = _provider(handler)
    page = provider.list_mentions(ACCOUNT, limit=50)
    assert [i.remote_id for i in page.items] == ["m-1"]
    assert page.items[0].parent_remote_id is None  # a mention is top-level
    assert page.next_cursor is None
    assert calls[0].url.path == "/2/users/900001/mentions"


def test_list_mentions_falls_back_to_users_me():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "900002"}})
        return httpx.Response(200, json={"data": [], "meta": {"result_count": 0}})

    provider = _provider(handler)
    page = provider.list_mentions({"access_token": "x-bearer", "workspace_id": "ws-1"})
    assert page.items == []
    paths = [r.url.path for r in calls]
    assert paths == ["/2/users/me", "/2/users/900002/mentions"]


def test_list_mentions_without_user_id_is_not_configured():
    provider = _provider(
        lambda request: httpx.Response(200, json={"data": {}})
    )
    with pytest.raises(ProviderNotConfigured):
        provider.list_mentions({"access_token": "x-bearer", "workspace_id": "ws-1"})


# ---------------------------------------------------------------------------
# REPLY_COMMENT / DELETE_COMMENT
# ---------------------------------------------------------------------------

def test_reply_returns_receipt_with_reply_context():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"id": "t-900"}})

    provider = _provider(handler)
    receipt = provider.reply_to_comment(ACCOUNT, TWEET, "Appreciate you!")
    assert receipt.remote_reply_id == "t-900"
    assert receipt.mock is False
    assert receipt.raw == {"data": {"id": "t-900"}}

    request = calls[0]
    assert request.method == "POST" and request.url.path == "/2/tweets"
    body = json.loads(request.content)
    assert body["text"] == "Appreciate you!"
    assert body["reply"] == {"in_reply_to_tweet_id": TWEET}
    assert request.headers["Authorization"] == "Bearer x-bearer"


def test_delete_returns_receipt():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"deleted": True}})

    provider = _provider(handler)
    receipt = provider.delete_comment(ACCOUNT, TWEET)
    assert receipt.remote_reply_id == TWEET
    assert receipt.raw == {"data": {"deleted": True}}
    assert calls[0].method == "DELETE"
    assert calls[0].url.path == "/2/tweets/" + TWEET


def test_reply_validates_text_and_ids():
    provider = _provider(_no_network)
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, TWEET, "  ")
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, "", "hi")


# ---------------------------------------------------------------------------
# capabilities + credentials + error mapping
# ---------------------------------------------------------------------------

def test_declared_capabilities_gate_operations():
    provider = _provider(_no_network)
    for cap in (
        Capability.READ_COMMENTS,
        Capability.READ_MENTIONS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
    ):
        assert provider.supports(cap) is True, cap
    # X has no DM ingestion wired up — nothing to gate, nothing to pretend
    assert not hasattr(provider, "list_messages")


def test_provider_not_configured_without_credentials():
    provider = _provider(_no_network)
    with pytest.raises(ProviderNotConfigured) as exc:
        provider.list_comments({}, post_remote_id=TWEET)
    assert "x:" in str(exc.value)


def test_rate_limit_maps_to_provider_rate_limited():
    provider = _provider(
        lambda request: httpx.Response(
            429, headers={"retry-after": "15"}, text="too many requests"
        )
    )
    with pytest.raises(ProviderRateLimited) as exc:
        provider.list_comments(ACCOUNT, post_remote_id=TWEET)
    assert exc.value.retry_after == 15.0


def test_auth_error_maps_to_provider_not_configured():
    provider = _provider(
        lambda request: httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(ProviderNotConfigured):
        provider.list_comments(ACCOUNT, post_remote_id=TWEET)


# ---------------------------------------------------------------------------
# live smoke (opt-in: -m live + real credentials in the environment)
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("X_BEARER_TOKEN") and os.getenv("X_TEST_TWEET_ID")),
    reason="X_BEARER_TOKEN + X_TEST_TWEET_ID not set",
)
def test_live_x_list_comments():
    provider = XProvider()
    account = {
        "access_token": os.environ["X_BEARER_TOKEN"],
        "workspace_id": "live",
        "external_id": os.getenv("X_USER_ID", ""),
    }
    page = provider.list_comments(
        account, limit=10, post_remote_id=os.environ["X_TEST_TWEET_ID"]
    )
    assert isinstance(page.items, list)
    assert page.next_cursor is None or isinstance(page.next_cursor, str)
    provider.close()
