"""Export profiles: the 8 builtin presets + their config validation.

Contracts §11. Every builtin preset maps to a ``config_json`` of the shape::

    {width, height, fps, video_codec, bitrate_kbps, audio_codec,
     audio_bitrate_kbps, audio_channels, captions: {enabled, formats[]},
     watermark: {enabled, text?, asset_id?}, color: {matrix, transfer} | null}

Two presets deliberately have **no** video stream:

* ``AUDIO_ONLY``  -- ``width``/``height`` are ``None`` and the only legal
  container formats are ``MP3`` / ``WAV``.
* ``CAPTIONS_ONLY`` -- no media at all; only ``SRT``/``VTT``/``ASS``/``TXT``.

Everything is validated in one place (``validate_config``) so the API, the
format registry and the job executor all reject the same thing for the same
reason. Raises :class:`ExportValidationError` (a ``ValueError``) which the
route maps to HTTP 422.

Row persistence (``seed_builtins`` / ``list_profiles`` / ``create_profile`` /
``update_profile``) uses the Lane F ``ExportProfile`` table as-is: the 8
builtins are global rows with ``workspace_id = NULL`` so every workspace sees
them, and a workspace clone is the only way to edit one (the API answers 409
for a PUT on a builtin row -- see ``api/v1/exports.py``).
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExportProfile

logger = logging.getLogger("ymoney.collab")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: The 8 preset names carried by ``export_profiles.preset``.
PRESETS: tuple[str, ...] = (
    "YOUTUBE_4K",
    "YOUTUBE_1080P",
    "SHORTS_1080x1920",
    "INSTAGRAM_REEL",
    "TIKTOK",
    "ARCHIVE_MASTER",
    "AUDIO_ONLY",
    "CAPTIONS_ONLY",
)

#: Presets that produce no video stream (width/height are ``None``).
NO_VIDEO_PRESETS: frozenset[str] = frozenset({"AUDIO_ONLY", "CAPTIONS_ONLY"})

#: Legal container formats per preset family. The format registry is the other
#: half of the check (``available()``) -- this is the *intent* half.
AUDIO_ONLY_FORMATS: frozenset[str] = frozenset({"MP3", "WAV"})
CAPTIONS_ONLY_FORMATS: frozenset[str] = frozenset({"SRT", "VTT", "ASS", "TXT"})

VIDEO_CODECS: frozenset[str] = frozenset({"h264", "libx264", "hevc", "libvpx", "libvpx-vp9"})
AUDIO_CODECS: frozenset[str] = frozenset({"aac", "mp3", "libmp3lame", "pcm_s16le", "libopus", "libvorbis"})

#: container format -> legal video codecs (empty = audio/text container)
FORMAT_VIDEO_CODECS: dict[str, frozenset[str]] = {
    "MP4": frozenset({"h264", "libx264", "hevc"}),
    "MOV": frozenset({"h264", "libx264", "hevc", "prores"}),
    "WebM": frozenset({"libvpx", "libvpx-vp9", "vp8", "vp9"}),
}
#: container format -> legal audio codecs
FORMAT_AUDIO_CODECS: dict[str, frozenset[str]] = {
    "MP4": frozenset({"aac"}),
    "MOV": frozenset({"aac", "pcm_s16le"}),
    "WebM": frozenset({"libopus", "libvorbis"}),
    "MP3": frozenset({"mp3", "libmp3lame"}),
    "WAV": frozenset({"pcm_s16le"}),
}

MIN_FPS = 1.0
MAX_FPS = 120.0

#: Preset -> (width, height, fps, video_codec, bitrate, audio, a-bitrate,
#:             channels, captions, watermark, color)
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "YOUTUBE_4K": {
        "name": "YouTube 4K",
        "config": {
            "width": 3840, "height": 2160, "fps": 30.0,
            "video_codec": "libx264", "bitrate_kbps": 45000,
            "audio_codec": "aac", "audio_bitrate_kbps": 384, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "VTT"]},
            "watermark": {"enabled": False},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    "YOUTUBE_1080P": {
        "name": "YouTube 1080p",
        "config": {
            "width": 1920, "height": 1080, "fps": 30.0,
            "video_codec": "libx264", "bitrate_kbps": 8000,
            "audio_codec": "aac", "audio_bitrate_kbps": 192, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "VTT"]},
            "watermark": {"enabled": False},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    "SHORTS_1080x1920": {
        "name": "Shorts 1080x1920",
        "config": {
            "width": 1080, "height": 1920, "fps": 30.0,
            "video_codec": "libx264", "bitrate_kbps": 6000,
            "audio_codec": "aac", "audio_bitrate_kbps": 192, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "VTT", "ASS"]},
            "watermark": {"enabled": True, "text": "YMONEY"},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    "INSTAGRAM_REEL": {
        "name": "Instagram Reel",
        "config": {
            "width": 1080, "height": 1920, "fps": 30.0,
            "video_codec": "libx264", "bitrate_kbps": 5500,
            "audio_codec": "aac", "audio_bitrate_kbps": 160, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "VTT"]},
            "watermark": {"enabled": True, "text": "YMONEY"},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    "TIKTOK": {
        "name": "TikTok",
        "config": {
            "width": 1080, "height": 1920, "fps": 30.0,
            "video_codec": "libx264", "bitrate_kbps": 5000,
            "audio_codec": "aac", "audio_bitrate_kbps": 160, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "VTT", "ASS"]},
            "watermark": {"enabled": True, "text": "YMONEY"},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    # ARCHIVE_MASTER is source passthrough on purpose: width/height/fps are
    # None so the exporter copies the source geometry and only raises the
    # bitrate quality ceiling. Documented in the module docstring of
    # formats.py -- a fixed 1080p master would silently downsample 4K masters.
    "ARCHIVE_MASTER": {
        "name": "Archive Master",
        "config": {
            "width": None, "height": None, "fps": None,
            "video_codec": "libx264", "bitrate_kbps": 24000,
            "audio_codec": "aac", "audio_bitrate_kbps": 320, "audio_channels": 2,
            "captions": {"enabled": True, "formats": ["SRT", "TXT"]},
            "watermark": {"enabled": False},
            "color": {"matrix": "bt709", "transfer": "bt709"},
        },
    },
    "AUDIO_ONLY": {
        "name": "Audio Only",
        "config": {
            "width": None, "height": None, "fps": None,
            # audio_codec is deliberately null: AUDIO_ONLY may target MP3
            # (libmp3lame) OR WAV (pcm_s16le), and no single codec is legal
            # for both. null means "the container decides" -- formats.py
            # hardcodes the container-legal encoder per format.
            "video_codec": None, "bitrate_kbps": None,
            "audio_codec": None, "audio_bitrate_kbps": 192, "audio_channels": 2,
            "captions": {"enabled": False, "formats": []},
            "watermark": {"enabled": False},
            "color": None,
        },
    },
    "CAPTIONS_ONLY": {
        "name": "Captions Only",
        "config": {
            "width": None, "height": None, "fps": None,
            "video_codec": None, "bitrate_kbps": None,
            "audio_codec": None, "audio_bitrate_kbps": None, "audio_channels": None,
            "captions": {"enabled": True, "formats": ["SRT", "VTT", "ASS", "TXT"]},
            "watermark": {"enabled": False},
            "color": None,
        },
    },
}


#: Case-insensitive lookup: "shorts_1080x1920" and "SHORTS_1080X1920" both map
#: back to the canonical "SHORTS_1080x1920" -- the lowercase `x` in the
#: contract's preset name means a bare ``.upper()`` dict lookup would never
#: match it and would report a valid preset as unknown.
_PRESET_LOOKUP: dict[str, str] = {p.upper(): p for p in PRESETS}


def canonical_preset(preset: str | None) -> str:
    """Canonical preset name, or the input unchanged when it is unknown.

    This helper only normalizes case -- callers that must REJECT an unknown
    preset check membership in :data:`PRESETS` (or catch
    :class:`ExportValidationError`).
    """
    return _PRESET_LOOKUP.get(str(preset or "").upper(), str(preset or ""))


class ExportValidationError(ValueError):
    """Config / preset / format mismatch. Routes map this to HTTP 422."""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExportValidationError(f"{field} must be an integer") from None


def _validate_dimensions(config: dict) -> None:
    width, height = config.get("width"), config.get("height")
    if width is None and height is None:
        return  # source passthrough / no video
    if width is None or height is None:
        raise ExportValidationError("width and height must both be set or both null")
    w, h = _as_int(width, "width"), _as_int(height, "height")
    if w <= 0 or h <= 0:
        raise ExportValidationError("width and height must be greater than 0")


def _validate_fps(config: dict) -> None:
    fps = config.get("fps")
    if fps is None:
        return
    try:
        value = float(fps)
    except (TypeError, ValueError):
        raise ExportValidationError("fps must be a number") from None
    if not MIN_FPS <= value <= MAX_FPS:
        raise ExportValidationError(
            f"fps must be between {MIN_FPS:g} and {MAX_FPS:g}")


def validate_config(config: dict | None) -> dict:
    """Validate + normalize a config_json. Returns a fresh dict.

    Raises :class:`ExportValidationError` (a ``ValueError``) for every
    rejected shape: non-positive dimensions, fps outside 1..120, unknown
    codecs, malformed captions/watermark/color blocks.
    """
    if not isinstance(config, dict):
        raise ExportValidationError("config must be an object")
    out = copy.deepcopy(config)
    _validate_dimensions(out)
    _validate_fps(out)

    video_codec = out.get("video_codec")
    if video_codec is not None and str(video_codec) not in VIDEO_CODECS:
        raise ExportValidationError(f"unknown video_codec '{video_codec}'")
    audio_codec = out.get("audio_codec")
    if audio_codec is not None and str(audio_codec) not in AUDIO_CODECS:
        raise ExportValidationError(f"unknown audio_codec '{audio_codec}'")

    bitrate = out.get("bitrate_kbps")
    if bitrate is not None and _as_int(bitrate, "bitrate_kbps") <= 0:
        raise ExportValidationError("bitrate_kbps must be greater than 0")

    captions = out.get("captions") or {}
    if not isinstance(captions, dict):
        raise ExportValidationError("captions must be an object")
    formats = captions.get("formats") or []
    if not isinstance(formats, list) or any(
            not isinstance(f, str) or not f for f in formats):
        raise ExportValidationError("captions.formats must be a list of strings")
    captions["enabled"] = bool(captions.get("enabled"))
    captions["formats"] = [f for f in formats]
    out["captions"] = captions

    watermark = out.get("watermark") or {}
    if not isinstance(watermark, dict):
        raise ExportValidationError("watermark must be an object")
    watermark["enabled"] = bool(watermark.get("enabled"))
    out["watermark"] = watermark

    color = out.get("color")
    if color is not None and not isinstance(color, dict):
        raise ExportValidationError("color must be an object or null")
    return out


#: Case-insensitive container lookup: "webm" / "WEBM" / "WebM" all resolve to
#: the one "WebM" entry. Without this a ``.upper()``-ed format string would
#: silently SKIP the container/codec check and hand libx264 to a .webm muxer.
_VIDEO_CODECS_UPPER: dict[str, frozenset[str]] = {
    k.upper(): v for k, v in FORMAT_VIDEO_CODECS.items()
}
_AUDIO_CODECS_UPPER: dict[str, frozenset[str]] = {
    k.upper(): v for k, v in FORMAT_AUDIO_CODECS.items()
}


def check_profile_format(preset: str, config: dict, fmt: str) -> None:
    """Reject a format the preset can never produce (contracts §11).

    * ``AUDIO_ONLY``  -> MP3 | WAV only
    * ``CAPTIONS_ONLY`` -> SRT | VTT | ASS | TXT only
    * any video config -> codecs must be legal for the container
    """
    # keep the caller's own spelling for error messages ("WebM", not "WEBM")
    label = str(fmt or "").strip() or "that format"
    fmt = label.upper()
    key = canonical_preset(preset)
    if key == "AUDIO_ONLY" and fmt not in AUDIO_ONLY_FORMATS:
        raise ExportValidationError(
            f"AUDIO_ONLY supports only {sorted(AUDIO_ONLY_FORMATS)}, got '{fmt}'")
    if key == "CAPTIONS_ONLY" and fmt not in CAPTIONS_ONLY_FORMATS:
        raise ExportValidationError(
            f"CAPTIONS_ONLY supports only {sorted(CAPTIONS_ONLY_FORMATS)}, got '{fmt}'")
    if fmt in _VIDEO_CODECS_UPPER:
        codec = config.get("video_codec")
        if codec is not None and str(codec) not in _VIDEO_CODECS_UPPER[fmt]:
            raise ExportValidationError(
                f"video_codec '{codec}' is not valid for {label} "
                f"(expected one of {sorted(_VIDEO_CODECS_UPPER[fmt])})")
        # The audio codec is only checked for the VIDEO containers: MP3 and
        # WAV each fix their own codec (libmp3lame / pcm_s16le), so a preset
        # that names aac for its video track must still be allowed to extract
        # an MP3 -- formats.py picks the container-legal encoder there.
        if fmt in _AUDIO_CODECS_UPPER:
            codec = config.get("audio_codec")
            if codec is not None and str(codec) not in _AUDIO_CODECS_UPPER[fmt]:
                raise ExportValidationError(
                    f"audio_codec '{codec}' is not valid for {label} "
                    f"(expected one of {sorted(_AUDIO_CODECS_UPPER[fmt])})")


def preset_config(preset: str) -> dict:
    """A fresh copy of one builtin preset's config (validated on the way out)."""
    key = canonical_preset(preset)
    if key not in BUILTIN_PROFILES:
        raise ExportValidationError(
            f"unknown preset '{preset}'; expected one of {list(PRESETS)}")
    return validate_config(copy.deepcopy(BUILTIN_PROFILES[key]["config"]))


