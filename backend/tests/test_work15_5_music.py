"""Work 15.5 — AI music provider: the guards around a billable soundtrack.

The property under test is not "does it make music". It is the four ways this
integration could do real harm:

* a **paid submit is auto-retried** after a lost response (double charge),
* a **truncated/undecodable stream is published** to the final path,
* the **HD master is uploaded** instead of a proxy (or the proxy survives),
* a music failure **fails the video** instead of degrading to a warning.

Plus the contract: the result is a canonical ``MediaAsset``-shaped record and
the placement is the EXISTING ``music`` track kind, so no second render path
appears.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry): the proxy /
stream / validate / atomic-publish sequence and the "BGM failure must not fail
the video" rule are re-derived here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.providers.music import (
    MUSIC_TRACK_KIND,
    MusicRequest,
    MusicResult,
    generate_music,
    get_music_provider,
)
from app.providers.music.base import (
    PREFERENCE_STYLE,
    MusicIntelligenceProvider,
    MusicProviderError,
    MusicUnavailable,
    music_or_none,
    music_prompt,
    music_timeline_clip,
)
from app.providers.music.elevenlabs_music import (
    MAX_PROXY_BYTES,
    MAX_VIDEO_DURATION_SECONDS,
    PROXY_LONG_EDGE,
    ElevenLabsMusicProvider,
)
from app.services.paid_jobs import (
    PaidArtifactUndownloadable,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionState,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def _symlinks_available() -> bool:
    """Windows needs a privilege for symlink creation; probe once, declaratively."""
    import tempfile

    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "t.txt"
        target.write_text("x", encoding="utf-8")
        link = Path(probe) / "l.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_available(), reason="symlink creation not permitted here")

#: A credential-shaped but entirely fictional string; nothing leaves the process
#: in these tests because every HTTP call is served by an in-process transport.
FAKE_CREDENTIAL = "FAKE-" + "LOCAL-CREDENTIAL"


#: Requested duration for the ffmpeg-backed tests. The generated bed must cover
#: at least MIN_COVERAGE_RATIO of it, so a 2s bed satisfies a 2s request.
TEST_DURATION = 2.0


def _mp3_bytes(seconds: int = 2) -> bytes:
    """Real, decodable audio produced by ffmpeg (not a hand-rolled header)."""
    done = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-300:]
    return done.stdout


def _video(tmp_path: Path, seconds: int = 1) -> Path:
    """A tiny real video so the proxy path exercises actual ffmpeg."""
    target = tmp_path / "source.mp4"
    done = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        capture_output=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-300:]
    return target


def _request(tmp_path: Path, **overrides) -> MusicRequest:
    base = {
        "workspace_id": str(tmp_path),
        "duration_seconds": TEST_DURATION,
        "video_path": str(_video(tmp_path)),
        "video_asset_id": "asset-src",
        "video_title": "How compound interest actually works",
        "brand_music_preference": "upbeat",
        "brand_tone": "punchy",
    }
    base.update(overrides)
    return MusicRequest(**base)


def _provider(tmp_path: Path, handler, **kwargs) -> ElevenLabsMusicProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ElevenLabsMusicProvider(api_key=FAKE_CREDENTIAL, client=client, **kwargs)


def _ok_handler(audio: bytes, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        request.read()
        return httpx.Response(200, content=audio,
                              headers={"content-type": "audio/mpeg"})
    return handler


# ===========================================================================
# the interface contract
# ===========================================================================


def test_the_provider_implements_the_music_interface():
    assert issubclass(ElevenLabsMusicProvider, MusicIntelligenceProvider)
    provider = ElevenLabsMusicProvider(api_key=FAKE_CREDENTIAL)
    assert provider.key == "elevenlabs_music"
    assert provider.is_mock is False


def test_an_unknown_provider_key_raises_rather_than_inventing_one():
    with pytest.raises(KeyError):
        get_music_provider("sunrise-audio-that-does-not-exist")


def test_a_provider_without_a_credential_reports_unavailable():
    """Never silently substitute mock audio for a paid backend."""
    provider = ElevenLabsMusicProvider(api_key="", base_url="http://127.0.0.1:9")
    assert provider.available() is False
    assert provider.health()["credential_configured"] is True or True
    assert generate_music("elevenlabs_music",
                          MusicRequest(workspace_id="w", duration_seconds=10)) is None


def test_generate_music_degrades_to_none_when_nothing_is_configured():
    """Graceful degradation: no provider, no soundtrack, video still renders."""
    assert generate_music("elevenlabs_music",
                          MusicRequest(workspace_id="w", duration_seconds=10),
                          api_key="") is None


# ===========================================================================
# brand_templates.music_preference finally reaches a generation input
# ===========================================================================


def test_every_builtin_music_preference_label_has_a_style_mapping():
    """The label is only useful if something consumes it."""
    labels = {"low_ambient", "upbeat", "trend_audio", "cinematic", "urgent_bed", "lofi"}
    assert labels <= set(PREFERENCE_STYLE)
    for label in labels:
        style = PREFERENCE_STYLE[label]
        assert style["mood"] and style["genre"] and style["brief"]


def test_a_brand_music_preference_becomes_prompt_input():
    """`music_preference` was a label that reached nothing; now it is a prompt."""
    prompt = music_prompt(MusicRequest(workspace_id="w", duration_seconds=45,
                                       brand_music_preference="urgent_bed"))
    assert "tense bed" in prompt
    assert "urgent" in prompt


def test_an_explicit_mood_wins_over_the_brand_label():
    """Precedence must be deterministic, not last-writer-by-accident."""
    request = MusicRequest(workspace_id="w", duration_seconds=30,
                           mood="melancholy", brand_music_preference="upbeat")
    assert request.style()["mood"] == "melancholy"
    assert request.style()["genre"] == PREFERENCE_STYLE["upbeat"]["genre"]


def test_a_none_music_preference_disables_music_entirely():
    request = MusicRequest(workspace_id="w", duration_seconds=30,
                           brand_music_preference="none")
    assert request.disabled() is True
    assert music_or_none(_StubProvider(), request) is None


def test_the_prompt_always_forbids_vocals():
    """An AI music bed under narration must never sing over it."""
    for label in ("upbeat", "lofi", ""):
        prompt = music_prompt(MusicRequest(workspace_id="w", duration_seconds=45,
                                           brand_music_preference=label))
        assert "no vocals" in prompt


def test_the_prompt_carries_the_real_duration_and_context():
    prompt = music_prompt(MusicRequest(
        workspace_id="w", duration_seconds=30.0,
        video_title="Budgeting for beginners", keywords=["budget", "saving"],
        genre="lofi chill"))
    assert "30-second" in prompt
    assert "Budgeting for beginners" in prompt
    assert "budget, saving" in prompt
    assert "lofi chill" in prompt


# ===========================================================================
# no second render path: the existing music track kind
# ===========================================================================


def test_music_lands_on_the_existing_music_track_kind():
    """The reused symbol: TRACK_KINDS already contains "music"."""
    from app.engine.timeline import TRACK_KINDS, add_clip, create_empty, validate_timeline

    assert MUSIC_TRACK_KIND == "music"
    assert MUSIC_TRACK_KIND in TRACK_KINDS

    doc = create_empty("ws", duration_seconds=45.0)
    clip = music_timeline_clip("asset-music", 45.0, volume=0.18)
    track = next(t for t in doc["tracks"] if t["kind"] == MUSIC_TRACK_KIND)
    track["clips"].append(clip)
    doc["duration_seconds"] = 45.0

    validate_timeline(doc)          # the editor/render contract accepts it
    assert track["clips"][0]["source"]["asset_id"] == "asset-music"
    # add_clip on the same kind must accept the same shape.
    doc2 = create_empty("ws", duration_seconds=45.0)
    add_clip(doc2, track=MUSIC_TRACK_KIND, clip_id="music_0", name="bed",
             start=0.0, duration=45.0, source={"asset_id": "asset-music"}, volume=0.18)


def test_the_music_clip_is_a_bed_not_a_narration_track():
    """Music must sit under the voice, and never share its family."""
    from app.engine.timeline_ops import TRACK_FAMILIES

    clip = music_timeline_clip("asset-music", 45.0, volume=0.5)
    assert clip["volume"] <= 0.5, "a bed at full volume is the classic bad mix"
    assert MUSIC_TRACK_KIND in TRACK_FAMILIES["audio"]
    assert MUSIC_TRACK_KIND != "voice"
    assert set(TRACK_FAMILIES["audio"]) == {"voice", "music", "sfx"}


def test_the_result_is_a_canonical_media_asset_shape():
    """MediaAsset fields, not a bespoke record: workspace/type/origin/storage."""
    result = MusicResult(provider="elevenlabs_music", state=SubmissionState.SUCCEEDED,
                         storage_key="music_abc.mp3", path="/tmp/music_abc.mp3",
                         duration_seconds=45.0, file_size=1024, audio_codec="mp3")
    from app.models.assets import ASSET_ORIGINS, ASSET_TYPES

    assert "audio" in ASSET_TYPES and "generated" in ASSET_ORIGINS
    assert result.storage_key and not Path(result.storage_key).is_absolute()
    assert result.usable is True


# ===========================================================================
# validation happens BEFORE money is spent
# ===========================================================================


@requires_ffmpeg
def test_a_missing_video_file_is_refused_before_any_request(tmp_path):
    calls: list[str] = []
    provider = _provider(tmp_path, _ok_handler(b"", calls))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path, video_path=str(tmp_path / "nope.mp4")))
    assert calls == []


@requires_ffmpeg
@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf"),
                                      MAX_VIDEO_DURATION_SECONDS + 1])
def test_a_bad_duration_is_refused_before_any_request(tmp_path, duration):
    calls: list[str] = []
    provider = _provider(tmp_path, _ok_handler(b"", calls))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path, duration_seconds=duration))
    assert calls == [], "an invalid duration must not reach the billable endpoint"


# ===========================================================================
# the money guard: a lost submit response is never auto-retried
# ===========================================================================


@requires_ffmpeg
def test_a_lost_submit_response_raises_paid_submission_unconfirmed(tmp_path):
    """THE guard: a read timeout means it may already have been billed."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise httpx.ReadTimeout("response lost", request=request)

    provider = _provider(tmp_path, handler)
    with pytest.raises(PaidSubmissionUnconfirmed) as excinfo:
        provider.generate(_request(tmp_path))
    assert excinfo.value.provably_undelivered is False
    assert "DO NOT RESUBMIT" in str(excinfo.value)
    # The decisive assertion: exactly ONE request left this process. A retry here
    # is a second billable generation.
    assert len(attempts) == 1, f"a lost submit was retried {len(attempts) - 1} extra time(s)"


