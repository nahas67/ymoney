"""Platform format requirements (normalized, single source of truth).

Aspect ratio is chosen per target platform so agents never hardcode guesses.
"""

from __future__ import annotations

PLATFORM_ASPECT: dict[str, str] = {
    "youtube": "9:16",      # Shorts
    "tiktok": "9:16",
    "facebook": "9:16",     # Reels
    "instagram": "9:16",    # Reels
}

SUPPORTED_PLATFORMS = tuple(PLATFORM_ASPECT.keys())


def aspect_for_platforms(platforms: list[str] | None) -> str:
    """Aspect for a set of target platforms; vertical unless every target is absent."""
    if not platforms:
        return "9:16"
    aspects = {PLATFORM_ASPECT.get(p.strip().lower(), "9:16") for p in platforms}
    # mixed requirements currently resolve to vertical (all supported platforms are 9:16)
    return aspects.pop() if len(aspects) == 1 else "9:16"
