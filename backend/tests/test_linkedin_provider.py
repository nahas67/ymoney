"""Work 09 Lane A — LinkedIn provider contract tests (injected transport).

Contract coverage: threaded comment reads with offset-cursor resume, nested
reply flattening, activity-URN derivation for replies, capability gating,
credential failures. Live smoke at the bottom is ``-m live`` AND credential
gated — it never runs in the default suite and never runs without real creds.
"""
from __future__ import annotations

import json
import os
from urllib.parse import unquote

import httpx
import pytest

from app.providers.social import get_provider
from app.providers.social.base import (
    ProviderCapabilityError,
    ProviderNotConfigured,
    ProviderRateLimited,
)
from app.providers.social.linkedin import LinkedInProvider

ACCOUNT = {"workspace_id": "ws-1", "external_id": "42", "access_token": "li-token"}
ACTIVITY = "urn:li:activity:7001"
COMMENT_URN = "urn:li:comment:(activity:7001,comment:8001)"
EXPECTED_PATH = "/rest/socialActions/urn:li:activity:7001/comments"


def _path(request: httpx.Request) -> str:
    """Percent-decoded path (httpx ``raw_path`` carries the query too)."""
    return unquote(request.url.raw_path.decode().split("?", 1)[0])


def _comment(
    remote_id: str,
    text: str,
    *,
    parent: str | None = None,
    replies: list[dict] | None = None,
    created_ms: int = 1757000000000,
    author: str = "urn:li:person:9",
) -> dict:
    element: dict = {
        "id": remote_id,
        "message": {"text": text},
        "author": author,
        "createdAt": created_ms,
    }
    if parent:
        element["parentComment"] = parent
    if replies:
        element["elements"] = replies
    return element


def _provider(handler) -> LinkedInProvider:
    return get_provider("linkedin", transport=httpx.MockTransport(handler))


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call: {request.method} {request.url}")


# ---------------------------------------------------------------------------
# READ_COMMENTS
# ---------------------------------------------------------------------------

def test_list_comments_pagination_and_cursor_resume():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.params.get("startingAt") == "2":
            return httpx.Response(
                200,
                json={
                    "elements": [_comment("c3", "third")],
                    "paging": {"count": 1, "start": 2, "total": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "elements": [_comment("c1", "first"), _comment("c2", "second")],
                "paging": {"count": 2, "start": 0, "total": 3},
            },
        )

    provider = _provider(handler)
    page1 = provider.list_comments(ACCOUNT, limit=2, post_remote_id=ACTIVITY)
    assert [i.remote_id for i in page1.items] == ["c1", "c2"]
    assert page1.next_cursor == "2"

    page2 = provider.list_comments(
        ACCOUNT, cursor=page1.next_cursor, limit=2, post_remote_id=ACTIVITY
    )
    assert [i.remote_id for i in page2.items] == ["c3"]
    assert page2.next_cursor is None

    # wire shape: Rest.li-URL-encoded activity URN + threadedReplies query
    assert _path(calls[0]) == EXPECTED_PATH
    assert calls[0].url.params.get("q") == "threadedReplies"
    assert calls[0].url.params.get("count") == "2"
    assert calls[1].url.params.get("startingAt") == "2"  # cursor verbatim
    assert calls[0].headers["Authorization"] == "Bearer li-token"
    assert calls[0].headers["LinkedIn-Version"] == LinkedInProvider.VERSION
    assert calls[0].headers["X-Restli-Protocol-Version"] == "2.0.0"


def test_list_comments_maps_fields_and_flattens_nested_replies():
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={
                "elements": [
                    _comment(
                        "top",
                        "hello there",
                        replies=[_comment("nested", "a reply", parent="top")],
                    )
                ],
                "paging": {"count": 2, "start": 0, "total": 2},
            },
        )
    )
    page = provider.list_comments(ACCOUNT, post_remote_id=ACTIVITY)
    assert [i.remote_id for i in page.items] == ["top", "nested"]
    top, nested = page.items
    assert top.text == "hello there"
    assert top.parent_remote_id is None
    assert top.author_remote_id == "urn:li:person:9"
    assert top.created_at is not None and top.created_at.tzinfo is not None
    assert nested.parent_remote_id == "top"
    assert top.thread_id == ACTIVITY and top.post_remote_id == ACTIVITY


