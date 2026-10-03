"""Work 09 Lane A — platform capability registry contract.

Covers: specs/capabilities/supports/specs()/campaign_platforms/account_platform,
the Work 09 additions (linkedin/x campaign keys resolve), KeyError/ValueError
behavior, honest declared-capability sanity (including that every claimed
PUBLISH_* platform has a registered publisher), and the publisher registration
that backs those claims.

Deterministic: no network — publishers under test get an injected
``httpx.MockTransport``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.engine.campaign.platforms import (
    ACCOUNT_PLATFORM,
    CAMPAIGN_PLATFORMS,
    HANDOFF_PLATFORMS,
    get_profile,
)
from app.engine.platform_registry import (
    IMPLEMENTED_CAPABILITIES,
    Capability,
    PlatformSpec,
    get_registry,
)
from app.providers.social import SOCIAL_PLATFORMS, get_provider

#: Capabilities a provider may implement on the read/reply side.
INBOX_CAPS = frozenset(
    c for c in Capability if c.name.startswith(("READ_", "REPLY_", "DELETE_"))
)
#: Capabilities only a publisher can back.
PUBLISH_CAPS = frozenset(c for c in Capability if c.name.startswith("PUBLISH_"))


# ---------------------------------------------------------------------------
# spec / capabilities / supports / specs
# ---------------------------------------------------------------------------

def test_registry_singleton_and_spec_shape():
    registry = get_registry()
    assert get_registry() is registry

    spec = registry.spec("youtube")
    assert isinstance(spec, PlatformSpec)
    assert spec.platform == "youtube"
    assert spec.has(Capability.READ_COMMENTS)
    assert spec.has("READ_COMMENTS")  # string form coerces
    lo, hi = spec.duration_s
    assert 0 < lo <= hi
    assert spec.aspect_ratios and spec.media and spec.metadata_limits
    assert spec.supports_inbox is True
    assert spec.supports_analytics is True


def test_capabilities_and_supports_agree():
    registry = get_registry()
    caps = registry.capabilities("linkedin")
    assert Capability.READ_COMMENTS in caps
    assert registry.supports("linkedin", Capability.READ_COMMENTS) is True
    assert registry.supports("linkedin", "READ_COMMENTS") is True
    assert registry.supports("linkedin", Capability.DELETE_COMMENT) is False
    assert registry.spec("linkedin").capabilities == caps


def test_specs_sorted_and_complete():
    registry = get_registry()
    specs = registry.specs()
    assert [s.platform for s in specs] == sorted(s.platform for s in specs)
    assert {s.platform for s in specs} == set(ACCOUNT_PLATFORM.values())


# ---------------------------------------------------------------------------
# campaign_platforms / account_platform (Work 09: linkedin + x included)
# ---------------------------------------------------------------------------

def test_all_campaign_platforms_resolve_including_linkedin_x():
    registry = get_registry()
    assert set(registry.campaign_platforms()) == set(CAMPAIGN_PLATFORMS)
    assert {"linkedin", "x"} <= set(CAMPAIGN_PLATFORMS)

    for campaign_key in CAMPAIGN_PLATFORMS:
        account = registry.account_platform(campaign_key)
        spec = registry.spec(account)
        assert campaign_key in spec.campaign_platforms, campaign_key
        assert account in set(ACCOUNT_PLATFORM.values())

    assert registry.account_platform("linkedin") == "linkedin"
    assert registry.account_platform("x") == "x"
    assert registry.account_platform("youtube_shorts") == "youtube"


# ---------------------------------------------------------------------------
# unknown inputs fail closed
# ---------------------------------------------------------------------------

def test_unknown_platform_raises_keyerror():
    registry = get_registry()
    for call in (
        lambda: registry.spec("myspace"),
        lambda: registry.capabilities("myspace"),
        lambda: registry.supports("myspace", Capability.READ_COMMENTS),
        lambda: registry.account_platform("myspace"),
    ):
        with pytest.raises(KeyError):
            call()


def test_unknown_capability_valueerror():
    with pytest.raises(ValueError):
        get_registry().supports("youtube", "PARTY_TIME")


# ---------------------------------------------------------------------------
# declared capabilities are honest
# ---------------------------------------------------------------------------

def test_declared_capabilities_are_sane():
    from app.providers.publishers.factory import _registry as publishers

    assert set(IMPLEMENTED_CAPABILITIES) == set(ACCOUNT_PLATFORM.values())
    for platform, caps in IMPLEMENTED_CAPABILITIES.items():
        assert isinstance(caps, frozenset), platform
        # Work 14: snapchat declares NO legacy capability on purpose -- a user
        # handoff implements none of the autonomous ones, and it carries
        # USER_HANDOFF in the Work 14 vocabulary instead. An empty legacy set is
        # therefore legitimate, but only when a handoff is declared.
        if not caps:
            assert platform in HANDOFF_PLATFORMS, (
                f"{platform} declares no capability and is not a handoff")
            continue
        assert all(isinstance(c, Capability) for c in caps), platform
        # a PUBLISH_* claim must be backed by a registered publisher
        if caps & PUBLISH_CAPS:
            assert platform in publishers, f"{platform} claims PUBLISH_* but has no publisher"

    # Work 09 additions are declared, not silently absent
    assert Capability.READ_COMMENTS in IMPLEMENTED_CAPABILITIES["linkedin"]
    assert Capability.REPLY_COMMENT in IMPLEMENTED_CAPABILITIES["linkedin"]
    assert Capability.READ_COMMENTS in IMPLEMENTED_CAPABILITIES["x"]
    assert Capability.READ_MENTIONS in IMPLEMENTED_CAPABILITIES["x"]
    # TikTok claims only what social/tiktok.py implements (list + delete)
    assert Capability.REPLY_COMMENT not in IMPLEMENTED_CAPABILITIES["tiktok"]
    # READ_MESSAGES is claimed nowhere — no DM ingestion exists in this repo
    assert all(
        Capability.READ_MESSAGES not in caps
        for caps in IMPLEMENTED_CAPABILITIES.values()
    )


def test_provider_capabilities_match_registry_inbox_declares():
    """Each provider's frozenset == the registry's inbox slice for that platform."""
    registry = get_registry()
    for platform in SOCIAL_PLATFORMS:
        provider = get_provider(platform)
        assert provider.platform == platform
        assert provider.capabilities == (registry.capabilities(platform) & INBOX_CAPS), platform


def test_inbox_and_analytics_flags_follow_capabilities():
    registry = get_registry()
    for spec in registry.specs():
        assert spec.supports_inbox == bool(spec.capabilities & INBOX_CAPS), spec.platform
        assert spec.supports_analytics == (Capability.FETCH_METRICS in spec.capabilities)
    assert registry.spec("linkedin").supports_inbox is True
    assert registry.spec("x").supports_inbox is True


def test_profile_numbers_come_from_campaign_profiles():
    registry = get_registry()
    for spec in registry.specs():
        profile = get_profile(spec.campaign_platforms[0])
        assert spec.aspect_ratios == tuple(profile["aspects"]), spec.platform
        assert spec.metadata_limits == profile["metadata"], spec.platform
        assert spec.duration_s == (
            float(profile["preferred_duration"][0]),
            float(profile["max_duration"]),
        ), spec.platform


# ---------------------------------------------------------------------------
# publisher registration (backs the PUBLISH_* claims)
# ---------------------------------------------------------------------------

def test_linkedin_and_x_publishers_registered():
    from app.providers.publishers.factory import _registry, get_publisher
    from app.providers.publishers.linkedin import LinkedInPublisher
    from app.providers.publishers.x import XPublisher

    assert isinstance(_registry["linkedin"], LinkedInPublisher)
    assert isinstance(_registry["x"], XPublisher)
    assert get_publisher("linkedin", has_account=True) is _registry["linkedin"]
    assert get_publisher("x", has_account=True) is _registry["x"]


def test_publishers_fail_honestly_without_credentials():
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.linkedin import LinkedInPublisher
    from app.providers.publishers.x import XPublisher

    meta = PublishMetadata(title="t", description="d")
    for publisher in (LinkedInPublisher(), XPublisher()):
        result = publisher.publish("", meta, {})
        assert result.success is False, publisher.platform
        assert "missing credentials" in result.error


def test_linkedin_text_publish_posts_to_rest_posts():
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.linkedin import LinkedInPublisher

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"}, json={})

    publisher = LinkedInPublisher(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = publisher.publish(
        "",
        PublishMetadata(title="Hello", description="world"),
        {"access_token": "tok", "external_id": "999"},
    )
    assert result.success is True
    assert result.remote_post_id == "urn:li:share:42"
    request = seen[0]
    assert request.url.path == "/rest/posts"
    body = json.loads(request.content)
    assert body["author"] == "urn:li:person:999"
    assert "content" not in body  # text-only post carries no media
    assert request.headers["Authorization"] == "Bearer tok"


def test_x_text_publish_posts_to_v2_tweets():
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.x import XPublisher

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"id": "t-1"}})

    publisher = XPublisher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = publisher.publish(
        "",
        PublishMetadata(title="Markets", description="up today", hashtags=["#stocks"]),
        {"access_token": "tok"},
    )
    assert result.success is True
    assert result.remote_post_id == "t-1"
    assert result.remote_url == "https://x.com/i/status/t-1"
    request = seen[0]
    assert request.url.path == "/2/tweets"
    body = json.loads(request.content)
    assert body["text"].startswith("Markets up today")
    assert "media" not in body
    assert request.headers["Authorization"] == "Bearer tok"