def preset_name(preset: str) -> str:
    """Display name of a builtin preset (``preset`` itself when unknown)."""
    key = canonical_preset(preset)
    return str(BUILTIN_PROFILES.get(key, {}).get("name") or key or "custom")


# ---------------------------------------------------------------------------
# row persistence
# ---------------------------------------------------------------------------


def seed_builtins(db: Session) -> list[ExportProfile]:
    """Idempotently insert the 8 builtin profiles as global (ws_id NULL) rows."""
    existing = {
        str(row.preset): row
        for row in db.scalars(
            select(ExportProfile).where(ExportProfile.is_builtin.is_(True))
        ).all()
    }
    out: list[ExportProfile] = []
    for preset in PRESETS:
        row = existing.get(preset)
        if row is not None:
            out.append(row)
            continue
        row = ExportProfile(
            workspace_id=None,
            name=preset_name(preset),
            preset=preset,
            config_json=preset_config(preset),
            is_builtin=True,
            created_by=None,
        )
        db.add(row)
        out.append(row)
    db.flush()
    return out


def list_profiles(db: Session, workspace_id: str) -> list[ExportProfile]:
    """Global builtins + this workspace's own profiles, presets first."""
    return list(
        db.scalars(
            select(ExportProfile)
            .where(
                (ExportProfile.workspace_id.is_(None))
                | (ExportProfile.workspace_id == workspace_id)
            )
            .order_by(ExportProfile.is_builtin.desc(), ExportProfile.created_at)
        ).all()
    )


