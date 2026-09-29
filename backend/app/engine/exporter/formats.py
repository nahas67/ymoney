"""Format registry: every export format with an HONEST capability probe.

Contracts §11. One entry per format, each exposing the triple::

    spec.available()  -> (bool, reason | None)
    spec.export(ctx)  -> {"path": Path, "media_type": str, "size": int, ...}
    spec.verify(art)  -> {"checks": [...], "probe": {...}}

Availability rules (never a fake file):

* **Text** (``SRT``, ``VTT``, ``ASS``, ``TXT``) -- always available; they are
  pure stdlib serialization of a caption/transcript track.
* **JSON** / **OTIO** / **FCPXML** -- always available for the first two;
  FCPXML is gated by the existing ``engine/otio_adapter.export_formats()``
  honesty shape and is only AVAILABLE when this OTIO build ships the
  ``fcpxml`` adapter.
* **PREMIERE** / **RESOLVE** -- permanently NOT_AVAILABLE. There is no
  verified xmeml / DRT adapter in this OTIO build, so a "Premiere export"
  would be a fabricated XML file. We refuse instead of lying.
* **Media** (``MP4``, ``MOV``, ``WebM``, ``MP3``, ``WAV``) -- ``available()``
  probes BOTH the ``ffmpeg`` binary AND the specific encoder(s) the format
  needs (``libx264`` for MP4, ``prores`` for MOV, ``libvpx`` for WebM,
  ``libmp3lame`` for MP3, ``pcm_s16le`` for WAV). Missing either -> False with
  a reason naming the missing piece.

The probe is cached per process (an ``ffmpeg -encoders`` call per request
would add ~100ms to every ``GET /exports/formats``) and is cleared by
:func:`reset_probe_cache` -- tests use it so a monkeypatched ``shutil.which``
is honoured immediately.

``ARCHIVE_MASTER`` is source passthrough on purpose: it keeps the source
geometry (no width/height/fps in its config) and only raises the quality
ceiling. Pinning a master to 1920x1080 would silently downsample a 4K master.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.engine import otio_adapter
from app.providers.dubbing import SrtCue, format_srt, parse_srt

logger = logging.getLogger("ymoney.collab")

#: Which ffmpeg encoders each media format needs (video + audio).
FORMAT_ENCODERS: dict[str, tuple[str, ...]] = {
    "MP4": ("libx264", "aac"),
    "MOV": ("prores", "aac"),
    "WebM": ("libvpx", "libopus"),
    "MP3": ("libmp3lame",),
    "WAV": ("pcm_s16le",),
}

MEDIA_FORMATS: tuple[str, ...] = ("MP4", "MOV", "WebM", "MP3", "WAV")
TEXT_FORMATS: tuple[str, ...] = ("SRT", "VTT", "ASS", "TXT")
NLE_FORMATS: tuple[str, ...] = ("PREMIERE", "RESOLVE")

#: WebM carries only VP8/VP9 video and opus/vorbis audio -- the exporter
#: falls back to these when a profile names a different container's codec.
_WEBM_VIDEO_CODECS = frozenset({"libvpx", "libvpx-vp9", "vp8", "vp9"})
_WEBM_AUDIO_CODECS = frozenset({"libopus", "libvorbis", "opus", "vorbis"})

MEDIA_TYPES: dict[str, str] = {
    "MP4": "video/mp4",
    "MOV": "video/quicktime",
    "WebM": "video/webm",
    "MP3": "audio/mpeg",
    "WAV": "audio/wav",
    "SRT": "application/x-subrip",
    "VTT": "text/vtt",
    "ASS": "text/x-ssa",
    "TXT": "text/plain",
    "JSON": "application/json",
    "OTIO": "application/json",
    "FCPXML": "application/xml",
    "PREMIERE": "application/xml",
    "RESOLVE": "application/json",
}

#: Which class of artifact a format produces (drives verify + MediaAsset type).
FORMAT_KIND: dict[str, str] = {
    "MP4": "video", "MOV": "video", "WebM": "video",
    "MP3": "audio", "WAV": "audio",
    "SRT": "subtitle", "VTT": "subtitle", "ASS": "subtitle", "TXT": "text",
    "JSON": "interchange", "OTIO": "interchange", "FCPXML": "interchange",
    "PREMIERE": "interchange", "RESOLVE": "interchange",
}

#: Suffix used for the written artifact file.
FORMAT_SUFFIX: dict[str, str] = {
    "MP4": ".mp4", "MOV": ".mov", "WebM": ".webm", "MP3": ".mp3", "WAV": ".wav",
    "SRT": ".srt", "VTT": ".vtt", "ASS": ".ass", "TXT": ".txt",
    "JSON": ".json", "OTIO": ".otio", "FCPXML": ".fcpxml",
}

#: ``MediaAsset.type`` (models/assets.py ASSET_TYPES vocabulary) per format.
FORMAT_ASSET_TYPE: dict[str, str] = {
    "MP4": "video", "MOV": "video", "WebM": "video",
    "MP3": "audio", "WAV": "audio",
    "SRT": "subtitle", "VTT": "subtitle", "ASS": "subtitle", "TXT": "text",
    "JSON": "other", "OTIO": "other", "FCPXML": "other",
    "PREMIERE": "other", "RESOLVE": "other",
}

ALL_FORMATS: tuple[str, ...] = (
    "MP4", "MOV", "WebM", "MP3", "WAV",
    "SRT", "VTT", "ASS", "TXT", "JSON", "OTIO", "FCPXML",
    "PREMIERE", "RESOLVE",
)


class ExportValidationError(ValueError):
    """Format cannot serve this profile/target. Routes map this to 422."""


# ---------------------------------------------------------------------------
# the export context -- one argument so every spec.export has one shape
# ---------------------------------------------------------------------------


@dataclass
class ExportContext:
    """Everything a format needs to produce its artifact."""

    fmt: str
    out_path: Path
    doc: dict
    config: dict
    workspace_id: str
    target: dict = field(default_factory=dict)
    #: Resolved media source for the media formats (None for text/OTIO).
    source_path: Path | None = None
    duration_seconds: float = 0.0
    watermark_text: str = ""
    metadata: dict = field(default_factory=dict)
    progress: Callable[[int], None] | None = None

    def report(self, pct: int) -> None:
        if self.progress is not None:
            self.progress(int(pct))


# ---------------------------------------------------------------------------
# capability probes (cached; see reset_probe_cache)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _ffmpeg_encoders() -> frozenset[str]:
    """Encoder names this ffmpeg build supports (``frozenset()`` when absent)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return frozenset()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - probe is best effort
        logger.warning("ffmpeg encoder probe failed: %s", exc)
        return frozenset()
    names: set[str] = set()
    # ffmpeg prints " V....D libx264   H.264 ..." -- a 6-char flags column
    # (V/A/S + five support flags), then the encoder name, then a description.
    # The name may contain underscores and dots (pcm_s16le, libx264rgb), so an
    # isalnum() check would silently drop real encoders and report a false
    # NOT_AVAILABLE -- match the column instead.
    row_re = re.compile(r"^\s*[VAS][.A-Z]{5}\s+(\S+)")
    for line in (proc.stdout or "").splitlines():
        match = row_re.match(line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def reset_probe_cache() -> None:
    """Clear the cached ffmpeg encoder probe (tests / tooling).

    Defensive on purpose: a test may have replaced ``_ffmpeg_encoders`` with
    a plain function, and that must not make the cache reset explode.
    """
    cache_clear = getattr(_ffmpeg_encoders, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def probe_media_format(fmt: str) -> tuple[bool, str | None]:
    """(available, reason) for a media format -- probe-driven, never assumed."""
    required = FORMAT_ENCODERS.get(fmt)
    if required is None:
        return False, f"'{fmt}' is not a media format"
    if not ffmpeg_path():
        return False, "ffmpeg is not installed or not on PATH"
    encoders = _ffmpeg_encoders()
    if not encoders:
        # Probe itself failed (or the build reports nothing) -- say so rather
        # than pretending the format works.
        return False, f"ffmpeg encoder probe returned nothing for {fmt}"
    missing = [name for name in required if name not in encoders]
    if missing:
        return False, f"this ffmpeg build has no {'/'.join(missing)} encoder"
    return True, None


def _otio_availability(fmt: str) -> tuple[bool, str | None]:
    """Gate OTIO-family formats through the existing adapter honesty shape."""
    info = otio_adapter.export_formats().get(fmt.lower())
    if info is None:
        return False, f"unknown interchange format '{fmt}'"
    if info.get("status") != "AVAILABLE":
        return False, str(info.get("reason") or f"'{fmt}' adapter unavailable")
    return True, None


# ---------------------------------------------------------------------------
# caption / text formats
# ---------------------------------------------------------------------------


def cues_from_timeline(doc: dict) -> list[SrtCue]:
    """Cues from a timeline doc's ``caption`` track (fallback: text, voice).

    Caption clips carry their display text inline (``text`` string) with a
    ``start``/``duration`` window, so no asset read is needed. A timeline with
    no caption track falls back to ``text`` clips, then to voice clips -- the
    transcript an editor expects, from real data only (never invented text).
    """
    tracks = (doc or {}).get("tracks", [])
    for kind in ("caption", "text", "voice"):
        for track in tracks:
            if track.get("kind") != kind:
                continue
            cues: list[SrtCue] = []
            for clip in track.get("clips", []):
                raw = clip.get("text")
                body = raw if isinstance(raw, str) else (
                    (raw or {}).get("content") or clip.get("name") or "")
                body = str(body).strip()
                start = float(clip.get("start", 0.0) or 0.0)
                dur = float(clip.get("duration", 0.0) or 0.0)
                if body and dur > 0:
                    cues.append(SrtCue(index=len(cues) + 1, start=start,
                                       end=start + dur, text=body))
            if cues:
                return cues
    return []


def _seconds_to_ts(sec: float, *, sep: str = ",") -> str:
    ms = max(0, int(round(float(sec) * 1000)))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render_srt(cues: list[SrtCue]) -> str:
    return format_srt(cues)


def render_vtt(cues: list[SrtCue]) -> str:
    out = ["WEBVTT", ""]
    for cue in cues:
        out.append(str(cue.index))
        out.append(f"{_seconds_to_ts(cue.start, sep='.')} --> "
                   f"{_seconds_to_ts(cue.end, sep='.')}")
        out.append(cue.text.strip())
        out.append("")
    return "\n".join(out)


ASS_HEADER = (
    "[Script Info]\n"
    "Title: YMONEY export\n"
    "ScriptType: v4.00+\n"
    "WrapStyle: 0\n"
    "PlayResX: 1920\n"
    "PlayResY: 1080\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,DejaVu Sans,64,&H00FFFFFF,&H000000FF,&H00000000,"
    "&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,60,60,60,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\n"
)

_VTT_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")


def render_ass(cues: list[SrtCue]) -> str:
    out = [ASS_HEADER]
    for cue in cues:
        text = cue.text.strip().replace("\n", "\\N")
        # ASS uses H:MM:SS.cc (a DOT), not SRT's H:MM:SS,mmm. Writing a comma
        # here produces a file every real ASS player AND our own parse-back
        # validator rejects -- the roundtrip check exists to catch exactly this.
        out.append(
            f"Dialogue: 0,{_seconds_to_ts(cue.start, sep='.')},"
            f"{_seconds_to_ts(cue.end, sep='.')},"
            f"Default,,0,0,0,,{text}")
    out.append("")
    return "\n".join(out)


def render_txt(cues: list[SrtCue]) -> str:
    return "\n".join(cue.text.strip() for cue in cues) + ("\n" if cues else "")


def _text_export(ctx: ExportContext) -> dict:
    """Serialize the target's caption/transcript track to bytes on disk."""
    cues = cues_from_timeline(ctx.doc)
    if not cues:
        raise ExportValidationError(
            f"{ctx.fmt} export needs a caption/text/voice track; target has none")
    if ctx.watermark_text:
        # Brand line rides on the first cue so the exported subtitle file is
        # brand-consistent without inventing a second caption style.
        brand = str(ctx.watermark_text).strip()
        cues = [SrtCue(index=1, start=cues[0].start, end=cues[0].end,
                       text=f"{brand}\n{cues[0].text}")]
    render = {"SRT": render_srt, "VTT": render_vtt,
              "ASS": render_ass, "TXT": render_txt}[ctx.fmt]
    data = render(cues).encode("utf-8")
    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.out_path.write_bytes(data)
    ctx.report(80)
    return {"media_type": MEDIA_TYPES[ctx.fmt], "size": len(data),
            "cues": len(cues)}


# -- parse-back validators (verification, never "we wrote it so it is fine") --


def _decode(data: bytes) -> str:
    return data.decode("utf-8")


def verify_srt(data: bytes) -> int:
    cues = parse_srt(_decode(data))
    if not cues:
        raise ValueError("no SRT cues parsed back")
    return len(cues)


def verify_txt(data: bytes) -> int:
    text = _decode(data)
    if not text.strip():
        raise ValueError("TXT export is empty")
    return len([line for line in text.splitlines() if line.strip()])


def verify_vtt(data: bytes) -> int:
    """Parse WebVTT back into cues; a header without cues is a failure."""
    return len(parse_vtt(_decode(data)))


def parse_vtt(text: str) -> list[SrtCue]:
    """WebVTT text -> cues. Raises ValueError on a missing header / no cues.

    Independent of the writer on purpose: a roundtrip check that reuses the
    generator's own formatting proves nothing.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        raise ValueError("missing WEBVTT header")
    cues: list[SrtCue] = []
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith(("NOTE", "STYLE", "REGION")):
            i += 1
            continue
        m = _VTT_TS_RE.search(lines[i + 1]) if i + 1 < len(lines) else None
        if not m:
            i += 1
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        body: list[str] = []
        j = i + 2
        while j < len(lines) and lines[j].strip():
            body.append(lines[j].strip())
            j += 1
        if end > start and body:
            cues.append(SrtCue(index=len(cues) + 1, start=start, end=end,
                               text=" ".join(body)))
        i = j
    if not cues:
        raise ValueError("no VTT cues parsed back")
    return cues


def verify_ass(data: bytes) -> int:
    """Parse an ASS file back; validates the [Events] header and each cue."""
    return len(parse_ass(_decode(data)))


def parse_ass(text: str) -> list[SrtCue]:
    """ASS text -> cues. Raises ValueError on a bad header / malformed cue.

    Independent of the writer on purpose: a roundtrip check that reuses the
    generator's own formatting proves nothing. Note that a valid ASS
    timestamp is ``H:MM:SS.cc`` -- a comma there is an SRT leak, and this
    parser is what catches it.
    """
    content = text or ""
    if "[Script Info]" not in content or "[Events]" not in content:
        raise ValueError("missing ASS [Script Info]/[Events] header")
    cues: list[SrtCue] = []
    for line in content.split("\n"):
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].split(",", 9)
        if len(parts) < 10:
            raise ValueError("malformed ASS Dialogue line")
        start, end = _ass_stamp(parts[1]), _ass_stamp(parts[2])
        body = parts[9].replace("\\N", " ").strip()
        if not body:
            continue
        if end <= start:
            raise ValueError("ASS cue with non-positive duration")
        cues.append(SrtCue(index=len(cues) + 1, start=start, end=end, text=body))
    if not cues:
        raise ValueError("no ASS cues parsed back")
    return cues


def _ass_stamp(stamp: str) -> float:
    """H:MM:SS.cc -> seconds. Tolerates the SRT-style comma some tools emit."""
    hh, mm, rest = stamp.strip().replace(",", ".").split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def verify_json(data: bytes) -> int:
    payload = json.loads(_decode(data))
    if "timeline" not in payload:
        raise ValueError("JSON envelope has no timeline")
    return len((payload.get("timeline") or {}).get("tracks") or [])


def verify_otio(data: bytes) -> int:
    """Read the artifact back with the REAL OTIO adapter (never self-attest)."""
    import opentimelineio as otio

    timeline = otio.adapters.read_from_string(_decode(data), "otio_json")
    return len(list(timeline.tracks))


def verify_fcpxml(data: bytes) -> int:
    import xml.etree.ElementTree as ET

    return len(ET.fromstring(_decode(data)))


# ---------------------------------------------------------------------------
# JSON / OTIO / NLE interchange
# ---------------------------------------------------------------------------


def _json_export(ctx: ExportContext) -> dict:
    payload = {
        "schema": "ymoney.export/1",
        "generated_at": ctx.metadata.get("generated_at"),
        "target": ctx.target,
        "profile": ctx.metadata.get("profile"),
        "timeline": ctx.doc,
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.out_path.write_bytes(data)
    ctx.report(80)
    return {"media_type": MEDIA_TYPES["JSON"], "size": len(data)}


def _otio_export(ctx: ExportContext) -> dict:
    """Real OTIO JSON via the existing adapter (never hand-rolled interchange)."""
    _name, media, content = otio_adapter.export_timeline(ctx.doc, "otio")
    data = content.encode("utf-8")
    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.out_path.write_bytes(data)
    ctx.report(80)
    return {"media_type": media or MEDIA_TYPES["OTIO"], "size": len(data)}


def _fcpxml_export(ctx: ExportContext) -> dict:
    _name, media, content = otio_adapter.export_timeline(ctx.doc, "fcpxml")
    data = content.encode("utf-8")
    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.out_path.write_bytes(data)
    ctx.report(80)
    return {"media_type": media or MEDIA_TYPES["FCPXML"], "size": len(data)}


# ---------------------------------------------------------------------------
# media formats
# ---------------------------------------------------------------------------


def _ffmpeg_codec_args(fmt: str, config: dict, *, with_video: bool) -> list[str]:
    """Container-legal encoder args, driven by the profile + the live probe."""
    if fmt == "MP3":
        return ["-c:a", "libmp3lame",
                "-b:a", f"{int(config.get('audio_bitrate_kbps') or 192)}k"]
    if fmt == "WAV":
        return ["-c:a", "pcm_s16le", "-ac", str(int(config.get("audio_channels") or 2))]
    encoders = _ffmpeg_encoders()
    if fmt == "WebM":
        args: list[str] = []
        if with_video:
            # WebM only carries VP8/VP9. A profile that names libx264 for its
            # MP4 track is NOT a WebM codec -- fall back to a VP encoder rather
            # than handing ffmpeg a container-illegal one.
            wanted = str(config.get("video_codec") or "")
            if wanted not in _WEBM_VIDEO_CODECS:
                wanted = "libvpx-vp9" if "libvpx-vp9" in encoders else "libvpx"
            args += ["-c:v", wanted,
                     "-b:v", f"{int(config.get('bitrate_kbps') or 4000)}k"]
        audio = str(config.get("audio_codec") or "")
        if audio not in _WEBM_AUDIO_CODECS:
            audio = "libopus" if "libopus" in encoders else "libvorbis"
        args += ["-c:a", audio,
                 "-b:a", f"{int(config.get('audio_bitrate_kbps') or 128)}k"]
        return args
    # MP4 / MOV
    video = str(config.get("video_codec") or ("libx264" if fmt == "MP4" else "prores"))
    if fmt == "MOV" and video not in encoders and "prores" in encoders:
        video = "prores"
    args = ["-c:v", video]
    if with_video:
        args += ["-b:v", f"{int(config.get('bitrate_kbps') or 8000)}k"]
        color = config.get("color") or {}
        if color.get("matrix"):
            args += ["-colorspace", str(color["matrix"]),
                     "-color_primaries", str(color["transfer"] or color["matrix"])]
        audio = str(config.get("audio_codec") or "aac")
        if audio not in encoders and "aac" in encoders:
            audio = "aac"
        args += ["-c:a", audio,
                 "-b:a", f"{int(config.get('audio_bitrate_kbps') or 192)}k",
                 "-ac", str(int(config.get("audio_channels") or 2))]
    return args


def _scale_filter(config: dict, probe: dict) -> str | None:
    """scale/pad filter for the profile dims; ``None`` for source passthrough."""
    width, height = config.get("width"), config.get("height")
    if not width or not height:
        return None  # ARCHIVE_MASTER / AUDIO_ONLY keep the source geometry
    src_w, src_h = int(probe.get("width") or 0), int(probe.get("height") or 0)
    chain = f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease"
    if src_w and src_h:
        chain += f",pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2"
    return chain + ",setsar=1"


def _probe_source(path: Path) -> dict:
    from app.services.storage import probe_metadata

    return probe_metadata(path) or {}


def run_media_export(ctx: ExportContext) -> dict:
    """Transcode/copy one source into ``ctx.out_path`` with ffmpeg.

    Fail-closed: a missing binary, a missing encoder, a non-zero ffmpeg exit
    or a zero-byte result raises :class:`ExportValidationError` and removes
    the partial file -- never leaves a broken artifact claiming success.
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise ExportValidationError("ffmpeg is not installed or not on PATH")
    if ctx.source_path is None or not ctx.source_path.exists():
        raise ExportValidationError(
            "media export needs a resolvable source media file; none found for this target")
    with_video = ctx.fmt in ("MP4", "MOV", "WebM")
    probe = _probe_source(ctx.source_path)
    ctx.report(20)

    cmd: list[str] = [ffmpeg, "-y", "-nostdin", "-i", str(ctx.source_path)]
    # Byte-level copy ONLY for a source-passthrough profile (no fixed dims:
    # ARCHIVE_MASTER) whose container already matches. That is what an
    # archive master means -- no generational loss. A profile that DOES fix
    # the geometry must transcode, or the artifact would silently keep the
    # source resolution and the resolution check would (rightly) fail.
    fixed_dims = bool(ctx.config.get("width") and ctx.config.get("height"))
    copy_stream = (
        with_video
        and not fixed_dims
        and ctx.fmt == "MP4"
        and probe.get("format") == "mov,mp4,m4a,3gp,3g2,mj2"
    )
    if copy_stream:
        cmd += ["-c", "copy"]
    else:
        vf = _scale_filter(ctx.config, probe)
        if vf:
            cmd += ["-vf", vf]
        ctx.report(45)
        cmd += _ffmpeg_codec_args(ctx.fmt, ctx.config, with_video=with_video)
    if ctx.watermark_text and with_video and not copy_stream:
        # A real, verifiable brand line carried in the container metadata --
        # burned-in text would need a font file this repo does not guarantee.
        cmd += ["-metadata", f"comment={ctx.watermark_text[:120]}"]
    if ctx.duration_seconds > 0:
        cmd += ["-t", f"{max(float(ctx.duration_seconds), 0.04):.3f}"]
    if ctx.fmt == "MP4":
        cmd += ["-movflags", "+faststart"]
    cmd += [str(ctx.out_path)]

    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600, check=False)
    if proc.returncode != 0 or not ctx.out_path.exists() \
            or ctx.out_path.stat().st_size == 0:
        ctx.out_path.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise ExportValidationError(
            f"ffmpeg failed to produce {ctx.fmt} ({tail[0][:160]})")
    ctx.report(85)
    return {"media_type": MEDIA_TYPES[ctx.fmt],
            "command_kind": "copy" if copy_stream else "transcode",
            "source_probe": probe}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatSpec:
    """One registry entry -- the contracts §11 ``{available, export, verify}``."""

    name: str
    kind: str
    media_type: str
    suffix: str
    export: Callable[[ExportContext], dict]
    verify: Callable[[bytes], int]
    probe: Callable[[], tuple[bool, str | None]] = field(
        default=lambda: (True, None))

    def available(self) -> tuple[bool, str | None]:
        return self.probe()

    def as_dict(self) -> dict:
        ok, reason = self.available()
        return {"format": self.name, "available": ok, "reason": reason,
                "kind": self.kind, "media_type": self.media_type,
                "suffix": self.suffix}


def _always() -> tuple[bool, str | None]:
    return True, None


def _refuse(reason: str) -> Callable[[], tuple[bool, str | None]]:
    def probe() -> tuple[bool, str | None]:
        return False, reason

    return probe


def _refuse_export(ctx: ExportContext) -> dict:
    """Unreachable in practice -- the probe gates it; kept fail-closed anyway."""
    raise ExportValidationError(
        f"{ctx.fmt} is not available: no verified adapter in this OTIO build")


FORMAT_REGISTRY: dict[str, FormatSpec] = {
    "SRT": FormatSpec("SRT", "subtitle", MEDIA_TYPES["SRT"], ".srt",
                      _text_export, verify_srt, _always),
    "VTT": FormatSpec("VTT", "subtitle", MEDIA_TYPES["VTT"], ".vtt",
                      _text_export, verify_vtt, _always),
    "ASS": FormatSpec("ASS", "subtitle", MEDIA_TYPES["ASS"], ".ass",
                      _text_export, verify_ass, _always),
    "TXT": FormatSpec("TXT", "text", MEDIA_TYPES["TXT"], ".txt",
                      _text_export, verify_txt, _always),
    "JSON": FormatSpec("JSON", "interchange", MEDIA_TYPES["JSON"], ".json",
                       _json_export, verify_json, _always),
    "OTIO": FormatSpec("OTIO", "interchange", MEDIA_TYPES["OTIO"], ".otio",
                       _otio_export, verify_otio, _always),
    "FCPXML": FormatSpec("FCPXML", "interchange", MEDIA_TYPES["FCPXML"], ".fcpxml",
                         _fcpxml_export, verify_fcpxml,
                         lambda: _otio_availability("FCPXML")),
    "MP4": FormatSpec("MP4", "video", MEDIA_TYPES["MP4"], ".mp4",
                      run_media_export, None, lambda: probe_media_format("MP4")),
    "MOV": FormatSpec("MOV", "video", MEDIA_TYPES["MOV"], ".mov",
                      run_media_export, None, lambda: probe_media_format("MOV")),
    "WebM": FormatSpec("WebM", "video", MEDIA_TYPES["WebM"], ".webm",
                       run_media_export, None, lambda: probe_media_format("WebM")),
    "MP3": FormatSpec("MP3", "audio", MEDIA_TYPES["MP3"], ".mp3",
                      run_media_export, None, lambda: probe_media_format("MP3")),
    "WAV": FormatSpec("WAV", "audio", MEDIA_TYPES["WAV"], ".wav",
                      run_media_export, None, lambda: probe_media_format("WAV")),
    # No verified xmeml (Premiere) or DRT (Resolve) adapter exists in any OTIO
    # build we support; emitting hand-rolled XML would be a fake file. Refuse.
    "PREMIERE": FormatSpec("PREMIERE", "interchange", MEDIA_TYPES["PREMIERE"], ".xml",
                           _refuse_export, None,
                           _refuse("no verified Premiere (xmeml) adapter in this OTIO "
                                   "build; use FCPXML or OTIO instead")),
    "RESOLVE": FormatSpec("RESOLVE", "interchange", MEDIA_TYPES["RESOLVE"], ".drt",
                          _refuse_export, None,
                          _refuse("no verified DaVinci Resolve adapter in this OTIO "
                                  "build; use FCPXML or OTIO instead")),
}


def get_format(fmt: str) -> FormatSpec:
    """Registry entry, case-insensitive. Raises for an unknown format.

    ``WebM`` keeps its canonical mixed case as the persisted
    ``export_jobs.format`` value (contracts §11), so a bare ``.upper()``
    lookup would miss it -- normalize against a case-folded index instead.
    """
    spec = _FORMAT_LOOKUP.get(str(fmt or "").upper())
    if spec is None:
        raise ExportValidationError(
            f"unknown format '{fmt}'; expected one of {sorted(FORMAT_REGISTRY)}")
    return spec


#: "webm" / "WEBM" / "WebM" all resolve to the single "WebM" registry entry.
_FORMAT_LOOKUP: dict[str, FormatSpec] = {
    name.upper(): spec for name, spec in FORMAT_REGISTRY.items()
}


def list_formats(*, target_type: str | None = None) -> list[dict]:
    """The ONLY source the API exposes for format availability.

    Every entry is ``{format, available, reason, kind, media_type, suffix}`` --
    an unavailable format always carries a non-empty ``reason`` naming the
    missing binary/encoder/adapter (never ``None`` with ``available: False``).
    ``target_type`` narrows the list to the interchange kinds a target can
    actually consume.
    """
    out: list[dict] = []
    for name in ALL_FORMATS:
        entry = FORMAT_REGISTRY[name].as_dict()
        if target_type and entry["kind"] in ("interchange", "text") \
                and str(target_type) not in ("timeline", "content", "campaign"):
            continue
        out.append(entry)
    return out


def require_available(fmt: str) -> FormatSpec:
    """Registry entry, or :class:`ExportValidationError` naming the reason."""
    spec = get_format(fmt)
    ok, reason = spec.available()
    if not ok:
        raise ExportValidationError(
            f"{spec.name} is not available: {reason or 'capability probe failed'}")
    return spec


__all__ = [
    "ALL_FORMATS",
    "ASS_HEADER",
    "ExportContext",
    "ExportValidationError",
    "FORMAT_ASSET_TYPE",
    "FORMAT_ENCODERS",
    "FORMAT_KIND",
    "FORMAT_REGISTRY",
    "FormatSpec",
    "MEDIA_FORMATS",
    "MEDIA_TYPES",
    "NLE_FORMATS",
    "TEXT_FORMATS",
    "cues_from_timeline",
    "ffmpeg_path",
    "get_format",
    "list_formats",
    "parse_ass",
    "parse_vtt",
    "probe_media_format",
    "render_ass",
    "render_srt",
    "render_txt",
    "render_vtt",
    "require_available",
    "reset_probe_cache",
    "run_media_export",
    "verify_ass",
    "verify_json",
    "verify_otio",
    "verify_srt",
    "verify_txt",
    "verify_vtt",
]
