"""Shared ffmpeg/ffprobe helpers for every Work 12 lane (Lane A foundation).

The single place media intelligence touches the ffmpeg binary. Lanes B (speech
alignment/VAD), C (audio enhancement), D (silence/fillers), E (faces), F
(segmentation + active speaker) and G (reframe/layouts) all build on these
functions so measurement semantics are identical everywhere and no lane
re-invents a parser.

Hard rules (contracts §0):

* **stdlib + ffmpeg only** -- ``subprocess`` with list args, no shell, no new
  dependency, and every call has a FINITE timeout so a hung binary can never
  wedge a request or a worker.
* **Never raise.** Every function returns an empty/``None``/``{"ok": False}``
  value on failure and logs the reason on the ``ymoney.intel`` logger. The ONE
  deliberate exception is :func:`run_filter` refusing ``dst == src``: writing
  over a source file would break the derived-only invariant, and that is a
  programming error, not an environment failure.
* **Never mutate the input.** :func:`run_filter` and :func:`concat_audio`
  always write to a NEW path (callers pass a derived path under the workspace
  storage root and register the result as a new ``MediaAsset``).
* **Report what was measured.** A metric that the invocation did not produce is
  ``None`` -- never a plausible default. ``measure_peaks`` returns
  ``clipped_samples: None`` on ffmpeg builds whose ``astats`` does not print it.

Each docstring names the EXACT ffmpeg invocation used, so a later lane can
audit or reproduce a measurement without reading the code.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("ymoney.intel")

#: Default wall-clock ceilings (seconds). Every subprocess call is bounded.
PROBE_TIMEOUT = 30
MEASURE_TIMEOUT = 300
FILTER_TIMEOUT = 600
FRAME_TIMEOUT = 600

#: stderr tail kept in a result dict -- enough to diagnose, never a stack dump.
STDERR_TAIL_CHARS = 2000

#: filters that operate on VIDEO; used to pick -vf vs -af when the caller does
#: not say explicitly. Anything else is treated as an audio chain.
_VIDEO_FILTER_PREFIXES: tuple[str, ...] = (
    "blend", "boxblur", "crop", "drawbox", "drawtext", "eq", "fps", "gblur",
    "hflip", "overlay", "pad", "rotate", "scale", "select", "setsar",
    "split", "tile", "transpose", "unsharp", "vflip", "zoompan", "colorchannelmixer",
)

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_SILENCE_DURATION = re.compile(r"silence_duration:\s*(-?[\d.]+)")


# ---------------------------------------------------------------------------
# binary guards + the one place a subprocess is spawned
# ---------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    """True when the ``ffmpeg`` binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    """True when the ``ffprobe`` binary is on PATH."""
    return shutil.which("ffprobe") is not None