def load_profile(db: Session, workspace_id: str, profile_id: str) -> ExportProfile:
    """Workspace-scoped profile fetch; a foreign id reads as missing (404)."""
    row = db.get(ExportProfile, str(profile_id or ""))
    if row is None:
        raise KeyError("profile not found")
    if row.workspace_id is not None and row.workspace_id != workspace_id:
        raise KeyError("profile not found")
    return row


def create_profile(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    preset: str | None = None,
    config: dict | None = None,
    created_by: str | None = None,
) -> ExportProfile:
    """Create a workspace profile, optionally cloning a builtin preset.

    ``config`` wins over ``preset`` when both are given; a bare preset clone
    starts from the builtin config so a POST can never invent an invalid one.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ExportValidationError("name is required")
    if config is None:
        if not preset:
            raise ExportValidationError("either preset or config is required")
        base = preset_config(preset)
    else:
        base = validate_config(config)
        if preset:
            preset_config(preset)  # rejects an unknown preset early
    row = ExportProfile(
        workspace_id=workspace_id,
        name=clean_name[:120],
        preset=canonical_preset(preset) or "CUSTOM",
        config_json=base,
        is_builtin=False,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def update_profile(
    db: Session, row: ExportProfile, *, name: str | None = None,
    config: dict | None = None,
) -> ExportProfile:
    """Update a workspace (non-builtin) profile in place."""
    if row.is_builtin:
        raise ValueError("builtin profiles are read-only; clone the preset instead")
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ExportValidationError("name is required")
        row.name = clean[:120]
    if config is not None:
        row.config_json = validate_config(config)
    db.flush()
    return row


def watermark_text(db: Session | None, workspace_id: str, config: dict) -> str:
    """Watermark text, falling back to the BrandDNA brand name.

    Best effort by design: an explicit ``watermark.text`` wins, then the
    brand module's effective name, then empty. Any failure degrades silently
    to empty -- a missing brand must never fail an export.
    """
    watermark = (config or {}).get("watermark") or {}
    if not watermark.get("enabled"):
        return ""
    explicit = str(watermark.get("text") or "").strip()
    if explicit:
        return explicit
    if db is None:
        return ""
    try:
        from app.models import BrandEffectiveConfig  # noqa: PLC0415 - optional
    except Exception:  # pragma: no cover - brand module always present
        return ""
    try:
        row = db.scalars(
            select(BrandEffectiveConfig)
            .where(BrandEffectiveConfig.workspace_id == workspace_id)
            .order_by(BrandEffectiveConfig.created_at.desc())
            .limit(1)
        ).first()
    except Exception:  # pragma: no cover - degrade, never fail the export
        return ""
    if row is None:
        return ""
    payload = dict(row.effective_json or {})
    effective = payload.get("effective")
    brand = dict(effective) if isinstance(effective, dict) else payload
    return str(brand.get("brand_name") or brand.get("name") or "").strip()


__all__ = [
    "AUDIO_ONLY_FORMATS",
    "BUILTIN_PROFILES",
    "CAPTIONS_ONLY_FORMATS",
    "ExportValidationError",
    "FORMAT_AUDIO_CODECS",
    "FORMAT_VIDEO_CODECS",
    "NO_VIDEO_PRESETS",
    "PRESETS",
    "check_profile_format",
    "create_profile",
    "list_profiles",
    "load_profile",
    "preset_config",
    "preset_name",
    "seed_builtins",
    "update_profile",
    "validate_config",
    "watermark_text",
]