def test_list_comments_requires_post_and_valid_cursor():
    provider = _provider(_no_network)
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT)
    with pytest.raises(ValueError):
        provider.list_comments(ACCOUNT, cursor="not-an-offset", post_remote_id=ACTIVITY)


def test_bare_activity_id_is_normalized():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"elements": [], "paging": {}})

    provider = _provider(handler)
    provider.list_comments(ACCOUNT, post_remote_id="7001")
    assert _path(calls[0]) == EXPECTED_PATH


# ---------------------------------------------------------------------------
# REPLY_COMMENT
# ---------------------------------------------------------------------------

def test_reply_targets_activity_endpoint_with_parent_comment():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201, json={"id": "urn:li:comment:(activity:7001,comment:9001)"}
        )

    provider = _provider(handler)
    receipt = provider.reply_to_comment(ACCOUNT, COMMENT_URN, "Nice point!")
    assert receipt.remote_reply_id == "urn:li:comment:(activity:7001,comment:9001)"
    assert receipt.mock is False
    assert receipt.raw["id"].startswith("urn:li:comment:")

    request = calls[0]
    assert request.method == "POST"
    assert _path(request) == EXPECTED_PATH
    body = json.loads(request.content)
    assert body["parentComment"] == COMMENT_URN
    assert body["message"]["text"] == "Nice point!"
    assert request.headers["Authorization"] == "Bearer li-token"


def test_reply_without_derivable_activity_is_an_honest_error():
    provider = _provider(_no_network)
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, "not-a-comment-urn", "hi")


def test_reply_validates_text_and_ids():
    provider = _provider(_no_network)
    with pytest.raises(ValueError):
        provider.reply_to_comment(ACCOUNT, COMMENT_URN, "   ")


# ---------------------------------------------------------------------------
# capabilities + credentials
# ---------------------------------------------------------------------------

def test_declared_capabilities_gate_operations():
    provider = _provider(_no_network)
    assert provider.supports("READ_COMMENTS") is True
    assert provider.supports("REPLY_COMMENT") is True
    assert provider.supports("DELETE_COMMENT") is False
    assert provider.supports("READ_MENTIONS") is False
    with pytest.raises(ProviderCapabilityError):
        provider.delete_comment(ACCOUNT, COMMENT_URN)
    with pytest.raises(ProviderCapabilityError):
        provider.list_mentions(ACCOUNT)


def test_provider_not_configured_without_credentials():
    provider = _provider(_no_network)
    with pytest.raises(ProviderNotConfigured) as exc:
        provider.list_comments({}, post_remote_id=ACTIVITY)
    assert "linkedin" in str(exc.value)


def test_rate_limit_maps_to_provider_rate_limited():
    provider = _provider(
        lambda request: httpx.Response(
            429, headers={"retry-after": "30"}, text="throttled"
        )
    )
    with pytest.raises(ProviderRateLimited) as exc:
        provider.list_comments(ACCOUNT, post_remote_id=ACTIVITY)
    assert exc.value.retry_after == 30.0


# ---------------------------------------------------------------------------
# live smoke (opt-in: -m live + real credentials in the environment)
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("LINKEDIN_ACCESS_TOKEN") and os.getenv("LINKEDIN_ACTIVITY_URN")),
    reason="LINKEDIN_ACCESS_TOKEN + LINKEDIN_ACTIVITY_URN not set",
)
def test_live_linkedin_list_comments():
    provider = LinkedInProvider()
    account = {
        "access_token": os.environ["LINKEDIN_ACCESS_TOKEN"],
        "workspace_id": "live",
        "external_id": "",
    }
    page = provider.list_comments(
        account, limit=5, post_remote_id=os.environ["LINKEDIN_ACTIVITY_URN"]
    )
    assert isinstance(page.items, list)
    assert page.next_cursor is None or isinstance(page.next_cursor, str)
    provider.close()
