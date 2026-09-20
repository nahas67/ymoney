"""Compliance checks (E5 trust): platform spec preflight + reused-content risk.

Preflight validates a finished render against each target platform's public
specs (duration, aspect, resolution, size, container) BEFORE any network call —
spec failures become terminal job failures, never wasted uploads.

Reused-content risk scores how likely a video trips originality monetization
filters (YPP reused-content, TikTok originality, Meta unoriginal demotion):
own-catalog similarity, thin scripts, and missing visual planning.
"""

from __future__ import annotations

from pathlib import Path

# Verified against platform docs Sept 2026 (see ALL_IN_ONE_PLAN research notes).
PLATFORM_SPECS: dict[str, dict] = {
    "youtube": {
        "max_seconds": 180,   # Shorts cap (square/vertical, since Oct 2024)
        "aspects": ("9:16", "1:1", "16:9"),
        "short_aspects": ("9:16", "1:1"),
        "min_width": 250,
        "max_bytes": 256 * 1024**3,
        "containers": ("mp4", "mov"),
    },
    "tiktok": {
        "max_seconds": 600,   # 10-min uploads; short-form expected
        "aspects": ("9:16", "1:1"),
        "min_width": 250,
        "max_bytes": 4 * 1024**3,
        "containers": ("mp4", "mov"),
    },
    "facebook": {
        "min_seconds": 3,
        "max_seconds": 90,    # Reels; 60s+ may underperform/distribute poorly
        "aspects": ("9:16",),
        "min_width": 540,
        "max_bytes": 1024**3,
        "containers": ("mp4",),
    },
    "instagram": {
        "min_seconds": 3,
        "max_seconds": 900,   # uploads to 15 min; Reels tab favors ≤90s
        "tab_seconds": 90,
        "aspects": ("9:16",),
        "min_width": 250,
        "max_bytes": 300 * 1024**2,
        "containers": ("mp4", "mov"),
    },
}


def _aspect(w: int | None, h: int | None) -> str:
    if not w or not h:
        return "unknown"
    if abs(w * 16 - h * 9) <= max(w, h):
        return "9:16"
    if abs(w - h) <= max(w, h) * 0.02:
        return "1:1"
    if abs(w * 9 - h * 16) <= max(w, h):
        return "16:9"
    return "other"


