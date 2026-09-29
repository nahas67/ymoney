"""Provider selection for lip-sync.

Selection is explicit-or-automatic and NEVER raises: with nothing configured
the factory returns `UnavailableAdapter`, so importing/starting `app` works
on a machine with no GPU, no MuseTalk checkout and no model weights.

    LIPSYNC_PROVIDER=auto|musetalk|external|unavailable|none   (default: auto)
"""

from __future__ import annotations

from loguru import logger

from app.engine.lipsync.base import LipSyncProvider, env_str
from app.engine.lipsync.external import ExternalAdapter
from app.engine.lipsync.musetalk import MuseTalkAdapter
from app.engine.lipsync.unavailable import UnavailableAdapter

_PROVIDER_NAMES = {"musetalk", "external", "unavailable", "none", "off", "auto", ""}


def _external_configured() -> bool:
    return bool(env_str("LIPSYNC_EXTERNAL_BASE_URL"))


def build_provider(name: str | None = None) -> LipSyncProvider:
    """Build a provider by name (default: LIPSYNC_PROVIDER, else auto)."""
    choice = (name or env_str("LIPSYNC_PROVIDER") or "auto").strip().lower()
    try:
        if choice not in _PROVIDER_NAMES:
            logger.warning(f"unknown LIPSYNC_PROVIDER '{choice}'; using fail-closed fallback")
            choice = "unavailable"
        if choice in {"unavailable", "none", "off"}:
            return UnavailableAdapter(
                reason=f"lip-sync provider explicitly set to '{choice}'"
            )
        if choice == "musetalk":
            return MuseTalkAdapter()
        if choice == "external":
            if not _external_configured():
                return UnavailableAdapter(
                    reason="LIPSYNC_PROVIDER=external but LIPSYNC_EXTERNAL_BASE_URL is empty",
                    remediation=(
                        "set LIPSYNC_EXTERNAL_BASE_URL to your lip-sync worker "
                        "(e.g. https://gpu-box:8080) or use LIPSYNC_PROVIDER=auto"
                    ),
                )
            return ExternalAdapter()
        # auto: prefer a ready MuseTalk install, then a configured endpoint,
        # otherwise the honest fail-closed fallback.
        musetalk = MuseTalkAdapter()
        if musetalk.health().available:
            return musetalk
        if _external_configured():
            return ExternalAdapter()
        return UnavailableAdapter(
            reason="no lip-sync backend ready (MuseTalk not installed, no external endpoint)",
            remediation=UnavailableAdapter().health().remediation,
        )
    except Exception as exc:  # pragma: no cover - selection must never break startup
        logger.exception(f"lip-sync provider selection failed: {exc}")
        return UnavailableAdapter(reason=f"provider selection failed: {exc}")


def get_lipsync_provider(name: str | None = None) -> LipSyncProvider:
    return build_provider(name)


__all__ = ["build_provider", "get_lipsync_provider"]
