"""Media-intelligence provider contract (Work 12 Lane A) -- contracts §1.1.

One ABC every intelligence backend implements, plus the frozen value objects
the rest of the stack passes around. The shape mirrors the two existing
precedents (``engine/intelligence/providers/base.py`` and
``engine/lipsync/base.py``): health first, capabilities second, work third.

Invariants every implementation must honour:

* **Honest unavailability.** No model/provider installed => ``health()``
  reports ``available=False`` with a ``reason``. The registry then resolves to
  ``None`` and the run ends in the terminal ``UNAVAILABLE`` state. Speakers,
  faces, masks, word timings and confidence are NEVER fabricated.
* **No sensitive inference.** Nothing here may return a demographic, identity
  or biometric attribute. Speaker/track ids are anonymous session labels.
* **Boot without models.** Heavy imports live inside ``health()``/``run()``
  bodies -- never at module import -- so ``from app.main import app`` works in
  a venv with zero ML packages installed.
* **Cooperative work.** ``run`` must call ``progress(0..1)``, poll
  ``should_cancel()`` and honour ``deadline``; use :func:`check_control` (or
  :func:`safe_health`) rather than hand-rolling those checks.
* **Derived only.** A provider writes NEW files; it never edits its input.

License discipline (contracts §1.4): code AND model terms are reported
separately and ``commercial_use`` is never promoted optimistically. The
defaults below are deliberately UNVERIFIED -- an orchestrator-side audit fills
the verified values and dates in ``docs/oss/MEDIA_INTEL_LICENSES.md``; until a
human audits a component nothing may claim PERMITTED.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: how a provider executes (reported verbatim in ``ProviderHealth.mode``).
MODE_LOCAL = "local"
MODE_REMOTE = "remote"
MODE_SUBPROCESS = "subprocess"

#: ``LicenseInfo.commercial_use`` vocabulary (contracts §1.4).
COMMERCIAL_PERMITTED = "PERMITTED"
COMMERCIAL_REVIEW_REQUIRED = "REVIEW_REQUIRED"
COMMERCIAL_PROHIBITED = "PROHIBITED"
COMMERCIAL_UNVERIFIED = "UNVERIFIED"
COMMERCIAL_USE_VALUES: tuple[str, ...] = (
    COMMERCIAL_PERMITTED,
    COMMERCIAL_REVIEW_REQUIRED,
    COMMERCIAL_PROHIBITED,
    COMMERCIAL_UNVERIFIED,
)

#: Honest default until an audit fills the verified values in. Deliberately not
#: PERMITTED: a provider that has not been audited for commercial use must be
#: refused in commercial mode rather than silently allowed.
UNVERIFIED_COMMERCIAL = COMMERCIAL_UNVERIFIED
UNVERIFIED_MODEL_LICENSE = "SEE_MODEL_CARD"
UNVERIFIED_CODE_LICENSE = "UNKNOWN"


class ProviderUnavailable(ValueError):
    """No usable provider (missing package, weights, token, or endpoint).

    Subclasses ``ValueError`` on purpose: a route's domain-error guard maps it
    to 422, and the run service maps it to the terminal ``UNAVAILABLE`` state
    instead of a failure.
    """


class ProviderCancelled(Exception):
    """Cooperative cancellation observed by the provider (contracts §1.1)."""


class ProviderTimeout(Exception):
    """The provider's deadline elapsed before the work finished."""


@dataclass(frozen=True)
class ResourceSpec:
    """Scheduling + cost inputs for one provider (contracts §1.1)."""

    gpu: bool = False
    vram_mb: int = 0
    ram_mb: int = 0
    model_bytes: int = 0
    cpu_seconds_per_audio_minute: float = 0.0
    notes: str = ""


@dataclass(frozen=True)
class LicenseInfo:
    """Code AND model terms, reported separately (contracts §1.4)."""

    code_license: str  # SPDX id or "UNKNOWN"
    code_license_url: str
    model_license: str  # SPDX id, "SEE_MODEL_CARD", or "UNKNOWN"
    model_license_url: str = ""
    model_gated: bool = False
    # PERMITTED | REVIEW_REQUIRED | PROHIBITED | UNVERIFIED. The default is the
    # honest one: a provider that never states its terms is UNVERIFIED, never
    # implicitly permissive (the contracts sketch lists this field without a
    # default, which is not orderable in a dataclass -- UNVERIFIED is the only
    # safe default).
    commercial_use: str = COMMERCIAL_UNVERIFIED
    audited_on: str = ""  # ISO date of the license audit
    notes: str = ""


