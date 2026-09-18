"""Phase A capability tests: TTS providers, HN trend source, clip repurposing."""


import pytest

from app.providers.trends import TrendSourceError, create_source
from app.providers.tts import (
    EdgeTTSProvider,
    MockTTSProvider,
    TTSError,
    TTSResult,
    get_tts_provider,
    tts_provider_status,
    wav_duration_seconds,
)

# ---------------------------------------------------------------------------
# TTS provider layer
# ---------------------------------------------------------------------------


def test_mock_tts_produces_labeled_silence():
    provider = MockTTSProvider()
    result = provider.synthesize("Nobody told you this about AI agents. " * 6)
    assert isinstance(result, TTSResult)
    assert result.is_mock is True
    assert result.provider == "mock"
    assert result.format == "wav"
    assert result.audio_bytes[:4] == b"RIFF"
    duration = wav_duration_seconds(result.audio_bytes, sample_rate=result.sample_rate or 16000)
    assert 3.0 <= duration <= 30.0


def test_mock_tts_rate_affects_duration():
    provider = MockTTSProvider()
    normal = provider.synthesize("word " * 52, rate=1.0)
    fast = provider.synthesize("word " * 52, rate=2.0)
    d_normal = wav_duration_seconds(normal.audio_bytes, normal.sample_rate)
    d_fast = wav_duration_seconds(fast.audio_bytes, fast.sample_rate)
    assert d_fast < d_normal


def test_edge_provider_rate_formatting():
    assert EdgeTTSProvider._rate_str(1.0) == "+0%"
    assert EdgeTTSProvider._rate_str(1.2) == "+20%"
    assert EdgeTTSProvider._rate_str(0.8) == "-20%"
    assert EdgeTTSProvider._rate_str(0) == "+0%"  # clamped
    assert EdgeTTSProvider._volume_str(None) == "+0%"


def test_tts_factory_default_is_edge():
    from app.core.config import settings

    original = settings.tts_provider
    try:
        settings.tts_provider = ""
        provider = get_tts_provider()
        assert provider.name == "edge"
    finally:
        settings.tts_provider = original


def test_tts_factory_mock():
    provider = get_tts_provider("mock")
    assert provider.name == "mock"


def test_tts_factory_kokoro_requires_base_url(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "tts_provider", "kokoro")
    monkeypatch.setattr(settings, "kokoro_base_url", "")
    with pytest.raises(TTSError):
        get_tts_provider("kokoro")


def test_tts_factory_kokoro_with_url(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "kokoro_base_url", "http://127.0.0.1:9990/v1")
    provider = get_tts_provider("kokoro")
    assert provider.name == "kokoro"
    assert provider.base_url == "http://127.0.0.1:9990/v1"


def test_tts_status_reports_provider(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.tts_provider", "mock")
    status = tts_provider_status()
    assert status["provider"] == "mock"
    assert status["healthy"] is True
    assert status["is_mock"] is True


def test_tts_rejects_empty_text():
    with pytest.raises(TTSError):
        MockTTSProvider().synthesize("   ")


# ---------------------------------------------------------------------------
# Hacker News trend source (keyless social listening)
# ---------------------------------------------------------------------------


def test_hn_source_registered():
    src = create_source("hacker_news", {"min_points": 5, "days": 30})
    assert src.kind == "hacker_news"
    assert src.min_points == 5


def test_hn_source_unknown_kind_raises():
    with pytest.raises(TrendSourceError):
        create_source("does_not_exist", {})


def test_hn_source_fetches_real_stories():
    """Live smoke test against the public Algolia API (keyless)."""
    src = create_source("hacker_news", {"min_points": 50, "days": 30})
    candidates = src.fetch("AI", 5)
    assert 1 <= len(candidates) <= 5
    top = candidates[0]
    assert top.topic
    assert top.source == "hacker_news"
    assert top.external_ref  # URL provenance
    assert top.velocity_hint is not None
    assert top.raw["points"] >= 1


# ---------------------------------------------------------------------------
# Clip repurposing
# ---------------------------------------------------------------------------


def test_clip_status_reports_capabilities():
    from app.providers.clips import get_repurposer

    status = get_repurposer().status()
    assert {"yt_dlp", "ffmpeg", "download_supported", "cut_supported",
            "whisper", "scene_detect", "face_track", "caption_presets"} <= set(status)
    assert isinstance(status["ffmpeg"], bool)


def test_even_segments_deterministic():
    from app.providers.clips import ClipRepurposer

    segs = ClipRepurposer._even_segments(180.0, 45.0, 4)
    assert len(segs) == 4
    assert segs[0][0] == 0.0
    for (s1, e1), (s2, e2) in zip(segs, segs[1:]):
        assert e1 - s1 <= 45.0 + 0.01
        assert s2 >= e1  # non-overlapping


def test_clip_acquire_rejects_bad_input():
    from app.providers.clips import ClipError, get_repurposer

    repurposer = get_repurposer()
    with pytest.raises(ClipError):
        repurposer.acquire("", "ws-test")
    with pytest.raises(ClipError):
        repurposer.acquire("/nonexistent/path/video.mp4", "ws-test")


def test_clip_cut_requires_ffmpeg(tmp_path, monkeypatch):
    from app.providers import clips as clips_mod

    monkeypatch.setattr(clips_mod, "ffmpeg_available", lambda: False)
    source = clips_mod.SourceInfo(
        title="t", duration=60.0, width=1920, height=1080,
        local_path=tmp_path / "src.mp4",
    )
    repurposer = clips_mod.ClipRepurposer(work_root=tmp_path / "work")
    with pytest.raises(clips_mod.ClipError):
        repurposer.cut_segments(source, "ws-test")


def test_clip_cut_produces_real_segments(tmp_path):
    """End-to-end with real ffmpeg: synthetic source -> vertical clips."""
    import subprocess as sp

    from app.providers import clips as clips_mod

    if not clips_mod.ffmpeg_available():
        pytest.skip("ffmpeg not installed")

    src = tmp_path / "src.mp4"
    # 30s 320x240 test pattern with silence
    sp.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=30:size=320x240:rate=15",
         "-f", "lavfi", "-i", "anullsrc=duration=30:sample_rate=44100",
         "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", str(src)],
        check=True, timeout=120,
    )
    source = clips_mod.SourceInfo(
        title="synthetic", duration=30.0, width=320, height=240, local_path=src,
    )
    monkey_free_root = tmp_path / "storage"
    monkey_free_root.mkdir(exist_ok=True)
    repurposer = clips_mod.ClipRepurposer(work_root=tmp_path / "work")
    # patch STORAGE_ROOT used for outputs
    original_root = clips_mod.STORAGE_ROOT
    clips_mod.STORAGE_ROOT = monkey_free_root
    try:
        results = repurposer.cut_segments(
            source, "ws-clip-test", clip_seconds=10.0, max_clips=2, vertical=True
        )
    finally:
        clips_mod.STORAGE_ROOT = original_root
    assert len(results) == 2
    for r in results:
        assert r.duration == pytest.approx(10.0, abs=1.5)
        assert r.height == 1920 and r.width == 1080  # vertical conversion
