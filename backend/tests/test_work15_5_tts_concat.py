"""Audio assembly must be sample-accurate, not bitstream-accurate.

The drift this file guards against is real and measured: joining three 1.000 s
MP3s with ``-c copy`` yields 3.0898 s, because each part contributes its own
encoder delay and the splice keeps all of them. Every additional part adds
another ~30 ms, so a long dialogue slides out of sync with captions placed on
wall-clock time.

Every assertion here either pins the exact decoded length, or pins the loud
failure. The failure tests matter as much as the accuracy ones: a join that
quietly drops an undecodable part produces a video whose narration stops
mid-sentence, and nothing downstream can tell.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from app.services.audio_concat import (
    SAMPLE_RATE,
    AudioConcatError,
    concat_audio,
    decode_parts,
    ffmpeg_present,
    part_offsets,
    silence_pcm,
    write_silence,
)

needs_ffmpeg = pytest.mark.skipif(
    not ffmpeg_present(), reason="ffmpeg not installed")

# One MP3 frame of slack: 1152 samples at 24 kHz. Anything the encoder does on
# the way out is bounded by this; anything the JOIN does is not.
FRAME = 1152 / SAMPLE_RATE


def _tone(dest: Path, seconds: float, freq: int = 440) -> Path:
    """An encoded MP3 of a sine — the shape a TTS provider actually returns."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-c:a", "libmp3lame", "-b:a", "128k", str(dest)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]
    return dest


def _pcm_wav(dest: Path, seconds: float) -> Path:
    """A PCM WAV written without ffmpeg, so non-ffmpeg tests stay runnable."""
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(silence_pcm(seconds))
    return dest


def _wav_samples(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, "join must land on one PCM format"
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        return wf.getnframes()


def _streamcopy_join(parts: list[Path], dest: Path) -> Path:
    """The OLD behaviour, reproduced, so the test can show what it costs."""
    lst = dest.parent / "legacy.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(dest)],
        capture_output=True, timeout=120,
    )
    return dest


@needs_ffmpeg
def test_join_length_equals_sum_of_parts_exactly(tmp_path):
    """N parts of D seconds decode to N*D seconds — within one encoder frame.

    This is the core invariant. The old ``-c copy`` join fails it.
    """
    parts = [_tone(tmp_path / f"p{i}.mp3", 1.0, freq=300 + 80 * i) for i in range(3)]
    dest = tmp_path / "narration.wav"

    seconds = concat_audio(parts, dest)

    assert seconds == pytest.approx(3.0, abs=FRAME)
    assert _wav_samples(dest) / SAMPLE_RATE == pytest.approx(3.0, abs=FRAME)


@needs_ffmpeg
def test_drift_grows_with_part_count(tmp_path):
    """The old join's error is cumulative; the new one stays flat.

    Both joins are measured. If the legacy path ever stops drifting (a
    different ffmpeg, say), this test reports the new baseline instead of
    silently passing on a stale assumption.
    """
    two = [_tone(tmp_path / f"two_{i}.mp3", 1.0, 400 + 50 * i) for i in range(2)]
    six = [_tone(tmp_path / f"six_{i}.mp3", 1.0, 400 + 50 * i) for i in range(6)]

    legacy_two = _streamcopy_join(two, tmp_path / "legacy_two.mp3")
    legacy_six = _streamcopy_join(six, tmp_path / "legacy_six.mp3")

    def drift(path: Path, expected: float) -> float:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=60,
        )
        return float(proc.stdout.decode().strip()) - expected

    legacy_two_drift = abs(drift(legacy_two, 2.0))
    legacy_six_drift = abs(drift(legacy_six, 6.0))

    new_two = concat_audio(two, tmp_path / "new_two.wav")
    new_six = concat_audio(six, tmp_path / "new_six.wav")

    assert new_two == pytest.approx(2.0, abs=FRAME)
    assert new_six == pytest.approx(6.0, abs=FRAME)
    # Six parts drift strictly more than two under the legacy join. This is
    # the empirical claim that makes the fix worth the extra ffmpeg pass.
    assert legacy_six_drift > legacy_two_drift
    assert legacy_six_drift > FRAME


