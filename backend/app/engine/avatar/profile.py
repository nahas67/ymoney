"""Avatar profiles + the `AvatarProvider` interface (Work 07 Lane C).

One typed record (`AvatarProfile`) describes WHO may be rendered, from which
workspace asset, with which voice/framing/language/brand association. One
provider interface (`AvatarProvider`) hides which backend does the work, so
callers never touch SadTalker/WavLip/server details directly:

    provider = get_provider()            # configured lane
    provider.health()                    # {ready, detail, lanes, license_notes}
    provider.render(profile, audio_ref)  # consent enforced FIRST, always

Guard rails (hard requirements):
  * `require_authorized()` raises `AvatarConsentError` unless the profile is
    `consent_state == "authorized"` AND carries authorization metadata — no
    bypass, no "public figure" helper, no silent default.
  * Unknown/unconfigured lanes fall back to `UnavailableAvatarProvider`,
    which reports `ready: False` and fails closed with remediation text.
  * Every render returns a lineage dict (source asset, provider, backend,
    consent snapshot, driving audio) that the caller persists.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.providers.avatar import (
    AvatarError,
    avatar_backend,
    avatar_status,
    render_avatar,
)

CONSENT_STATES = ("authorized", "pending", "revoked")
EXPR_PRESETS = ("neutral", "smile", "brow_raise", "blink", "talkative")
MOTION_PRESETS = ("still", "subtle", "energetic", "nod")
FRAMINGS = ("tight", "medium_closeup", "medium", "wide", "square")
PROFILE_STATUSES = ("active", "disabled")


class AvatarConsentError(Exception):
    """Render denied: the workspace has not authorized this portrait."""


# ---------------------------------------------------------------------------
# consent + profile record
# ---------------------------------------------------------------------------


@dataclass
class ConsentMetadata:
    """Explicit ownership/authorization record for one portrait/voice."""

    state: str = "pending"          # authorized | pending | revoked
    source: str = ""                # where the authorization comes from
    granted_by: str = ""            # who granted it (user id / email / doc id)
    granted_at: str = ""            # ISO timestamp
    authorization_evidence: dict = field(default_factory=dict)
    statement: str = ""             # user attestation text

    @property
    def is_authorized(self) -> bool:
        return self.state == "authorized"

    @property
    def has_metadata(self) -> bool:
        """Authorization must point at something concrete (evidence or source)."""
        return bool(self.authorization_evidence or self.source)

    def missing_for_authorization(self) -> list[str]:
        missing = []
        if not self.source:
            missing.append("source")
        if not self.authorization_evidence:
            missing.append("authorization_evidence")
        return missing

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "source": self.source,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "authorization_evidence": dict(self.authorization_evidence),
            "statement": self.statement,
        }

    @classmethod
    def from_json(cls, data: dict | None) -> ConsentMetadata:
        raw = dict(data or {})
        state = str(raw.get("state") or "pending").lower()
        if state not in CONSENT_STATES:
            state = "pending"
        return cls(
            state=state,
            source=str(raw.get("source") or ""),
            granted_by=str(raw.get("granted_by") or ""),
            granted_at=str(raw.get("granted_at") or ""),
            authorization_evidence=dict(raw.get("authorization_evidence") or {}),
            statement=str(raw.get("statement") or ""),
        )


@dataclass
class AvatarProfile:
    """Typed avatar record — what the workspace wants to render, and whether
    it is allowed to."""

    id: str
    workspace_id: str
    name: str
    source_asset_ref: str                       # MediaAsset id (portrait/video)
    voice_ref: str = ""                         # MediaAsset id of driving voice
    expression_preset: str = "neutral"
    motion_preset: str = "subtle"
    framing: str = "medium_closeup"
    background: str = "studio"
    language: str = "en"
    brand_association: str = ""                 # brand kit / brand voice tag
    provider: str = ""                          # preferred lane ("" = configured)
    status: str = "active"
    consent: ConsentMetadata = field(default_factory=ConsentMetadata)

    @property
    def consent_state(self) -> str:
        return self.consent.state

    def to_json(self) -> dict:
        """profile_json payload (everything except row-level columns)."""
        return {
            "name": self.name,
            "source_asset_ref": self.source_asset_ref,
            "voice_ref": self.voice_ref,
            "expression_preset": self.expression_preset,
            "motion_preset": self.motion_preset,
            "framing": self.framing,
            "background": self.background,
            "language": self.language,
            "brand_association": self.brand_association,
            "provider": self.provider,
            "status": self.status,
        }

    @classmethod
    def from_row(cls, row: Any) -> AvatarProfile:
        """Build the typed record from an `AvatarProfileRow`.

        The row's `consent_state` is authoritative (it is what the API flips
        on authorize/revoke); `consent_json` carries the evidence payload.
        """
        data = dict(getattr(row, "profile_json", None) or {})
        consent = ConsentMetadata.from_json(getattr(row, "consent_json", None))
        consent.state = str(getattr(row, "consent_state", "") or "pending").lower()
        return cls(
            id=str(row.id),
            workspace_id=str(row.workspace_id),
            name=str(data.get("name") or getattr(row, "name", "") or ""),
            source_asset_ref=str(
                getattr(row, "source_asset_ref", "") or data.get("source_asset_ref") or ""
            ),
            voice_ref=str(data.get("voice_ref") or ""),
            expression_preset=str(data.get("expression_preset") or "neutral"),
            motion_preset=str(data.get("motion_preset") or "subtle"),
            framing=str(data.get("framing") or "medium_closeup"),
            background=str(data.get("background") or "studio"),
            language=str(data.get("language") or "en"),
            brand_association=str(data.get("brand_association") or ""),
            provider=str(getattr(row, "provider", "") or data.get("provider") or ""),
            status=str(getattr(row, "status", "") or "active"),
            consent=consent,
        )


def require_authorized(profile: AvatarProfile, *, action: str = "render") -> None:
    """Hard gate for custom-avatar renders. Raises with remediation text."""
    if profile.consent_state != "authorized":
        raise AvatarConsentError(
            f"avatar '{profile.name or profile.id}' is NOT authorized for {action}: "
            f"consent_state='{profile.consent_state}'. Custom avatars require explicit "
            f"ownership/authorization metadata first "
            f"(POST /workspaces/{profile.workspace_id}/avatars/{profile.id}/authorize "
            f"with source + authorization_evidence); revoked/pending portraits never render."
        )
    if not profile.consent.has_metadata:
        raise AvatarConsentError(
            f"avatar '{profile.name or profile.id}' is marked authorized but carries no "
            f"authorization metadata (source/authorization_evidence) — refusing {action}."
        )


# ---------------------------------------------------------------------------
# provider interface
# ---------------------------------------------------------------------------


@dataclass
class AvatarRenderResult:
    path: str
    provider: str
    backend: str
    duration: float = 0.0
    is_mock: bool = False
    lineage: dict = field(default_factory=dict)


class AvatarProvider(ABC):
    """Normalizes one or more render backends behind a single contract."""

    key: str = "base"

    @abstractmethod
    def health(self) -> dict:
        """{ready, detail, ...} — ready False when the lane cannot render."""

    @abstractmethod
    def capabilities(self) -> dict:
        """What this provider can do (inputs, outputs, presets, consent)."""

    @abstractmethod
    def render(self, profile: AvatarProfile, audio_ref: str,
               opts: dict | None = None) -> AvatarRenderResult:
        """Render one clip. MUST enforce consent before any work."""


class UnavailableAvatarProvider(AvatarProvider):
    """Fallback for unknown/unconfigured lanes: never renders, always explains."""

    def __init__(self, reason: str, *, key: str = "unavailable") -> None:
        self.reason = reason
        self.key = f"avatar.{key}"

    def health(self) -> dict:
        return {"ready": False, "detail": self.reason, "provider": self.key,
                "ffmpeg": False, "lanes": {}}

    def capabilities(self) -> dict:
        return {"ready": False, "reason": self.reason, "renders": False,
                "consent_required": True, "inputs": [], "outputs": []}

    def render(self, profile: AvatarProfile, audio_ref: str,
               opts: dict | None = None) -> AvatarRenderResult:
        require_authorized(profile)  # consent is checked even when unavailable
        raise AvatarError(f"avatar backend unavailable: {self.reason}")


class BackendAvatarProvider(AvatarProvider):
    """Adapter over `app.providers.avatar` lanes (server|sadtalker|wavlip|mock)."""

    LANES = ("server", "sadtalker", "wavlip", "mock")

    def __init__(self, backend: str = "") -> None:
        self.backend = (backend or avatar_backend() or "server").lower()

    @property
    def key(self) -> str:
        return f"avatar.{self.backend}"

    def health(self) -> dict:
        try:
            status = avatar_status()
        except Exception as exc:  # noqa: BLE001 — health must never raise
            return {"ready": False, "detail": f"status probe failed: {type(exc).__name__}",
                    "provider": self.key}
        ready = bool(status.get("ready"))
        return {
            "ready": ready,
            "detail": status.get("detail", ""),
            "backend": status.get("backend", self.backend),
            "provider": self.key,
            "ffmpeg": bool(status.get("ffmpeg")),
            "lanes": dict(status.get("lanes") or {}),
            "license_notes": dict(status.get("license_notes") or {}),
        }

    def capabilities(self) -> dict:
        health = self.health()
        return {
            "ready": health["ready"],
            "renders": True,
            "backend": self.backend,
            "lanes": list(self.LANES),
            "inputs": ["image", "video"],
            "outputs": ["mp4"],
            "audio_driven": True,
            "expression_presets": list(EXPR_PRESETS),
            "motion_presets": list(MOTION_PRESETS),
            "framings": list(FRAMINGS),
            "consent_required": True,
            "consent_states": list(CONSENT_STATES),
            "license_notes": health.get("license_notes", {}),
            "detail": health.get("detail", ""),
        }

    def render(self, profile: AvatarProfile, audio_ref: str,
               opts: dict | None = None) -> AvatarRenderResult:
        # 1) consent — before any file or backend work (hard requirement)
        require_authorized(profile)
        if not profile.source_asset_ref:
            raise AvatarError(
                f"avatar '{profile.name or profile.id}' has no source portrait asset — "
                f"upload it under Assets and set source_asset_ref")
        opts = dict(opts or {})
        backend = str(opts.get("backend") or profile.provider or self.backend or "").lower()
        if backend and backend not in self.LANES:
            raise AvatarError(f"unknown avatar backend '{backend}' "
                              f"({'|'.join(self.LANES)})")
        if not audio_ref:
            raise AvatarError("driving audio ref is required")
        # 2) delegate to the existing fail-closed backend stack
        clip = render_avatar(
            profile.source_asset_ref, audio_ref, profile.workspace_id,
            filename=opts.get("filename") or None,
            backend=backend,
        )
        lineage = {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "source_asset_ref": profile.source_asset_ref,
            "audio_asset_ref": audio_ref,
            "provider": self.key,
            "backend": clip.backend,
            "consent": profile.consent.to_json(),
            "expression_preset": profile.expression_preset,
            "motion_preset": profile.motion_preset,
            "framing": profile.framing,
            "language": profile.language,
            "is_mock": bool(clip.is_mock),
            "duration": float(clip.duration or 0.0),
            "rendered_at": int(time.time()),
        }
        return AvatarRenderResult(
            path=clip.path, provider=self.key, backend=clip.backend,
            duration=float(clip.duration or 0.0), is_mock=bool(clip.is_mock),
            lineage=lineage,
        )


def get_provider(name: str = "") -> AvatarProvider:
    """Provider for a lane (configured lane when `name` is empty).

    Unknown lanes degrade to `UnavailableAvatarProvider` — never an exception
    at import/startup time, so the app boots without a GPU or checkout.
    """
    backend = (name or avatar_backend() or "").lower()
    if backend not in BackendAvatarProvider.LANES:
        return UnavailableAvatarProvider(
            f"unknown avatar backend '{backend or 'unset'}' "
            f"(expected {'|'.join(BackendAvatarProvider.LANES)})",
            key=backend or "unset",
        )
    return BackendAvatarProvider(backend)


def avatar_health() -> dict:
    """Consent policy + provider health for `GET /avatars/health`."""
    provider = get_provider()
    health = provider.health()
    return {
        "provider": provider.key,
        "ready": bool(health.get("ready")),
        "detail": health.get("detail", ""),
        "consent_required": True,
        "consent_states": list(CONSENT_STATES),
        "health": health,
        "capabilities": provider.capabilities(),
    }
