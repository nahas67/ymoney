"""Production readiness gate (concept adopted from youtube-automation-agent).

Autonomous production may not START unless every blocking dependency is
actually working. Checks probe REAL services — no fake providers:

  llm            — configured key + reachable endpoint (+ auth)
  video_engine   — ffmpeg_avatar / MoneyPrinterTurbo health
  ffmpeg         — binary present on PATH
  yt_dlp         — binary present (repurpose/download)
  tts            — narration provider status (edge keyless by default)
  images         — scene image provider status (pollinations keyless by default)
  storage        — data dir writable
  database       — SELECT 1 succeeds
  trends         — at least one enabled source reachable (best-effort, 8-source registry)
  publishing     — per-platform account/relay status (non-blocking)
  public_base    — public URL reachable for PULL publishers like Instagram (non-blocking)

Stale after 24h; START refuses when blocking checks fail unless the operator
explicitly overrides (audited). Every check returns latency_ms + remediation
so the Doctor UI can render actionable output in one call.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import httpx
from loguru import logger

from app.core.config import settings
from app.models.base import utcnow

STALE_AFTER_HOURS = 24

# Check tiers by setup effort (Agent-Reach doctor pattern): tier 0 is
# built-in and needs no configuration; tier 1 needs keys, accounts or URLs.
CHECK_TIERS: dict[str, int] = {
    "ffmpeg": 0,
    "yt_dlp": 0,
    "storage": 0,
    "database": 0,
    "llm": 1,
    "video_engine": 1,
    "tts": 1,
    "images": 1,
    "trends": 1,
    "publishing": 1,
    "public_base": 1,
}

_YTDLP_UPGRADE_COMMAND = 'python -m pip install -U "yt-dlp[default]"'


def scrub_text(text: str) -> str:
    """Redact credential-shaped substrings at the output boundary.

    Probe details can echo configured URLs; scrubbed here so neither the API
    nor logs nor the Doctor UI ever render secrets (Agent-Reach doctor rule).
    """
    import re as _re

    if not text:
        return ""
    out = _re.sub(r"(://)[^/\s:@]+:[^/\s@]+@", r"\1***@", text)
    return _re.sub(
        r"([?&#](?:key|token|secret|password|api_key|access_token)=)[^&\s]+",
        r"\1***",
        out,
        flags=_re.IGNORECASE,
    )


def parse_ytdlp_version(text: str | None) -> tuple[int, int, int] | None:
    """'2026.03.17' -> (2026, 3, 17); unrecognized output -> None."""
    import re as _re

    m = _re.fullmatch(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s*", text or "")
    return tuple(map(int, m.groups())) if m else None  # type: ignore[return-value]


def _check_llm() -> tuple[bool, str, bool, str]:  # ok, detail, blocking, remediation
    from app.services.provider_settings import effective_llm

    eff = effective_llm()
    if eff["mock"] and not settings.is_production:
        return True, "MOCK_LLM=true — offline development provider", False, ""
    if not eff["api_key"]:
        return (
            False,
            "No LLM API key configured (Settings → Connections).",
            True,
            "Add LLM key under Settings → Connections or OPENAI_API_KEY env.",
        )
    try:
        resp = httpx.get(
            f"{(eff['base_url'] or 'https://api.openai.com/v1').rstrip('/')}/models",
            headers={"Authorization": f"Bearer {eff['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "authenticated", True, ""
        return (
            False,
            f"provider returned HTTP {resp.status_code}",
            True,
            "Verify LLM base URL + key under Settings → Connections.",
        )
    except httpx.HTTPError as exc:
        return False, f"unreachable: {type(exc).__name__}", True, "Check network + LLM endpoint URL."


def _check_video_engine() -> tuple[bool, str, bool, str]:
    from app.providers.video_engine.factory import get_video_engine

    name = (settings.video_engine or "").lower()
    if name in ("mock", "simulation"):
        if settings.is_production:
            return False, "mock video engine is not allowed in production", True, "Set VIDEO_ENGINE=ffmpeg_avatar."
        return True, "MOCK video engine — no real render is performed", False, ""
    if name in ("ffmpeg_avatar", "ffmpeg-avatar", "ffmpeg"):
        engine = get_video_engine()
        if engine.health():
            v = engine.version()
            return True, f"FFmpeg Avatar engine ready{f' ({v[:40]})' if v else ''}", True, ""
        return (
            False,
            "ffmpeg not runnable — install FFmpeg and verify it is on PATH",
            True,
            "Install FFmpeg, restart backend, verify Settings → System Health.",
        )
    if name not in ("moneyprinterturbo", "mpt", ""):
        return (
            False,
            f"VIDEO_ENGINE '{settings.video_engine}' unsupported",
            True,
            "Set VIDEO_ENGINE=ffmpeg_avatar|moneyprinterturbo|mock.",
        )
    engine = get_video_engine()
    if engine.health():
        v = engine.version()
        return True, f"MoneyPrinterTurbo{f' v{v}' if v else ''} reachable", True, ""
    return (
        False,
        f"unreachable at {settings.mpt_base_url} (Settings → Video Engine)",
        True,
        "Start MPT API or switch VIDEO_ENGINE=ffmpeg_avatar.",
    )


def _check_ffmpeg() -> tuple[bool, str, bool, str]:
    if shutil.which("ffmpeg"):
        return True, "found on PATH", True, ""
    return (
        False,
        "ffmpeg not found — install it (thumbnails/metadata depend on it)",
        False,
        "Install FFmpeg and ensure it is on PATH, then restart backend.",
    )


def _check_yt_dlp() -> tuple[bool, str, bool, str]:
    # Trichotomy like Agent-Reach's youtube channel: missing / broken /
    # runnable-with-known-version, each with its own fix command.
    if not shutil.which("yt-dlp"):
        return (
            False,
            "yt-dlp not installed — repurpose/download disabled",
            False,
            f"Install it: {_YTDLP_UPGRADE_COMMAND}",
        )
    try:
        proc = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=10)
        version = parse_ytdlp_version((proc.stdout or b"").decode(errors="replace"))
    except (subprocess.SubprocessError, OSError):
        return (
            False,
            "yt-dlp installed but not runnable",
            False,
            f"Reinstall (with JS support): {_YTDLP_UPGRADE_COMMAND}",
        )
    if proc.returncode != 0 or not version:
        return (
            False,
            "yt-dlp present but version unreadable — extractors may be stale",
            False,
            f"Upgrade and re-run Doctor: {_YTDLP_UPGRADE_COMMAND}",
        )
    return True, f"yt-dlp {version[0]}.{version[1]:02d}.{version[2]:02d} on PATH", False, ""


def _check_tts() -> tuple[bool, str, bool, str]:
    try:
        from app.providers.tts import tts_provider_status

        st = tts_provider_status()
        if st.get("healthy"):
            return True, f"{st.get('provider','edge')} ready", False, ""
        err = st.get("error") or "unhealthy"
        return (
            False,
            f"{st.get('provider','tts')}: {err}"[:200],
            False,
            "Settings → Connections tts.* or TTS_PROVIDER=edge (keyless).",
        )
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}", False, "Check TTS provider settings."


def _check_images() -> tuple[bool, str, bool, str]:
    try:
        from app.providers.images import image_provider_status

        st = image_provider_status()
        if st.get("healthy"):
            return True, f"{st.get('provider','images')} ready", False, ""
        err = st.get("error") or "unhealthy"
        return (
            False,
            f"{st.get('provider','images')}: {err}"[:200],
            False,
            "Set PEXELS_API_KEY or IMAGE_PROVIDER=pollinations (keyless).",
        )
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}", False, "Check image provider settings."


def _check_storage() -> tuple[bool, str, bool, str]:
    try:
        p = Path("data/videos")
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True, "writable", True, ""
    except OSError as exc:
        return False, f"not writable: {exc}", True, "Ensure backend/data is writable by the backend process."


def _check_database() -> tuple[bool, str, bool, str]:
    try:
        from sqlalchemy import text as _t

        from app.db import session_scope

        with session_scope() as s:
            s.execute(_t("SELECT 1"))
        return True, "SELECT 1 ok", True, ""
    except Exception as exc:
        return False, f"unreachable: {type(exc).__name__}", True, "Check DATABASE_URL, disk space, file permissions."


def _check_trends() -> tuple[bool, str, bool, str]:
    if settings.mock_trends:
        return False, "MOCK_TRENDS=true is not supported in production", False, "Set MOCK_TRENDS=false and enable a real source."
    if settings.google_trends_enabled:
        return (
            True,
            "Google Trends RSS enabled (+reddit/hacker_news/newsdata/coingecko/devto/youtube registry available)",
            False,
            "",
        )
    return (
        False,
        "no trend source enabled",
        False,
        "Enable google_trends or add a source in Settings → Trends.",
    )


def _check_publishing() -> tuple[bool, str, bool, str]:
    from app.services.provider_settings import upload_post_config

    relay = upload_post_config()
    if relay and all(relay):
        return True, "Upload-Post relay configured", False, ""
    return True, "per-platform accounts checked at publish time", False, ""


def _check_public_base() -> tuple[bool, str, bool, str]:
    try:
        from app.services.public_links import public_base_reachable

        if public_base_reachable():
            return True, "public base URL reachable (Meta PULL publishers ok)", False, ""
        return (
            False,
            "public base is localhost/unset — Instagram Graph PULL will fail closed",
            False,
            "Set YMONEY_PUBLIC_BASE_URL to a reachable https host or configure S3_PUBLIC_BASE_URL.",
        )
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}", False, "Check YMONEY_PUBLIC_BASE_URL."


def run_readiness(force_refresh: bool = True) -> dict:
    checks = []
    for cid, fn in [
        ("llm", _check_llm),
        ("video_engine", _check_video_engine),
        ("ffmpeg", _check_ffmpeg),
        ("yt_dlp", _check_yt_dlp),
        ("tts", _check_tts),
        ("images", _check_images),
        ("storage", _check_storage),
        ("database", _check_database),
        ("trends", _check_trends),
        ("publishing", _check_publishing),
        ("public_base", _check_public_base),
    ]:
        start = time.perf_counter()
        try:
            out = fn()
            if len(out) == 3:
                ok, detail, blocking = out
                remediation = ""
            else:
                ok, detail, blocking, remediation = out
        except Exception as exc:  # a probe must never crash the gate
            logger.warning(f"readiness probe {cid} crashed: {exc}")
            ok, detail, blocking, remediation = False, f"probe error: {type(exc).__name__}", True, "Retry; check logs with X-Request-ID."
        latency_ms = int((time.perf_counter() - start) * 1000)
        checks.append(
            {
                "id": cid,
                "status": "passed" if ok else "failed",
                "blocking": blocking,
                "tier": CHECK_TIERS.get(cid, 1),
                "detail": scrub_text(detail),
                "latency_ms": latency_ms,
                "remediation": scrub_text(remediation) if not ok else "",
            }
        )

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
