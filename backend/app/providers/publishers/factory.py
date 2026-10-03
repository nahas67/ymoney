"""Publisher factory — PRODUCTION ONLY.

Three resolution paths, checked in order by the Publisher Agent:
  1. UploadPostRelay when key is configured (handles tiktok/ig/yt/fb)
  2. Direct platform publishers when accounts have OAuth tokens
  3. PublishingBlocked error with remediation

There is NO mock publisher in the product. Tests inject their own doubles.
"""

from __future__ import annotations

from app.providers.publishers.base import BasePublisher, PublishMetadata, PublishResult
from app.providers.publishers.bluesky import BlueskyPublisher
from app.providers.publishers.linkedin import LinkedInPublisher
from app.providers.publishers.pinterest import PinterestPublisher
from app.providers.publishers.platforms import (
    FacebookPagePublisher,
    InstagramPublisher,
    TikTokPublisher,
    UploadPostRelay,
    YouTubePublisher,
)
from app.providers.publishers.snapchat import SnapchatHandoffPublisher
from app.providers.publishers.threads import ThreadsPublisher
from app.providers.publishers.x import XPublisher

_registry: dict[str, BasePublisher] = {
    "youtube": YouTubePublisher(),
    "tiktok": TikTokPublisher(),
    "facebook": FacebookPagePublisher(),
    "instagram": InstagramPublisher(),
    # Work 09: native LinkedIn/X publishers (no relay required).
    "linkedin": LinkedInPublisher(),
    "x": XPublisher(),
    # Work 14: expanded distribution, all against official platform APIs.
    "threads": ThreadsPublisher(),
    "pinterest": PinterestPublisher(),
    "bluesky": BlueskyPublisher(),
    # Snapchat is a USER_HANDOFF provider: it prepares media and returns a
    # handoff record. It NEVER publishes autonomously, which is why it is
    # registered separately from DIRECT_PUBLISH platforms and why the
    # publish flow treats it as a different outcome.
    "snapchat": SnapchatHandoffPublisher(),
}

#: Platforms whose publisher cannot produce a live publication on its own.
#: The publish flow reads this instead of branching on the platform name, so
#: no `if platform == "snapchat"` logic is scattered through campaign code.
HANDOFF_PLATFORMS: frozenset[str] = frozenset(
    name for name, publisher in _registry.items()
    if getattr(publisher, "handoff_only", False))


class PublishingBlocked(Exception):
    def __init__(self, platform: str, reason: str = "AUTHENTICATION REQUIRED"):
        super().__init__(f"{platform}: {reason}")
        self.platform = platform
        self.remediation = (
            "Publishing → Connect account (OAuth or relay), "
            "or add Upload-Post API key under Settings → Connections."
        )


def _relay_config() -> tuple[str | None, str | None]:
    from app.services.provider_settings import upload_post_config

    return upload_post_config()


def relay_ready() -> bool:
    key, user = _relay_config()
    return bool(key and user)


def get_publisher(platform: str, *, has_account: bool = False) -> BasePublisher:
    if has_account and platform in _registry:
        return _registry[platform]
    if relay_ready():
        return UploadPostRelay(*_relay_config())
    raise PublishingBlocked(platform)


__all__ = [
    "HANDOFF_PLATFORMS",
    "PublishMetadata",
    "PublishResult",
    "PublishingBlocked",
    "get_publisher",
    "relay_ready",
]
