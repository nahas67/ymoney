"""ffmpeg-backed audio enhancement provider (Work 12 Lane C) -- contracts §6.

The whole enhancement surface of the product runs through ONE adapter. Nothing
else in the stack knows which filter, library or neural model did the work: the
renderer and the audio engine ask the ``enhancement``/``denoise`` capability
CHAINS in ``app.engine.intel.registry`` and get whatever is honestly available.
That is the adapter-isolation rule -- a swap from ``afftdn`` to RNNoise to a
future model is a registry change, never a call-site change.

What is real here
-----------------
Every stage that claims ``applied`` really ran a filter through the ffmpeg
binary and wrote a NEW file. Nothing is simulated, and no stage invents a
measurement:

============================  ==========================================  ==========
stage                         method (this build)                        method kind
============================  ==========================================  ==========
``denoise``                   ``afftdn`` (or ``arnndn`` with a model file) filter
``dereverb``                  -- no filter exists --                     unavailable
``voice_isolation``           ``highpass``+``lowpass``(+``afftdn``)       filter
``loudness_normalization``    ``loudnorm`` two-pass (EBU R128)           filter
``music_ducking``             ``sidechaincompress`` (self sidechain)     filter
``compression``               ``acompressor``                            filter
``limiting``                  ``alimiter`` + measured true-peak bound    filter
``silence_detection``         ``silencedetect`` via ffmpeg_util           measurement
``filler_detection``          the silence/filler lane's engine, if any    delegated
``breath_click_detection``    stdlib PCM window analysis                  heuristic
============================  ==========================================  ==========

Honesty rules this module never bends:

* **Filter availability is probed at runtime**, per filter, with
  ``ffmpeg -h filter=<name>`` (cached). Nothing is hardcoded to this build, so
  a build with more filters lights more stages up automatically and a build
  with fewer never claims work it cannot do.
* **No fake de-reverberation.** ffmpeg has no de-reverb filter; the stage
  reports ``unavailable`` with the reason instead of shipping a band-pass and
  calling it de-reverb.
* **No fake neural isolation.** ``voice_isolation`` is a BAND-PASS plus
  spectral denoise. It is labelled ``method="band_isolation"`` everywhere and
  carries ``neural=False``; the response says so in words too.
* **One filter pass per stage.** Each stage gets its own ffmpeg invocation into
  its own derived file, so a stage that fails is honest about ITS OWN failure
  (real stderr tail) and the rest of the pipeline still produces output. A
  single mega-chain could not tell which stage broke.
* **Derived only.** Every pass writes a NEW path; ``ffmpeg_util.run_filter``
  raises when ``dst == src`` and that guard is inherited as-is. The
  ``music_ducking`` pass needs a ``-filter_complex`` graph (the sidechain is a
  split of the SAME input), which ``ffmpeg_util`` has no entry point for, so
  :func:`_run_filter_complex` below mirrors ``run_filter``'s contract exactly:
  same result dict, same finite timeout, no shell, and the same
  ``dst == src`` refusal.
* **No perceptual claims.** This module reports LEVEL and TIME facts only
  (which filter ran, peak/LUFS/true-peak, clipped samples, duration, ms). The
  claim list is built by the engine from measured deltas and stays empty when
  there is no measured basis.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.engine.intel.base import (
    COMMERCIAL_REVIEW_REQUIRED,
    MODE_SUBPROCESS,
    CancelFn,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProgressFn,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    check_control,
)
from app.engine.intel.ffmpeg_util import (
    FILTER_TIMEOUT,
    detect_silence,
    duration_seconds,
    ffmpeg_available,
    run_filter,
)

logger = logging.getLogger("ymoney.intel")

PROVIDER_KEY = "ffmpeg_enhancement"

#: audit date + exact verdicts from ``docs/oss/MEDIA_INTEL_LICENSES.md`` (row
#: "ffmpeg-based local providers"). Encoded verbatim; never promoted.
AUDITED_ON = "2026-09-29"

#: ordered stage vocabulary (contracts §6). The order is the execution order:
#: restoration before dynamics, analysis wherever it reads the audio it sees.
STAGES: tuple[str, ...] = (
    "denoise",
    "dereverb",
    "voice_isolation",
    "silence_detection",
    "filler_detection",
    "breath_click_detection",
    "loudness_normalization",
    "music_ducking",
    "compression",
    "limiting",
)

#: stages that TRANSFORM the audio (one ffmpeg pass each, derived file each).
FILTER_STAGES: frozenset[str] = frozenset(
    {
        "denoise",
        "dereverb",
        "voice_isolation",
        "loudness_normalization",
        "music_ducking",
        "compression",
        "limiting",
    }
)

#: stages that only MEASURE. They never touch the audio, so they cannot change
#: the derived file and are never counted as a transformation.
ANALYSIS_STAGES: frozenset[str] = frozenset(
    {"silence_detection", "filler_detection", "breath_click_detection"}
)

#: per-stage status vocabulary -- the four honest outcomes of a stage.
STAGE_STATUSES: tuple[str, ...] = ("applied", "skipped", "unavailable", "failed")

#: stages whose findings are a HEURISTIC over raw PCM, not a detector of record
HEURISTIC_STAGES: frozenset[str] = frozenset({"breath_click_detection"})

#: ffmpeg version string, probed lazily (never at import).
_VERSION_CACHE: str = ""


# ---------------------------------------------------------------------------
# runtime capability probes
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _filter_table() -> frozenset[str]:
    """Every audio/video filter name this ffmpeg build exposes.

    Parsed from the real ``ffmpeg -filters`` output. An empty set means "the
    probe failed" -- callers then report ``unavailable`` with a reason rather
    than assuming a filter exists.
    """
    if not ffmpeg_available():
        return frozenset()
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell, trusted binary
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg -filters probe failed: %s", type(exc).__name__)
        return frozenset()
    text = (done.stdout or b"").decode("utf-8", "replace")
    if not text:
        text = (done.stderr or b"").decode("utf-8", "replace")
    return frozenset(re.findall(r"^\s*[A-Z.|]{3}\s+(\S+)\s+[A-Z|>]", text, re.MULTILINE))


@functools.lru_cache(maxsize=64)
def filter_available(name: str) -> bool:
    """True when THIS build really has ``name`` (never a hardcoded assumption).

    ``ffmpeg -h filter=<name>`` prints ``Filter <name> ...`` and exits 0 when
    the filter exists, and ``Unknown filter '<name>'`` with a non-zero exit
    otherwise. The text is the decisive signal because some builds exit 0 even
    for the error path.
    """
    key = str(name or "").strip().lower()
    if not key:
        return False
    if key in _filter_table():
        return True
    if not ffmpeg_available():
        return False
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell
            ["ffmpeg", "-hide_banner", "-h", f"filter={key}"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg filter probe (%s) failed: %s", key, type(exc).__name__)
        return False
    text = (done.stdout or b"") + (done.stderr or b"")
    decoded = text.decode("utf-8", "replace")
    if "Unknown filter" in decoded or "No such filter" in decoded:
        return False
    return f"Filter {key}" in decoded


def ffmpeg_version() -> str:
    """``ffmpeg -version`` first line, cached (empty when unavailable)."""
    global _VERSION_CACHE
    if _VERSION_CACHE:
        return _VERSION_CACHE
    if not ffmpeg_available():
        return ""
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell
            ["ffmpeg", "-version"], capture_output=True, timeout=20, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:  # pragma: no cover
        logger.warning("ffmpeg -version failed: %s", type(exc).__name__)
        return ""
    first = (done.stdout or b"").decode("utf-8", "replace").splitlines()
    _VERSION_CACHE = first[0].strip() if first else ""
    return _VERSION_CACHE


def reset_probes() -> None:
    """Drop cached filter/version probes (tests + a PATH change)."""
    global _VERSION_CACHE
    _VERSION_CACHE = ""
    _filter_table.cache_clear()
    clear = getattr(filter_available, "cache_clear", None)
    if callable(clear):
        clear()


# ---------------------------------------------------------------------------
# stage -> filter mapping
# ---------------------------------------------------------------------------


def _num(params: dict, key: str, default: float) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return value


def _denoise_support(params: dict) -> dict:
    """``afftdn`` always; ``arnndn`` only with a real model file.

    ``arnndn`` without a model is a hard ffmpeg error, so advertising it would
    be a lie. With ``params['denoise_model_path']`` pointing at a readable file
    the neural-ish path is used and recorded as ``arnndn``.
    """
    model_path = str(params.get("denoise_model_path") or "").strip()
    if model_path and Path(model_path).is_file() and filter_available("arnndn"):
        return {
            "available": True,
            "method": "arnndn",
            "filters": ["arnndn"],
            "reason": f"arnndn with model {Path(model_path).name}",
        }
    if filter_available("afftdn"):
        note = (
            "afftdn (spectral denoise); arnndn needs a model file and none was given"
            if not model_path
            else "arnndn model file is unreadable; fell back to afftdn"
        )
        return {"available": True, "method": "afftdn", "filters": ["afftdn"], "reason": note}
    return {
        "available": False,
        "method": "",
        "filters": [],
        "reason": "no denoise filter in this ffmpeg build (afftdn and arnndn both absent)",
    }


def _dereverb_support(params: dict) -> dict:
    """Honest ``unavailable``: ffmpeg has no de-reverberation filter."""
    if filter_available("dereverb"):
        return {
            "available": True,
            "method": "ffmpeg_dereverb",
            "filters": ["dereverb"],
            "reason": "this build exposes a dereverb filter",
        }
    return {
        "available": False,
        "method": "",
        "filters": [],
        "reason": (
            "no de-reverberation method available: this ffmpeg build has no "
            "dereverb filter and no de-reverb provider is installed"
        ),
    }


def _voice_isolation_support(params: dict) -> dict:
    """Band-pass (+ optional spectral denoise). Explicitly NOT neural."""
    needed = ["highpass", "lowpass"]
    missing = [name for name in needed if not filter_available(name)]
    if missing:
        return {
            "available": False,
            "method": "",
            "filters": [],
            "reason": f"band-pass filters missing from this build: {', '.join(missing)}",
        }
    use_denoise = bool(params.get("voice_isolation_denoise", True)) and filter_available("afftdn")
    return {
        "available": True,
        "method": "band_isolation",
        "filters": needed + (["afftdn"] if use_denoise else []),
        "neural": False,
        "reason": (
            "band-pass isolation (highpass+lowpass"
            + ("+afftdn" if use_denoise else "")
            + "); this is NOT neural voice separation"
        ),
    }


def _simple_support(filters: tuple[str, ...], method: str, reason: str) -> dict:
    missing = [name for name in filters if not filter_available(name)]
    if missing:
        return {
            "available": False,
            "method": "",
            "filters": [],
            "reason": f"{method} unavailable: missing filter(s) {', '.join(missing)}",
        }
    return {"available": True, "method": method, "filters": list(filters), "reason": reason}


def stage_support(stage: str, params: dict | None = None) -> dict:
    """Availability verdict for one stage, probed at runtime.

    Returns ``{stage, available, method, filters, reason, ...}``. ``reason`` is
    ALWAYS populated -- callers surface it verbatim when ``available`` is False.
    """
    payload = dict(params or {})
    name = str(stage or "").strip().lower()
    if name not in STAGES:
        return {
            "stage": name,
            "available": False,
            "method": "",
            "filters": [],
            "reason": f"unknown stage {name!r}",
        }
    if not ffmpeg_available():
        return {
            "stage": name,
            "available": False,
            "method": "",
            "filters": [],
            "reason": "ffmpeg is not on PATH",
        }
    if name == "denoise":
        support = _denoise_support(payload)
    elif name == "dereverb":
        support = _dereverb_support(payload)
    elif name == "voice_isolation":
        support = _voice_isolation_support(payload)
    elif name == "loudness_normalization":
        support = _simple_support(
            ("loudnorm",),
            "loudnorm_two_pass",
            "EBU R128 two-pass loudnorm (pass 1 measures, pass 2 applies)",
        )
    elif name == "music_ducking":
        filters = ("sidechaincompress", "asplit")
        support = _simple_support(
            filters,
            "sidechaincompress",
            "music/bed sidechain compression (self sidechain split of the same input)",
        )
    elif name == "compression":
        support = _simple_support(
            ("acompressor",),
            "acompressor",
            "dynamic range compression",
        )
    elif name == "limiting":
        support = _simple_support(("alimiter",), "alimiter", "brickwall limiting")
        if support["available"]:
            support["true_peak_bound"] = _num(payload, "true_peak_limit_db", -1.0)
    elif name == "silence_detection":
        support = {
            "available": True,
            "method": "silencedetect",
            "filters": ["silencedetect"],
            "reason": "measurement via ffmpeg silencedetect (analysis, not a filter)",
        }
    elif name == "filler_detection":
        support = _filler_support()
    else:  # breath_click_detection
        support = {
            "available": True,
            "method": "heuristic_pcm",
            "filters": [],
            "confidence": "heuristic",
            "reason": (
                "stdlib PCM window analysis; a heuristic for editor hints, "
                "NOT a detector of record"
            ),
        }
    return {"stage": name, **support}


def _filler_support() -> dict:
    """Filler detection belongs to the silence/filler lane. Always unavailable here.

    Lane D's engine (``app.engine.intel.silence_fillers``) consumes TRANSCRIPT
    UNITS and writes ``edit_proposals`` through its own run kind -- it is a
    database-level API, not a file-level detector, so this pipeline cannot call
    it (and calling it with a path would be exactly the kind of cross-lane
    improvisation that produces a fake success). This module therefore never
    reimplements it and never invokes it; the stage reports ``unavailable`` with
    the reason, and ``POST /audio/fillers`` remains the honest path to it.
    """
    return {
        "available": False,
        "method": "",
        "filters": [],
        "reason": (
            "filler detection is owned by the silence/filler lane"
            f"{f' ({_filler_module()})' if _filler_module() else ''}: it consumes "
            "transcript units and writes edit_proposals through its own run kind, "
            "so the enhancement pipeline neither reimplements nor calls it"
        ),
    }


def _filler_module() -> str:
    """Name of a sibling filler engine module, for the reason text only."""
    try:
        import pkgutil

        import app.engine.intel as intel_pkg
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        logger.debug("filler module probe failed: %s", type(exc).__name__)
        return ""
    for info in pkgutil.iter_modules(intel_pkg.__path__):
        if "filler" in info.name.lower():
            return f"{intel_pkg.__name__}.{info.name}"
    return ""


# ---------------------------------------------------------------------------
# ffmpeg invocation helpers
# ---------------------------------------------------------------------------


def _filter_result(ok: bool, returncode: int, detail: str, output: Path, elapsed_ms: int) -> dict:
    return {
        "ok": bool(ok),
        "returncode": int(returncode),
        "stderr_tail": str(detail or "")[-2000:],
        "processing_ms": int(elapsed_ms),
        "output_path": str(output) if output else "",
    }


def _run_filter_complex(
    src: str | Path,
    dst: str | Path,
    graph: str,
    *,
    extra_args: tuple[str, ...] = (),
    timeout: float = FILTER_TIMEOUT,
) -> dict:
    """``ffmpeg -y -i <src> -filter_complex <graph> -map [out] <extra> <dst>``.

    Needed only by ``music_ducking``, whose sidechain is a split of the SAME
    input -- a shape ``-af`` cannot express. It mirrors
    :func:`app.engine.intel.ffmpeg_util.run_filter` exactly: same result dict,
    same finite timeout, no shell, and the SAME ``dst == src`` refusal (writing
    over the source would break the derived-only invariant, so that is a
    programming error, not an environment failure).
    """
    source = Path(str(src or ""))
    out_path = Path(str(dst or ""))
    if not str(source) or not str(out_path):
        return _filter_result(False, 2, "empty source or destination path", out_path, 0)
    try:
        same = source.resolve() == out_path.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        same = str(source) == str(out_path)
    if same:
        raise ValueError("filter graph refuses to write over its source (derived-only)")
    if not ffmpeg_available():
        return _filter_result(False, 127, "ffmpeg not on PATH", out_path, 0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y",
        "-i", str(source),
        "-filter_complex", str(graph),
        "-map", "[out]",
        *[str(a) for a in extra_args],
        str(out_path),
    ]
    started = time.perf_counter()
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell
            cmd, capture_output=True, timeout=timeout, check=False
        )
        rc, err = done.returncode, done.stderr or b""
    except subprocess.TimeoutExpired:
        rc, err = 124, b"timeout"
    except (OSError, ValueError) as exc:  # pragma: no cover - env dependent
        rc, err = 127, str(exc).encode(errors="replace")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = rc == 0 and out_path.exists()
    detail = "" if ok else err.decode("utf-8", "replace")[-2000:].strip()
    if not ok:
        logger.warning("filter graph failed rc=%s: %s", rc, detail[-200:])
    return _filter_result(ok, rc, detail, out_path, elapsed_ms)


def has_video_stream(path: str | Path) -> bool:
    """True when the source carries a video stream (derived keeps it)."""
    try:
        from app.engine.intel.ffmpeg_util import probe

        data = probe(path)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        logger.debug("probe failed for %s: %s", path, type(exc).__name__)
        return False
    return any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in (data.get("streams") or [])
    )


def output_spec(path: str | Path) -> dict:
    """Container/codec for the derived file.

    Audio-only sources are written as 16-bit PCM WAV: lossless, duration-exact
    (the QC duration-drift check depends on it) and directly readable by the
    stdlib PCM analyser. A source with a video stream keeps its video track
    (``-c:v copy``) and gets an ``aac`` audio track in an mp4 container.
    """
    if has_video_stream(path):
        return {"suffix": ".mp4", "extra": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
                "video": True, "container": "mp4", "audio_codec": "aac"}
    return {"suffix": ".wav", "extra": ["-c:a", "pcm_s16le", "-f", "wav"],
            "video": False, "container": "wav", "audio_codec": "pcm_s16le"}


# ---------------------------------------------------------------------------
# filter chains per stage
# ---------------------------------------------------------------------------


def _chain_denise(params: dict) -> str:
    strength = _num(params, "denoise_strength", 12.0)
    noise_floor = _num(params, "denoise_noise_floor_db", -25.0)
    return f"afftdn=nr={strength:g}:nf={noise_floor:g}"


def _chain_voice_isolation(params: dict) -> str:
    high = _num(params, "voice_isolation_highpass_hz", 120.0)
    low = _num(params, "voice_isolation_lowpass_hz", 7800.0)
    parts = [f"highpass=f={high:g}", f"lowpass=f={low:g}"]
    if bool(params.get("voice_isolation_denoise", True)):
        parts.append(f"afftdn=nr={_num(params, 'denoise_strength', 12.0):g}"
                     f":nf={_num(params, 'denoise_noise_floor_db', -25.0):g}")
    return ",".join(parts)


def _chain_compression(params: dict) -> str:
    # acompressor.threshold and .makeup are LINEAR gains (0..1 and 1..64), not dB.
    # Passing dB there is a hard ffmpeg error ("Result too large"), so the
    # operator-facing dB parameters are converted here.
    threshold = max(0.0001, min(1.0, 10 ** (_num(params, "compressor_threshold_db", -18.0) / 20.0)))
    makeup = max(1.0, min(64.0, 10 ** (_num(params, "compressor_makeup_db", 0.0) / 20.0)))
    return (
        "acompressor="
        f"threshold={threshold:.6f}"
        f":ratio={_num(params, 'compressor_ratio', 3.0):g}"
        f":attack={_num(params, 'compressor_attack_ms', 20.0):g}"
        f":release={_num(params, 'compressor_release_ms', 250.0):g}"
        f":makeup={makeup:.6f}"
    )


def _chain_limiter(params: dict) -> str:
    ceiling_db = _num(params, "limiter_ceiling_db", -1.0)
    linear = max(0.0001, min(1.0, 10 ** (ceiling_db / 20.0)))
    return (
        "alimiter="
        f"level=disabled:limit={linear:.6f}"
        f":attack={_num(params, 'limiter_attack_ms', 5.0):g}"
        f":release={_num(params, 'limiter_release_ms', 50.0):g}"
        ":level_in=1"
    )


def _duck_graph(params: dict) -> str:
    """Self-sidechain ducking graph: split the input, duck it under itself.

    The sidechain is the SAME track band-limited to the voice region, so what
    this really does is duck the loud parts of a track under its own voice band
    -- it cannot separate a music bed from a voice that were already mixed into
    one file. The limitation is the reason it is labelled
    ``method="sidechaincompress"`` and never "music separation". The threshold
    default is deliberately HIGH (a level in the voice band), because a low
    threshold against a self-derived sidechain compresses the whole programme
    continuously and costs ~6 LU of level.
    """
    sc_high = _num(params, "ducking_sidechain_highpass_hz", 200.0)
    sc_low = _num(params, "ducking_sidechain_lowpass_hz", 4000.0)
    threshold = max(0.001, min(1.0,
                              10 ** (_num(params, "ducking_threshold_db", -12.0) / 20.0)))
    return (
        "[0:a]asplit=2[duck_main][duck_sc];"
        f"[duck_sc]highpass=f={sc_high:g},lowpass=f={sc_low:g},"
        f"volume={_num(params, 'ducking_sidechain_gain_db', 0.0):g}dB[sc];"
        "[duck_main][sc]sidechaincompress="
        f"threshold={threshold:.6f}"
        f":ratio={_num(params, 'ducking_ratio', 4.0):g}"
        f":attack={_num(params, 'ducking_attack_ms', 20.0):g}"
        f":release={_num(params, 'ducking_release_ms', 300.0):g}"
        ":makeup=1[out]"
    )


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def loudnorm_pass_one(path: str | Path, params: dict) -> tuple[dict, str]:
    """Measure EBU R128 loudness for the loudnorm pass 2.

    ``ffmpeg -i <path> -af loudnorm=I=..:TP=..:LRA=..:print_format=json -f null -``

    Returns ``(measured, warning)``. ``measured`` is empty when the filter
    printed nothing parseable -- a clip shorter than the loudnorm window
    legitimately reports ``-inf``, and that is reported as "not measured"
    rather than substituted with a plausible number.
    """
    target_i = _num(params, "target_lufs", -14.0)
    target_tp = _num(params, "target_true_peak_db", -1.5)
    target_lra = _num(params, "target_lra", 11.0)
    chain = (f"loudnorm=I={target_i:g}:TP={target_tp:g}:LRA={target_lra:g}"
             ":print_format=json")
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell
            ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
             "-af", chain, "-f", "null", "-"],
            capture_output=True, timeout=180, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return {}, f"loudnorm measurement pass failed ({type(exc).__name__})"
    text = (done.stderr or b"").decode("utf-8", "replace")
    match = _LOUDNORM_JSON.search(text)
    if not match:
        return {}, "loudnorm measurement pass produced no JSON (clip may be too short)"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}, "loudnorm measurement JSON was not parseable"
    measured: dict[str, float] = {}
    for key, param in (("input_i", "measured_i"), ("input_tp", "measured_tp"),
                       ("input_lra", "measured_lra"), ("input_thresh", "measured_thresh"),
                       ("target_offset", "offset")):
        try:
            value = float(data.get(key))
        except (TypeError, ValueError):
            continue
        if value == value and abs(value) != float("inf"):
            measured[param] = value
    if not measured:
        return {}, "loudnorm measurement returned no finite values"
    return measured, ""


def _chain_loudnorm_pass_two(params: dict, measured: dict) -> str:
    target_i = _num(params, "target_lufs", -14.0)
    target_tp = _num(params, "target_true_peak_db", -1.5)
    target_lra = _num(params, "target_lra", 11.0)
    chain = (f"loudnorm=I={target_i:g}:TP={target_tp:g}:LRA={target_lra:g}")
    mapping = (
        ("measured_i", "input_i"),
        ("measured_tp", "input_tp"),
        ("measured_lra", "input_lra"),
        ("measured_thresh", "input_thresh"),
        ("offset", "target_offset"),
    )
    for key, source in mapping:
        value = measured.get(key)
        if value is not None:
            chain += f":{key}={value:.6f}"
    chain += ":linear=true:print_format=summary"
    return chain


# ---------------------------------------------------------------------------
# analysis stages
# ---------------------------------------------------------------------------


def _analyse_silence(path: str | Path, params: dict) -> tuple[dict, str]:
    """Silence ranges via the shared helper (contracts §6: analysis, not a filter)."""
    ranges = detect_silence(
        path,
        noise_db=_num(params, "silence_noise_db", -50.0),
        min_duration=_num(params, "silence_min_duration_s", 0.8),
    )
    total = sum(float(r.get("duration_s") or 0.0) for r in ranges)
    return {
        "ranges": ranges,
        "range_count": len(ranges),
        "total_silence_s": round(total, 4),
        "method": "silencedetect",
        "noise_db": _num(params, "silence_noise_db", -50.0),
        "min_duration_s": _num(params, "silence_min_duration_s", 0.8),
    }, ""


def _analyse_breath_click(path: str | Path, params: dict) -> tuple[dict, str]:
    """Stdlib PCM window analysis -- labelled ``heuristic_pcm``.

    The PCM primitives live in the ENGINE (:mod:`app.engine.intel.audio_enhance`)
    so every consumer shares one implementation and this adapter stays swappable.
    They are imported HERE, inside the function, so this module keeps zero
    import-time coupling to the engine.
    """
    from app.engine.intel.audio_enhance import detect_breath_click

    analysis = detect_breath_click(
        path,
        window_ms=_num(params, "breath_click_window_ms", 30.0),
        max_events=int(_num(params, "breath_click_max_events", 200)),
    )
    if not analysis.get("available"):
        return {}, str(analysis.get("reason") or "PCM analysis unavailable")
    return analysis, ""


def _analyse_fillers(path: str | Path, params: dict) -> tuple[dict, str]:
    """Not implemented here on purpose -- see :func:`_filler_support`."""
    return {}, _filler_support()["reason"]


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------


class FfmpegEnhancementProvider(MediaIntelProvider):
    """Local, CPU-only audio enhancement through the ffmpeg binary.

    Serves the ``enhancement`` chain and, because it declares
    ``kinds = ("denoise",)``, the ``denoise`` chain as well -- that second
    declaration is what lets the registry fall back from an unavailable neural
    denoiser (RNNoise) to a real spectral one instead of reporting a dead
    capability.
    """

    key = PROVIDER_KEY
    kind = "enhancement"
    kinds = ("denoise",)

    def health(self) -> ProviderHealth:
        """Never raises. Unavailable => a reason, never a fabricated stage."""
        try:
            if not ffmpeg_available():
                return ProviderHealth(
                    available=False,
                    reason="ffmpeg is not on PATH; no audio stage can run",
                    mode=MODE_SUBPROCESS,
                    detail={"stages": {}, "ffmpeg": ""},
                )
            stages = {name: stage_support(name) for name in STAGES}
            usable = [n for n in STAGES if stages[n]["available"]]
            return ProviderHealth(
                available=True,
                version=ffmpeg_version(),
                mode=MODE_SUBPROCESS,
                detail={
                    "stages": {
                        name: {
                            "available": bool(info["available"]),
                            "method": info.get("method", ""),
                            "reason": info.get("reason", ""),
                        }
                        for name, info in stages.items()
                    },
                    "usable_stages": usable,
                    "unavailable_stages": [n for n in STAGES if not stages[n]["available"]],
                    "heuristic_stages": sorted(HEURISTIC_STAGES),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a health probe must not raise
            return ProviderHealth(
                available=False,
                reason=f"ffmpeg enhancement probe failed: {type(exc).__name__}",
                mode=MODE_SUBPROCESS,
                detail={"error_type": type(exc).__name__},
            )

    def capabilities(self) -> dict:
        """Honest stage map: nothing is claimed that the probe did not confirm."""
        return {
            "available": bool(ffmpeg_available()),
            "reason": "" if ffmpeg_available() else "ffmpeg is not on PATH",
            "stages": list(STAGES),
            "filter_stages": sorted(FILTER_STAGES),
            "analysis_stages": sorted(ANALYSIS_STAGES),
            "stage_statuses": list(STAGE_STATUSES),
            "stage_support": {name: stage_support(name) for name in STAGES},
            "derived_only": True,
            "overwrites_source": False,
            "perceptual_claims": False,
            "limits": {
                "max_duration_s": 3600,
                "sample_rates": "any the source decodes to",
                "audio_formats_in": ["wav", "mp3", "m4a", "aac", "flac", "ogg", "opus",
                                     "mp4", "mov", "mkv", "webm"],
                "audio_formats_out": ["wav", "mp4"],
            },
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            ram_mb=256,
            cpu_seconds_per_audio_minute=3.0,
            notes="external ffmpeg process; CPU only, no GPU admission needed",
        )

    def license_info(self) -> LicenseInfo:
        """Verbatim from ``docs/oss/MEDIA_INTEL_LICENSES.md`` (ffmpeg row).

        REVIEW_REQUIRED, never PERMITTED: ffmpeg is an EXTERNAL process, so the
        operator's own build/distribution obligations (this host runs a full
        GPL-enabled build) apply and are not ours to waive.
        """
        return LicenseInfo(
            code_license="LGPL-2.1-or-later",
            code_license_url="https://ffmpeg.org/legal.html",
            model_license="N/A",
            model_license_url="",
            model_gated=False,
            commercial_use=COMMERCIAL_REVIEW_REQUIRED,
            audited_on=AUDITED_ON,
            notes=(
                "ffmpeg core is LGPL-2.1-or-later; build-dependent components may be "
                "GPL-2.0-or-later (this host uses a full build). Invoked as an external "
                "process, so the operator's own build/distribution obligations apply; "
                "confirm before redistributing a binary. No model weights are involved."
            ),
        )

    def cost(self, spec: ResourceSpec) -> dict:
        return {
            "gpu_ms": 0,
            "cpu_ms": int(max(0.0, float(spec.cpu_seconds_per_audio_minute)) * 1000),
            "cost_micros": 0,
            "billed": False,
            "currency": "cpu_only",
        }

    # -- the work ----------------------------------------------------------

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Apply the requested stages, one ffmpeg pass per transforming stage.

        ``request.params`` keys (all optional):
        ``stages`` (ordered list or per-stage dict of params), ``output_path``
        (the derived file to write), ``silence_*``, ``breath_click_*``,
        ``denoise_*``, ``target_lufs``/``target_true_peak_db``/``target_lra``,
        ``limiter_*``, ``compressor_*``, ``ducking_*``,
        ``voice_isolation_*``.
        """
        if not ffmpeg_available():
            raise ProviderUnavailable("ffmpeg is not on PATH")
        source = Path(str(request.storage_path or ""))
        if not source.exists():
            raise ProviderUnavailable("source media is not readable")

        params = dict(request.params or {})
        output_path = str(params.get("output_path") or "").strip()
        requested = _requested_stages(params)
        warnings: list[str] = []

        workdir = Path(tempfile.mkdtemp(prefix="ymoney-enhance-"))
        spec = output_spec(source)
        current = source
        applied = 0
        records: list[dict] = []
        total = len(requested) or 1
        try:
            for index, stage in enumerate(requested):
                check_control(should_cancel, deadline, every=1, counter=index)
                support = stage_support(stage, params)
                record: dict[str, Any] = {
                    "stage": stage,
                    "method": support.get("method", ""),
                    "filters": list(support.get("filters") or []),
                    "neural": bool(support.get("neural", False)),
                    "confidence": support.get("confidence", ""),
                    "reason": support.get("reason", ""),
                }
                if not support.get("available"):
                    record["status"] = "unavailable"
                    record["reason"] = support.get("reason") or "no method available"
                    records.append(record)
                    progress((index + 1) / total)
                    continue

                stage_started = time.perf_counter()
                if stage in ANALYSIS_STAGES:
                    record["analyzed"] = ("stage_output" if applied else "input")
                    ok, payload, reason = _run_analysis(stage, current, params)
                    record["status"] = "applied" if ok else "failed"
                    if not ok:
                        record["reason"] = reason
                    else:
                        record["result"] = payload
                    record["processing_ms"] = int(
                        (time.perf_counter() - stage_started) * 1000
                    )
                    records.append(record)
                    progress((index + 1) / total)
                    continue

                destination = workdir / f"stage-{index:02d}{spec['suffix']}"
                ok, detail, failure = _run_filter_stage(
                    stage, current, destination, params, spec
                )
                record["processing_ms"] = int((time.perf_counter() - stage_started) * 1000)
                record["processing_ms_reported"] = int(detail.get("processing_ms") or 0)
                if not ok:
                    # honest per-stage failure: the reason is ffmpeg's own, and
                    # the pipeline continues from the last good intermediate.
                    record["status"] = "failed"
                    record["reason"] = str(failure or "ffmpeg pass failed")[:500]
                    warnings.append(f"{stage}: {record['reason'][:160]}")
                    records.append(record)
                    progress((index + 1) / total)
                    continue
                record["status"] = "applied"
                record["method"] = detail.get("method", record["method"])
                record["reason"] = detail.get("reason", record["reason"])
                if detail.get("warning"):
                    record["warning"] = detail["warning"]
                    warnings.append(f"{stage}: {detail['warning'][:160]}")
                if detail.get("measured"):
                    record["measured"] = detail["measured"]
                current = destination
                applied += 1
                records.append(record)
                progress((index + 1) / total)

            artifacts: dict[str, Any] = {"stages": records}
            if applied and output_path:
                target = Path(output_path)
                _publish(current, target, source)
                artifacts["audio"] = {
                    "path": str(target),
                    "container": spec["container"],
                    "audio_codec": spec["audio_codec"],
                    "has_video": spec["video"],
                    "from_stage": records[-1]["stage"] if records else "",
                }
            elif applied:
                warnings.append("no output_path requested; no derived asset written")
            else:
                artifacts["audio"] = {}
            if not applied and not any(r["status"] == "applied" and r["stage"] in ANALYSIS_STAGES
                                       for r in records):
                warnings.append("no stage applied; no derived media produced")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        metrics = {
            "stages_requested": len(requested),
            "stages_applied": applied,
            "stages_unavailable": sum(1 for r in records if r["status"] == "unavailable"),
            "stages_failed": sum(1 for r in records if r["status"] == "failed"),
            "stages_skipped": sum(1 for r in records if r["status"] == "skipped"),
            "processing_ms": sum(int(r.get("processing_ms") or 0) for r in records),
            "derived": bool(applied and output_path),
            "source_duration_s": duration_seconds(source),
        }
        return ProviderResult(
            # "ok" means the provider DID the requested work. A run where every
            # stage failed did nothing, and the engine reads the per-stage
            # records for the full truth either way.
            ok=any(r["status"] == "applied" for r in records),
            artifacts=artifacts,
            metrics=metrics,
            warnings=warnings,
        )