def preflight(video_path: str, platform: str) -> dict:
    """Validate a render against one platform's specs.

    Returns {passed, platform, checks: [{name, severity: fail|warn|ok, detail}]}.
    Unknown platforms fail closed (never assume an API accepts the file).
    """
    from app.services.storage import probe_metadata

    spec = PLATFORM_SPECS.get((platform or "").lower())
    if spec is None:
        return {"passed": False, "platform": platform,
                "checks": [{"name": "platform", "severity": "fail",
                            "detail": f"no spec known for '{platform}' — refusing blind upload"}]}
    checks: list[dict] = []
    path = Path(video_path)
    if not path.exists():
        return {"passed": False, "platform": platform,
                "checks": [{"name": "file", "severity": "fail", "detail": "video file missing"}]}
    size = path.stat().st_size
    if size <= 0:
        checks.append({"name": "size", "severity": "fail", "detail": "video file is empty"})
    elif size > spec["max_bytes"]:
        checks.append({"name": "size", "severity": "fail",
                       "detail": f"{size / 1024**2:.0f}MB exceeds {spec['max_bytes'] / 1024**2:.0f}MB cap"})
    else:
        checks.append({"name": "size", "severity": "ok",
                       "detail": f"{size / 1024 / 1024:.1f}MB within cap"})
    meta = probe_metadata(path)
    dur = meta.get("duration_seconds") or 0
    width, height = meta.get("width"), meta.get("height")
    fmt = str(meta.get("format") or "")
    if not dur:
        checks.append({"name": "duration", "severity": "warn",
                       "detail": "duration unreadable — platform may reject"})
    else:
        if spec.get("min_seconds") and dur < spec["min_seconds"]:
            checks.append({"name": "duration", "severity": "fail",
                           "detail": f"{dur:.0f}s below {spec['min_seconds']}s minimum"})
        elif dur > spec["max_seconds"]:
            checks.append({"name": "duration", "severity": "fail",
                           "detail": f"{dur:.0f}s exceeds {spec['max_seconds']}s cap"})
        else:
            checks.append({"name": "duration", "severity": "ok", "detail": f"{dur:.0f}s in spec"})
            if platform == "instagram" and dur > spec.get("tab_seconds", 90):
                checks.append({"name": "reels-tab", "severity": "warn",
                               "detail": f"{dur:.0f}s uploads fine but Reels tab favors ≤90s"})
            if platform == "facebook" and dur > 60:
                checks.append({"name": "reels-length", "severity": "warn",
                               "detail": f"{dur:.0f}s may distribute poorly as a Reel (sweet spot ≤60s)"})
    aspect = _aspect(width, height)
    if aspect == "unknown":
        checks.append({"name": "aspect", "severity": "warn", "detail": "dimensions unreadable"})
    elif aspect not in spec["aspects"]:
        severity = "fail" if platform in ("facebook", "instagram") else "warn"
        checks.append({"name": "aspect", "severity": severity,
                       "detail": f"{aspect} ({width}x{height}) — {platform} wants {', '.join(spec['aspects'])}"})
    else:
        checks.append({"name": "aspect", "severity": "ok", "detail": f"{aspect} {width}x{height}"})
        if platform == "youtube" and aspect not in spec["short_aspects"]:
            checks.append({"name": "shorts", "severity": "warn",
                           "detail": "horizontal video uploads as long-form, not a Short"})
    if width and width < spec["min_width"]:
        checks.append({"name": "resolution", "severity": "fail" if platform == "facebook" else "warn",
                       "detail": f"width {width}px below {spec['min_width']}px floor"})
    elif width:
        checks.append({"name": "resolution", "severity": "ok", "detail": f"{width}px wide"})
        if width < 720:
            checks.append({"name": "resolution-hd", "severity": "warn",
                           "detail": "below 720p — soft/enforcement risk on some surfaces"})
    suffix = path.suffix.lower().lstrip(".")
    if suffix and suffix not in spec["containers"] and "mp4" not in fmt:
        checks.append({"name": "container", "severity": "warn",
                       "detail": f".{suffix} — {platform} prefers {', '.join(spec['containers'])}"})
    else:
        checks.append({"name": "container", "severity": "ok", "detail": f".{suffix or 'mp4'} accepted"})
    failed = [c for c in checks if c["severity"] == "fail"]
    return {"passed": not failed, "platform": platform, "checks": checks}


def reused_content_score(topic: str, script: str, workspace_id: str,
                         visual_keywords: list[str] | None = None) -> dict:
    """0-100 reused-content risk + level + reasons (higher = riskier)."""
    from app.db import session_scope
    from app.engine.decision import topic_similarity
    from app.models import ContentItem
    from sqlalchemy import select

    score = 8.0  # baseline: stock/AI pipeline carries inherent reuse risk
    reasons = ["AI/stock composite baseline"]
    words = len((script or "").split())
    if words and words < 40:
        score += 14
        reasons.append(f"thin script ({words} words)")
    if not (visual_keywords or []):
        score += 8
        reasons.append("no visual plan")
    worst, worst_topic = 0.0, ""
    try:
        with session_scope() as s:
            rows = s.scalars(
                select(ContentItem.topic)
                .where(ContentItem.workspace_id == workspace_id,
                       ContentItem.status.in_(["PUBLISHED", "ANALYZING", "LEARNED"]))
                .order_by(ContentItem.created_at.desc()).limit(40)
            ).all()
            for rt in rows:
                sim = topic_similarity(topic or "", rt or "")
                if sim > worst:
                    worst, worst_topic = sim, rt
    except Exception:
        rows = []
    if rows:
        score += min(45.0, worst * 55)
        if worst >= 0.3:
            reasons.append(f"{worst:.0%} similar to own catalog '{(worst_topic or '')[:50]}'")
    score = max(0.0, min(100.0, round(score, 1)))
    level = "high" if score >= 65 else ("medium" if score >= 35 else "low")
    return {"score": score, "level": level, "reasons": reasons,
            "worst_similarity": round(worst, 3)}


__all__ = ["PLATFORM_SPECS", "preflight", "reused_content_score"]