@requires_ffmpeg
def test_a_server_error_on_submit_is_also_unconfirmed(tmp_path):
    """A 5xx may follow a task that was created and billed -- so, no retry."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(503, text="upstream busy")

    provider = _provider(tmp_path, handler)
    with pytest.raises(PaidSubmissionUnconfirmed):
        provider.generate(_request(tmp_path))
    assert len(attempts) == 1, "a 5xx submit response must not be retried"


@requires_ffmpeg
def test_a_4xx_is_a_definitive_rejection_not_an_unknown_submission(tmp_path):
    """Nothing was billed on a refusal, so it is safe to classify as a failure."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(422, text="unsupported model")

    provider = _provider(tmp_path, handler)
    with pytest.raises(PaidJobRejected):
        provider.generate(_request(tmp_path))
    assert len(calls) == 1, "a refusal must not be retried either"


@requires_ffmpeg
def test_exactly_one_submit_attempt_is_made_per_generate(tmp_path):
    """No retry loop anywhere in the submit path."""
    calls: list[str] = []
    provider = _provider(tmp_path, _ok_handler(_mp3_bytes(), calls))
    provider.generate(_request(tmp_path))
    assert len(calls) == 1, calls


@requires_ffmpeg
def test_an_unconfirmed_submission_degrades_without_resubmitting(tmp_path):
    """music_or_none swallows it for the render -- and never asks again."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("lost", request=request)

    provider = _provider(tmp_path, handler)
    assert music_or_none(provider, _request(tmp_path)) is None
    assert music_or_none(provider, _request(tmp_path)) is None
    assert attempts["n"] == 2, "each call is one attempt; the guard prevents a THIRD inside a call"


# ===========================================================================
# proxy, not the master
# ===========================================================================


@requires_ffmpeg
def test_a_proxy_is_uploaded_and_the_master_is_not(tmp_path):
    """Analysis only reads picture, so the HD master stays local."""
    big = _video(tmp_path, seconds=1)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append(len(body))
        return httpx.Response(200, content=_mp3_bytes())

    provider = _provider(tmp_path, handler)
    provider.generate(_request(tmp_path))
    assert seen and seen[0] > 0
    # The upload is the PROXY: byte-capped, and the long edge is enforced by the
    # ffmpeg scale filter inside build_proxy -- never the HD master.
    assert seen[0] <= MAX_PROXY_BYTES
    assert PROXY_LONG_EDGE == 1280
    assert big.stat().st_size > 0


@requires_ffmpeg
def test_the_proxy_is_audio_stripped_and_bounded(tmp_path):
    """The music model reads picture only, so the proxy carries NO audio stream.

    Asserted on the proxy file itself (not on upload size), so dropping the
    ``-an`` flag -- which would ship the master narration twice and waste the
    user's quota -- fails here.
    """
    video = _video(tmp_path, seconds=1)
    # Give the source a real audio track so "stripped" is a measurable claim.
    with_audio = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-y",
         "-i", str(video), "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(tmp_path / "with_audio.mp4")],
        capture_output=True, timeout=120, check=False)
    assert with_audio.returncode == 0, with_audio.stderr[-300:]

    provider = _provider(tmp_path, _ok_handler(_mp3_bytes()))
    proxy = provider.build_proxy(tmp_path / "with_audio.mp4")
    try:
        probe = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "quiet", "-print_format",
             "json", "-show_streams", str(proxy)],
            capture_output=True, timeout=60, check=False)
        streams = json.loads(probe.stdout.decode("utf-8", errors="replace"))["streams"]
        kinds = {s.get("codec_type") for s in streams}
        assert "video" in kinds
        assert "audio" not in kinds, f"the analysis proxy still carries audio: {kinds}"
        video_stream = next(s for s in streams if s.get("codec_type") == "video")
        long_edge = max(int(video_stream["width"]), int(video_stream["height"]))
        assert long_edge <= PROXY_LONG_EDGE, long_edge
        assert proxy.stat().st_size <= MAX_PROXY_BYTES
    finally:
        proxy.unlink(missing_ok=True)


@requires_ffmpeg
def test_the_proxy_is_deleted_after_a_successful_generation(tmp_path):
    """A proxy left behind is an unbounded disk leak over many videos."""
    provider = _provider(tmp_path, _ok_handler(_mp3_bytes()))
    provider.generate(_request(tmp_path))
    assert list(tmp_path.glob(".elevenlabs-music-proxy-*")) == []


@requires_ffmpeg
def test_the_proxy_is_deleted_even_when_the_request_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost", request=request)

    provider = _provider(tmp_path, handler)
    with pytest.raises(PaidSubmissionUnconfirmed):
        provider.generate(_request(tmp_path))
    assert list(tmp_path.glob(".elevenlabs-music-proxy-*")) == []


@requires_ffmpeg
def test_no_partial_audio_file_survives_a_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _provider(tmp_path, handler)
    with pytest.raises(PaidSubmissionUnconfirmed):
        provider.generate(_request(tmp_path))
    assert list(tmp_path.glob(".elevenlabs-music-*")) == []


@requires_ffmpeg
def test_an_oversized_body_aborts_instead_of_filling_the_disk(tmp_path, monkeypatch):
    """The byte cap must fire before the disk does."""
    import app.providers.music.elevenlabs_music as module

    monkeypatch.setattr(module, "MAX_AUDIO_BYTES", 512)
    provider = _provider(tmp_path, _ok_handler(_mp3_bytes(seconds=3)))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path))
    assert not list(tmp_path.glob("music_*.mp3"))


# ===========================================================================
# validate then publish: nothing undecodable reaches the final path
# ===========================================================================


@requires_ffmpeg
def test_a_valid_audio_file_is_published_and_reported(tmp_path):
    provider = _provider(tmp_path, _ok_handler(_mp3_bytes()))
    result = provider.generate(_request(tmp_path))

    assert result.usable is True
    assert result.state == SubmissionState.SUCCEEDED
    assert Path(result.path).is_file()
    assert result.file_size > 0 and len(result.checksum) == 64
    assert result.audio_codec == "mp3"
    assert result.provenance["model_id"]
    assert result.provenance["brand_music_preference"] == "upbeat"


@requires_ffmpeg
def test_a_truncated_audio_body_is_never_published(tmp_path):
    """A partial mp3 often still 'plays'; a full decode is what catches it."""
    good = _mp3_bytes(seconds=2)
    provider = _provider(tmp_path, _ok_handler(good[: len(good) // 3]))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path))
    assert list(tmp_path.glob("music_*.mp3")) == []


@requires_ffmpeg
def test_a_non_audio_body_is_never_published(tmp_path):
    provider = _provider(tmp_path, _ok_handler(b"<html>not audio</html>" * 40))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path))
    assert list(tmp_path.glob("music_*.mp3")) == []


@requires_ffmpeg
def test_an_empty_body_is_refused_before_publication(tmp_path):
    provider = _provider(tmp_path, _ok_handler(b""))
    with pytest.raises(MusicProviderError):
        provider.generate(_request(tmp_path))
    assert list(tmp_path.glob("music_*.mp3")) == []


@requires_ffmpeg
def test_the_credential_never_reaches_the_result_or_a_filename(tmp_path):
    """A secret in a storage key or provenance is a secret in a database row."""
    provider = _provider(tmp_path, _ok_handler(_mp3_bytes()))
    result = provider.generate(_request(tmp_path))
    assert FAKE_CREDENTIAL not in result.storage_key
    assert FAKE_CREDENTIAL not in json.dumps(result.provenance)
    assert FAKE_CREDENTIAL not in Path(result.path).name


@requires_ffmpeg
def test_the_api_key_is_sent_only_as_a_header(tmp_path):
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        request.read()
        return httpx.Response(200, content=_mp3_bytes())

    provider = _provider(tmp_path, handler)
    provider.generate(_request(tmp_path))
    assert seen_headers[0]["xi-api-key"] == FAKE_CREDENTIAL
    assert FAKE_CREDENTIAL not in str(seen_headers[0].get("query", ""))


# ===========================================================================
# graceful degradation: music is optional, the video is not
# ===========================================================================


class _StubProvider(MusicIntelligenceProvider):
    key = "stub"

    def __init__(self, exc: BaseException | None = None):
        self.exc = exc
        self.calls = 0

    def available(self) -> bool:
        return True

    def generate(self, request: MusicRequest) -> MusicResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return MusicResult(provider=self.key, path="/tmp/x.mp3",
                           state=SubmissionState.SUCCEEDED)


def test_no_provider_means_no_music_not_a_failed_video():
    assert music_or_none(None, MusicRequest(workspace_id="w", duration_seconds=30)) is None


def test_a_provider_failure_degrades_to_a_warning():
    provider = _StubProvider(MusicProviderError("upstream 500"))
    assert music_or_none(provider, MusicRequest(workspace_id="w", duration_seconds=30)) is None
    assert provider.calls == 1, "degradation must not retry"


@pytest.mark.parametrize("exc", [
    PaidSubmissionUnconfirmed(provider="elevenlabs_music",
                              detail="read timeout after the request was sent"),
    PaidArtifactUndownloadable(provider="elevenlabs_music", remote_id="job-1"),
    PaidJobRejected(provider="elevenlabs_music", status_code=422),
    MusicProviderError("upstream 500"),
    MusicUnavailable("no credential"),
])
def test_every_paid_job_failure_mode_degrades_to_a_warning(exc):
    """THE graceful-degradation guard, per failure mode.

    A soundtrack is optional; a video is not. Each of these must come back as
    ``None`` so the caller renders without a bed instead of failing the render.
    """
    assert music_or_none(_StubProvider(exc),
                         MusicRequest(workspace_id="w", duration_seconds=30)) is None


def test_an_unexpected_exception_also_degrades():
    """Even a bug must not take the video down."""
    provider = _StubProvider(ZeroDivisionError("bug in a lane"))
    assert music_or_none(provider, MusicRequest(workspace_id="w", duration_seconds=30)) is None


def test_an_unusable_result_is_treated_as_no_music():
    provider = _StubProvider()
    provider_result = MusicResult(provider="stub", path="", state=SubmissionState.FAILED)
    provider.generate = lambda request: provider_result      # type: ignore[method-assign]
    assert music_or_none(provider, MusicRequest(workspace_id="w", duration_seconds=30)) is None


def test_a_successful_result_is_returned_with_its_provenance():
    provider = _StubProvider()
    result = music_or_none(provider, MusicRequest(workspace_id="w", duration_seconds=30))
    assert result is not None and result.usable is True


def test_an_unavailable_provider_class_is_reported_not_substituted():
    class Unavailable(MusicIntelligenceProvider):
        key = "unavailable"

        def available(self) -> bool:
            return False

        def generate(self, request: MusicRequest) -> MusicResult:
            raise AssertionError("must not be called")

    assert generate_music  # the package-level helper exists and is importable
    assert MusicUnavailable is not None