def _run(cmd: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
    """``subprocess.run`` that never raises. Returns (rc, stdout, stderr)."""
    try:
        done = subprocess.run(  # noqa: S603 - list args, no shell, trusted binary
            list(cmd),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timeout after %ss: %s", timeout, " ".join(cmd[:4]))
        return 124, b"", b"timeout"
    except (OSError, ValueError) as exc:
        logger.warning("ffmpeg could not run (%s): %s", type(exc).__name__, cmd[:2])
        return 127, b"", str(exc).encode(errors="replace")
    return done.returncode, done.stdout or b"", done.stderr or b""


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _tail(raw: bytes, limit: int = STDERR_TAIL_CHARS) -> str:
    """Last ``limit`` chars of stderr, whitespace-collapsed to one tail line."""
    return _text(raw)[-limit:].strip()


def _as_float(value: str | None) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


def probe(path: str | Path) -> dict:
    """``ffprobe -v quiet -print_format json -show_format -show_streams <path>``.

    Returns the parsed dict, or ``{}`` when ffprobe is missing, the file is
    unreadable, or the JSON is malformed (mirrors ``services/storage.py``).
    """
    target = str(path or "")
    if not target or not ffprobe_available():
        return {}
    rc, out, _ = _run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", target],
        PROBE_TIMEOUT,
    )
    if rc != 0:
        return {}
    try:
        data = json.loads(_text(out) or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def duration_seconds(path: str | Path) -> float | None:
    """Duration in seconds from the container/stream, or None when unknown."""
    data = probe(path)
    if not data:
        return None
    fmt = data.get("format") or {}
    for candidate in (fmt.get("duration"), *(s.get("duration") for s in data.get("streams", []))):
        value = _as_float(candidate if isinstance(candidate, (str, int, float)) else None)
        if value is not None and value >= 0:
            return value
    return None


def _video_stream(data: dict) -> dict:
    for stream in data.get("streams", []) or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            return stream
    return {}


# ---------------------------------------------------------------------------
# audio measurement
# ---------------------------------------------------------------------------


def measure_loudness(path: str | Path) -> dict:
    """EBU R128 loudness via ``ffmpeg -i <path> -af ebur128=peak=true -f null -``.

    Parses ONLY the trailing ``Summary:`` block (the per-frame lines also carry
    ``I:``/``LRA:``/``TPK:`` and would otherwise be matched by mistake)::

        Summary:
            I:    -27.8 LUFS
            LRA:     0.0 LU
          True peak:
            Peak:  -24.1 dBFS

    Returns ``{"integrated_lufs", "lra", "peak_dbfs", "true_peak_dbfs"}``.
    ``peak_dbfs`` is ALWAYS ``None`` here: ``ebur128`` reports only the true
    peak -- call :func:`measure_peaks` for the sample peak. Any field the
    invocation did not print is ``None``.
    """
    empty = {
        "integrated_lufs": None,
        "lra": None,
        "peak_dbfs": None,
        "true_peak_dbfs": None,
    }
    target = str(path or "")
    if not target or not ffmpeg_available():
        return empty
    _, _, err = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", target,
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        MEASURE_TIMEOUT,
    )
    text = _text(err)
    marker = text.rfind("Summary:")
    if marker < 0:
        return empty
    summary = text[marker:]
    integrated = re.search(r"\bI:\s*(-?[\d.]+)\s*LUFS", summary)
    lra = re.search(r"\bLRA:\s*(-?[\d.]+)\s*LU\b", summary)
    peak = re.search(r"\bPeak:\s*(-?[\d.]+)\s*dBFS", summary)
    return {
        "integrated_lufs": _as_float(integrated.group(1)) if integrated else None,
        "lra": _as_float(lra.group(1)) if lra else None,
        "peak_dbfs": None,  # ebur128 does not measure the sample peak
        "true_peak_dbfs": _as_float(peak.group(1)) if peak else None,
    }


def measure_peaks(path: str | Path) -> dict:
    """Sample peak / flat factor via ``ffmpeg -i <path> -af astats=metadata=1:reset=0 -f null -``.

    Parses the ``astats`` summary lines::

        Peak level dB: -24.082135
        Flat factor: 0.000000
        Number of samples: 264600

    Returns ``{"peak_dbfs", "samples", "flat_factor", "clipped_samples"}``.
    ``clipped_samples`` is ``None`` unless the build actually prints a clipped
    sample count (ffmpeg 8.1.1 does not) -- it is never estimated.
    """
    empty = {"peak_dbfs": None, "samples": None, "flat_factor": None,
             "clipped_samples": None}
    target = str(path or "")
    if not target or not ffmpeg_available():
        return empty
    _, _, err = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", target,
         "-af", "astats=metadata=1:reset=0", "-f", "null", "-"],
        MEASURE_TIMEOUT,
    )
    text = _text(err)
    peak = re.search(r"Peak level dB:\s*(-?[\d.]+)", text)
    flat = re.search(r"Flat factor:\s*(-?[\d.]+)", text)
    samples = re.search(r"Number of samples:\s*(\d+)", text)
    clipped = re.search(r"Number of clipped samples:\s*(\d+)", text)
    return {
        "peak_dbfs": _as_float(peak.group(1)) if peak else None,
        "samples": int(samples.group(1)) if samples else None,
        "flat_factor": _as_float(flat.group(1)) if flat else None,
        "clipped_samples": int(clipped.group(1)) if clipped else None,
    }


