"""Motion-graphics card renderer on HyperFrames (Apache-2.0, local-first).

HyperFrames turns HTML+CSS compositions into deterministic MP4s via headless
Chrome + FFmpeg — kinetic titles, stat hits, CTAs and lower-thirds that plain
FFmpeg drawtext cannot do. YMONEY uses it as an optional motion lane:

- default: pure-FFmpeg cards (existing behavior everywhere),
- when HyperFrames + a working browser are present: designed motion cards.

Capability is probed, never assumed: CLI version, lint gate, browser health and
ffmpeg are all checked up front, and every failure fails closed with the exact
remediation (same contract as the clip repurposer).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.services.storage import STORAGE_ROOT

WORK_DIR = Path("data/motion")

_DEFAULT_KINDS = ("hook", "stat", "cta", "lower")


def _registry_kinds() -> tuple[str, ...]:
    try:
        from app.services.templates import list_templates

        ids = tuple(t["id"] for t in list_templates("motion"))
        return ids or _DEFAULT_KINDS
    except Exception:
        return _DEFAULT_KINDS


CARD_KINDS = _registry_kinds()


class MotionError(Exception):
    pass


def _npx() -> list[str] | None:
    exe = shutil.which("npx")
    if not exe:
        return None
    if os.name == "nt":
        return ["cmd", "/c", "npx"]
    return [exe]


def hyperframes_available() -> bool:
    """CLI installed and reports a version."""
    prefix = _npx()
    if not prefix:
        return False
    try:
        proc = subprocess.run(prefix + ["-y", "hyperframes", "--version"],
                              capture_output=True, text=True, timeout=120)
        return proc.returncode == 0 and bool((proc.stdout or "").strip())
    except (subprocess.SubprocessError, OSError):
        return False


def hyperframes_version() -> str | None:
    prefix = _npx()
    if not prefix:
        return None
    try:
        proc = subprocess.run(prefix + ["-y", "hyperframes", "--version"],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip().splitlines()[-1][:40] or None
    except (subprocess.SubprocessError, OSError):
        return None


def browser_healthy(timeout: int = 25) -> bool:
    """A Chrome binary that actually launches (headless-shell downloads can be
    broken on some hosts; system Chrome may be sandbox-blocked)."""
    prefix = _npx()
    if not prefix:
        return False
    try:
        proc = subprocess.run(prefix + ["-y", "hyperframes", "browser", "path"],
                              capture_output=True, text=True, timeout=60)
        exe = (proc.stdout or "").strip().splitlines()
        chrome = exe[-1].strip() if exe else ""
        if not chrome or not Path(chrome).exists():
            return False
        probe = subprocess.run([chrome, "--version"], capture_output=True,
                               text=True, timeout=timeout)
        return probe.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def ffmpeg_present() -> bool:
    return bool(shutil.which("ffmpeg"))


def _canonical_kinds() -> list[str]:
    ids = set(_registry_kinds())
    return [k for k in ("hook", "stat", "cta", "lower") if k in ids] + sorted(ids - {"hook", "stat", "cta", "lower"})


def motion_status() -> dict:
    cli = hyperframes_available()
    return {
        "cli": cli,
        "version": hyperframes_version() if cli else None,
        "browser": browser_healthy() if cli else False,
        "ffmpeg": ffmpeg_present(),
        "ready": bool(cli and ffmpeg_present() and browser_healthy()) if cli else False,
        "kinds": _canonical_kinds(),
    }


@dataclass
class MotionCard:
    path: str
    kind: str
    duration: float
    width: int = 1080
    height: int = 1920


def _escape_html(text: str) -> str:
    import html as _h

    return _h.escape(text or "", quote=True)


def build_composition(kind: str, title: str, subtitle: str = "",
                      accent: str = "#22c55e", duration: float = 3.0) -> str:
    """Standalone 9:16 HTML composition (CSS-keyframe only, no JS timeline)."""
    kind = (kind or "hook").lower()
    if kind not in CARD_KINDS:
        raise MotionError(f"unknown card kind '{kind}' (hook|stat|cta|lower)")
    duration = max(1.0, min(10.0, float(duration or 3.0)))
    t, st = _escape_html(title), _escape_html(subtitle)

    bodies = {
        "hook": (
            f'<div class="bg"></div><div class="kick"></div>'
            f'<h1 class="clip hook" data-start="0.15" data-duration="{duration - 0.3:.1f}" data-track-index="1">{t}</h1>'
            + (f'<p class="clip sub" data-start="0.5" data-duration="{duration - 0.6:.1f}" data-track-index="1">{st}</p>' if st else "")
        ),
        "stat": (
            f'<div class="bg"></div>'
            f'<div class="clip statnum" data-start="0.15" data-duration="{duration - 0.3:.1f}" data-track-index="1">{t}</div>'
            + (f'<div class="clip statlabel" data-start="0.5" data-duration="{duration - 0.6:.1f}" data-track-index="1">{st}</div>' if st else "")
        ),
        "cta": (
            f'<div class="bg"></div>'
            f'<h1 class="clip ctatitle" data-start="0.15" data-duration="{duration - 0.3:.1f}" data-track-index="1">{t or "Follow for more"}</h1>'
            f'<div class="clip ctabtn" data-start="0.6" data-duration="{duration - 0.7:.1f}" data-track-index="1">{st or "▶ Follow"}</div>'
        ),
        "lower": (
            f'<div class="clip lowerbar" data-start="0.1" data-duration="{duration - 0.2:.1f}" data-track-index="1">'
            f'<span class="lowertitle">{t}</span>'
            + (f'<span class="lowersub">{st}</span>' if st else "") + "</div>"
        ),
    }
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body style="margin:0">
<div id="stage" data-composition-id="ymoney-{kind}" data-start="0" data-duration="{duration:.1f}" data-width="1080" data-height="1920" data-fps="30" data-no-timeline style="width:1080px;height:1920px;background:#0a0a0c;position:relative;overflow:hidden;font-family:Arial,Helvetica,sans-serif">
<style>
.bg{{position:absolute;inset:0;background:radial-gradient(circle at 50% 30%,#182018 0%,#0a0a0c 70%)}}
.kick{{position:absolute;inset:0;background:{accent};opacity:.08;animation:kick {duration:.1f}s ease both}}
h1.hook{{position:absolute;top:640px;width:1080px;text-align:center;color:#fff;font-size:96px;line-height:1.08;margin:0;padding:0 70px;box-sizing:border-box;animation:pop {duration:.1f}s cubic-bezier(.2,1.4,.4,1) both}}
p.sub{{position:absolute;top:1080px;width:1080px;text-align:center;color:#d4d4d8;font-size:44px;margin:0;padding:0 90px;box-sizing:border-box;animation:rise {duration:.1f}s ease .35s both}}
.statnum{{position:absolute;top:560px;width:1080px;text-align:center;color:{accent};font-size:170px;font-weight:900;animation:pop {duration:.1f}s cubic-bezier(.2,1.4,.4,1) both}}
.statlabel{{position:absolute;top:820px;width:1080px;text-align:center;color:#fff;font-size:56px;padding:0 80px;box-sizing:border-box;animation:rise {duration:.1f}s ease .35s both}}
.ctatitle{{position:absolute;top:660px;width:1080px;text-align:center;color:#fff;font-size:88px;margin:0;padding:0 70px;box-sizing:border-box;animation:pop {duration:.1f}s cubic-bezier(.2,1.4,.4,1) both}}
.ctabtn{{position:absolute;top:1020px;left:290px;width:500px;text-align:center;color:#06130a;background:{accent};font-size:52px;font-weight:900;padding:28px 0;border-radius:80px;animation:rise {duration:.1f}s ease .45s both}}
.lowerbar{{position:absolute;left:60px;bottom:420px;max-width:960px;background:rgba(10,10,12,.82);border-left:14px solid {accent};padding:30px 40px;animation:slide {duration:.1f}s ease both}}
.lowertitle{{display:block;color:#fff;font-size:58px;font-weight:800}}
.lowersub{{display:block;color:#a1a1aa;font-size:40px;margin-top:8px}}
@keyframes pop{{from{{opacity:0;transform:scale(.75)}}to{{opacity:1;transform:scale(1)}}}}
@keyframes rise{{from{{opacity:0;transform:translateY(60px)}}to{{opacity:1;transform:none}}}}
@keyframes slide{{from{{opacity:0;transform:translateX(-80px)}}to{{opacity:1;transform:none}}}}
@keyframes kick{{from{{opacity:.22}}to{{opacity:.08}}}}
</style>
{bodies[kind]}
</div></body></html>"""