@dataclass(frozen=True)
class ProviderHealth:
    """Availability verdict. ``reason`` is REQUIRED when unavailable."""

    available: bool
    reason: str = ""
    version: str = ""
    mode: str = ""  # "local" | "remote" | "subprocess"
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "available": bool(self.available),
            "reason": self.reason,
            "version": self.version,
            "mode": self.mode,
            "detail": dict(self.detail or {}),
        }


@dataclass(frozen=True)
class ProviderResult:
    """The outcome of one :meth:`MediaIntelProvider.run` call.

    ``artifacts`` is ``kind -> {asset_id?, path?, payload}`` -- a provider
    reports what it wrote; nothing here is ever inferred by the caller.
    ``error`` is a SHORT generic code: it is written to the run row and can
    reach the API, so internals/stacks never go there.
    """

    ok: bool
    artifacts: dict
    metrics: dict
    warnings: list[str]
    error: str = ""


@dataclass(frozen=True)
class IntelRequest:
    """One unit of work handed to a provider.

    ``storage_path`` is a real file on local disk (resolved by the service
    layer from the workspace asset); providers read it and write NEW files.
    ``params`` is the already-validated, JSON-serialisable parameter dict.
    """

    workspace_id: str
    asset_id: str
    storage_path: str
    params: dict = field(default_factory=dict)
    asset_checksum: str = ""
    run_id: str = ""


#: Cooperative-control callback types.
ProgressFn = Callable[[float], None]
CancelFn = Callable[[], bool]


def check_control(
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
    *,
    every: int = 1,
    counter: int = 0,
) -> None:
    """Poll cancellation + the deadline; raise the matching control error.

    Call this at every loop step of a long run. ``deadline`` is an absolute
    ``time.monotonic()`` value (build it with :func:`deadline_in`); ``None``
    means "no deadline". ``every``/``counter`` let a hot loop poll cheaply --
    pass ``every=n, counter=i`` and the checks only run every ``n`` iterations.
    """
    if every > 1 and counter % every:
        return
    if should_cancel is not None and should_cancel():
        raise ProviderCancelled("cancelled by operator")
    if deadline is not None and time.monotonic() >= deadline:
        raise ProviderTimeout("provider deadline exceeded")


def deadline_in(seconds: float | None) -> float | None:
    """Absolute monotonic deadline ``seconds`` from now (None passes through)."""
    if seconds is None:
        return None
    return time.monotonic() + float(seconds)


def safe_health(provider: Any) -> ProviderHealth:
    """``provider.health()`` that can never raise (contracts §1.1).

    A provider whose probe explodes is treated as UNAVAILABLE with the
    exception TYPE as the reason -- honest, and it keeps the API and the
    registry alive instead of turning a bad adapter into a 500.
    """
    try:
        health = provider.health()
    except Exception as exc:  # noqa: BLE001 -- a health probe must never propagate
        return ProviderHealth(
            available=False,
            reason=f"health probe failed: {type(exc).__name__}",
            mode=MODE_LOCAL,
            detail={"error_type": type(exc).__name__},
        )
    if not isinstance(health, ProviderHealth):  # defensive: a bad adapter
        return ProviderHealth(
            available=False,
            reason="health probe returned no ProviderHealth",
            detail={"error_type": type(getattr(health, "__class__", object)).__name__},
        )
    if not health.available and not health.reason:
        # an unavailable verdict without a reason is not honest; repair it
        return ProviderHealth(
            available=False,
            reason="provider reported unavailable without a reason",
            version=health.version,
            mode=health.mode,
            detail=dict(health.detail or {}),
        )
    return health


def unverified_license(
    *,
    code_license: str = UNVERIFIED_CODE_LICENSE,
    code_license_url: str = "",
    model_license: str = UNVERIFIED_MODEL_LICENSE,
    model_license_url: str = "",
    model_gated: bool = False,
    notes: str = "not yet audited; see docs/oss/MEDIA_INTEL_LICENSES.md",
) -> LicenseInfo:
    """LicenseInfo with the honest UNVERIFIED commercial verdict.

    Every provider uses this until an operator records a real audit. It exists
    so a new adapter can never accidentally claim ``PERMITTED``.
    """
    return LicenseInfo(
        code_license=code_license,
        code_license_url=code_license_url,
        model_license=model_license,
        model_license_url=model_license_url,
        model_gated=model_gated,
        commercial_use=UNVERIFIED_COMMERCIAL,
        audited_on="",
        notes=notes,
    )


