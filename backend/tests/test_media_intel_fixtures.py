"""Self-test for `tests/media_intel_fixtures.py`.

Proves the shared Work 12 media fixtures are what the sibling lanes assume:

* deterministic - two builds of the same fixture are byte-identical;
* real - built by an actual ffmpeg encode and measured after an actual decode,
  never hand-written bytes and never a mocked subprocess;
* parseable - every inspection helper returns the documented shape and recovers
  the drawn ground truth from the decoded pixels.

Audio-only tests are fast and unmarked. Everything that encodes or decodes video
is `@pytest.mark.slow` (the repo's default addopts deselect `slow`).
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from tests import media_intel_fixtures as fx

pytestmark = pytest.mark.skipif(
    not fx.fixture_paths_available(), reason="ffmpeg/ffprobe not installed"
)

BOX = 48
VID_W, VID_H, VID_SECONDS, VID_FPS = 320, 180, 2.0, 10


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _raw_digest(path: Path) -> str:
    """Digest of the fully decoded raw frames (container-independent)."""
    proc = fx.ffmpeg_run(
        ["-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    )
    return hashlib.md5(proc.stdout).hexdigest()


# --- collection hygiene -----------------------------------------------------


def test_module_is_not_collected_as_tests():
    """Only the fixed-name video builder carries a test_ prefix, and it opts out."""
    exported = [name for name in fx.__all__ if name.startswith("test_")]
    assert exported == ["test_pattern_mp4"]
    assert fx.test_pattern_mp4.__test__ is False
    for name in fx.__all__:
        assert callable(getattr(fx, name)), name


# --- capability probes ------------------------------------------------------


def test_filter_probe_reflects_the_real_build():
    """Filters are queried at runtime - rnnoise really is absent on this host."""
    filters = fx.ffmpeg_filters()
    assert len(filters) > 50
    # used by this module itself, so it must exist wherever the fixtures run
    for name in ("silencedetect", "ebur128", "aevalsrc", "anoisesrc", "anullsrc", "overlay"):
        assert fx.ffmpeg_filter_ok(name), name
    assert not fx.ffmpeg_filter_ok("definitely_not_a_real_filter_xyz")
    # Do NOT assert rnnoise: it is absent in this environment and present in
    # some builds, which is exactly why callers must probe at runtime.
    assert "rnnoise" not in filters or fx.ffmpeg_filter_ok("rnnoise")


# --- audio fixtures ---------------------------------------------------------


def test_patterned_speechlike_has_exactly_one_two_second_gap(tmp_path):
    """The 3-2-3 construction: 8 s file, one silence at 3.0 s for ~2.0 s."""
    wav = fx.patterned_speechlike_wav(tmp_path / "pattern.wav")
    ranges = fx.wav_silence_ranges(wav)

    assert len(ranges) == 1, ranges
    (only,) = ranges
    assert abs(only["start_s"] - 3.0) <= 0.05, only
    assert abs(only["duration_s"] - 2.0) <= 0.05, only
    assert abs(only["end_s"] - 5.0) <= 0.05, only
    assert abs(only["end_s"] - (only["start_s"] + only["duration_s"])) <= 1e-4

    samples, rate = fx.wav_samples(wav)
    assert rate == 48_000
    assert abs(len(samples) / rate - 8.0) <= 1 / rate


def test_patterned_speechlike_gap_is_true_digital_silence(tmp_path):
    """The gap is exactly zero samples, not merely quiet - real media, real silence."""
    wav = fx.patterned_speechlike_wav(tmp_path / "pattern.wav")
    samples, rate = fx.wav_samples(wav)

    gap = samples[3 * rate : 5 * rate]
    assert gap and set(gap) == {0.0}
    assert max(abs(v) for v in samples[: 3 * rate]) > 0.4
    assert max(abs(v) for v in samples[5 * rate :]) > 0.4


def test_tone_wav_hits_the_requested_peak_and_never_clips(tmp_path):
    wav = fx.tone_wav(tmp_path / "tone.wav", amplitude_db=-6.0)
    peak = fx.wav_peak_dbfs(wav)
    rms = fx.wav_rms_dbfs(wav)

    assert abs(peak - (-6.0)) <= 0.05, peak
    assert abs(rms - (peak - 3.01)) <= 0.1, (rms, peak)  # full-scale sine
    assert fx.wav_clipped_samples(wav) == 0

    quiet = fx.tone_wav(tmp_path / "quiet.wav", amplitude_db=-20.0)
    assert abs(fx.wav_peak_dbfs(quiet) - (-20.0)) <= 0.05


def test_clipped_tone_reports_full_scale_samples(tmp_path):
    """QC clipping input: true peak 0 dBFS and a real clipped-sample count."""
    clipped = fx.clipped_tone_wav(tmp_path / "clip.wav")
    count = fx.wav_clipped_samples(clipped)

    assert count > 0, count
    assert abs(fx.wav_peak_dbfs(clipped)) <= 0.01
    samples, rate = fx.wav_samples(clipped)
    assert 0.05 < count / len(samples) < 0.95  # heavily clipped, not a digital square
    assert abs(len(samples) / rate - 2.0) <= 1 / rate


def test_noisy_tone_has_a_measurable_noise_floor(tmp_path):
    """Tone-to-noise ratio is assertable (a signal-level statement only)."""
    clean = fx.tone_wav(tmp_path / "clean.wav", seconds=3.0)
    noisy = fx.noisy_tone_wav(tmp_path / "noisy.wav", seconds=3.0)

    a, rate = fx.wav_samples(clean)
    b, _ = fx.wav_samples(noisy)
    assert len(a) == len(b)
    assert _md5(clean) != _md5(noisy)

    signal_rms = math.sqrt(sum(v * v for v in a) / len(a))
    diff_rms = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) / len(a))
    snr_db = 20 * math.log10(signal_rms / diff_rms)
    assert 15.0 < snr_db < 40.0, snr_db


def test_silent_wav_is_digital_silence_and_detected(tmp_path):
    wav = fx.silent_wav(tmp_path / "silent.wav", seconds=2.0)
    samples, rate = fx.wav_samples(wav)

    assert set(samples) == {0.0}
    assert fx.wav_rms_dbfs(wav) == float("-inf")
    assert fx.wav_peak_dbfs(wav) == float("-inf")
    ranges = fx.wav_silence_ranges(wav)
    assert len(ranges) == 1
    assert abs(ranges[0]["start_s"]) <= 0.01
    assert abs(ranges[0]["duration_s"] - 2.0) <= 0.05


def test_loud_ripple_reports_a_real_loudness_spread(tmp_path):
    """Loudness normalisation has something to measure: LRA well above 1 LU."""
    wav = fx.loud_ripple_wav(tmp_path / "ripple.wav")
    loudness = fx.wav_loudness(wav)

    assert set(loudness) == {"integrated_lufs", "lra", "peak_dbfs", "true_peak_dbfs"}
    assert loudness["lra"] is not None and loudness["lra"] > 1.0, loudness
    assert loudness["integrated_lufs"] is not None, loudness
    assert loudness["true_peak_dbfs"] is not None, loudness
    # The ripple steps the gain abruptly, which rings the oversampled true-peak
    # filter a little above the sample peak (measured +0.68 dB on this build).
    assert loudness["true_peak_dbfs"] <= loudness["peak_dbfs"] + 1.5, loudness


def test_loudness_never_invents_numbers_for_unreadable_input(tmp_path):
    """Unreadable input fails loudly instead of returning fabricated loudness."""
    junk = tmp_path / "not-media.wav"
    junk.write_bytes(b"this is not a wav file")
    with pytest.raises(RuntimeError, match="ffmpeg exited"):
        fx.wav_loudness(junk)


def test_audio_fixtures_are_byte_identical_on_rebuild(tmp_path):
    """Determinism: the same call twice produces the same bytes and duration."""
    builders = [
        ("tone", fx.tone_wav, {}),
        ("pattern", fx.patterned_speechlike_wav, {}),
        ("noisy", fx.noisy_tone_wav, {}),
        ("clip", fx.clipped_tone_wav, {}),
        ("silent", fx.silent_wav, {}),
        ("ripple", fx.loud_ripple_wav, {}),
    ]
    for name, builder, kwargs in builders:
        first = builder(tmp_path / f"{name}-1.wav", **kwargs)
        second = builder(tmp_path / f"{name}-2.wav", **kwargs)
        assert _md5(first) == _md5(second), f"{name} is not byte-deterministic"
        a, _ = fx.wav_samples(first)
        b, _ = fx.wav_samples(second)
        assert len(a) == len(b), f"{name} duration drifted"


def test_wav_samples_rejects_non_16_bit_pcm(tmp_path):
    """Documented constraint: 16-bit PCM only, loud failure otherwise."""
    raw = tmp_path / "u8.wav"
    fx.ffmpeg_run(
        ["-y", "-v", "error", "-f", "lavfi",
         "-i", "aevalsrc=0.5*sin(2*PI*440*t):s=48000:d=0.5",
         "-c:a", "pcm_u8", "-f", "wav", str(raw)]
    )
    with pytest.raises(ValueError, match="16-bit"):
        fx.wav_samples(raw)


# --- video fixtures (slow: real encode + decode) ---------------------------


@pytest.mark.slow
def test_moving_box_ground_truth_is_recovered_from_real_pixels(tmp_path):
    """Detector doubles can be validated: box position/size come from the frames."""
    mp4 = fx.test_pattern_mp4(tmp_path / "box.mp4")
    boxes = fx.bright_box_per_frame(mp4)

    assert len(boxes) >= 4
    previous_x = -1
    for entry in boxes:
        assert entry["w"] is not None, entry
        assert abs(entry["w"] - BOX) <= 2, entry
        assert abs(entry["h"] - BOX) <= 2, entry
        assert abs(entry["y"] - (VID_H - BOX) // 2) <= 2, entry
        assert entry["mean_luma"] > 200, entry  # the box really is the bright part
        expected_x = (VID_W - BOX) * entry["t_s"] / VID_SECONDS
        assert abs(entry["x"] - expected_x) <= 2, (entry, expected_x)
        assert entry["x"] > previous_x, entry
        previous_x = entry["x"]


@pytest.mark.slow
def test_static_box_has_motion_only_from_the_moving_case(tmp_path):
    moving = fx.test_pattern_mp4(tmp_path / "moving.mp4")
    static = fx.test_pattern_mp4(tmp_path / "static.mp4", moving_box=False)

    moving_energy = fx.frame_motion_energy(moving)
    static_energy = fx.frame_motion_energy(static)

    assert moving_energy[0] == 0.0  # no predecessor for the first frame
    assert all(value > 0.0 for value in moving_energy[1:]), moving_energy
    assert all(value == 0.0 for value in static_energy), static_energy
    assert len({b["x"] for b in fx.bright_box_per_frame(static)}) == 1


@pytest.mark.slow
def test_video_frame_luma_extracts_real_png_frames(tmp_path):
    """Sibling lanes can hand these exact PNGs to a detector double."""
    mp4 = fx.test_pattern_mp4(tmp_path / "box.mp4")
    frames = fx.video_frame_luma(mp4)

    assert len(frames) == len(fx.bright_box_per_frame(mp4))
    for i, frame in enumerate(frames):
        assert frame["index"] == i
        assert frame["t_s"] == round(frame["src_frame"] / VID_FPS, 4)
        image = Path(frame["path"])
        assert image.exists() and image.stat().st_size > 100
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert 0 <= frame["min_luma"] <= frame["max_luma"] == 255
        # 48x48 white box on a 320x180 black frame == ~6.4 % coverage
        assert 5.0 < frame["mean_luma"] < 20.0, frame


@pytest.mark.slow
def test_black_mp4_is_dark(tmp_path):
    """Black-frame QC input: near-black decoded frames, not just a black container."""
    mp4 = fx.black_mp4(tmp_path / "black.mp4")
    frames = fx.video_frame_luma(mp4)

    assert frames
    for frame in frames:
        assert frame["mean_luma"] < 10, frame
        assert frame["max_luma"] <= 2, frame


@pytest.mark.slow
def test_mp4_rebuild_is_byte_identical_and_decodes_identically(tmp_path):
    """Bit-exact containers AND identical decoded frames (documented assertion)."""
    first = fx.test_pattern_mp4(tmp_path / "box-1.mp4")
    second = fx.test_pattern_mp4(tmp_path / "box-2.mp4")

    assert _md5(first) == _md5(second)
    assert _raw_digest(first) == _raw_digest(second)
    assert first.stat().st_size > 0
