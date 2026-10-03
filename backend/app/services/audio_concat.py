"""Sample-accurate audio assembly for the narration lane.

Joining encoded audio segments with a stream copy (``-c:a copy`` /
``-c copy``) is NOT concatenation: every MP3 carries its own encoder delay and
padding frame, and a demuxer that splices the bitstreams splices those
priming samples too. Measured on three 1.000 s LAME parts, a concat demuxer
join yields **3.0898 s** and makes ffmpeg complain that the muxer was handed
non-monotonic DTS. The error is cumulative: N parts drift N times, and the
subtitles (which are placed by wall-clock) slide further out of sync with
every additional segment.

The fix is to stop treating "the file" as the unit. Every input is DECODED to
one common PCM format (24 kHz, 16-bit signed, mono), the raw samples are
concatenated, and the result is ENCODED EXACTLY ONCE. There is exactly one
encoder delay in the output, and the true length is known from the sample
count rather than from a container header.

Design rules that make this safe to call from a render pipeline:

* **Loud failure.** A missing, empty, or undecodable part raises
  :class:`AudioConcatError` naming the offending index. It is never skipped.
  A silently shortened narration track is indistinguishable from a bug, and
  the caller would ship a video whose audio stops mid-sentence.
* **Exact accounting.** :func:`concat_audio` returns the duration derived
  from the decoded sample count, so a caller placing captions derives offsets
  from samples too (see :func:`part_offsets`).
* **No new dependencies.** stdlib ``wave``/``subprocess`` plus ffmpeg.

The decode/encode strategy is the behaviour of the MoneyPrinterTurbo 1.3.7
narration service; the failure semantics, the exact-length guarantee and the
API shape are YMONEY's.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CHANNELS",
    "AudioConcatError",
    "DecodedAudio",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "concat_audio",
    "decode_part",
    "decode_parts",
    "ffmpeg_binary",
    "ffmpeg_present",
    "part_offsets",
    "probe_duration_seconds",
    "silence_pcm",
    "write_silence",
]

#: The single PCM format every part is decoded to before splicing. 24 kHz / mono
#: / s16le is a superset of what speech synthesizers emit and matches the
#: donor's narration service, so nothing is up-sampled.
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes, pcm_s16le

#: One MP3 frame at :data:`SAMPLE_RATE`. Used as the tolerance when comparing a
#: decoded duration against an expected one.
FRAME_SECONDS = 1152 / SAMPLE_RATE

_ENCODE_TIMEOUT = 180
_DECODE_TIMEOUT = 120


class AudioConcatError(RuntimeError):
    """A part could not be decoded, or the joined track could not be written.

    Raised instead of returning a short track. The narration lane is the only
    thing that knows how many segments were supposed to be spoken, so a
    failure to prove that all of them landed must abort the assembly.
    """


def ffmpeg_present() -> bool:
    """True when an ffmpeg binary is on PATH."""
    return bool(shutil.which("ffmpeg"))


def ffmpeg_binary() -> str:
    """Absolute path to ffmpeg, or :class:`AudioConcatError` when absent."""
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioConcatError("ffmpeg not found — cannot assemble audio")
    return found


@dataclass(frozen=True)
class DecodedAudio:
    """One part after decoding, with its true length in samples."""

    index: int
    path: Path
    n_samples: int

    @property
    def seconds(self) -> float:
        """Duration derived from the decoded sample count (never a header)."""
        return self.n_samples / SAMPLE_RATE


def _read_pcm_wav(path: Path, index: int) -> bytes | None:
    """Raw PCM frames when ``path`` is already in the target format.

    Returns ``None`` for any other format (including a WAV that merely looks
    right but cannot be opened) so the caller falls back to ffmpeg.
    """
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            if (wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != CHANNELS
                    or wf.getsampwidth() != SAMPLE_WIDTH):
                return None
            frames = wf.readframes(wf.getnframes())
    except (OSError, wave.Error, EOFError):
        return None
    if not frames:
        raise AudioConcatError(f"audio part {index} ({path.name}) decoded to zero samples")
    return frames


def _decode_with_ffmpeg(path: Path, index: int, workdir: Path) -> bytes:
    """Decode one part to 24 kHz / mono / s16le PCM and return its frames."""
    target = workdir / f"pcm_{index:04d}.wav"
    proc = subprocess.run(
        [
            ffmpeg_binary(), "-y", "-v", "error", "-i", str(path),
            "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-codec:a", "pcm_s16le", str(target),
        ],
        capture_output=True, timeout=_DECODE_TIMEOUT,
    )
    if proc.returncode != 0 or not target.exists() or target.stat().st_size <= 44:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise AudioConcatError(
            f"audio part {index} ({path.name}) could not be decoded: {detail or 'ffmpeg failed'}"
        )
    with wave.open(str(target), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    if not frames:
        raise AudioConcatError(f"audio part {index} ({path.name}) decoded to zero samples")
    return frames


def decode_part(path: Path | str, index: int, workdir: Path) -> DecodedAudio:
    """Decode ONE part to the common PCM format and report its sample count.

    Raises :class:`AudioConcatError` when the file is missing, empty, or
    carries no decodable audio.
    """
    src = Path(path)
    if not src.is_file():
        raise AudioConcatError(f"audio part {index} is missing: {src}")
    if src.stat().st_size == 0:
        raise AudioConcatError(f"audio part {index} is empty: {src.name}")
    frames = _read_pcm_wav(src, index)
    if frames is None:
        frames = _decode_with_ffmpeg(src, index, workdir)
    return DecodedAudio(index=index, path=src, n_samples=len(frames) // SAMPLE_WIDTH)


def decode_parts(parts: Sequence[Path | str], workdir: Path) -> list[DecodedAudio]:
    """Decode every part in order. Any bad part aborts the whole batch."""
    if not parts:
        raise AudioConcatError("no audio parts to join")
    return [decode_part(p, i, workdir) for i, p in enumerate(parts)]


def part_offsets(decoded: Sequence[DecodedAudio]) -> list[float]:
    """Start time of each part, from decoded sample counts.

    This is the offset a caption or subtitle must use. Reading it off the
    encoded container instead would reintroduce exactly the drift this module
    exists to remove.
    """
    out: list[float] = []
    cursor = 0
    for item in decoded:
        out.append(cursor / SAMPLE_RATE)
        cursor += item.n_samples
    return out


def _write_pcm_wav(pcm: bytes, dest: Path) -> None:
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


def concat_audio(parts: Sequence[Path | str], out_path: Path | str) -> float:
    """Join audio parts with one decode pass and one encode pass.

    ``parts`` are decoded to 24 kHz / 16-bit / mono PCM, spliced sample-wise,
    and encoded exactly once to ``out_path`` (a ``.wav`` output is written
    straight from the spliced PCM; anything else is encoded with libmp3lame).
    Returns the total duration in seconds, measured from the decoded sample
    count.

    Raises :class:`AudioConcatError` if any part is missing, empty or
    undecodable, or if the output could not be written. It never returns a
    track shorter than its parts.
    """
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ymoney-concat-") as tmp:
        workdir = Path(tmp)
        decoded = decode_parts(parts, workdir)

        pcm_parts: list[bytes] = []
        for item in decoded:
            frames = _read_pcm_wav(item.path, item.index)
            if frames is None:
                frames = _decode_with_ffmpeg(item.path, item.index, workdir)
            pcm_parts.append(frames)

        total_pcm = b"".join(pcm_parts)
        master = workdir / "master.wav"
        _write_pcm_wav(total_pcm, master)

        if dest.suffix.lower() == ".wav":
            shutil.copyfile(master, dest)
        else:
            proc = subprocess.run(
                [ffmpeg_binary(), "-y", "-v", "error", "-i", str(master),
                 "-codec:a", "libmp3lame", "-q:a", "4", str(dest)],
                capture_output=True, timeout=_ENCODE_TIMEOUT,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
                raise AudioConcatError(f"joined audio could not be encoded: {detail or 'ffmpeg failed'}")

    if not dest.is_file() or dest.stat().st_size <= 0:
        raise AudioConcatError(f"joined audio was not written: {dest}")
    return len(total_pcm) // SAMPLE_WIDTH / SAMPLE_RATE


def silence_pcm(duration_seconds: float) -> bytes:
    """Raw 24 kHz mono s16le silence of exactly ``duration_seconds``.

    Sample-exact by construction, so a pause inserted this way cannot drift.
    """
    seconds = max(float(duration_seconds or 0.0), 0.0)
    return b"\x00\x00" * int(round(seconds * SAMPLE_RATE))


def write_silence(out_path: Path | str, duration_seconds: float) -> float:
    """Write a real silent track of the requested duration. Returns its length.

    Used by the no-voice mode: a silent placeholder keeps every downstream
    stage (clip trimming, caption timeline, final mux) working unchanged
    instead of special-casing "there was no narration".
    """
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pcm = silence_pcm(duration_seconds)
    if not pcm:
        raise AudioConcatError(
            f"silent track would be zero-length ({duration_seconds!r}s) — "
            "refusing to write an empty placeholder"
        )
    if dest.suffix.lower() == ".wav":
        _write_pcm_wav(pcm, dest)
    else:
        proc = subprocess.run(
            [ffmpeg_binary(), "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono",
             "-t", f"{len(pcm) // SAMPLE_WIDTH / SAMPLE_RATE:.3f}",
             "-codec:a", "libmp3lame", "-q:a", "4", str(dest)],
            capture_output=True, timeout=_ENCODE_TIMEOUT,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise AudioConcatError(f"silent track could not be encoded: {detail or 'ffmpeg failed'}")
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise AudioConcatError(f"silent track was not written: {dest}")
    return len(pcm) // SAMPLE_WIDTH / SAMPLE_RATE


def probe_duration_seconds(path: Path | str) -> float | None:
    """Container duration via ffprobe, or ``None`` when unavailable.

    Advisory only — used for verification. Nothing in the assembly path
    depends on a container-reported duration.
    """
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return float((out.stdout or b"").decode("utf-8", "replace").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
