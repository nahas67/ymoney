"""Provider construction that NEVER raises (Work 12 Lane A) -- contracts §1.1.

Mirrors ``engine/lipsync/factory.py``: with nothing installed, the factory
returns an :class:`UnavailableAdapter` whose ``health().available`` is False and
whose reason says exactly what is missing. Startup, the ``GET /providers``
route and the run service therefore keep working in a venv with zero ML
packages, and a broken half-landed adapter can never turn into a 500.

Usage::

    provider = build_provider("alignment")            # first chain entry
    provider = build_provider("alignment", "whisperx_alignment")
    provider, reasons = resolve("alignment")          # chains + fallbacks
"""

from __future__ import annotations

import logging

from app.engine.intel.base import (
    MODE_LOCAL,
    LicenseInfo,
    MediaIntelProvider,
    ProviderHealth,
    ProviderResult,
    ResourceSpec,
    unverified_license,
)

logger = logging.getLogger("ymoney.intel")

#: Shown whenever an adapter could not be constructed; every real reason is
#: appended to it so the UI remediation stays actionable.
DEFAULT_REMEDIATION = (
    "Install the optional backend for this capability on the worker host "
    "(no new runtime dependency is required by the app itself)."
)


class UnavailableAdapter(MediaIntelProvider):
    """The honest "no backend" provider. Fails closed, never fabricates.

    ``health()`` reports ``available=False`` plus the reason it was created;
    ``run()`` raises :class:`~app.engine.intel.base.ProviderUnavailable` so no
    caller can mistake it for a working engine. It is a real
    :class:`MediaIntelProvider`, so the registry/API/readiness paths need no
    special case.
    """

    def __init__(
        self,
        *,
        key: str = "unavailable",
        kind: str = "",
        reason: str = "no media-intelligence backend installed",
        remediation: str = DEFAULT_REMEDIATION,
        detail: dict | None = None,
    ) -> None:
        self._key = str(key or "unavailable")
        self._kind = str(kind or "")
        self._reason = str(reason or "unavailable")
        self._remediation = str(remediation or "")
        self._detail = dict(detail or {})

    @property
    def key(self) -> str:
        return self._key

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def reason(self) -> str:
        """Why this adapter is unavailable (surfaced by the API)."""
        return self._reason

    @property
    def remediation(self) -> str:
        """Operator-facing fix hint (never an internal detail)."""
        return self._remediation

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            available=False,
            reason=self._reason,
            version="",
            mode=MODE_LOCAL,
            detail={**self._detail, "remediation": self._remediation},
        )

    def capabilities(self) -> dict:
        return {"available": False, "reason": self._reason}

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(gpu=False, notes="no backend installed")

    def license_info(self) -> LicenseInfo:
        return unverified_license(notes="no backend installed")

    def run(
        self,
        request,
        *,
        progress,
        should_cancel,
        deadline,
    ) -> ProviderResult:
        from app.engine.intel.base import ProviderUnavailable

        raise ProviderUnavailable(self._reason)

    def supports_kind(self, kind: str) -> bool:
        """A kind-less adapter stands in for whatever chain entry asked for it.

        When the factory could not even IMPORT the backend, the honest reason
        is "not installed" -- reporting "does not serve '<kind>'" instead would
        hide the real cause from the operator.
        """
        return not self._kind or super().supports_kind(kind)

    def cost(self, spec: ResourceSpec) -> dict:
        return {"gpu_ms": 0, "cpu_ms": 0, "cost_micros": 0, "billed": False}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<UnavailableAdapter key={self._key} reason={self._reason!r}>"


def _impl_module(key: str) -> str:
    """Module path the registry loads a provider key from."""
    return f"app.engine.intel.impl.{key}"


def build_provider(kind: str = "", name: str = "") -> MediaIntelProvider:
    """Build one provider. NEVER raises -- returns an UnavailableAdapter instead.

    ``name`` selects a provider key directly; otherwise the first entry of
    ``kind``'s registry chain is used (imported lazily). Unknown keys, missing
    modules, broken imports and constructor errors all become an
    :class:`UnavailableAdapter` carrying the reason -- the fail-closed
    behaviour the whole Work 12 layer depends on.
    """
    # Imported here, not at module scope: registry imports this module.
    from app.engine.intel import registry

    key = str(name or "").strip()
    kind_name = str(kind or "").strip().lower()
    if not key:
        chain = registry.chain_for(kind_name)
        key = chain[0] if chain else ""
        if not key:
            return UnavailableAdapter(
                key="unavailable",
                kind=kind_name,
                reason=f"unknown capability kind '{kind_name}'",
            )
    try:
        provider_class = registry.load_provider_class(key)
    except ImportError as exc:
        return UnavailableAdapter(
            key=key,
            kind=kind_name,
            reason=(
                f"provider '{key}' is not available "
                f"(module {_impl_module(key)} is not installed)"
            ),
            detail={"error_type": type(exc).__name__, "module": _impl_module(key)},
        )
    except Exception as exc:  # noqa: BLE001 - construction must never propagate
        logger.warning("provider '%s' failed to load: %s", key, type(exc).__name__)
        return UnavailableAdapter(
            key=key,
            kind=kind_name,
            reason=f"provider '{key}' failed to load ({type(exc).__name__})",
            detail={"error_type": type(exc).__name__},
        )
    try:
        provider = provider_class()
    except Exception as exc:  # noqa: BLE001 - construction must never propagate
        logger.warning("provider '%s' constructor failed: %s", key, exc)
        return UnavailableAdapter(
            key=key,
            kind=kind_name,
            reason=f"provider '{key}' failed to initialise ({type(exc).__name__})",
            detail={"error_type": type(exc).__name__},
        )
    if not provider.supports_kind(kind_name) and kind_name:
        # An explicit kind was requested and this provider does not serve it.
        return UnavailableAdapter(
            key=key,
            kind=kind_name,
            reason=f"provider '{key}' does not serve '{kind_name}'",
        )
    return provider


__all__ = ["DEFAULT_REMEDIATION", "UnavailableAdapter", "build_provider"]