def render_card(kind: str, title: str, workspace_id: str, subtitle: str = "",
                accent: str = "#22c55e", duration: float = 3.0,
                filename: str | None = None) -> MotionCard:
    """Lint-gate then render a motion card to the workspace storage boundary.

    Stock defaults defer to the workspace's motion template override, so brand
    kits work without code changes: pass explicit values to bypass it.
    """
    try:
        from app.services.templates import resolve_template

        tpl = (resolve_template("motion", (kind or "hook").lower(),
                                workspace_id or None).get("payload") or {})
        if accent == "#22c55e" and tpl.get("accent"):
            accent = str(tpl["accent"])
        if duration == 3.0 and tpl.get("duration"):
            duration = float(tpl["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    probe = motion_status()
    if not probe["cli"]:
        raise MotionError("hyperframes CLI not installed (npm i -g hyperframes or npx)")
    if not probe["ffmpeg"]:
        raise MotionError("ffmpeg not found — install it to render motion cards")
    if not probe["browser"]:
        raise MotionError("no working Chrome for HyperFrames rendering (see: hyperframes browser ensure)")
    prefix = _npx()
    assert prefix is not None
    job_dir = WORK_DIR / workspace_id
    job_dir.mkdir(parents=True, exist_ok=True)
    html = build_composition(kind, title, subtitle, accent, duration)
    (job_dir / "index.html").write_text(html, encoding="utf-8")
    lint = subprocess.run(prefix + ["-y", "hyperframes", "lint", str(job_dir)],
                          capture_output=True, text=True, timeout=120)
    if lint.returncode != 0:
        raise MotionError(f"composition failed lint: {(lint.stdout or lint.stderr or '')[:400]}")
    out = job_dir / (filename or f"{kind}-card.mp4")
    if out.exists():
        out.unlink()
    proc = subprocess.run(
        prefix + ["-y", "hyperframes", "render", str(job_dir), "-o", str(out), "--quiet"],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0 or not out.exists():
        tail = ((proc.stderr or proc.stdout) or "")[-500:]
        raise MotionError(f"motion render failed: {tail[:400]}")
    # move into the workspace storage boundary for serving/publishing
    dest_dir = Path("data/videos") / workspace_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / out.name
    if dest.exists():
        dest.unlink()
    out.replace(dest)
    try:
        (job_dir / "index.html").unlink(missing_ok=True)
    except OSError:
        pass
    logger.info(f"[motion] rendered {kind} card: {dest}")
    return MotionCard(path=str(dest), kind=kind, duration=duration)


__all__ = [
    "CARD_KINDS",
    "MotionCard",
    "MotionError",
    "browser_healthy",
    "build_composition",
    "ffmpeg_present",
    "hyperframes_available",
    "hyperframes_version",
    "motion_status",
    "render_card",
]