def detect_silence(
    path: str | Path,
    noise_db: float = -50.0,
    min_duration: float = 0.8,
) -> list[dict]:
    """Silence ranges via ``ffmpeg -i <path> -af silencedetect=noise=<n>dB:d=<s> -f null -``.

    Parses the filter's stderr lines::

        silence_start: 2
        silence_end: 4 | silence_duration: 2

    Returns ``[{"start_s", "end_s", "duration_s"}, ...]`` in media time, sorted
    by start. A ``silence_start`` with no matching end (trailing silence to EOF)
    is closed using :func:`duration_seconds`. A MEDIA START that is silent has
    no ``silence_start`` at all, so it is NOT reported -- callers that need it
    must treat ``start_s == 0`` as a boundary. Returns ``[]`` on any failure.
    """
    target = str(path or "")
    if not target or not ffmpeg_available():
        return []
    chain = f"silencedetect=noise={float(noise_db):g}dB:d={float(min_duration):g}"
    _, _, err = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", target,
         "-af", chain, "-f", "null", "-"],
        MEASURE_TIMEOUT,
    )
    text = _text(err)
    if "silence_start" not in text:
        return []
    total = duration_seconds(target)
    spans: list[dict] = []
    open_at: float | None = None
    for line in text.splitlines():
        start = _SILENCE_START.search(line)
        if start:
            open_at = _as_float(start.group(1))
            continue
        end = _SILENCE_END.search(line)
        if not end or open_at is None:
            continue
        end_at = _as_float(end.group(1))
        duration = _SILENCE_DURATION.search(line)
        if end_at is None:
            continue
        length = _as_float(duration.group(1)) if duration else max(0.0, end_at - open_at)
        spans.append({
            "start_s": open_at,
            "end_s": end_at,
            "duration_s": length if length is not None else max(0.0, end_at - open_at),
        })
        open_at = None
    if open_at is not None:
        end_at = total if total and total > open_at else open_at
        spans.append({
            "start_s": open_at,
            "end_s": end_at,
            "duration_s": max(0.0, end_at - open_at),
        })
    spans.sort(key=lambda s: s["start_s"])
    return spans


# ---------------------------------------------------------------------------
# filter application (always to a NEW file)
# ---------------------------------------------------------------------------


def _filter_args(filter_chain: str | Sequence[str], video: bool | None) -> list[str]:
    """Normalise a filter chain into CLI args (never raises).

    Accepts three shapes:
      * ``["-af", "loudnorm", "..."]``  -> used verbatim (caller chose -af/-vf)
      * ``["loudnorm", "..."]`` / ``"loudnorm,..."`` -> wrapped in -af (or -vf)
    When ``video`` is None the choice is made from a known video-filter prefix.
    """
    chain = list(filter_chain) if isinstance(filter_chain, (list, tuple)) else [
        str(filter_chain)
    ]
    if not chain:
        return []
    if chain[0] in {"-af", "-vf"}:
        return chain
    joined = ",".join(chain)
    if video is None:
        head = joined.strip().split(",", 1)[0].split("=", 1)[0].strip()
        video = head in _VIDEO_FILTER_PREFIXES
    return ["-vf" if video else "-af", joined]


