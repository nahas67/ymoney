"""Publisher factory — PRODUCTION ONLY.

Three resolution paths, checked in order by the Publisher Agent:
  1. UploadPostRelay when key is configured (handles tiktok/ig/yt/fb)
  2. Direct platform publishers when accounts have OAuth tokens
  3. PublishingBlocked error with remediation

There is NO mock publisher in the product. Tests inject their own doubles.
"""

from __future__ import annotations

from app.providers.publishers.base import BasePublisher, PublishMetadata, PublishResult
from app.providers.publishers.platforms import (
    FacebookPagePublisher,
    TikTokPublisher,
    UploadPostRelay,
    YouTubePublisher,
)

_registry: dict[str, BasePublisher] = {
    "youtube": YouTubePublisher(),
    "tiktok": TikTokPublisher(),
    "facebook": FacebookPagePublisher(),
}


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
    "PublishMetadata",
    "PublishResult",
    "PublishingBlocked",
    "get_publisher",
    "relay_ready",
]
