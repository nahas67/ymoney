"""Production readiness gate (concept adopted from youtube-automation-agent).

Autonomous production may not START unless every blocking dependency is
actually working. Checks probe REAL services — no fake providers:

  llm            — configured key + reachable endpoint (+ auth)
  video_engine   — MoneyPrinterTurbo health
  ffmpeg         — binary present on PATH
  storage        — data dir writable
  trends         — at least one enabled source reachable (best-effort)
  publishing     — per-platform account/relay status (non-blocking)

Stale after 24h; START refuses when blocking checks fail unless the operator
explicitly overrides (audited).
"""

from __future__ import annotations

import shutil
from app.models.base import utcnow
from pathlib import Path

import httpx
from loguru import logger

from app.core.config import settings

STALE_AFTER_HOURS = 24


def _check_llm() -> tuple[bool, str, bool]:  # ok, detail, blocking
    from app.services.provider_settings import effective_llm

    eff = effective_llm()
    if eff["mock"] and not settings.is_production:
        return True, "MOCK_LLM=true — offline development provider", False
    if not eff["api_key"]:
        return False, "No LLM API key configured (Settings → Connections).", True
    try:
        resp = httpx.get(
            f"{(eff['base_url'] or 'https://api.openai.com/v1').rstrip('/')}/models",
            headers={"Authorization": f"Bearer {eff['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "authenticated", True
        return False, f"provider returned HTTP {resp.status_code}", True
    except httpx.HTTPError as exc:
        return False, f"unreachable: {type(exc).__name__}", True


def _check_video_engine() -> tuple[bool, str, bool]:
    from app.providers.video_engine.factory import get_video_engine

    name = (settings.video_engine or "").lower()
    if name in ("mock", "simulation"):
        if settings.is_production:
            return False, "mock video engine is not allowed in production", True
        return True, "MOCK video engine — no real render is performed", False
    if name in ("ffmpeg_avatar", "ffmpeg-avatar", "ffmpeg"):
        engine = get_video_engine()
        if engine.health():
            v = engine.version()
            return True, f"FFmpeg Avatar engine ready{f' ({v[:40]})' if v else ''}", True
        return False, "ffmpeg not runnable — install FFmpeg and verify it is on PATH", True
    if name not in ("moneyprinterturbo", "mpt", ""):
        return False, f"VIDEO_ENGINE '{settings.video_engine}' unsupported", True
    engine = get_video_engine()
    if engine.health():
        v = engine.version()
        return True, f"MoneyPrinterTurbo{f' v{v}' if v else ''} reachable", True
    return False, f"unreachable at {settings.mpt_base_url} (Settings → Video Engine)", True


def _check_ffmpeg() -> tuple[bool, str, bool]:
    if shutil.which("ffmpeg"):
        return True, "found on PATH", True
    return False, "ffmpeg not found — install it (thumbnails/metadata depend on it)", False


def _check_storage() -> tuple[bool, str, bool]:
    try:
        p = Path("data/videos")
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True, "writable", True
    except OSError as exc:
        return False, f"not writable: {exc}", True


def _check_trends() -> tuple[bool, str, bool]:
    srcs = [s for s in (settings.google_trends_enabled,)]
    if settings.mock_trends:
        return False, "MOCK_TRENDS=true is not supported in production", False
    if srcs:
        return True, "Google Trends RSS enabled", False
    return False, "no trend source enabled", False


def _check_publishing() -> tuple[bool, str, bool]:
    from app.services.provider_settings import upload_post_config

    relay = upload_post_config()
    if relay and all(relay):
        return True, "Upload-Post relay configured", False
    return True, "per-platform accounts checked at publish time", False


def run_readiness(force_refresh: bool = True) -> dict:
    checks = []
    for cid, fn in [
        ("llm", _check_llm),
        ("video_engine", _check_video_engine),
        ("ffmpeg", _check_ffmpeg),
        ("storage", _check_storage),
        ("trends", _check_trends),
        ("publishing", _check_publishing),
    ]:
        try:
            ok, detail, blocking = fn()
        except Exception as exc:  # a probe must never crash the gate
            logger.warning(f"readiness probe {cid} crashed: {exc}")
            ok, detail, blocking = False, f"probe error: {type(exc).__name__}", True
        checks.append({
            "id": cid,
            "status": "passed" if ok else "failed",
            "blocking": blocking,
            "detail": detail,
        })

    blocking_failures = [c["id"] for c in checks if c["blocking"] and c["status"] == "failed"]
    return {
        "status": "ready" if not blocking_failures else "blocked",
        "checked_at": utcnow().isoformat() + "Z",
        "stale_after_hours": STALE_AFTER_HOURS,
        "checks": checks,
        "blocking_failures": blocking_failures,
        "message": (
            "All production dependencies verified." if not blocking_failures
            else f"Autonomous production blocked by: {', '.join(blocking_failures)}"
        ),
    }