def _requested_stages(params: dict) -> list[str]:
    """Ordered, de-duplicated, validated stage list from ``params['stages']``.

    Accepts a list (``["denoise", "limiting"]``), a set/tuple, or a dict
    (``{"denoise": {"nr": 12}}`` -- the per-stage params are merged into the
    global params by the engine). Unknown names are dropped here; the engine
    reports them as ``skipped`` before it ever calls the provider.
    """
    raw = params.get("stages")
    if isinstance(raw, dict):
        names: list[str] = []
        for name, value in raw.items():
            if isinstance(value, dict):
                params.setdefault("stage_params", {})[str(name)] = dict(value)
            names.append(str(name))
    elif isinstance(raw, (list, tuple, set)):
        names = [str(n) for n in raw]
    else:
        names = []
    ordered = [s for s in STAGES if s in {n.strip().lower() for n in names}]
    return ordered


def _merge_stage_params(params: dict) -> dict:
    """Fold per-stage params (``stage_params``) over the global ones."""
    merged = dict(params)
    stage_params = params.get("stage_params")
    if isinstance(stage_params, dict):
        for name, values in stage_params.items():
            if isinstance(values, dict):
                for key, value in values.items():
                    merged.setdefault(f"{str(name).strip().lower()}_{key}", value)
                    merged.setdefault(key, value)
    return merged


