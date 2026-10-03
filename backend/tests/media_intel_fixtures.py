"""Deterministic, real-media fixtures for the Work 12 media-intelligence lanes.

Every builder shells out to the real ``ffmpeg`` on PATH and writes a real media
file (PCM WAV / libx264 MP4). No hand-written WAV headers, no stubbed ffmpeg, no
mocked subprocess: sibling lanes can hand these files to real ffmpeg filters and
compare against measured ground truth.

Determinism
    * ``-fflags +bitexact -flags +bitexact`` drop the encoder/format tags and the
      MP4 ``creation_time`` box, so two builds of the same fixture are
      byte-identical for the same ffmpeg build.
    * every source is a fixed literal lavfi graph (``aevalsrc`` expressions,
      ``anoisesrc`` with a fixed ``seed``) - never wall-clock, never OS RNG.
    * frame counts come from the literal ``seconds`` x ``fps`` arguments, never
      from a probed container duration.

Measured facts (ffmpeg 8.1.1, 48 kHz mono pcm_s16le) - cite these, do not re-derive:
    * ``patterned_speechlike_wav()`` -> 8.000 s, ``silencedetect n=-50dB d=0.8``
      reports ``silence_start: 3`` / ``silence_end: 5.000021 |
      silence_duration: 2.000021``.
    * ``tone_wav(amplitude_db=-6)`` -> peak -6.02 dBFS, RMS -9.03 dBFS, 0 clipped.
    * ``clipped_tone_wav()`` -> peak 0.0 dBFS, ~37 % of samples at full scale.
    * ``noisy_tone_wav()`` -> tone -6 dBFS peak over -30 dBFS uniform noise
      (~25.7 dB tone-to-noise ratio by RMS); no claim of perceptual quality.
    * ``loud_ripple_wav()`` -> integrated -20.3 LUFS, LRA 6.8 LU, true peak
      -13.3 dBFS (measured with ``ebur128=peak=true``).
    * ``test_pattern_mp4()`` (moving box) -> recovered box is exactly 48x48 at
      y=66 with x within 1 px of ``(width - 48) * t / seconds``.
    * ``black_mp4()`` -> mean luma 0.0, and ``blackdetect=d=0.2:pix_th=0.05``
      reports a black range covering the whole clip.

Not a test module: every public callable is deliberately NOT named ``test_*`` so
pytest never collects this file. The self-test lives in
``test_media_intel_fixtures.py``.
"""

from __future__ import annotations

import array
import json
import math
import re
import shutil
import subprocess
import tempfile
import wave
from functools import lru_cache
from pathlib import Path

__all__ = [
    "black_mp4",
    "bright_box_per_frame",
    "clipped_tone_wav",
    "ffmpeg_filter_ok",
    "ffmpeg_filters",
    "ffmpeg_run",
    "fixture_paths_available",
    "frame_motion_energy",
    "loud_ripple_wav",
    "noisy_tone_wav",
    "patterned_speechlike_wav",
    "silent_wav",
    "test_pattern_mp4",
    "tone_wav",
    "video_frame_luma",
    "wav_clipped_samples",
    "wav_loudness",
    "wav_peak_dbfs",
    "wav_rms_dbfs",
    "wav_samples",
    "wav_silence_ranges",
]

# --- tunables (all literal, never wall-clock derived) -----------------------

_SAMPLE_RATE = 48_000
_TONE_AMP = 0.5  # -6.02 dBFS peak once quantised to pcm_s16le
_CLIP_AMP = 1.2  # ~37 % of samples land on full scale
_NOISE_SEED = 42  # fixed literal: same bytes on every build
_RIPPLE_SECTION_FLOOR = 3.0  # ebur128 LRA needs 3 s short-term windows
_RIPPLE_QUIET = 0.15
_RIPPLE_LOUD = 0.4
_BOX = 48  # drawn bright rectangle, square, centred vertically
_MAX_VIDEO_FRAMES = 256  # hard ceiling for the per-frame analysis helpers