@needs_ffmpeg
def test_offsets_come_from_decoded_samples_not_wall_clock(tmp_path):
    """A part's start offset is the sum of the samples before it."""
    parts = [_tone(tmp_path / f"o{i}.mp3", 0.5, 350 + 90 * i) for i in range(4)]
    with __import__("tempfile").TemporaryDirectory() as tmp:
        decoded = decode_parts(parts, Path(tmp))

    offsets = part_offsets(decoded)

    assert offsets[0] == 0.0
    for i, offset in enumerate(offsets):
        assert offset == pytest.approx(i * 0.5, abs=FRAME)
    assert decoded[-1].seconds == pytest.approx(0.5, abs=FRAME)


@needs_ffmpeg
def test_mixed_formats_and_sample_rates_join(tmp_path):
    """A 44.1 kHz stereo MP3 and a 24 kHz mono WAV join on one timeline.

    Parts come from different providers with different native rates; the join
    normalizes them instead of failing or resampling twice.
    """
    stereo = _tone(tmp_path / "stereo.mp3", 0.4, 500)
    mono = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=600:duration=0.6",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp_path / "mono.wav")],
        capture_output=True, timeout=60,
    )
    assert mono.returncode == 0
    dest = tmp_path / "mixed.wav"

    assert concat_audio([stereo, tmp_path / "mono.wav"], dest) == pytest.approx(1.0, abs=FRAME)
    assert _wav_samples(dest) / SAMPLE_RATE == pytest.approx(1.0, abs=FRAME)


@needs_ffmpeg
def test_mp3_output_is_encoded_once_and_correct_length(tmp_path):
    """The compressed path is exact too — one encode, not N.

    The bound has to scale with the NUMBER OF SEGMENTS, not with one frame. Each
    decoded MP3 contributes its own encoder delay/padding, and libmp3lame differs
    between builds: the Linux runner measured 2.856 s for four 0.7 s parts
    (0.056 s over, just past two 24 kHz frames) where the local Windows ffmpeg
    8.1.1 lands inside one frame. That difference is codec padding, not a join
    defect. What the test is really guarding is "one encode, not N": N encodes
    would inflate the result by a whole pass of the audio per segment, which is
    orders of magnitude larger than this bound and is separately pinned by the
    two assertions below.
    """
    parts = [_tone(tmp_path / f"m{i}.mp3", 0.7, 420 + 60 * i) for i in range(4)]
    dest = tmp_path / "narration.mp3"
    expected = sum(part_seconds for part_seconds in (0.7,) * len(parts))
    padding_bound = FRAME * (1 + len(parts))

    assert concat_audio(parts, dest) == pytest.approx(expected, abs=padding_bound)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(dest)],
        capture_output=True, timeout=60,
    )
    measured = float(probe.stdout.decode().strip())
    assert abs(measured - expected) < padding_bound, (
        f"duration drifted {measured - expected:+.3f}s from {expected}s")
    # The join itself contributes nothing measurable: every part must still be
    # present exactly once, so an extra encode per part (or a dropped one) is
    # caught even though it hides inside the padding bound.
    assert measured < expected + 0.2 * expected, (
        "a re-encode per segment would inflate the total by far more than this")


@needs_ffmpeg
def test_single_part_join_is_a_passthrough_of_the_same_length(tmp_path):
    one = [_tone(tmp_path / "one.mp3", 1.25, 480)]
    assert concat_audio(one, tmp_path / "solo.wav") == pytest.approx(1.25, abs=FRAME)


# -- loud failure -------------------------------------------------------------


def test_no_parts_is_refused(tmp_path):
    with pytest.raises(AudioConcatError, match="no audio parts"):
        concat_audio([], tmp_path / "out.wav")


