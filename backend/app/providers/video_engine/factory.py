"""Video engine factory with explicit local and production routing.

Development can use the clearly labeled MockVideoEngine. Production routes to
MoneyPrinterTurbo over HTTP and fails explicitly when it is unavailable.
"""

from __future__ import annotations

from app.core.config import settings
from app.providers.video_engine.base import BaseVideoEngine, VideoEngineError
from app.providers.video_engine.mock import MockVideoEngine
from app.providers.video_engine.mpt import MoneyPrinterTurboAdapter

_instances: dict[str, BaseVideoEngine] = {}
_GLOBAL_SCOPE = "__global__"


def _scope_key(workspace_id: str | None = None) -> str:
    if workspace_id:
        return workspace_id
    try:
        from app.services.provider_settings import current_workspace_id

        return current_workspace_id() or _GLOBAL_SCOPE
    except Exception:
        return _GLOBAL_SCOPE


class EngineNotConfigured(VideoEngineError):
    """Raised when autonomous production requires the video engine but none is
    configured/reachable. Remediation is shown to the operator."""

    def __init__(self, detail: str = "video engine not configured"):
        super().__init__(detail)
        self.remediation = "Settings → Video Engine: set the MoneyPrinterTurbo base URL and verify health."


def _effective_engine_config() -> dict:
    from app.services.provider_settings import get_credential

    base_url, base_src = get_credential("engine.base_url")
    timeout_raw, timeout_src = get_credential("engine.timeout_seconds")
    try:
        timeout = int(timeout_raw) if timeout_raw else settings.mpt_timeout_seconds
    except ValueError:
        timeout = settings.mpt_timeout_seconds
    return {
        "base_url": base_url or settings.mpt_base_url,
        "timeout": max(60, timeout),
        "sources": {"base_url": base_src, "timeout": timeout_src},
    }


def reset_video_engine(workspace_id: str | None = None) -> None:
    """Drop one tenant's cached engine, or all cached engines when omitted."""
    if workspace_id:
        _instances.pop(workspace_id, None)
    else:
        _instances.clear()


def get_video_engine(workspace_id: str | None = None) -> BaseVideoEngine:
    scope = _scope_key(workspace_id)
    if scope in _instances:
        return _instances[scope]
    name = (settings.video_engine or "").lower()
    if name in ("mock", "simulation"):
        # W11.5 E-MED: refuse a mock engine in production instead of relying
        # only on readiness (which `override_readiness` can bypass). Explicit
        # opt-in is required, exactly like the publishing factory.
        if getattr(settings, "is_production", False) and not getattr(
            settings, "allow_mock_in_production", False
        ):
            raise EngineNotConfigured(
                "video_engine='mock' is refused in production "
                "(set allow_mock_in_production=True to override deliberately)"
            )
        _instances[scope] = MockVideoEngine()
        return _instances[scope]
    if name in ("ffmpeg_avatar", "ffmpeg-avatar", "ffmpeg"):
        from app.providers.video_engine.ffmpeg_avatar import FFmpegAvatarEngine

        _instances[scope] = FFmpegAvatarEngine()
        return _instances[scope]
    if name not in ("moneyprinterturbo", "mpt", ""):
        raise EngineNotConfigured(
            f"VIDEO_ENGINE '{settings.video_engine}' is not a supported engine"
        )
    cfg = _effective_engine_config()
    _instances[scope] = MoneyPrinterTurboAdapter(base_url=cfg["base_url"], timeout=cfg["timeout"])
    return _instances[scope]


def engine_effective_config() -> dict:
    cfg = _effective_engine_config()

    def _src(key: str):
        try:
            from app.services.provider_settings import get_credential

            _, src = get_credential(key)
            return src
        except Exception:
            return "none"

    return {
        **cfg,
        "engine": (settings.video_engine or "moneyprinterturbo").lower(),
        "sources": {"base_url": _src("engine.base_url"), "timeout": _src("engine.timeout_seconds")},
    }


__all__ = [
    "EngineNotConfigured",
    "MockVideoEngine",
    "MoneyPrinterTurboAdapter",
    "engine_effective_config",
    "get_video_engine",
    "reset_video_engine",
]