_BITEXACT = ["-fflags", "+bitexact", "-flags", "+bitexact"]
_BASE = ["-y", "-hide_banner", "-loglevel", "error", *_BITEXACT]

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+|-inf|nan)")
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*(-?[\d.]+|-inf|nan)\s*\|\s*silence_duration:\s*(-?[\d.]+|-inf|nan)"
)
_LUF_RE = re.compile(r"I:\s*(-?[\d.]+|-inf|nan)\s*LUFS")
_LRA_RE = re.compile(r"LRA:\s*(-?[\d.]+|-inf|nan)\s*LU")
_TRUE_PEAK_RE = re.compile(r"Peak:\s*(-?[\d.]+|-inf|nan)\s*dBFS")
_FILTER_LINE_RE = re.compile(
    r"^\s*[A-Z.|]+\s+(\S+)\s+[A-Z|]+->[A-Z|V]+\s", re.MULTILINE
)
_FULL_SCALE = 32768.0


# --- process plumbing -------------------------------------------------------


def _require_ffmpeg() -> str:
    """Absolute ffmpeg path or a loud failure (never a silent empty result)."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise RuntimeError(
            "ffmpeg is required for the media-intelligence fixtures but was not "
            "found on PATH. Install ffmpeg (>=5.1 for -fps_mode) or skip the "
            "media tests; guard with fixture_paths_available()."
        )
    return exe


def ffmpeg_run(args, *, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run ffmpeg with a real argv list and return the CompletedProcess.

    Raises RuntimeError with the stderr tail on a non-zero exit, so a broken
    fixture never degrades into an empty file.
    """
    exe = _require_ffmpeg()
    cmd = [exe, *(str(a) for a in args)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {' '.join(cmd)}\n{tail}")
    return proc


def _encode(args, path) -> Path:
    """Run one bit-exact ffmpeg encode and assert the artefact is real."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_run([*_BASE, *args], timeout=300)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced no bytes for {out}")
    return out


def _num(value: float) -> str:
    """Fixed-precision literal for an ffmpeg filter argument."""
    return f"{value:.10g}"


def _tone_source(freq: float, amp: float, seconds: float, sample_rate: int) -> str:
    return f"aevalsrc={_num(amp)}*sin(2*PI*{_num(freq)}*t):s={sample_rate}:d={_num(seconds)}"


# --- audio fixtures ---------------------------------------------------------


def tone_wav(
    path,
    *,
    seconds: float = 1.0,
    freq: float = 440.0,
    sample_rate: int = _SAMPLE_RATE,
    amplitude_db: float = -6.0,
) -> Path:
    """Mono sine at a controlled peak (``amplitude_db`` is the true peak dBFS)."""
    amp = 10 ** (amplitude_db / 20.0)
    return _encode(
        [
            "-f", "lavfi", "-i", _tone_source(freq, amp, seconds, sample_rate),
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


def patterned_speechlike_wav(
    path,
    *,
    speech_s: tuple[float, ...] | list[float] = (3.0, 2.0, 3.0),
    gap_s: float = 2.0,
    freq: tuple[float, ...] | list[float] = (440.0, 880.0),
    sample_rate: int = _SAMPLE_RATE,
) -> Path:
    """Alternating speech-like tone / digital-silence timeline.

    ``speech_s`` is the timeline: even entries are speech-like tone segments
    (cycling through ``freq``), odd entries are SILENT gaps. ``gap_s`` is the
    authoritative duration of every gap, so the default ``(3.0, 2.0, 3.0)`` +
    ``gap_s=2.0`` reproduces the verified reference: 8.000 s with exactly one
    silence detected at 3.0 s for ~2.0 s
    (``silence_start: 3`` / ``silence_end: 5.000021``).
    """
    segments = [float(v) for v in speech_s]
    if len(segments) < 2:
        raise ValueError("speech_s needs at least one speech segment and one gap")
    freqs = [float(v) for v in freq] or [440.0]
    args: list[str] = []
    labels: list[str] = []
    speech_seen = 0
    for i, dur in enumerate(segments):
        if dur <= 0:
            raise ValueError(f"speech_s[{i}] must be > 0, got {dur}")
        if i % 2:
            src = f"anullsrc=r={sample_rate}:cl=mono:d={_num(gap_s)}"
        else:
            src = _tone_source(freqs[speech_seen % len(freqs)], _TONE_AMP, dur, sample_rate)
            speech_seen += 1
        args += ["-f", "lavfi", "-i", src]
        labels.append(f"[{i}:a]")
    graph = "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    return _encode(
        [
            *args,
            "-filter_complex", graph,
            "-map", "[out]",
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


def noisy_tone_wav(
    path,
    *,
    seconds: float = 3.0,
    freq: float = 440.0,
    noise_db: float = -30.0,
    sample_rate: int = _SAMPLE_RATE,
) -> Path:
    """Tone plus seeded uniform broadband noise (denoise before/after input)."""
    noise_amp = 10 ** (noise_db / 20.0)
    return _encode(
        [
            "-f", "lavfi", "-i", _tone_source(freq, _TONE_AMP, seconds, sample_rate),
            "-f", "lavfi", "-i",
            f"anoisesrc=color=white:sample_rate={sample_rate}"
            f":amplitude={_num(noise_amp)}:duration={_num(seconds)}:seed={_NOISE_SEED}",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[out]",
            "-map", "[out]",
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


def clipped_tone_wav(path, *, seconds: float = 2.0, freq: float = 440.0) -> Path:
    """Over-driven tone: true peak 0 dBFS and a large full-scale sample count."""
    return _encode(
        [
            "-f", "lavfi", "-i", _tone_source(freq, _CLIP_AMP, seconds, _SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


def silent_wav(path, *, seconds: float = 2.0) -> Path:
    """Digital silence at 48 kHz mono (the "missing audio" QC input)."""
    return _encode(
        [
            "-f", "lavfi", "-i", f"anullsrc=r={_SAMPLE_RATE}:cl=mono",
            "-t", _num(seconds),
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


def loud_ripple_wav(path, *, seconds: float = 6.0) -> Path:
    """Alternating quiet/loud sections so loudness normalisation has work.

    Sections are ``max(3.0, seconds / 4)`` long because ``ebur128`` derives LRA
    from 3 s short-term windows - a faster ripple measures as a flat LRA of 0.
    """
    section = max(_RIPPLE_SECTION_FLOOR, seconds / 4.0)
    expr = (
        f"if(lt(mod(t\\,{_num(section * 2)})\\,{_num(section)})"
        f"\\,{_num(_RIPPLE_QUIET)}\\,{_num(_RIPPLE_LOUD)})"
    )
    return _encode(
        [
            "-f", "lavfi", "-i", _tone_source(220.0, _TONE_AMP, seconds, _SAMPLE_RATE),
            "-af", f"volume={expr}:eval=frame",
            "-c:a", "pcm_s16le", "-f", "wav", str(path),
        ],
        path,
    )


# --- video fixtures ---------------------------------------------------------


def test_pattern_mp4(
    path,
    *,
    seconds: float = 2.0,
    fps: int = 10,
    width: int = 320,
    height: int = 180,
    moving_box: bool = True,
) -> Path:
    """Real libx264 MP4: a white 48x48 box on black.

    With ``moving_box=True`` the box x position is ``(width - 48) * t / seconds``,
    so every frame has non-zero motion energy and the per-frame box ground truth
    is computable; the encoder truncates it to whole pixels (measured error <= 1
    px against that formula).
    """
    frames = max(1, int(round(seconds * fps)))
    box_x = (
        f"(main_w-overlay_w)*t/{_num(seconds)}" if moving_box
        else "(main_w-overlay_w)/2"
    )
    graph = (
        f"[0:v][1:v]overlay=x={box_x}:y=(main_h-overlay_h)/2:"
        f"eval=frame:shortest=0[v]"
    )
    return _encode(
        [
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={_num(seconds)}",
            "-f", "lavfi", "-i",
            f"color=c=white:s={_BOX}x{_BOX}:r={fps}:d={_num(seconds)}",
            "-filter_complex", graph,
            "-map", "[v]",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "medium", "-crf", "12",
            "-pix_fmt", "yuv420p", "-an",
            "-f", "mp4", str(path),
        ],
        path,
    )


# The public builder name is fixed by the fixture contract, but a `test_` prefix
# would make pytest collect the BUILDER as a test wherever a sibling lane does
# `from tests.media_intel_fixtures import test_pattern_mp4`. `__test__ = False`
# is pytest's documented opt-out, so the name stays stable and collection stays
# clean. (This module is also never a test module: no other public callable is
# `test_`-prefixed.)
test_pattern_mp4.__test__ = False


def black_mp4(
    path, *, seconds: float = 1.0, fps: int = 10, width: int = 160, height: int = 90
) -> Path:
    """Near-black clip (mean luma 0.0) for black-frame QC."""
    return _encode(
        [
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={_num(seconds)}",
            "-frames:v", str(max(1, int(round(seconds * fps)))),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an",
            "-f", "mp4", str(path),
        ],
        path,
    )


# --- audio inspection -------------------------------------------------------


def wav_samples(path) -> tuple[list[float], int]:
    """Mono float samples in [-1, 1] plus the sample rate.

    Requires 16-bit PCM (the format every fixture here encodes); multi-channel
    input is downmixed by averaging. Raises ValueError on any other layout
    rather than returning quiet nonsense.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getcomptype() != "NONE":
            raise ValueError(f"{path}: not uncompressed PCM ({handle.getcomptype()})")
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    pcm = array.array("h")
    pcm.frombytes(raw)
    if channels > 1:
        usable = len(pcm) - (len(pcm) % channels)
        pcm = array.array(
            "h",
            (
                sum(pcm[i : i + channels]) // channels
                for i in range(0, usable, channels)
            ),
        )
    return [v / _FULL_SCALE for v in pcm], rate


def wav_rms_dbfs(path) -> float:
    """RMS level in dBFS (``-inf`` for digital silence)."""
    samples, _ = wav_samples(path)
    if not samples:
        return float("-inf")
    mean_square = sum(v * v for v in samples) / len(samples)
    if mean_square <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(math.sqrt(mean_square))


def wav_peak_dbfs(path) -> float:
    """Absolute peak in dBFS (``-inf`` for digital silence)."""
    samples, _ = wav_samples(path)
    peak = max((abs(v) for v in samples), default=0.0)
    return 20.0 * math.log10(peak) if peak > 0.0 else float("-inf")


def wav_clipped_samples(path) -> int:
    """Count samples at full scale (|s16| >= 32767), i.e. hard-clipped peaks."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
        raw = handle.readframes(handle.getnframes())
    pcm = array.array("h")
    pcm.frombytes(raw)
    return sum(1 for v in pcm if v >= 32767 or v <= -32768)


def wav_silence_ranges(
    path, *, noise_db: float = -50.0, min_duration: float = 0.8
) -> list[dict]:
    """Parse real ``silencedetect`` output into ``{start_s, end_s, duration_s}``.

    Seconds are rounded to 4 decimals so results compare byte-for-byte across
    runs. A silence that runs to end-of-file without a reported end is closed at
    the real file duration (ffmpeg omits ``silence_end`` there).
    """
    proc = ffmpeg_run(
        ["-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=n={noise_db}dB:d={min_duration}", "-f", "null", "-"],
        timeout=300,
    )
    err = proc.stderr.decode("utf-8", "replace")
    starts = [float(m) for m in _SILENCE_START_RE.findall(err)]
    ends = [(float(a), float(b)) for a, b in _SILENCE_END_RE.findall(err)]
    total = _wav_duration(path)

    ranges: list[dict] = []
    for i, start in enumerate(starts):
        if i < len(ends):
            end, duration = ends[i]
        elif total is not None:
            end, duration = total, total - start
        else:
            continue
        ranges.append(
            {
                "start_s": round(start, 4),
                "end_s": round(end, 4),
                "duration_s": round(duration, 4),
            }
        )
    return ranges


def _wav_duration(path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (wave.Error, OSError, ZeroDivisionError):
        return None


def wav_loudness(path) -> dict:
    """EBU R128 loudness from real ``ebur128=peak=true`` measurement.

    ``peak_dbfs`` is the exact sample peak (stdlib reader), ``true_peak_dbfs``,
    ``integrated_lufs`` and ``lra`` come from ffmpeg. Any field ffmpeg cannot
    report (typically because the clip is shorter than the 3 s window) is None,
    never a fabricated number.
    """
    out = {
        "integrated_lufs": None,
        "lra": None,
        "peak_dbfs": None,
        "true_peak_dbfs": None,
    }
    try:
        peak = wav_peak_dbfs(path)
    except (wave.Error, OSError, ValueError):
        peak = None
    if peak is not None and math.isfinite(peak):
        out["peak_dbfs"] = round(peak, 3)

    proc = ffmpeg_run(
        ["-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        timeout=300,
    )
    err = proc.stderr.decode("utf-8", "replace")
    cut = err.rfind("Summary:")
    summary = err[cut:] if cut >= 0 else err
    for key, pattern in (
        ("integrated_lufs", _LUF_RE),
        ("lra", _LRA_RE),
        ("true_peak_dbfs", _TRUE_PEAK_RE),
    ):
        found = pattern.search(summary)
        if not found:
            continue
        value = float(found.group(1))
        if math.isfinite(value):
            out[key] = round(value, 3)
    return out


# --- video inspection -------------------------------------------------------


def _video_probe(path) -> dict:
    exe = shutil.which("ffprobe")
    if exe is None:
        raise RuntimeError("ffprobe is required to inspect video fixtures")
    proc = subprocess.run(  # noqa: S603
        [exe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-500:]}")
    streams = (json.loads(proc.stdout or "{}") or {}).get("streams") or []
    if not streams:
        raise ValueError(f"{path}: no video stream to inspect")
    stream = streams[0]
    num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den or 1) if float(den or 0) else 0.0
    frames = stream.get("nb_frames")
    count = int(frames) if frames not in (None, "N/A") else 0
    if count <= 0:
        duration = float(stream.get("duration") or 0.0)
        count = int(duration * fps)
    if fps <= 0 or count <= 0:
        raise ValueError(f"{path}: undeterminable frame rate / frame count")
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps,
        "frames": count,
    }


def _sample_plan(fps: float, out_fps: float, frames: int, max_frames: int) -> list[int]:
    """Source frame indices nearest to ``k / out_fps`` (half-up rounding).

    Selecting explicit source frames (instead of the ``fps`` filter) keeps
    ``t_s`` honest: the reported timestamp is the true timestamp of the pixels,
    so ground truth is exact rather than up to half a source frame off.
    """
    if out_fps <= 0 or max_frames <= 0:
        raise ValueError("out fps and max_frames must be > 0")
    count = min(max_frames, max(1, math.ceil(frames * out_fps / fps)))
    plan: list[int] = []
    for k in range(count):
        idx = min(frames - 1, int(math.floor(k * fps / out_fps + 0.5)))
        if not plan or idx != plan[-1]:
            plan.append(idx)
    return plan


def _select_filter(plan: list[int]) -> str:
    return "select='" + "+".join(f"eq(n\\,{i})" for i in plan) + "'"


def _raw_gray_frames(path, plan: list[int], width: int, height: int) -> list[bytes]:
    proc = ffmpeg_run(
        ["-v", "error", "-i", str(path), "-vf", _select_filter(plan),
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        timeout=300,
    )
    size = width * height
    data = proc.stdout
    return [data[i * size : (i + 1) * size] for i in range(len(data) // size)]


@lru_cache(maxsize=32)
def _threshold_lut(threshold: int) -> bytes:
    return bytes(255 if value > threshold else 0 for value in range(256))


def video_frame_luma(
    path, *, fps: float = 4, max_frames: int = 64, out_dir=None
) -> list[dict]:
    """Real decoded frames as PNGs plus their measured luma statistics.

    Each entry is ``{index, src_frame, t_s, mean_luma, max_luma, min_luma,
    path}`` where ``path`` is the extracted PNG, so a detector double can be
    handed the exact same pixels the statistics were computed from. When
    ``out_dir`` is None a fresh temp dir is created and owned by the caller.
    """
    info = _video_probe(path)
    plan = _sample_plan(info["fps"], fps, info["frames"], max_frames)
    raw = _raw_gray_frames(path, plan, info["width"], info["height"])
    if not raw:
        raise RuntimeError(f"{path}: ffmpeg returned no decodable frames")

    directory = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="ymoney-frames-"))
    directory.mkdir(parents=True, exist_ok=True)
    pattern = directory / "frame_%04d.png"
    ffmpeg_run(
        ["-y", "-v", "error", "-i", str(path), "-vf", _select_filter(plan),
         "-fps_mode", "passthrough", "-start_number", "0", "-f", "image2", str(pattern)],
        timeout=300,
    )
    written = sorted(directory.glob("frame_*.png"))
    if len(written) < len(plan):
        raise RuntimeError(f"{path}: expected {len(plan)} PNG frames, got {len(written)}")

    out = []
    for i, frame in enumerate(raw):
        out.append(
            {
                "index": i,
                "src_frame": plan[i],
                "t_s": round(plan[i] / info["fps"], 4),
                "mean_luma": round(sum(frame) / len(frame), 3),
                "max_luma": max(frame),
                "min_luma": min(frame),
                "path": str(written[i]),
            }
        )
    return out


def bright_box_per_frame(path, *, fps: float = 4, threshold: int = 200) -> list[dict]:
    """GROUND TRUTH for the drawn bright rectangle: one box per sampled frame.

    Raw ``gray`` frames are thresholded and reduced with row/column projections
    (stdlib ``array``/``bytes`` only, no numpy). Entries are
    ``{index, src_frame, t_s, x, y, w, h, mean_luma}``; the box fields are None
    when a frame has no pixel above ``threshold`` - never a guessed box.
    """
    info = _video_probe(path)
    plan = _sample_plan(info["fps"], fps, info["frames"], _MAX_VIDEO_FRAMES)
    width, height = info["width"], info["height"]
    lut = _threshold_lut(threshold)
    out = []
    for i, frame in enumerate(_raw_gray_frames(path, plan, width, height)):
        mask = frame.translate(lut)
        entry = {
            "index": i,
            "src_frame": plan[i],
            "t_s": round(plan[i] / info["fps"], 4),
            "x": None,
            "y": None,
            "w": None,
            "h": None,
            "mean_luma": None,
        }
        if mask.count(255):
            rows = [y for y in range(height) if mask[y * width : (y + 1) * width].count(255)]
            cols = [x for x in range(width) if mask[x::width].count(255)]
            y0, y1, x0, x1 = rows[0], rows[-1], cols[0], cols[-1]
            total = 0
            for row in range(y0, y1 + 1):
                total += sum(frame[row * width + x0 : row * width + x1 + 1])
            entry.update(
                {
                    "x": x0,
                    "y": y0,
                    "w": x1 - x0 + 1,
                    "h": y1 - y0 + 1,
                    "mean_luma": round(total / ((y1 - y0 + 1) * (x1 - x0 + 1)), 3),
                }
            )
        out.append(entry)
    return out


def frame_motion_energy(path, *, fps: float = 4) -> list[float]:
    """Mean absolute luma difference against the previous sampled frame.

    One value per sampled frame; the first frame is 0.0 (no predecessor). Real
    motion signal for active-speaker / reframe evidence, ~0.0 for static content.
    """
    info = _video_probe(path)
    plan = _sample_plan(info["fps"], fps, info["frames"], _MAX_VIDEO_FRAMES)
    frames = _raw_gray_frames(path, plan, info["width"], info["height"])
    energy = []
    previous = None
    for frame in frames:
        if previous is None:
            energy.append(0.0)
        else:
            diff = sum(abs(a - b) for a, b in zip(frame, previous, strict=True))
            energy.append(round(diff / len(frame), 4))
        previous = frame
    return energy


# --- capability probes ------------------------------------------------------


@lru_cache(maxsize=1)
def ffmpeg_filters() -> frozenset[str]:
    """Filter names this ffmpeg build actually has (parsed from ``-filters``).

    Query at runtime: the available set changes per build and per environment.
    """
    exe = _require_ffmpeg()
    proc = subprocess.run(  # noqa: S603
        [exe, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg -filters failed: {proc.stderr[-500:]}")
    return frozenset(_FILTER_LINE_RE.findall(proc.stdout))


def ffmpeg_filter_ok(filter_name: str) -> bool:
    """True when ffmpeg exposes ``filter_name`` (honest capability probe)."""
    return filter_name in ffmpeg_filters()


def fixture_paths_available() -> bool:
    """Cheap non-raising probe for the self-test skip path."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
