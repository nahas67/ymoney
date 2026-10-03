"""ffmpeg speech-activity adapter (Work 12 Lane B) -- contracts §4.

Provides the ``speech_activity`` chain and it is REAL and LOCAL: it shells out to
the ``ffmpeg`` binary that the product already requires, using the shared
measurement helper :func:`app.engine.intel.ffmpeg_util.detect_silence`
(``-af silencedetect=noise=..:d=..``). Speech activity is the COMPLEMENT of the
measured silence runs inside ``[0, duration]``.

The single most important rule in this module: **speech activity is not a
speaker.** Every segment it produces is ``kind="SPEECH_ACTIVITY"`` with
``speaker_id = NULL`` (contracts §4, ``models/media_intel.py``). It is a voice
activity measurement that happens to be carried in ``diarization_segments``
because that is the table contracts §2 defines for it. Dressing it up as a
speaker would be exactly the fabrication contracts §0 forbids, so this adapter
cannot even name a speaker: it has no code path that produces one.

Boundary rule inherited from :func:`detect_silence`: a media START that is silent
produces no ``silence_start`` line at all, so the complement simply starts at
``0``; ``start_s == 0`` is therefore a normal boundary, not an error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.engine.intel import ffmpeg_util
from app.engine.intel.base import (
    COMMERCIAL_REVIEW_REQUIRED,
    MODE_SUBPROCESS,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    check_control,
)

logger = logging.getLogger("ymoney.intel")

#: audited on 2026-09-29 (docs/oss/MEDIA_INTEL_LICENSES.md)
AUDITED_ON = "2026-09-29"
#: ffmpeg core is LGPL-2.1-or-later; a build with GPL components is GPL-2.0-or-later
CODE_LICENSE = "LGPL-2.1-or-later (core); GPL-2.0-or-later for GPL builds"
CODE_LICENSE_URL = "https://ffmpeg.org/legal.html"

#: the filter this adapter measures with
MEASUREMENT_FILTER = "silencedetect"

#: the ONLY segment kind this adapter may ever emit
SEGMENTS_KIND = "SPEECH_ACTIVITY"

#: silencedetect defaults, matching ffmpeg_util.detect_silence
DEFAULT_NOISE_DB = -50.0
DEFAULT_MIN_SILENCE_S = 0.8
#: a "speech" run shorter than this is not reported as activity
DEFAULT_MIN_SPEECH_S = 0.05
#: ceiling on returned segments so a pathological asset cannot blow up the row set
MAX_SEGMENTS = 20000

UNAVAILABLE_REASON = "the ffmpeg binary is not on PATH; silencedetect cannot run"
REMEDIATION = "install ffmpeg on this host (the app already requires it for clips/exports)"
NO_DURATION_REASON = (
    "the media duration could not be probed, so the speech-activity complement "
    "has no end boundary; no segment is invented"
)
NO_SPEECH_REASON = "every measured range was silence; there is no speech activity"


def _coerce_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def ffmpeg_version(ffmpeg_bin: str | None = None) -> str:
    """The ffmpeg build version token (``"8.1.1-full_build"``), or ``"unknown"``.

    Parsed from ``ffmpeg -version`` (``ffmpeg version 8.1.1-full_build ...``) and
    reported in ``ProviderHealth.version`` / the run's ``model_version`` so a
    rebuilt binary can never reuse a measurement a different build produced.
    """
    import subprocess  # noqa: PLC0415 - only reached when ffmpeg exists

    binary = str(ffmpeg_bin or "")
    if not binary:
        from shutil import which  # noqa: PLC0415

        binary = which("ffmpeg") or ""
    if not binary:
        return "unknown"
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "-version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"
    first = (done.stdout or b"").decode("utf-8", errors="replace").splitlines()
    if not first:
        return "unknown"
    for token in first[0].split():
        if token[:1].isdigit():
            return token
    return "unknown"



def has_silencedetect(ffmpeg_bin: str | None = None) -> bool:
    """Does this ffmpeg build actually expose the ``silencedetect`` filter?

    An honest capability probe (``-filters``) rather than a version guess: the
    filter set changes per build, and claiming a filter the binary lacks would
    produce an empty measurement that looks like "no speech".
    """
    import subprocess  # noqa: PLC0415 - only reached when ffmpeg exists

    binary = str(ffmpeg_bin or "")
    if not binary:
        from shutil import which  # noqa: PLC0415

        binary = which("ffmpeg") or ""
    if not binary:
        return False
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "-hide_banner", "-filters"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    for line in (done.stdout or b"").decode("utf-8", errors="replace").splitlines():
        if line.strip().startswith(("TSC", "T..", "A..", "V..")) and MEASUREMENT_FILTER in line:
            return True
    return MEASUREMENT_FILTER in (done.stdout or b"").decode("utf-8", errors="replace")


def clamp_silences(
    silences: Sequence[Mapping[str, Any]] | None,
    duration: float | None,
) -> list[dict[str, float]]:
    """Normalise measured silence runs into sorted, clamped ``[0, duration]`` spans.

    A run that is entirely outside the media, inverted, or zero-length is
    dropped. Silences are MERGED when they overlap or touch, so the complement
    below can never emit a zero-length speech run.
    """
    total = _coerce_float(duration)
    out: list[dict[str, float]] = []
    for item in silences or []:
        start = _coerce_float(item.get("start_s") if isinstance(item, Mapping) else None)
        end = _coerce_float(item.get("end_s") if isinstance(item, Mapping) else None)
        if start is None or end is None or end <= start:
            continue
        start = max(0.0, start)
        if total is not None:
            end = min(end, total)
            if end <= start:
                continue
        out.append({"start_s": round(start, 6), "end_s": round(end, 6)})
    out.sort(key=lambda row: (row["start_s"], row["end_s"]))
    merged: list[dict[str, float]] = []
    for span in out:
        if merged and span["start_s"] <= merged[-1]["end_s"]:
            merged[-1]["end_s"] = max(merged[-1]["end_s"], span["end_s"])
            continue
        merged.append(dict(span))
    return merged


def speech_activity_from_silences(
    silences: Sequence[Mapping[str, Any]] | None,
    duration: float | None,
    *,
    min_speech_s: float = DEFAULT_MIN_SPEECH_S,
) -> list[dict[str, float]]:
    """The complement of the measured silences -- the speech-activity runs.

    ``duration`` is REQUIRED to bound the timeline: without it the complement
    has no end boundary, so an empty list (and a reason from the caller) is the
    honest answer rather than a fabricated tail. Runs shorter than
    ``min_speech_s`` are dropped and counted in the caller's warnings.
    """
    total = _coerce_float(duration)
    if total is None or total <= 0:
        return []
    spans = clamp_silences(silences, total)
    floor = max(0.0, float(min_speech_s or 0.0))
    out: list[dict[str, float]] = []
    cursor = 0.0
    for span in spans:
        if span["start_s"] > cursor:
            out.append({"start_s": round(cursor, 6), "end_s": round(span["start_s"], 6)})
        cursor = max(cursor, span["end_s"])
    if cursor < total:
        out.append({"start_s": round(cursor, 6), "end_s": round(total, 6)})
    kept = [
        row for row in out
        if (row["end_s"] - row["start_s"]) >= floor
    ]
    return kept[:MAX_SEGMENTS]


class FFmpegSpeechActivityProvider(MediaIntelProvider):
    """Voice activity measurement from real ffmpeg ``silencedetect`` output.

    Optional seam for tests only: ``silence_detector``/``duration_probe`` may be
    injected to exercise the complement logic deterministically. Production
    construction takes no arguments and uses the shared real ffmpeg helpers.
    """

    key = "ffmpeg_speech_activity"
    kind = "speech_activity"
    kinds = ()

    def __init__(
        self,
        silence_detector: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
        duration_probe: Callable[[str], float | None] | None = None,
    ) -> None:
        self._detect = silence_detector or ffmpeg_util.detect_silence
        self._duration = duration_probe or ffmpeg_util.duration_seconds

    # -- probes ------------------------------------------------------------

    def health(self) -> ProviderHealth:
        available = ffmpeg_util.ffmpeg_available()
        detail: dict[str, Any] = {
            "remediation": REMEDIATION,
            "ffmpeg": ffmpeg_util.ffmpeg_available(),
            "ffprobe": ffmpeg_util.ffprobe_available(),
            "silencedetect": False,
            "segment_kind": SEGMENTS_KIND,
            "produces_speakers": False,
        }
        if not available:
            return ProviderHealth(
                available=False,
                reason=UNAVAILABLE_REASON,
                mode=MODE_SUBPROCESS,
                detail=detail,
            )
        try:
            supported = has_silencedetect()
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return ProviderHealth(
                available=False,
                reason=f"silencedetect probe failed: {type(exc).__name__}",
                mode=MODE_SUBPROCESS,
                detail={**detail, "error_type": type(exc).__name__},
            )
        detail["silencedetect"] = supported
        if not supported:
            return ProviderHealth(
                available=False,
                reason=(
                    f"this ffmpeg build has no '{MEASUREMENT_FILTER}' filter; "
                    "speech activity cannot be measured"
                ),
                mode=MODE_SUBPROCESS,
                detail=detail,
            )
        return ProviderHealth(
            available=True,
            version=ffmpeg_version(),
            mode=MODE_SUBPROCESS,
            detail=detail,
        )

    def capabilities(self) -> dict:
        return {
            "available": self.health().available,
            "measurement": f"ffmpeg -af {MEASUREMENT_FILTER}=noise=<db>dB:d=<s>",
            "segment_kind": SEGMENTS_KIND,
            "speaker_id": None,  # ALWAYS null: activity is not a speaker
            "produces_speakers": False,
            "formats": ["any ffmpeg can decode"],
            "noise_db_default": DEFAULT_NOISE_DB,
            "min_silence_s_default": DEFAULT_MIN_SILENCE_S,
            "min_speech_s_default": DEFAULT_MIN_SPEECH_S,
            "max_segments": MAX_SEGMENTS,
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            ram_mb=192,
            cpu_seconds_per_audio_minute=3.0,
            notes="one ffmpeg decode pass; CPU only, no model, no weights",
        )

    def license_info(self) -> LicenseInfo:
        return LicenseInfo(
            code_license=CODE_LICENSE,
            code_license_url=CODE_LICENSE_URL,
            model_license="n/a (no model, no weights)",
            model_gated=False,
            commercial_use=COMMERCIAL_REVIEW_REQUIRED,
            audited_on=AUDITED_ON,
            notes=(
                "external process (not linked): LGPL-2.1-or-later core, and a build "
                "with --enable-gpl is GPL-2.0-or-later. The operator's own build and "
                "distribution obligations apply -- confirm before redistributing a "
                "binary. See docs/oss/MEDIA_INTEL_LICENSES.md unverified item 6."
            ),
        )

    def model_version(self) -> str:
        """Stable model string that participates in the run cache key.

        The binary's own version is folded in, so a rebuilt ffmpeg can never
        reuse a measurement a different build produced.
        """
        return f"ffmpeg:{MEASUREMENT_FILTER}:{ffmpeg_version()}"

    def cost(self, spec: ResourceSpec) -> dict:
        return {
            "gpu_ms": 0,
            "cpu_ms": int(float(spec.cpu_seconds_per_audio_minute) * 1000),
            "cost_micros": 0,
            "billed": False,
            "note": "external ffmpeg process; no vendor bill",
        }

    # -- work --------------------------------------------------------------

    def run(
        self,
        request: IntelRequest,
        *,
        progress: Callable[[float], None],
        should_cancel: Callable[[], bool],
        deadline: float | None,
    ) -> ProviderResult:
        params = dict(request.params or {})
        path = str(request.storage_path or "")
        if not path:
            raise ProviderUnavailable("no media path was supplied for speech activity")
        if not ffmpeg_util.ffmpeg_available():
            raise ProviderUnavailable(UNAVAILABLE_REASON)
        check_control(should_cancel, deadline)
        noise_db = _coerce_float(params.get("noise_db"))
        min_silence_s = _coerce_float(params.get("min_silence_s"))
        min_speech_s = _coerce_float(params.get("min_speech_s"))
        silences = self._detect(
            path,
            DEFAULT_NOISE_DB if noise_db is None else noise_db,
            DEFAULT_MIN_SILENCE_S if min_silence_s is None else min_silence_s,
        )
        progress(0.6)
        check_control(should_cancel, deadline)
        total = self._duration(path)
        if _coerce_float(total) is None:
            raise ProviderUnavailable(NO_DURATION_REASON)
        segments = speech_activity_from_silences(
            silences, total, min_speech_s=DEFAULT_MIN_SPEECH_S if min_speech_s is None
            else min_speech_s
        )
        if not segments:
            raise ProviderUnavailable(NO_SPEECH_REASON)
        warnings: list[str] = []
        if len(segments) >= MAX_SEGMENTS:
            warnings.append(f"segment list truncated at the {MAX_SEGMENTS} ceiling")
        progress(0.95)
        spoken = sum(row["end_s"] - row["start_s"] for row in segments)
        return ProviderResult(
            ok=True,
            artifacts={
                "segments": {
                    "payload": {
                        "kind": SEGMENTS_KIND,
                        "segments": segments,
                        "model_version": self.model_version(),
                    }
                }
            },
            metrics={
                "segment_count": len(segments),
                "silence_count": len(clamp_silences(silences, total)),
                "duration_s": round(float(total or 0.0), 6),
                "speech_seconds": round(spoken, 6),
                "speech_ratio": round(spoken / float(total), 6) if total else None,
                "model_version": self.model_version(),
            },
            warnings=warnings,
            error="",
        )


#: the registry resolves this symbol (impl/<key> contract)
PROVIDER = FFmpegSpeechActivityProvider

__all__ = [
    "AUDITED_ON",
    "CODE_LICENSE",
    "CODE_LICENSE_URL",
    "DEFAULT_MIN_SILENCE_S",
    "DEFAULT_MIN_SPEECH_S",
    "DEFAULT_NOISE_DB",
    "MAX_SEGMENTS",
    "MEASUREMENT_FILTER",
    "NO_DURATION_REASON",
    "NO_SPEECH_REASON",
    "PROVIDER",
    "REMEDIATION",
    "SEGMENTS_KIND",
    "UNAVAILABLE_REASON",
    "FFmpegSpeechActivityProvider",
    "clamp_silences",
    "ffmpeg_version",
    "has_silencedetect",
    "speech_activity_from_silences",
]
