"""Avatar system: typed profiles, provider abstraction, consent-gated render.

`AvatarProfile` is the durable contract (portrait source, voice, framing,
consent); `AvatarProvider` normalizes the existing backends in
`app.providers.avatar` (server | sadtalker | wavlip | mock) behind one
interface with an `UnavailableAvatarProvider` fallback.

SAFETY: custom avatars REQUIRE consent_state == "authorized" (explicit
ownership/authorization metadata) before any render. There are no
public-figure impersonation helpers and no consent bypass anywhere in this
package — `require_authorized()` is called by every render path.
"""

from __future__ import annotations

from app.engine.avatar.profile import (
    CONSENT_STATES,
    EXPR_PRESETS,
    MOTION_PRESETS,
    AvatarConsentError,
    AvatarProfile,
    AvatarProvider,
    AvatarRenderResult,
    BackendAvatarProvider,
    ConsentMetadata,
    UnavailableAvatarProvider,
    avatar_health,
    get_provider,
    require_authorized,
)

__all__ = [
    "CONSENT_STATES",
    "EXPR_PRESETS",
    "MOTION_PRESETS",
    "AvatarConsentError",
    "AvatarProfile",
    "AvatarProvider",
    "AvatarRenderResult",
    "BackendAvatarProvider",
    "ConsentMetadata",
    "UnavailableAvatarProvider",
    "avatar_health",
    "get_provider",
    "require_authorized",
]
