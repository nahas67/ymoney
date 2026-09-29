"""Social inbox providers — one official-API implementation per platform.

``get_provider(platform)`` is the single entry point the community sync,
moderation and inbox lanes import **lazily**::

    from app.providers.social import get_provider
    provider = get_provider("youtube")

Concrete providers live next to this module and declare their capabilities
honestly (see :mod:`app.engine.platform_registry` for the platform-level
view). Unknown platforms raise ``KeyError``; missing credentials raise
``ProviderNotConfigured`` — never simulated data.

Optional ``client=``/``transport=`` keywords exist for tests (an ``httpx``
``MockTransport``); production callers never pass them. Provider modules are
imported lazily inside ``get_provider`` so importing this package stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.providers.social.base import (
    MAX_PAGE_SIZE,
    CommentItem,
    Page,
    ProviderCapabilityError,
    ProviderNotConfigured,
    ProviderRateLimited,
    Receipt,
    SocialPlatformProvider,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

#: Account platforms that have a social (inbox) provider in this package.
SOCIAL_PLATFORMS: tuple[str, ...] = (
    "facebook",
    "instagram",
    "linkedin",
    "tiktok",
    "x",
    "youtube",
)

_TABLE: dict[str, type[SocialPlatformProvider]] | None = None


def _provider_table() -> dict[str, type[SocialPlatformProvider]]:
    """Platform -> provider class (built once; imports stay lazy)."""
    global _TABLE
    if _TABLE is None:
        from app.providers.social.facebook import FacebookProvider
        from app.providers.social.instagram import InstagramProvider
        from app.providers.social.linkedin import LinkedInProvider
        from app.providers.social.tiktok import TikTokProvider
        from app.providers.social.x import XProvider
        from app.providers.social.youtube import YouTubeProvider

        _TABLE = {
            "facebook": FacebookProvider,
            "instagram": InstagramProvider,
            "linkedin": LinkedInProvider,
            "tiktok": TikTokProvider,
            "x": XProvider,
            "youtube": YouTubeProvider,
        }
    return _TABLE


def get_provider(
    platform: str,
    *,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SocialPlatformProvider:
    """Return a fresh provider for ``platform``.

    Raises ``KeyError`` for an unknown platform (same contract as
    ``platform_registry.spec``) so callers can tell "not a platform" apart
    from "platform exists but is not configured" (``ProviderNotConfigured``).
    """
    key = str(platform or "").strip().lower()
    table = _provider_table()
    try:
        cls = table[key]
    except KeyError:
        raise KeyError(
            f"unknown social platform {platform!r}; pick from {sorted(table)}"
        ) from None
    return cls(client=client, transport=transport)


__all__ = [
    "MAX_PAGE_SIZE",
    "SOCIAL_PLATFORMS",
    "CommentItem",
    "Page",
    "ProviderCapabilityError",
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "Receipt",
    "SocialPlatformProvider",
    "get_provider",
]
