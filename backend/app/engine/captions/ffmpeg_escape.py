"""Shared, hardened ffmpeg text primitives (Work 13).

These helpers are the ONLY sanctioned way a workspace-controlled string
reaches the ffmpeg filter graph. They were hardened in Work 11.5 (C-F4) and
are now shared by the pre-existing renderer
(``app.providers.video_engine.timeline_render``) and the Work 13 caption /
motion builder so there is exactly ONE implementation to audit.

Two invariants:

1. **No arbitrary filter fragments.** Nothing here accepts a caller-supplied
   ffmpeg expression. Values are escaped, colours are allowlisted, positions
   are computed from arithmetic, and enums are resolved through closed maps.
2. **Honest degradation.** When no font can be resolved the caller is told so
   and skips text overlays rather than rendering a broken frame.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "FONT_CANDIDATES",
    "escape_drawtext",
    "escape_filterfile",
    "escape_filter_value",
    "family_font_candidates",
    "resolve_font",
    "safe_fontcolor",
    "safe_text_color",
    "wrap_text",
]

#: Default render fonts, in fallback order.
FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

#: Where a named family may live, per platform. Used only to HONOUR an
#: explicit family request; the default list above is still the fallback.
_FAMILY_DIRS: dict[str, tuple[str, ...]] = {
    "win32": ("C:/Windows/Fonts/",),
    "linux": (
        "/usr/share/fonts/truetype/dejavu/",
        "/usr/share/fonts/truetype/liberation/",
        "/usr/share/fonts/",
    ),
    "darwin": ("/System/Library/Fonts/", "/Library/Fonts/"),
}

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def escape_drawtext(text: str) -> str:
    """Escape a ``drawtext`` text payload (inside single quotes).

    Filter-parser specials are escaped; newlines become spaces because
    ``drawtext`` cannot render them in a single filter instance.
    """
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace(":", "\\:")
            .replace(",", "\\,")
            .replace("\n", " "))


def escape_filterfile(path: str) -> str:
    """Escape a filesystem path used as a ``fontfile='...'`` value.

    The drive-letter colon (``C:/...``) splits filter options unless escaped;
    the proven form is ``fontfile='C\\:/Windows/Fonts/arial.ttf'``.
    """
    return (str(path or "")
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace(",", "\\,")
            .replace("'", "\\'")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace(";", "\\;"))


def escape_filter_value(value: str) -> str:
    """Escape an unquoted filter-graph option value.

    ``color``/``x``/``y`` and every other option value here originates from
    timeline clip dicts (workspace-controlled). An unescaped ``:`` breaks out
    of one option into the next; ``'``, ``,``, ``[``/``]`` and ``;`` break the
    graph structure itself. Backslash is replaced first so the escaping is
    idempotent.
    """
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(";", "\\;")
    )


def safe_fontcolor(color: str, *, fallback: str = "white") -> str:
    """Named colours and ``#hex`` only; anything else falls back.

    This is an allowlist, not an escaper: a colour that is not provably inert
    never reaches the filter graph at all.
    """
    candidate = str(color or "").strip()
    if _HEX_COLOR_RE.match(candidate):
        return candidate.lower()
    if re.fullmatch(r"[A-Za-z]{3,24}", candidate):
        return candidate.lower()
    return fallback


#: Alias with a clearer name for new call sites.
safe_text_color = safe_fontcolor


def family_font_candidates(family: str) -> list[str]:
    """Candidate files for a requested font family, best first.

    An empty list means "no opinion" and the caller falls back to
    :data:`FONT_CANDIDATES`. The family token is validated by
    ``CaptionStyle`` before it ever gets here, and the result is only ever a
    list of candidate PATHS that are then existence-checked -- no path from a
    caption is trusted without ``Path(...).exists()``.
    """
    token = str(family or "").strip()
    if not token:
        return []
    slug = token.replace(" ", "").replace("-", "").replace("_", "")
    if not slug or not re.fullmatch(r"[A-Za-z0-9]{1,40}", slug):
        return []
    dirs: list[str] = []
    if os.name == "nt":
        dirs.extend(_FAMILY_DIRS["win32"])
    else:
        dirs.extend(_FAMILY_DIRS["linux"])
        dirs.extend(_FAMILY_DIRS["darwin"])
    out: list[str] = []
    for directory in dirs:
        for suffix in (".ttf", ".otf", ".ttc"):
            out.append(f"{directory}{slug}{suffix}")
    return out


def resolve_font(family: str = "", *, env_var: str = "YMONEY_FONT_FILE") -> str | None:
    """Resolve a render font file, or ``None`` when none exists.

    Order: explicit env override -> requested family -> default candidates.
    Returns ``None`` rather than a guess so the caller can degrade honestly
    (skip overlays + record a warning) instead of rendering unreadable text.
    """
    override = os.environ.get(env_var, "")
    if override and Path(override).exists():
        return override
    for cand in family_font_candidates(family):
        if Path(cand).exists():
            return cand
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def wrap_text(text: str, max_chars_per_line: int) -> str:
    """Wrap on word boundaries for ``drawtext`` (which cannot wrap itself).

    A word longer than the limit is left intact rather than chopped -- a
    chopped word is worse than an overflowing one, and QC reports the
    overflow.
    """
    limit = max(4, int(max_chars_per_line or 0) or 32)
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        words = paragraph.split()
        if not words:
            continue
        current: list[str] = []
        length = 0
        for word in words:
            add = len(word) + (1 if length else 0)
            if length + add > limit and current:
                lines.append(" ".join(current))
                current = [word]
                length = len(word)
            else:
                current.append(word)
                length += add
        if current:
            lines.append(" ".join(current))
    return "\n".join(lines)