def run_filter(
    src: str | Path,
    dst: str | Path,
    filter_chain: str | Sequence[str],
    *,
    extra_args: Sequence[str] = (),
    timeout: float = FILTER_TIMEOUT,
    video: bool | None = None,
) -> dict:
    """``ffmpeg -y -i <src> <filter args> <extra args> <dst>`` -- NEW file only.

    ``filter_chain`` may be a string (``"loudnorm=I=-16:TP=-1.5"``), a list of
    filter args, or a list that already starts with ``-af``/``-vf``. Pass
    ``video=True``/``False`` to force the stream type when auto-detection is
    wrong; otherwise a known video-filter prefix decides.

    Returns
    ``{"ok", "returncode", "stderr_tail", "processing_ms", "output_path"}``.
    ``ok`` is True only on returncode 0 AND an existing output file.

    Raises ``ValueError`` when ``dst`` resolves to ``src``: overwriting the
    source would break the derived-only invariant (contracts §0).
    """
    source = Path(str(src or ""))
    out_path = Path(str(dst or ""))
    if not source or not out_path:
        return _filter_result(False, 2, "empty source or destination path", out_path, 0)
    try:
        same = source.resolve() == out_path.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        same = str(source) == str(out_path)
    if same:
        raise ValueError("run_filter refuses to write over its source (derived-only)")
    if not ffmpeg_available():
        return _filter_result(False, 127, "ffmpeg not on PATH", out_path, 0)

    args = _filter_args(filter_chain, video)
    if not args:
        return _filter_result(False, 2, "empty filter chain", out_path, 0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y",
        "-i", str(source), *args, *[str(a) for a in extra_args], str(out_path),
    ]
    started = time.perf_counter()
    rc, _, err = _run(cmd, timeout)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = rc == 0 and out_path.exists()
    detail = "" if ok else _tail(err)
    result = _filter_result(ok, rc, detail, out_path, elapsed_ms)
    if not ok:
        logger.warning("run_filter failed rc=%s out=%s: %s", rc, out_path.name, detail[-200:])
    return result


def _filter_result(
    ok: bool, returncode: int, detail: str, output_path: Path, processing_ms: int
) -> dict:
    return {
        "ok": bool(ok),
        "returncode": int(returncode),
        "stderr_tail": str(detail or "")[-STDERR_TAIL_CHARS:],
        "processing_ms": int(processing_ms),
        "output_path": str(output_path) if output_path else "",
    }


def concat_audio(paths: Sequence[str | Path], out_path: str | Path) -> dict:
    """Join audio files with a real ``filter_complex concat``.

    ``ffmpeg -i a -i b ... -filter_complex "[0:a][1:a]...concat=n=N:v=0:a=1[out]" -map "[out] <out_path>"``

    Returns the same dict shape as :func:`run_filter` (``ok`` False on any
    failure, including fewer than one input or a missing binary). Test fixtures
    and multi-segment exports use it; it never overwrites an input.
    """
    inputs = [str(p) for p in (paths or []) if p]
    target = Path(str(out_path or ""))
    if not inputs or not ffmpeg_available():
        return _filter_result(False, 2, "no inputs or ffmpeg missing", target, 0)
    try:
        if any(Path(i).resolve() == target.resolve() for i in inputs):
            raise ValueError("concat_audio refuses to write over one of its inputs")
    except OSError:  # pragma: no cover - unresolvable path
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    chain = "".join(f"[{i}:a]" for i in range(len(inputs)))
    started = time.perf_counter()
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y",
        *[arg for i in inputs for arg in ("-i", i)],
        "-filter_complex", f"{chain}concat=n={len(inputs)}:v=0:a=1[out]",
        "-map", "[out]", str(target),
    ]
    rc, _, err = _run(cmd, FILTER_TIMEOUT)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = rc == 0 and target.exists()
    if not ok:
        logger.warning("concat_audio failed rc=%s: %s", rc, _tail(err)[-200:])
    return _filter_result(ok, rc, "" if ok else _tail(err), target, elapsed_ms)


# ---------------------------------------------------------------------------
# frames + motion energy (face / segmentation / reframe / active-speaker lanes)
# ---------------------------------------------------------------------------


def extract_frames(
    path: str | Path,
    fps: float,
    out_dir: str | Path,
    *,
    width: int | None = None,
    timeout: float = FRAME_TIMEOUT,
) -> list[str]:
    """Write PNG frames to ``out_dir`` -- for the face/segmentation lanes.

    ``ffmpeg -y -i <path> -vf fps=<fps>[,scale=<width>:-1] <out_dir>/frame_%06d.png``

    Returns the sorted list of written frame paths (``[]`` on any failure).
    ``width`` downscales (height follows, aspect preserved). Frames are written
    to a NEW directory -- the source media is only read.
    """
    target = str(path or "")
    directory = Path(str(out_dir or ""))
    if not target or not ffmpeg_available():
        return []
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        return []
    if rate <= 0:
        return []
    chain = f"fps={rate:g}"
    if width:
        chain += f",scale={int(width)}:-1"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("extract_frames mkdir failed: %s", exc)
        return []
    rc, _, err = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y", "-i", target,
         "-vf", chain, str(directory / "frame_%06d.png")],
        timeout,
    )
    if rc != 0:
        logger.warning("extract_frames failed rc=%s: %s", rc, _tail(err)[-200:])
        return []
    return sorted(str(p) for p in directory.glob("frame_*.png"))