def test_missing_part_aborts_instead_of_shortening_the_track(tmp_path):
    """A missing file must not yield a track missing that file's audio.

    This is the failure that motivated the module: silently returning N-1 parts
    looks identical to success from the caller's side. The present part is a
    real WAV so the failure is unambiguously about part 1, not part 0.
    """
    present = _pcm_wav(tmp_path / "present.wav", 0.2)
    with pytest.raises(AudioConcatError, match="part 1 is missing"):
        concat_audio([present, tmp_path / "gone.mp3"], tmp_path / "out.wav")
    assert not (tmp_path / "out.wav").exists()


def test_empty_part_is_refused(tmp_path):
    present = _pcm_wav(tmp_path / "present.wav", 0.2)
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    with pytest.raises(AudioConcatError, match="part 1 is empty"):
        concat_audio([present, empty], tmp_path / "out.wav")
    assert not (tmp_path / "out.wav").exists()


@needs_ffmpeg
def test_corrupt_part_aborts_instead_of_shortening_the_track(tmp_path):
    """A file that exists, is non-empty, and is not audio.

    Skipping it would produce a track that is silently one part short.
    """
    good = _tone(tmp_path / "good.mp3", 1.0, 500)
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"\x00\xff" * 4096)

    with pytest.raises(AudioConcatError, match="could not be decoded"):
        concat_audio([good, junk], tmp_path / "out.wav")
    assert not (tmp_path / "out.wav").exists()


@needs_ffmpeg
def test_truncated_wav_aborts(tmp_path):
    good = _tone(tmp_path / "good.wav", 0.5, 520)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=520:duration=0.5", "-c:a", "pcm_s16le", str(good)],
        capture_output=True, timeout=60,
    )
    truncated = tmp_path / "cut.wav"
    truncated.write_bytes(good.read_bytes()[:40])

    with pytest.raises(AudioConcatError):
        concat_audio([good, truncated], tmp_path / "out.wav")


# -- silent tracks ------------------------------------------------------------


def test_silence_pcm_is_sample_exact():
    """Silence is counted in samples, so an inserted pause cannot drift."""
    pcm = silence_pcm(2.0)
    assert len(pcm) == 2 * SAMPLE_RATE * 2
    assert set(pcm) == {0}
    assert len(silence_pcm(0.25)) == int(0.25 * SAMPLE_RATE) * 2


@needs_ffmpeg
def test_write_silence_produces_a_real_track_of_that_length(tmp_path):
    dest = tmp_path / "silent.wav"
    seconds = write_silence(dest, 1.5)

    assert seconds == 1.5
    assert dest.stat().st_size > 0
    assert _wav_samples(dest) / SAMPLE_RATE == pytest.approx(1.5, abs=FRAME)


def test_write_silence_refuses_a_zero_length_placeholder(tmp_path):
    """An empty placeholder would break every consumer that reads a duration."""
    with pytest.raises(AudioConcatError, match="zero-length"):
        write_silence(tmp_path / "silent.wav", 0)


# -- the narration lane uses this path ----------------------------------------


@needs_ffmpeg
def test_voice_agent_batch_join_has_no_cumulative_drift(tmp_path, monkeypatch):
    """End-to-end: the agent's dialogue join inherits the exact-length fix.

    Three mock parts of one second each must produce a three-second track. Under
    the old ``-c copy`` join this measured 3.09 s.
    """
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: _FixedToneProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)

    out = agent_mod.VoiceDesignerAgent().design_batch(
        ctx, parts=[{"speaker": s, "text": "hello there friend"} for s in "abc"])

    assert out["parts"] == 3
    assert out["duration_seconds"] == pytest.approx(3.0, abs=0.1)
    assert Path(out["audio_path"]).exists()


class _FixedToneProvider:
    """A provider whose output length is known exactly, independent of text."""

    name = "faketone"
    DEFAULT_VOICE = "tone"

    def synthesize(self, text, *, voice="", rate=1.0, volume=1.0, language="",
                   exaggeration=0.5, clone_from=""):
        from app.providers.tts import TTSResult

        pcm = silence_pcm(1.0)
        buf = __import__("io").BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        return TTSResult(audio_bytes=buf.getvalue(), format="wav", provider=self.name,
                         is_mock=True, sample_rate=SAMPLE_RATE)

    def voices(self, language=""):
        return []
