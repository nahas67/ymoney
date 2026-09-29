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