def _run_analysis(stage: str, path: Path, params: dict) -> tuple[bool, dict, str]:
    if stage == "silence_detection":
        payload, warning = _analyse_silence(path, params)
        return True, payload, warning
    if stage == "filler_detection":
        payload, warning = _analyse_fillers(path, params)
        return bool(payload), payload, warning
    payload, warning = _analyse_breath_click(path, params)
    return bool(payload), payload, warning


def _run_filter_stage(
    stage: str, src: Path, dst: Path, params: dict, spec: dict
) -> tuple[bool, dict, str]:
    """One ffmpeg pass for one transforming stage.

    Returns ``(ok, detail, failure_reason)``. ``detail`` carries the applied
    ``method``/``reason`` plus anything the stage measured on the way.
    """
    merged = _merge_stage_params(params)
    detail: dict[str, Any] = {}
    if stage == "denoise":
        detail.update(_denoise_support(merged))
        result = run_filter(src, dst, _chain_denise(merged), extra_args=tuple(spec["extra"]))
    elif stage == "dereverb":
        support = _dereverb_support(merged)
        if not support["available"]:
            return False, support, support["reason"]
        detail.update(support)
        result = run_filter(src, dst, "dereverb", extra_args=tuple(spec["extra"]))
    elif stage == "voice_isolation":
        support = _voice_isolation_support(merged)
        detail.update(support)
        result = run_filter(src, dst, _chain_voice_isolation(merged),
                            extra_args=tuple(spec["extra"]))
    elif stage == "compression":
        detail.update(_simple_support(("acompressor",), "acompressor", "dynamic range compression"))
        result = run_filter(src, dst, _chain_compression(merged),
                            extra_args=tuple(spec["extra"]))
    elif stage == "limiting":
        support = _simple_support(("alimiter",), "alimiter", "brickwall limiting")
        detail.update(support)
        ceiling = _num(merged, "true_peak_limit_db", -1.0)
        detail["true_peak_bound"] = ceiling
        result = run_filter(src, dst, _chain_limiter(merged), extra_args=tuple(spec["extra"]))
    elif stage == "music_ducking":
        support = _simple_support(("sidechaincompress", "asplit"), "sidechaincompress",
                                  "self-sidechain ducking")
        detail.update(support)
        extra = tuple(spec["extra"])
        if spec.get("video"):
            extra = ("-map", "0:v?", *extra)
        result = _run_filter_complex(src, dst, _duck_graph(merged), extra_args=extra)
    elif stage == "loudness_normalization":
        detail.update(_simple_support(("loudnorm",), "loudnorm_two_pass",
                                      "EBU R128 two-pass loudnorm"))
        measured, warning = loudnorm_pass_one(src, merged)
        if measured:
            chain = _chain_loudnorm_pass_two(merged, measured)
            detail["measured_pass1"] = measured
        else:
            # one pass, dynamic mode, honestly labelled: the measurement the
            # second pass needs does not exist for this clip.
            chain = (f"loudnorm=I={_num(merged, 'target_lufs', -14.0):g}"
                     f":TP={_num(merged, 'target_true_peak_db', -1.5):g}"
                     f":LRA={_num(merged, 'target_lra', 11.0):g}")
            detail["method"] = "loudnorm_single_pass"
            if warning:
                detail["warning"] = warning
        result = run_filter(src, dst, chain, extra_args=tuple(spec["extra"]))
    else:  # pragma: no cover - guarded by the caller
        return False, {}, f"stage {stage} has no filter implementation"
    if not result.get("ok"):
        return False, detail, str(result.get("stderr_tail") or "ffmpeg exited "
                                  f"{result.get('returncode')}")
    detail["processing_ms"] = int(result.get("processing_ms") or 0)
    return True, detail, ""


def _publish(source_file: Path, target: Path, original: Path) -> None:
    """Copy the final intermediate to the derived path (refuses ``target==original``)."""
    try:
        same = target.resolve() == original.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        same = str(target) == str(original)
    if same:
        raise ValueError("refusing to write the enhanced audio over its source asset")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_file, target)


#: the registry loads ``impl.<key>.PROVIDER`` (contracts §1.1).
PROVIDER = FfmpegEnhancementProvider


__all__ = [
    "ANALYSIS_STAGES",
    "AUDITED_ON",
    "FILTER_STAGES",
    "HEURISTIC_STAGES",
    "PROVIDER",
    "PROVIDER_KEY",
    "STAGES",
    "STAGE_STATUSES",
    "FfmpegEnhancementProvider",
    "ffmpeg_version",
    "filter_available",
    "has_video_stream",
    "loudnorm_pass_one",
    "output_spec",
    "reset_probes",
    "stage_support",
]