def _raw_gray_frames(path: str | Path, fps: float) -> tuple[bytes, int, int]:
    """Decode a video to raw 8-bit gray frames on stdout.

    ``ffmpeg -i <path> -vf fps=<fps> -pix_fmt gray -f rawvideo -``

    Returns ``(data, width, height)`` -- ``(b"", 0, 0)`` on failure. Raw gray is
    deliberate: it is the only frame encoding the STDLIB can read back without
    an image library, which keeps Work 12 dependency-free.
    """
    target = str(path or "")
    stream = _video_stream(probe(target))
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return b"", 0, 0
    if not target or width <= 0 or height <= 0 or not ffmpeg_available():
        return b"", 0, 0
    rc, out, _ = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", target,
         "-vf", f"fps={float(fps):g}", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        FRAME_TIMEOUT,
    )
    if rc != 0:
        return b"", 0, 0
    return out, width, height


def frame_difference_energy(
    frames_or_path: str | Path | Sequence[str | Path],
    fps: float,
) -> list[float]:
    """Per-second-pair motion energy in ``[0.0, 1.0]`` -- real measurements.

    Two accepted inputs:

    * a MEDIA PATH: frames are decoded with
      ``-vf fps=<fps> -pix_fmt gray -f rawvideo -`` (see :func:`_raw_gray_frames`);
    * a list of ``.raw`` GRAY frame files of identical size (written by a lane's
      own extraction pass) -- read with plain ``open().read()``.

    For every consecutive frame pair the mean absolute luma difference divided
    by 255 is returned, so index ``i`` describes the interval between frame
    ``i`` and ``i+1``. Used as OPTIONAL active-speaker evidence
    (``method=motion_energy``, contracts §10) -- never as a speaker guess.

    Returns ``[]`` when the input is unusable (no video stream, unreadable
    files, mismatched frame sizes, ffmpeg missing). Stdlib only.
    """
    data = b""
    frame_bytes = 0
    if isinstance(frames_or_path, (str, Path)):
        data, width, height = _raw_gray_frames(frames_or_path, fps)
        frame_bytes = width * height
    else:
        chunks: list[bytes] = []
        for item in frames_or_path or []:
            candidate = Path(str(item))
            if candidate.suffix.lower() != ".raw":
                logger.debug("frame_difference_energy ignores non-raw frame %s", candidate.name)
                return []
            try:
                chunks.append(candidate.read_bytes())
            except OSError:
                return []
        if not chunks or not chunks[0]:
            return []
        frame_bytes = len(chunks[0])
        if any(len(c) != frame_bytes for c in chunks):
            return []
        data = b"".join(chunks)
    if not data or frame_bytes <= 0:
        return []
    count = len(data) // frame_bytes
    if count < 2:
        return []
    energy: list[float] = []
    previous = data[:frame_bytes]
    for i in range(1, count):
        current = data[i * frame_bytes:(i + 1) * frame_bytes]
        total = sum(abs(a - b) for a, b in zip(previous, current, strict=False))
        energy.append(total / (frame_bytes * 255.0))
        previous = current
    return energy


__all__ = [
    "FILTER_TIMEOUT",
    "FRAME_TIMEOUT",
    "MEASURE_TIMEOUT",
    "PROBE_TIMEOUT",
    "concat_audio",
    "detect_silence",
    "duration_seconds",
    "extract_frames",
    "ffmpeg_available",
    "ffprobe_available",
    "frame_difference_energy",
    "measure_loudness",
    "measure_peaks",
    "probe",
    "run_filter",
]