class MediaIntelProvider(abc.ABC):
    """One media-intelligence backend. Subclasses implement :meth:`run`.

    ``key`` and ``kind`` are abstract, so a half-implemented adapter fails at
    instantiation instead of at first use. Implementations set them as class
    attributes (``key = "ffmpeg_enhancement"``), which overrides the abstract
    property.

    A provider may serve more than one capability chain: declare the extra
    chains in ``kinds`` (``kinds = ("denoise",)`` next to ``kind =
    "enhancement"``) and the registry will consider it for both.
    """

    #: extra capability chains beyond ``kind`` (contracts §1.1 registry order)
    kinds: tuple[str, ...] = ()

    @property
    @abc.abstractmethod
    def key(self) -> str:
        """Stable provider key, e.g. ``ffmpeg_enhancement``."""

    @property
    @abc.abstractmethod
    def kind(self) -> str:
        """Primary capability kind, e.g. ``enhancement``."""

    @abc.abstractmethod
    def health(self) -> ProviderHealth:
        """Availability verdict. MUST NOT raise and MUST NOT import a heavy
        model eagerly; return a ``reason`` when unavailable."""

    @abc.abstractmethod
    def capabilities(self) -> dict:
        """Honest feature/limit map (formats, sample rates, max duration,
        stages, aspects). Never claim a capability the probe did not confirm."""

    @abc.abstractmethod
    def resource_requirements(self) -> ResourceSpec:
        """GPU/CPU + memory cost inputs used for scheduling and cost."""

    @abc.abstractmethod
    def license_info(self) -> LicenseInfo:
        """Code AND model terms (contracts §1.4)."""

    @abc.abstractmethod
    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Do the work. Cooperative: call ``progress(0..1)``, poll
        ``should_cancel()``, respect ``deadline``; raise
        ``ProviderCancelled`` / ``ProviderTimeout`` / ``ProviderUnavailable``."""

    @abc.abstractmethod
    def cost(self, spec: ResourceSpec) -> dict:
        """``{"gpu_ms": int, "cpu_ms": int, "cost_micros": int, ...}`` for work
        described by ``spec`` (plus whatever the caller measured)."""

    # -- shared behaviour -------------------------------------------------

    def supports_kind(self, kind: str) -> bool:
        """True when this provider serves ``kind`` (primary or extra chain)."""
        return kind == self.kind or kind in self.kinds

    def chain_kinds(self) -> tuple[str, ...]:
        """Every chain this provider belongs to, primary first."""
        return (self.kind, *self.kinds)

    def to_dict(self) -> dict:
        """Health + capabilities + license + resources, for the API (§14).

        Deliberately built from :func:`safe_health` and a defensive license
        read, so one broken adapter can never take the whole listing down.
        """
        health = safe_health(self)
        try:
            capabilities = dict(self.capabilities() or {})
        except Exception as exc:  # noqa: BLE001 - listing must survive
            capabilities = {"error": f"capabilities unavailable: {type(exc).__name__}"}
        try:
            license_info = self.license_info()
        except Exception as exc:  # noqa: BLE001 - listing must survive
            license_info = unverified_license(
                notes=f"license probe failed: {type(exc).__name__}"
            )
        try:
            resources = self.resource_requirements()
        except Exception as exc:  # noqa: BLE001 - listing must survive
            resources = ResourceSpec(notes=f"resource probe failed: {type(exc).__name__}")
        return {
            "key": str(getattr(self, "key", "") or ""),
            "kinds": [k for k in self.chain_kinds() if k],
            "health": health.to_dict(),
            "capabilities": capabilities,
            "license": {
                "code_license": license_info.code_license,
                "code_license_url": license_info.code_license_url,
                "model_license": license_info.model_license,
                "model_license_url": license_info.model_license_url,
                "model_gated": bool(license_info.model_gated),
                "commercial_use": license_info.commercial_use,
                "audited_on": license_info.audited_on,
                "notes": license_info.notes,
            },
            "resources": {
                "gpu": bool(resources.gpu),
                "vram_mb": int(resources.vram_mb),
                "ram_mb": int(resources.ram_mb),
                "model_bytes": int(resources.model_bytes),
                "cpu_seconds_per_audio_minute": float(
                    resources.cpu_seconds_per_audio_minute
                ),
                "notes": resources.notes,
            },
        }


__all__ = [
    "COMMERCIAL_PERMITTED",
    "COMMERCIAL_PROHIBITED",
    "COMMERCIAL_REVIEW_REQUIRED",
    "COMMERCIAL_UNVERIFIED",
    "COMMERCIAL_USE_VALUES",
    "CancelFn",
    "IntelRequest",
    "LicenseInfo",
    "MODE_LOCAL",
    "MODE_REMOTE",
    "MODE_SUBPROCESS",
    "MediaIntelProvider",
    "ProgressFn",
    "ProviderCancelled",
    "ProviderHealth",
    "ProviderResult",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ResourceSpec",
    "check_control",
    "deadline_in",
    "safe_health",
    "unverified_license",
]
