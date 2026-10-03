"""Platform capability registry (Work 09, Lane A).

One place that answers "what can this platform actually do in YMONEY?" so
campaign/inbox/analytics logic never scatters ``if platform == ...`` checks.

Design rules
------------
* **Honest capabilities only.** A :class:`Capability` is declared for a
  platform only when YMONEY has a real implementation against an official
  platform API (or an existing in-repo provider). Undeclared means
  "not implemented here" — callers must fail closed, never pretend.
* **Single source of truth for numbers.** Media/format constraints and
  metadata limits are read straight from
  :mod:`app.engine.campaign.platforms` (``PLATFORM_PROFILES`` /
  ``CAMPAIGN_PLATFORMS`` / ``ACCOUNT_PLATFORM``); nothing is duplicated.
* Registry data is derived once at import and treated as read-only.

Mapping
-------
``campaign platform`` (e.g. ``youtube_shorts``) is what Work 04 campaigns
target; ``account platform`` (e.g. ``youtube``) is the ``SocialAccount``
namespace publishers/inbox use. ``ACCOUNT_PLATFORM`` is the canonical map.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.engine.campaign.platforms import (
    ACCOUNT_PLATFORM,
    CAMPAIGN_PLATFORMS,
    get_profile,
)


class Capability(StrEnum):
    """Declared, implemented platform capability."""

    PUBLISH_VIDEO = "PUBLISH_VIDEO"
    PUBLISH_SHORT = "PUBLISH_SHORT"
    PUBLISH_IMAGE = "PUBLISH_IMAGE"
    PUBLISH_TEXT = "PUBLISH_TEXT"
    READ_COMMENTS = "READ_COMMENTS"
    REPLY_COMMENT = "REPLY_COMMENT"
    READ_MENTIONS = "READ_MENTIONS"
    READ_MESSAGES = "READ_MESSAGES"
    DELETE_COMMENT = "DELETE_COMMENT"

    def __str__(self) -> str:
        # StrEnum-style: str(Capability.X) == "X" so callers/logs serialize
        # the plain value instead of "Capability.X".
        return self.value
    FETCH_METRICS = "FETCH_METRICS"

    # -- Work 14 distribution vocabulary (§1) ----------------------------
    # Deliberately ADDITIVE rather than aliases of the Work 09 names above.
    # PUBLISH_SHORT and PUBLISH_VIDEO are genuinely different claims (a
    # platform may accept long video but not Shorts), so collapsing them would
    # silently widen what existing platforms claim. These names answer the
    # distribution question ("what can this platform do with content?") and
    # :func:`assert_vocabularies_agree` proves the two sets never disagree.
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    CAROUSEL = "CAROUSEL"
    REPLY = "REPLY"
    LINK = "LINK"
    ALT_TEXT = "ALT_TEXT"
    COMMENTS = "COMMENTS"
    METRICS = "METRICS"
    #: An official API accepts the publish and returns a remote id from a
    #: server, with no human step.
    DIRECT_PUBLISH = "DIRECT_PUBLISH"
    #: Media is prepared and verified; a HUMAN publishes it in the app.
    USER_HANDOFF = "USER_HANDOFF"


#: Work 14 capability -> the Work 09 capability that must also be declared.
#: Used by the consistency check so the two vocabularies can never drift.
_DISTRIBUTION_VOCABULARY: dict[Capability, Capability | None] = {
    Capability.TEXT: Capability.PUBLISH_TEXT,
    Capability.IMAGE: Capability.PUBLISH_IMAGE,
    Capability.VIDEO: Capability.PUBLISH_VIDEO,
    Capability.CAROUSEL: None,          # no Work 09 equivalent
    Capability.REPLY: Capability.REPLY_COMMENT,
    Capability.LINK: None,              # no Work 09 equivalent
    Capability.ALT_TEXT: None,          # no Work 09 equivalent
    Capability.COMMENTS: Capability.READ_COMMENTS,
    Capability.METRICS: Capability.FETCH_METRICS,
    Capability.DIRECT_PUBLISH: None,    # implied by any PUBLISH_* above
    Capability.USER_HANDOFF: None,      # no Work 09 equivalent
}

#: New Work 14 vocabulary members (everything the task asked for).
DISTRIBUTION_CAPABILITIES: frozenset[Capability] = frozenset(
    _DISTRIBUTION_VOCABULARY)


def assert_vocabularies_agree(platform: str,
                             caps: frozenset[Capability]) -> None:
    """Fail loudly if a platform claims a distribution capability it has no
    Work 09 counterpart for.

    This is the guard against the two vocabularies drifting: claiming ``VIDEO``
    without ``PUBLISH_VIDEO`` would let the distribution layer promise a
    publish the legacy publish layer has no contract for.
    """
    for dist, legacy in _DISTRIBUTION_VOCABULARY.items():
        if dist in caps and legacy is not None and legacy not in caps:
            raise RuntimeError(
                f"{platform} declares {dist} but not its Work 09 counterpart "
                f"{legacy}; the two capability vocabularies must agree")


#: Inbox-ish capabilities (a platform "supports inbox" when it has one).
_INBOX_CAPS = frozenset({
    Capability.READ_COMMENTS,
    Capability.REPLY_COMMENT,
    Capability.READ_MENTIONS,
    Capability.READ_MESSAGES,
})

#: Capabilities YMONEY actually implements per **account** platform.
#:
#: Sources (all real code in this repo):
#:   youtube    providers/publishers/platforms.py (videos.insert) +
#:              providers/social/youtube.py (commentThreads/comments) +
#:              providers/analytics (Data API + Analytics API)
#:   tiktok     publishers/platforms.py (Content Posting API) +
#:              social/tiktok.py (Business API comment list/delete) +
#:              providers/analytics (Display API query)
#:   facebook   publishers/platforms.py (Graph reels) + social/facebook.py
#:              (Graph comments) + providers/analytics (Graph insights)
#:   instagram  publishers/platforms.py (Graph reels container) +
#:              social/instagram.py (Graph comments) + providers/analytics
#:   linkedin   publishers/linkedin.py (Assets API + /rest/posts: video,
#:              image, text) + social/linkedin.py (rest/socialActions)
#:   x          publishers/x.py (POST /2/tweets + v1.1 media upload) +
#:              social/x.py (search/replies/mentions/tweets delete)
#:
#: NOT declared anywhere (never implemented, so never claimed):
#:   READ_MESSAGES — no DM/message ingestion is implemented on any platform.
#:   READ_MENTIONS — only X implements it (others have no official endpoint
#:                   wired up).
#:   FETCH_METRICS — only where an in-repo analytics provider exists
#:                   (LinkedIn/X metrics providers are not implemented yet).
#:   PUBLISH_IMAGE/PUBLISH_TEXT — only where the publisher implements it
#:                   (LinkedIn, X; Instagram image posts are official but
#:                   NOT implemented by InstagramPublisher, so undeclared).
#:   REPLY_COMMENT on TikTok — /v2/video/comment/publish/ needs the parent
#:                   video_id alongside the comment id, which the reply
#:                   contract (account, remote_id, text) cannot carry; the
#:                   provider implements list+delete only, so no reply is
#:                   claimed.
IMPLEMENTED_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "youtube": frozenset({
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
        Capability.FETCH_METRICS,
    }),
    "tiktok": frozenset({
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.DELETE_COMMENT,
        Capability.FETCH_METRICS,
    }),
    "facebook": frozenset({
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
        Capability.FETCH_METRICS,
    }),
    "instagram": frozenset({
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.DELETE_COMMENT,
        Capability.FETCH_METRICS,
    }),
    "linkedin": frozenset({
        Capability.PUBLISH_TEXT,
        Capability.PUBLISH_IMAGE,
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
    }),
    "x": frozenset({
        Capability.PUBLISH_TEXT,
        Capability.PUBLISH_IMAGE,
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.READ_MENTIONS,
        Capability.DELETE_COMMENT,
    }),
    # -- Work 14: the Work 09 half of the new platforms -------------------
    # These are the LEGACY capability names, declared only where the Work 14
    # provider really implements them against the official API. The Work 14
    # vocabulary additions live in WORK14_CAPABILITIES below.
    #
    # threads -- POST /{threads-user-id}/threads (media_type) then
    #   /threads_publish; replies via the same pair with reply_to_id;
    #   GET /{media-id}/replies for reads; /insights for metrics.
    # bluesky -- createRecord on app.bsky.feed.post; replies via replyRef;
    #   getPostThread for reads. FETCH_METRICS is deliberately absent: no
    #   documented AppView metric set/limit to implement honestly.
    "threads": frozenset({
        Capability.PUBLISH_TEXT,
        Capability.PUBLISH_IMAGE,
        Capability.PUBLISH_VIDEO,
        Capability.PUBLISH_SHORT,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
        Capability.FETCH_METRICS,
    }),
    "bluesky": frozenset({
        Capability.PUBLISH_TEXT,
        Capability.PUBLISH_IMAGE,
        Capability.PUBLISH_VIDEO,
        Capability.READ_COMMENTS,
        Capability.REPLY_COMMENT,
    }),
    # pinterest -- POST /v5/pins (image_url | image_base64 | video_id |
    #   multiple_image_*), the documented staged video upload
    #   (POST /v5/media -> presigned S3 -> poll until 'succeeded'), board
    #   selection via GET /v5/boards, and DELETE /v5/pins/{id}.
    #   FETCH_METRICS is declared: GET /v5/pins/{id}/analytics documents an
    #   exact metric enum (IMPRESSION, SAVE, OUTBOUND_CLICK, PIN_CLICK, ...).
    "pinterest": frozenset({
        Capability.PUBLISH_TEXT,
        Capability.PUBLISH_IMAGE,
        Capability.PUBLISH_VIDEO,
        Capability.FETCH_METRICS,
    }),
    # snapchat -- intentionally EMPTY. A user handoff implements none of the
    # autonomous Work 09 capabilities; USER_HANDOFF is declared in the Work 14
    # map instead.
    "snapchat": frozenset(),
}

# ---------------------------------------------------------------------------
# Work 14 distribution declarations (§1)
# ---------------------------------------------------------------------------
# Declared ONLY where this repo implements the capability against the official
# API, and only where the official docs state the permission/endpoint exists.
#
# threads  -- Meta Threads API (graph.threads.net/v1.0). One container
#   endpoint POST /{threads-user-id}/threads with media_type, published via
#   POST /{threads-user-id}/threads_publish. Carousel is officially supported
#   (2..20 children). Alt text officially supported on image/video/carousel.
#   Replies: threads_manage_replies, and a THIRD-PARTY reply additionally needs
#   threads_keyword_search or threads_manage_mentions -- so REPLY is declared
#   but the provider gates third-party replies.
#   insights: likes/replies/reposts/quotes are GA; `impressions` is NOT a
#   queryable metric and `clicks` is user-level only, so neither is claimed.
#
# bluesky  -- AT Protocol. app.bsky.feed.post via com.atproto.repo.createRecord
#   returns uri + cid (both persisted). Images (alt REQUIRED, 2MB, max 4),
#   video (app.bsky.embed.video via video.bsky.app), quotes
#   (app.bsky.embed.record) and external cards (uri+title+description all
#   mandatory) are officially supported. Replies use replyRef with BOTH root
#   and parent strongRefs. getPostThread lists replies (getReplies does not
#   exist) so COMMENTS is declared.
#
# snapchat -- NO DIRECT_PUBLISH. Organic publishing is a USER_HANDOFF: the
#   official Snap Kit / Share Kit surfaces all end in the app where a human
#   taps send. An official Public Profile publishing API DOES exist
#   (businessapi.snapchat.com POST /v1/public_profiles/{id}/stories and
#   .../spotlights) but is ALLOWLIST-ONLY, requires a Snap Business
#   Organization, and Snap's own docs contradict themselves on whether that
#   surface is read-only -- and the required scope is UNVERIFIED. It is
#   therefore recorded and contract-tested but NOT enabled, so no
#   DIRECT_PUBLISH is declared. FETCH_METRICS is likewise allowlist-gated.
#
# pinterest -- see PINTEREST_CAPABILITIES below.
WORK14_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "threads": frozenset({
        Capability.TEXT,
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.CAROUSEL,
        Capability.REPLY,
        Capability.LINK,
        Capability.ALT_TEXT,
        Capability.COMMENTS,
        Capability.METRICS,
        Capability.DIRECT_PUBLISH,
    }),
    "bluesky": frozenset({
        Capability.TEXT,
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.LINK,
        Capability.ALT_TEXT,
        Capability.COMMENTS,
        Capability.DIRECT_PUBLISH,
    }),
    # pinterest -- image Pins, video Pins (staged upload) and 2..5-item
    # carousels. TITLE is part of the Pin body, not a separate concept.
    # REPLY and COMMENTS are NOT declared: Pinterest has no documented
    # comment/reply API for organic Pins.
    "pinterest": frozenset({
        Capability.TEXT,
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.CAROUSEL,
        Capability.LINK,
        Capability.ALT_TEXT,
        Capability.METRICS,
        Capability.DIRECT_PUBLISH,
    }),
    "snapchat": frozenset({
        Capability.USER_HANDOFF,
    }),
}


@dataclass(frozen=True)
class PlatformSpec:
    """Everything one account platform supports, in one read-only value."""

    platform: str
    campaign_platforms: tuple[str, ...]
    capabilities: frozenset[Capability]
    media: dict
    metadata_limits: dict
    aspect_ratios: tuple[str, ...]
    duration_s: tuple[float, float]  # (preferred min seconds, hard max seconds)
    supports_inbox: bool
    supports_analytics: bool

    def has(self, cap: Capability | str) -> bool:
        return _coerce(cap) in self.capabilities


def _coerce(cap: Capability | str) -> Capability:
    if isinstance(cap, Capability):
        return cap
    try:
        return Capability(str(cap))
    except ValueError as exc:
        raise ValueError(
            f"unknown capability {cap!r}; pick from {[c.value for c in Capability]}"
        ) from exc


class PlatformCapabilityRegistry:
    """Read-only lookup over the derived platform specs."""

    def __init__(self, specs: dict[str, PlatformSpec] | None = None) -> None:
        self._specs: dict[str, PlatformSpec] = dict(specs or _build_specs())

    def spec(self, platform: str) -> PlatformSpec:
        """Return the spec; KeyError when the account platform is unknown."""
        try:
            return self._specs[platform]
        except KeyError as exc:
            raise KeyError(
                f"unknown platform {platform!r}; pick from {sorted(self._specs)}"
            ) from exc

    def capabilities(self, platform: str) -> frozenset[Capability]:
        return self.spec(platform).capabilities

    def supports(self, platform: str, cap: Capability | str) -> bool:
        return _coerce(cap) in self.spec(platform).capabilities

    def specs(self) -> list[PlatformSpec]:
        return [self._specs[p] for p in sorted(self._specs)]

    def campaign_platforms(self) -> tuple[str, ...]:
        """Campaign keys Work 04 campaigns may target (CAMPAIGN_PLATFORMS)."""
        return CAMPAIGN_PLATFORMS

    def account_platform(self, campaign_platform: str) -> str:
        """Campaign key -> SocialAccount namespace; KeyError when unknown."""
        try:
            return ACCOUNT_PLATFORM[campaign_platform]
        except KeyError as exc:
            raise KeyError(
                f"unknown campaign platform {campaign_platform!r}; "
                f"pick from {sorted(ACCOUNT_PLATFORM)}"
            ) from exc


def _profile_for(account_platform: str) -> dict:
    """Primary campaign profile for an account platform (first key wins).

    ``youtube`` maps to two campaign keys (shorts + longform); the shorts
    profile is the primary because every campaign target is a short-form
    key, while ``youtube_longform`` only feeds master validation.
    """
    for campaign_key, acct in ACCOUNT_PLATFORM.items():
        if acct == account_platform:
            return get_profile(campaign_key)
    raise KeyError(f"no campaign profile maps to account platform {account_platform!r}")


def _build_specs() -> dict[str, PlatformSpec]:
    account_platforms = tuple(dict.fromkeys(ACCOUNT_PLATFORM.values()))
    missing = [p for p in account_platforms if p not in IMPLEMENTED_CAPABILITIES]
    if missing:
        # A platform joined ACCOUNT_PLATFORM without an honest capability
        # declaration — refuse to build a spec that silently claims nothing.
        raise RuntimeError(
            f"no capability declaration for account platform(s) {missing}; "
            "declare IMPLEMENTED_CAPABILITIES honestly or remove the mapping"
        )
    specs: dict[str, PlatformSpec] = {}
    for account_platform in account_platforms:
        profile = _profile_for(account_platform)
        campaign_keys = tuple(
            k for k, acct in ACCOUNT_PLATFORM.items() if acct == account_platform
        )
        capabilities = IMPLEMENTED_CAPABILITIES[account_platform]
        # Work 14: merge the distribution vocabulary and prove the two sets
        # never disagree before any spec is handed out.
        capabilities = frozenset(capabilities | WORK14_CAPABILITIES.get(
            account_platform, frozenset()))
        assert_vocabularies_agree(account_platform, capabilities)
        media = {k: v for k, v in profile.items() if k != "metadata"}
        specs[account_platform] = PlatformSpec(
            platform=account_platform,
            campaign_platforms=campaign_keys,
            capabilities=frozenset(capabilities),
            media=media,
            metadata_limits=dict(profile["metadata"]),
            aspect_ratios=tuple(profile["aspects"]),
            duration_s=(float(profile["preferred_duration"][0]),
                        float(profile["max_duration"])),
            supports_inbox=bool(capabilities & _INBOX_CAPS),
            supports_analytics=Capability.FETCH_METRICS in capabilities,
        )
    return specs


_REGISTRY: PlatformCapabilityRegistry | None = None


def get_registry() -> PlatformCapabilityRegistry:
    """Module-level singleton (cheap: specs are derived once)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PlatformCapabilityRegistry()
    return _REGISTRY


__all__ = [
    "Capability",
    "IMPLEMENTED_CAPABILITIES",
    "PlatformCapabilityRegistry",
    "PlatformSpec",
    "get_registry",
]
