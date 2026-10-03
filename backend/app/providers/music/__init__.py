"""AI music providers: one interface, one concrete backend.

The package entry point is :func:`get_music_provider` and the safe call is
:func:`generate_music`, which never raises: a soundtrack failure degrades to a
warning so the video still renders (see ``base.music_or_none``).

Placement is the existing ``music`` timeline track kind -- no second render
path. Import lazily so importing this package stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.providers.music.base import (
    MUSIC_TRACK_KIND,
    MusicIntelligenceProvider,
    MusicNotConfigured,
    MusicPlanRequired,
    MusicProviderError,
    MusicRequest,
    MusicResult,
    MusicUnavailable,
    music_or_none,
    music_prompt,
    music_timeline_clip,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

#: Backend keys this package can build.
MUSIC_PROVIDERS: tuple[str, ...] = ("elevenlabs_music",)

_ALIASES: dict[str, str] = {
    "elevenlabs": "elevenlabs_music",
    "xi_music": "elevenlabs_music",
    "elevenlabs_music": "elevenlabs_music",
}


def get_music_provider(key: str, *, api_key: str = "", workspace_id: str = "",
                       client: httpx.Client | None = None
                       ) -> MusicIntelligenceProvider:
    """Build a provider by key. Raises ``KeyError`` for an unknown backend.

    Raises :class:`MusicNotConfigured` when the selected backend has no usable
    credential or dependency -- an unavailable provider is reported, never
    silently replaced with mock audio.
    """
    resolved = _ALIASES.get(str(key or "").strip().lower())
    if resolved is None:
        raise KeyError(
            f"unknown music provider {key!r}; pick from {sorted(MUSIC_PROVIDERS)}")
    if resolved == "elevenlabs_music":
        from app.providers.music.elevenlabs_music import ElevenLabsMusicProvider

        provider = ElevenLabsMusicProvider(api_key=api_key,
                                          workspace_id=workspace_id, client=client)
        if not provider.available():
            raise MusicNotConfigured(
                "elevenlabs music needs an API key and ffmpeg on PATH")
        return provider
    raise KeyError(f"music provider {resolved!r} has no factory")


def generate_music(key: str, request: MusicRequest, *,
                   api_key: str = "", workspace_id: str = "",
                   client: httpx.Client | None = None) -> MusicResult | None:
    """Generate a soundtrack, or ``None`` when music is unavailable/failed.

    Never raises for a provider-side failure: the caller renders the video
    without a bed. Only an unknown provider key raises ``KeyError``.
    """
    try:
        provider = get_music_provider(key, api_key=api_key,
                                      workspace_id=workspace_id, client=client)
    except MusicUnavailable:
        return None
    return music_or_none(provider, request)


__all__ = [
    "MUSIC_PROVIDERS",
    "MUSIC_TRACK_KIND",
    "MusicIntelligenceProvider",
    "MusicNotConfigured",
    "MusicPlanRequired",
    "MusicProviderError",
    "MusicRequest",
    "MusicResult",
    "MusicUnavailable",
    "generate_music",
    "get_music_provider",
    "music_or_none",
    "music_prompt",
    "music_timeline_clip",
]